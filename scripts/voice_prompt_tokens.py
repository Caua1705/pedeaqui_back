"""Quantos tokens de TEXTO cada peca da sessao de voz custa.

Nao muda nada: le o que `issue_client_secret` monta e conta. Existe para
medir antes de cortar, e para conferir o ganho depois de cortar.

## Por que texto e nao audio

Numa sessao real de 75s medida em 15/08/2026: **22.349 tokens de texto de
entrada contra 4.038 de audio**. O texto e 5x o audio. Ele nao vem do que o
cliente fala — vem do que o SERVIDOR remonta a cada turno.

## O que e "por turno", e por que a conta explode

A Realtime nao guarda estado do lado de ca: a cada resposta, o modelo recebe
de novo **o prefixo inteiro** (instrucoes + declaracao da ferramenta) **mais
todo o historico ate ali**. Uma sessao com N respostas paga o prefixo N vezes,
e o historico do turno 1 e reenviado nas N-1 respostas seguintes.

Por isso a linha que importa nao e "quanto pesa o prompt", e sim
**prefixo x N + soma do historico acumulado**. E o que a projecao no fim
imprime.

O cache abate a maior parte disso (93% na sessao medida), mas abater nao e
zerar — e o cache so vale enquanto o COMECO do que e enviado nao muda.

## O que a medicao NAO alcanca

`tiktoken` conta a string. A OpenAI acrescenta enquadramento por item e
serializa o schema da ferramenta no formato dela, que nao e o JSON cru. Os
numeros daqui sao a ordem de grandeza certa e a comparacao ENTRE pecas e
justa; o total real fica alguns por cento acima.

Uso:

    python scripts/voice_prompt_tokens.py
    python scripts/voice_prompt_tokens.py --turnos 8 --produtos 5
"""

import argparse
import json
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import tiktoken

from src.ai.voice.realtime_client import SEARCH_TOOL
from src.ai.voice.voice_prompt import VOICE_INSTRUCTIONS, instructions_for
from src.core.config import settings


# A codificacao da familia gpt-4o / gpt-realtime. `encoding_for_model` nao
# conhece os nomes de realtime, entao a codificacao vai pelo nome.
CODIFICACAO = tiktoken.get_encoding("o200k_base")

# Um restaurante sem descricao, que e o piso do contexto; e um com a descricao
# no teto (`_MAX_CONTEXT_DESCRIPTION`, 300 caracteres), que e o topo.
CONTEXTO_MINIMO = "Nome do restaurante: Junior da Picanha"
CONTEXTO_MAXIMO = CONTEXTO_MINIMO + "\nSobre a casa: " + ("palavra " * 45)[:300]

# A loja tem o mesmo tamanho no piso e no teto: uma linha com um nome, e nada
# mais (ver `branch_context_for`). Entra nos dois numeros porque desde a
# revisao 20260820_0026 nenhuma sessao e emitida sem ela.
CONTEXTO_DA_LOJA = "Loja: Centro"

# Um resumo de busca com nomes de tamanho realista para uma churrascaria.
PRODUTOS_DE_EXEMPLO = [
    "Picanha na Chapa - R$ 89,90",
    "Costela no Bafo com Farofa - R$ 74,50",
    "Fraldinha Grelhada - R$ 68,00",
    "Maminha ao Alho - R$ 71,90",
    "Contrafile Acebolado - R$ 64,00",
]

# O que a Realtime devolve como item de chamada de ferramenta, e o que o
# navegador devolve como resultado. Os dois viram TEXTO no historico e sao
# reenviados em todos os turnos seguintes.
CHAMADA_DE_FERRAMENTA = json.dumps(
    {
        "type": "function_call",
        "name": "buscar_no_cardapio",
        "arguments": json.dumps({"consulta": "alguma carne pra dois"}, ensure_ascii=False),
    },
    ensure_ascii=False,
)

# Uma fala do atendente, no tamanho que o proprio prompt manda ("uma ou duas
# frases"). A fala anterior volta ao contexto como texto nos turnos seguintes.
FALA_DO_ATENDENTE = (
    "Temos a picanha na chapa, que sai bem servida, e a costela no bafo. "
    "Qual dos dois te agrada mais?"
)


def contar(texto: str) -> int:
    return len(CODIFICACAO.encode(texto))


def main() -> int:
    parser = argparse.ArgumentParser(description="Tokens de texto da sessao de voz.")
    parser.add_argument(
        "--turnos",
        type=int,
        default=8,
        help="Quantas respostas do modelo projetar. Padrao: 8 (~75s de conversa).",
    )
    parser.add_argument(
        "--produtos",
        type=int,
        default=5,
        help="Quantos produtos o resumo da busca devolve. Padrao: 5 (o de hoje).",
    )
    args = parser.parse_args()

    prefixo = _imprimir_prefixo()
    por_turno = _imprimir_crescimento(args.produtos)
    _imprimir_projecao(prefixo, por_turno, args.turnos)
    return 0


def _imprimir_prefixo() -> int:
    """O que vai na emissao da credencial e volta em TODA resposta."""
    instrucoes_minimo = contar(instructions_for(CONTEXTO_MINIMO, CONTEXTO_DA_LOJA))
    instrucoes_maximo = contar(instructions_for(CONTEXTO_MAXIMO, CONTEXTO_DA_LOJA))
    ferramenta = contar(json.dumps(SEARCH_TOOL, ensure_ascii=False))
    resto = contar(
        json.dumps(
            {
                "type": "realtime",
                "model": settings.VOICE_MODEL,
                "audio": {"output": {"voice": settings.VOICE_NAME}},
                "tool_choice": "auto",
            },
            ensure_ascii=False,
        )
    )

    print("PREFIXO FIXO — reenviado em toda resposta do modelo")
    print()
    _linha("instructions (regras)", contar(VOICE_INSTRUCTIONS))
    _linha("  + contexto, restaurante e loja (sem descricao)", instrucoes_minimo - contar(VOICE_INSTRUCTIONS))
    _linha("  + contexto, restaurante e loja (descricao no teto)", instrucoes_maximo - contar(VOICE_INSTRUCTIONS))
    _linha("    a linha da loja, sozinha", contar(CONTEXTO_DA_LOJA), recuo=True)
    _linha("declaracao da ferramenta (schema + descricoes)", ferramenta)
    _linha("resto do corpo (modelo, voz, tool_choice)", resto)
    print()
    _linha("PREFIXO, piso", instrucoes_minimo + ferramenta + resto)
    _linha("PREFIXO, teto", instrucoes_maximo + ferramenta + resto)
    print()

    _imprimir_dentro_da_ferramenta()
    return instrucoes_minimo + ferramenta + resto


def _imprimir_dentro_da_ferramenta() -> None:
    """A declaracao da ferramenta aberta, para saber o que da para cortar."""
    parametros = SEARCH_TOOL["parameters"]["properties"]

    print("  dentro da declaracao da ferramenta:")
    _linha("    description da tool", contar(SEARCH_TOOL["description"]), recuo=True)
    _linha("    description de `consulta`", contar(parametros["consulta"]["description"]), recuo=True)
    _linha(
        "    description de `preco_maximo`",
        contar(parametros["preco_maximo"]["description"]),
        recuo=True,
    )
    print()


def _imprimir_crescimento(quantidade_de_produtos: int) -> int:
    """O que UM turno completo acrescenta ao historico, para sempre."""
    resumo = "Produtos encontrados: " + "; ".join(PRODUTOS_DE_EXEMPLO[:quantidade_de_produtos])
    chamada = contar(CHAMADA_DE_FERRAMENTA)
    resultado = contar(resumo)
    fala = contar(FALA_DO_ATENDENTE)

    print(f"CRESCIMENTO POR TURNO — fala do cliente + busca de {quantidade_de_produtos} produtos")
    print()
    _linha("chamada da ferramenta (nome + argumentos)", chamada)
    _linha(f"resumo devolvido ({quantidade_de_produtos} produtos)", resultado)
    _linha("fala do atendente, que volta como texto", fala)
    print()
    _linha("TURNO COMPLETO", chamada + resultado + fala)
    _linha("  turno sem busca (so a fala)", fala)
    print()

    print("  o resumo, produto a produto:")
    for produto in PRODUTOS_DE_EXEMPLO[:quantidade_de_produtos]:
        _linha(f"    {produto}", contar("; " + produto), recuo=True)
    print()
    return chamada + resultado + fala


def _imprimir_projecao(prefixo: int, por_turno: int, turnos: int) -> None:
    """Prefixo x N mais o historico acumulado — a conta que a fatura ve.

    O historico do turno 1 e reenviado nos turnos 2..N, o do turno 2 nos
    3..N, e assim por diante. Somando: `por_turno * N * (N - 1) / 2`.
    """
    do_prefixo = prefixo * turnos
    do_historico = por_turno * turnos * (turnos - 1) // 2

    print(f"PROJECAO — sessao com {turnos} respostas do modelo")
    print()
    _linha(f"prefixo reenviado {turnos}x", do_prefixo)
    _linha("historico acumulado", do_historico)
    _linha("TOTAL de texto de entrada", do_prefixo + do_historico)
    print()
    print(f"  ({_porcentagem(do_prefixo, do_prefixo + do_historico)} do total e o prefixo)")


def _porcentagem(parte: int, total: int) -> str:
    return f"{100 * parte / total:.0f}%" if total else "—"


def _linha(rotulo: str, tokens: int, recuo: bool = False) -> None:
    largura = 52 if recuo else 48
    print(f"  {rotulo} {'.' * max(1, largura - len(rotulo))} {tokens:>6} tok")


if __name__ == "__main__":
    raise SystemExit(main())
