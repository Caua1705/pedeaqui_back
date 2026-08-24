"""A bateria de dez perguntas do Rapi, antes e depois de mexer no prompt.

EXISTE PORQUE O PROMPT DOMINA A LATENCIA. A decomposicao medida em 24/08/2026,
em producao, sobre oito turnos quentes:

    llm_call_ms = 1023 ms fixos + 14,2 ms por token de saida   (R2 = 0,94)

Contra a ENTRADA a mesma regressao da R2 = 0,19. Quer dizer: o tamanho do
prompt nao explica a espera, o custo fixo e **o comprimento da resposta**
explicam. Com o streaming cancelado, encurtar a resposta e o unico caminho que
reduz o tempo real — e ele e de graca.

Entao o numero que decide se uma mudanca de prompt valeu nao e o relogio: e
`output_tokens`, turno a turno. E o relogio confirma.

## A ARMADILHA QUE ESTE SCRIPT EXISTE PARA MEDIR

`output_tokens` da OpenAI **inclui `reasoning_tokens`** — os tokens que o
modelo gera antes de escrever a primeira palavra. Gerar e serial: cada um
deles e espera do cliente, mesmo com `reasoning_effort="minimal"`.

Um prompt com mais regras encurta o texto e pode alongar o raciocinio. As duas
coisas se cancelam sem aparecer em lugar nenhum, e o relatorio sairia dizendo
"a resposta ficou 25% menor" com o turno demorando igual. Por isso a
comparacao separa os dois:

    texto_tokens = output_tokens - reasoning_tokens

e o veredito e escrito em uma linha, no fim, em vez de ficar para quem ler a
tabela com atencao.

## COMO RODAR

Duas rodadas, uma antes de aplicar a mudanca e outra depois, e a comparacao:

    docker exec pedeaqui-api python scripts/afere_prompt.py \\
        --restaurant-id <uuid> --branch-id <uuid> --saida /tmp/antes.json

    # aplica a mudanca, faz o deploy, e:
    docker exec pedeaqui-api python scripts/afere_prompt.py \\
        --restaurant-id <uuid> --branch-id <uuid> --saida /tmp/depois.json

    python scripts/afere_prompt.py --compara /tmp/antes.json /tmp/depois.json

## AS QUATRO PORTAS, E POR QUE CADA UMA

**1. O `git_sha`.** Em 24/08/2026 tres commits ficaram sem `push`, producao
seguiu rodando o codigo anterior, e uma bateria inteira foi medida contra o
build errado — meia hora para descobrir, comparando frases de log com
`git log -S`. O script imprime o `GIT_SHA` da imagem em que ele esta rodando,
e `--exige-sha` faz dele uma porta em vez de uma linha para conferir depois.

**2. A loja tem de estar ABERTA nas duas pontas.** Loja fechada faz o prompt
novo escrever uma frase de aviso que o prompt velho nao escrevia. A rodada
"depois" sairia mais longa por um motivo que nao e o que se esta medindo. O
script RECUSA rodar com a loja fechada, e a checagem usa
`ChatService._build_branch_state` — o proprio codigo sob medicao.

**3. Uma sessao para as dez.** As perguntas sao uma CONVERSA, na ordem: a
regra "no maximo um turno com pergunta a cada tres" so tem o que medir se o
historico existir, e a nona repete a quarta de proposito. Dez sessoes
separadas mediriam dez primeiros turnos.

**4. Um turno de aquecimento antes da primeira.** `get_chat_client` tem
`lru_cache` por processo: a primeira chamada paga o handshake TLS com a
OpenAI (~150 ms) e o primeiro checkout do pool do Postgres. Sem o aquecimento
esse custo cai no turno 2 e some na media — foi o que fez "oi" parecer a
pergunta mais lenta da bateria. O turno de aquecimento nao entra em conta
nenhuma.

## O TURNO 1 E O UNICO QUE NAO PODE MUDAR

"oi" e saudacao enlatada: nao chama a busca nem o modelo, e responde em ~31 ms
(era 3761 ms). Ele nao entra na comparacao de LATENCIA, porque nao tem
`llm_call_ms` para comparar — mas entra na de COMPORTAMENTO, e e o unico turno
que se espera ver identico. Se o texto dele mudar, alguma coisa saiu do lugar.

## LE PRODUCAO, ESCREVE UM ARQUIVO E MAIS NADA

Nenhum INSERT, UPDATE ou DDL. O que ele gasta e uma chamada de embedding e uma
de LLM por pergunta — onze de cada por rodada, contando o aquecimento (a
saudacao do turno 1 nao gasta nenhuma das duas).

## ESTE SCRIPT ESTA QUEBRADO — 24/08/2026

Nao o use para medir. `docker exec` sobe um processo SEPARADO, que nao passa
pelo Uvicorn, entao as linhas de `[AI /chat perf]` que ele produz nao caem no
log que o parser le. Resultado: `SEM MODELO | total=0.0 ms` nos nove turnos, um
JSON de zeros gravado no disco, e a palavra "gravado" impressa na tela — a
falha mais cara que um medidor pode ter, porque ela parece sucesso.

Quem mede hoje e a bancada (`bancada.html`), que bate no `/chat` real por HTTP
e por isso passa pelo Uvicorn. Consertar isto nao e prioridade. O que continua
valendo daqui e a REGUA abaixo: ela e a lista versionada de perguntas, e a
bancada roda essa mesma lista, nessa mesma ordem.
"""

import argparse
import json
import logging
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from statistics import median

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from src.core.config import GIT_SHA_NAO_CARIMBADO, settings
from src.db.session import SessionLocal
from src.services.chat_service import ChatService


# A REGUA OFICIAL. Versionada aqui, e nao passada por argumento, porque uma
# bateria que muda entre as duas rodadas nao compara nada — e porque a de
# 24/08/2026 nunca foi versionada e teve de ser reconstruida de memoria.
#
# A ordem importa: e uma conversa. E a nona repete a quarta DE PROPOSITO —
# e o teste do cache de busca, que deve responder em ~16 ms na segunda vez.
#
# A DECIMA E O PIOR CASO CONHECIDO, e por isso ela e a ultima e nao sai daqui.
# Medida na bancada em 24/08/2026: 6670 ms, contra 946 ms do "oi" na MESMA
# sessao. E pergunta aberta — o modelo olha tudo o que a busca trouxe e monta
# uma recomendacao —, entao ela e onde o teto de tres produtos morde mais, e o
# `output_tokens` dela e o numero que diz se o teto funcionou onde importa.
PERGUNTAS_PADRAO = (
    "oi",
    "qual o horário de vocês?",
    "vocês entregam no Papicu?",
    "quanto custa a picanha?",
    "tem sobremesa?",
    "o que vem na maminha à moda?",
    "qual a diferença entre a picanha importada e a black angus?",
    "vocês têm sushi?",
    "quanto custa a picanha?",
    "o que você recomenda pra mim",
)

# Uma pergunta que nao esta na regua, so para pagar o handshake e o pool.
PERGUNTA_DE_AQUECIMENTO = "vocês têm bebida?"


_USAGE_RE = re.compile(
    r"input_tokens=(?P<input>\S+) \| cached_input_tokens=(?P<cached>\S+) \| "
    r"output_tokens=(?P<output>\S+) \| reasoning_tokens=(?P<reasoning>\S+) \| "
    r"total_tokens=(?P<total>\S+)"
)


class ColetorDeMedidas(logging.Handler):
    """Os numeros vem do LOG, e nao do retorno de `chat()`.

    `ChatResponse` carrega o que o app precisa desenhar na tela; o usage e o
    cronometro sao observabilidade e moram no logger, que e onde eles ja eram
    lidos a mao antes deste script existir. Ler do log tem uma vantagem que
    nao e conveniencia: mede exatamente o que producao mede, com o mesmo
    formato, entao um numero daqui e comparavel com um `grep` de la.
    """

    def __init__(self):
        super().__init__()
        self.turno = {}

    def emit(self, record):
        try:
            texto = record.getMessage()
        except Exception:
            return

        if "[AI /chat usage]" in texto:
            achado = _USAGE_RE.search(texto)
            if achado:
                for chave, bruto in achado.groupdict().items():
                    self.turno[f"{chave}_tokens"] = _inteiro(bruto)
            return

        for campo in ("llm_call_ms", "total_ms", "branch_state_ms", "retrieval_total_ms"):
            achado = re.search(rf"{campo}=(-?[\d.]+)", texto)
            if achado:
                self.turno[campo] = float(achado.group(1))

        achado = re.search(r"cold_start=(true|false)", texto)
        if achado:
            self.turno["cold_start"] = achado.group(1) == "true"

        for campo in ("system_chars", "branch_state_chars", "conversation_chars",
                      "products_chars", "products_count"):
            achado = re.search(rf"{campo}=(\d+)", texto)
            if achado:
                self.turno[campo] = int(achado.group(1))

    def zerar(self):
        self.turno = {}


def _inteiro(bruto):
    try:
        return int(bruto)
    except (TypeError, ValueError):
        return None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="A bateria de dez perguntas do Rapi, antes e depois do prompt."
    )
    parser.add_argument(
        "--compara",
        nargs=2,
        metavar=("ANTES.json", "DEPOIS.json"),
        help="So compara dois resultados ja medidos. Nao chama a OpenAI.",
    )
    parser.add_argument("--restaurant-id", type=uuid.UUID)
    parser.add_argument("--branch-id", type=uuid.UUID)
    parser.add_argument(
        "--saida",
        type=Path,
        help="Onde gravar o JSON desta rodada.",
    )
    parser.add_argument(
        "--perguntas",
        type=Path,
        help="Arquivo com uma pergunta por linha, no lugar da regua oficial. "
             "Use so para investigar; a comparacao oficial e com a regua.",
    )
    parser.add_argument(
        "--exige-sha",
        help="Recusa medir se o GIT_SHA da imagem nao for este. "
             "Ponha `git rev-parse --short HEAD` aqui.",
    )
    parser.add_argument(
        "--sem-aquecimento",
        action="store_true",
        help="Nao manda o turno descartavel antes da bateria. So para depurar: "
             "sem ele o handshake com a OpenAI cai no turno 2.",
    )
    parser.add_argument(
        "--permite-loja-fechada",
        action="store_true",
        help="Mede mesmo com a loja fechada. A comparacao fica invalida — o "
             "prompt novo escreve uma frase que o velho nao escrevia.",
    )
    args = parser.parse_args()

    if args.compara:
        return _comparar(Path(args.compara[0]), Path(args.compara[1]))

    faltando = [
        nome
        for nome, valor in (
            ("--restaurant-id", args.restaurant_id),
            ("--branch-id", args.branch_id),
            ("--saida", args.saida),
        )
        if valor is None
    ]
    if faltando:
        print(f"Faltou: {', '.join(faltando)}")
        return 2

    return _medir(args)


def _medir(args) -> int:
    sha = settings.GIT_SHA
    print(f"git_sha da imagem: {sha}")
    if sha == GIT_SHA_NAO_CARIMBADO:
        print(
            "AVISO: imagem sem carimbo. Nao ha como provar depois qual codigo "
            "gerou estes numeros. Veja o deploy em docs/operacao.md."
        )
    if args.exige_sha and sha != args.exige_sha:
        print(f"ERRO: --exige-sha={args.exige_sha}, mas a imagem e {sha}. Nao medi nada.")
        return 1

    perguntas = _perguntas(args)
    print(f"perguntas: {len(perguntas)}")

    coletor = ColetorDeMedidas()
    logging.getLogger("uvicorn.error").addHandler(coletor)

    db = SessionLocal()
    try:
        service = ChatService(db)
        restaurante = service._get_active_restaurant(args.restaurant_id)
        filial = service._get_active_branch(restaurante.id, args.branch_id)

        # A porta da loja aberta, pela mesma funcao que monta o bloco do
        # prompt. Um dublê aqui poderia discordar do que o modelo vai ler.
        estado = service._build_branch_state(
            restaurante, filial, datetime.now(timezone.utc)
        )
        print(f"estado da loja:\n{estado}")
        if not estado.startswith("Aberta agora: sim"):
            if not args.permite_loja_fechada:
                print(
                    "\nERRO: a loja esta fechada, e nao medi nada.\n"
                    "Com ela fechada o prompt novo escreve uma frase de aviso "
                    "que o velho nao escrevia, e a rodada 'depois' sairia mais "
                    "longa por um motivo que nao e o que se esta medindo.\n"
                    "Rode nas duas pontas com a loja aberta, ou passe "
                    "--permite-loja-fechada sabendo que a comparacao nao vale."
                )
                return 1
            print("AVISO: medindo com a loja FECHADA. A comparacao nao vale.")

        sessao = f"afere-{uuid.uuid4()}"

        if not args.sem_aquecimento:
            print(f"\naquecimento (descartado): {PERGUNTA_DE_AQUECIMENTO!r}")
            coletor.zerar()
            service.chat(restaurante.id, filial.id, sessao, PERGUNTA_DE_AQUECIMENTO)

        turnos = []
        for indice, pergunta in enumerate(perguntas, start=1):
            coletor.zerar()
            resposta = service.chat(restaurante.id, filial.id, sessao, pergunta)
            turno = dict(coletor.turno)
            turno.update(
                {
                    "n": indice,
                    "pergunta": pergunta,
                    "resposta": resposta.message,
                    "response_type": resposta.response_type,
                    "produtos": len(resposta.products or []),
                    # A saudacao enlatada nao chama o modelo: sem `llm_call_ms`
                    # nao ha latencia de modelo para comparar, e o turno so
                    # entra na tabela de comportamento.
                    "sem_modelo": "llm_call_ms" not in turno,
                }
            )
            turno["texto_tokens"] = _texto_tokens(turno)
            turnos.append(turno)
            _imprime_turno(turno)
    finally:
        logging.getLogger("uvicorn.error").removeHandler(coletor)
        db.close()

    resultado = {
        "git_sha": sha,
        "medido_em": datetime.now(timezone.utc).isoformat(),
        "restaurant_id": str(args.restaurant_id),
        "branch_id": str(args.branch_id),
        "estado_da_loja": estado,
        "system_chars": turnos[-1].get("system_chars"),
        "turnos": turnos,
    }
    args.saida.write_text(json.dumps(resultado, ensure_ascii=False, indent=2), "utf-8")
    print(f"\ngravado em {args.saida}")
    return 0


def _texto_tokens(turno):
    """Os tokens que viraram TEXTO NA TELA, sem o raciocinio.

    E o numero que responde "a resposta ficou mais curta?". O
    `output_tokens` cru responde outra pergunta — "o turno ficou mais
    rapido?" — e as duas se separam exatamente quando o prompt ganha regra.
    """
    saida = turno.get("output_tokens")
    raciocinio = turno.get("reasoning_tokens")
    if saida is None:
        return None
    return saida - (raciocinio or 0)


def _perguntas(args):
    if not args.perguntas:
        return list(PERGUNTAS_PADRAO)
    linhas = args.perguntas.read_text(encoding="utf-8").splitlines()
    return [linha.strip() for linha in linhas if linha.strip()]


def _imprime_turno(turno):
    if turno["sem_modelo"]:
        print(
            f"  {turno['n']}. {turno['pergunta'][:44]:<44} "
            f"SEM MODELO  total={turno.get('total_ms', 0):7.1f} ms"
        )
        return
    print(
        f"  {turno['n']}. {turno['pergunta'][:44]:<44} "
        f"saida={turno.get('output_tokens'):>4} "
        f"(texto={turno.get('texto_tokens'):>4} raciocinio={turno.get('reasoning_tokens'):>4}) "
        f"llm={turno.get('llm_call_ms', 0):7.1f} ms"
    )


# ---------------------------------------------------------------------------
# A comparacao, e o veredito escrito em uma linha
# ---------------------------------------------------------------------------


def _comparar(caminho_antes: Path, caminho_depois: Path) -> int:
    antes = json.loads(caminho_antes.read_text(encoding="utf-8"))
    depois = json.loads(caminho_depois.read_text(encoding="utf-8"))

    print(f"antes : git_sha={antes['git_sha']}  system_chars={antes.get('system_chars')}")
    print(f"depois: git_sha={depois['git_sha']}  system_chars={depois.get('system_chars')}")
    if antes["git_sha"] == depois["git_sha"]:
        print(
            "\nAVISO: as duas rodadas tem o MESMO git_sha. Ou o deploy nao "
            "subiu, ou uma das duas foi medida contra o build errado."
        )

    pares = list(zip(antes["turnos"], depois["turnos"]))
    if len(antes["turnos"]) != len(depois["turnos"]):
        print("\nERRO: as duas rodadas tem numeros de turnos diferentes.")
        return 1

    print("\nturno a turno (saida = texto + raciocinio):\n")
    cabecalho = (
        f"  {'#':>2} {'pergunta':<40} "
        f"{'saida':>13} {'texto':>13} {'racioc.':>13} {'llm_call_ms':>19}"
    )
    print(cabecalho)
    print("  " + "-" * (len(cabecalho) - 2))

    for a, d in pares:
        if a["sem_modelo"] and d["sem_modelo"]:
            igual = "identica" if a["resposta"] == d["resposta"] else "MUDOU"
            print(
                f"  {a['n']:>2} {a['pergunta'][:40]:<40} "
                f"{'sem modelo - resposta ' + igual:>60}"
            )
            continue
        print(
            f"  {a['n']:>2} {a['pergunta'][:40]:<40} "
            f"{_delta(a.get('output_tokens'), d.get('output_tokens')):>13} "
            f"{_delta(a.get('texto_tokens'), d.get('texto_tokens')):>13} "
            f"{_delta(a.get('reasoning_tokens'), d.get('reasoning_tokens')):>13} "
            f"{_delta(a.get('llm_call_ms'), d.get('llm_call_ms'), casas=0):>19}"
        )

    com_modelo = [(a, d) for a, d in pares if not a["sem_modelo"] and not d["sem_modelo"]]
    _veredito(com_modelo)

    _comportamento(pares)
    return 0


def _delta(antes, depois, casas=0):
    if antes is None or depois is None:
        return "-"
    diferenca = depois - antes
    sinal = "+" if diferenca > 0 else ""
    if casas == 0:
        return f"{antes:.0f}>{depois:.0f} ({sinal}{diferenca:.0f})"
    return f"{antes}>{depois} ({sinal}{diferenca})"


def _veredito(com_modelo):
    """A conclusao escrita, e nao deixada para quem ler a tabela com atencao.

    A regra combinada em 24/08/2026: **se `reasoning_tokens` subir mais do que
    o texto cair, a rodada nao valeu** e as enumeracoes do prompt sao cortadas.
    Ela vale porque `output_tokens` inclui o raciocinio, e sao os dois juntos
    que multiplicam os 14,2 ms — um texto 25% menor com o dobro de raciocinio
    e um turno mais LENTO com um relatorio bonito.
    """
    if not com_modelo:
        print("\nNenhum turno com modelo para comparar.")
        return

    pares_dict = [{"a": a, "d": d} for a, d in com_modelo]

    def med(chave, lado):
        valores = [p[lado].get(chave) for p in pares_dict if p[lado].get(chave) is not None]
        return median(valores) if valores else None

    saida_antes, saida_depois = med("output_tokens", "a"), med("output_tokens", "d")
    texto_antes, texto_depois = med("texto_tokens", "a"), med("texto_tokens", "d")
    rac_antes, rac_depois = med("reasoning_tokens", "a"), med("reasoning_tokens", "d")
    llm_antes, llm_depois = med("llm_call_ms", "a"), med("llm_call_ms", "d")

    print(f"\n  medianas ({len(com_modelo)} turnos com modelo):")
    print(f"    output_tokens    {saida_antes:>6} -> {saida_depois:>6}")
    print(f"    texto_tokens     {texto_antes:>6} -> {texto_depois:>6}")
    print(f"    reasoning_tokens {rac_antes:>6} -> {rac_depois:>6}")
    print(f"    llm_call_ms      {llm_antes:>6.0f} -> {llm_depois:>6.0f}")

    queda_do_texto = texto_antes - texto_depois
    alta_do_raciocinio = rac_depois - rac_antes
    delta_saida = saida_depois - saida_antes

    print("\n" + "=" * 72)
    if delta_saida < 0:
        print(
            f"A RODADA VALEU. output_tokens caiu {-delta_saida:.0f} na mediana "
            f"({-delta_saida * 14.2:.0f} ms por turno, a 14,2 ms/token)."
        )
        if alta_do_raciocinio > 0:
            print(
                f"  Ressalva: reasoning_tokens subiu {alta_do_raciocinio:.0f} e comeu "
                f"parte da queda de {queda_do_texto:.0f} do texto."
            )
    else:
        print(
            f"A RODADA NAO VALEU. output_tokens SUBIU {delta_saida:.0f} na mediana "
            f"({delta_saida * 14.2:.0f} ms por turno)."
        )
        if alta_do_raciocinio >= queda_do_texto:
            print(
                f"  A causa e o raciocinio: o texto caiu {queda_do_texto:.0f} e "
                f"reasoning_tokens subiu {alta_do_raciocinio:.0f}."
            )
            print("  O combinado e cortar as enumeracoes do prompt e medir de novo.")
    print("=" * 72)


def _comportamento(pares):
    """O que a latencia nao mede: se o assistente parou de fazer o que fazia.

    Tres defeitos motivaram a rodada de 24/08/2026, e nenhum deles aparece em
    `output_tokens`: dizer o nome da casa, fechar todo turno com pergunta, e
    inventar descricao de produto. A contagem abaixo e grosseira de proposito
    — ela aponta onde olhar, e quem julga e quem le as respostas.
    """
    print("\ncomportamento (contagem grosseira; leia as respostas):\n")
    # A saudacao enlatada fica de FORA da contagem: ela termina com pergunta
    # ("Oi! Como posso ajudar?") por escrito, num texto que nao passa pelo
    # modelo. Contada junto, ela poe um ponto fixo nos dois lados e faz "1/9"
    # parecer regra desobedecida.
    com_modelo = [p for p in pares if not p[0]["sem_modelo"] and not p[1]["sem_modelo"]]
    for rotulo, indice in (("antes ", 0), ("depois", 1)):
        respostas = [p[indice]["resposta"] or "" for p in com_modelo]
        com_pergunta = sum(1 for r in respostas if r.rstrip().endswith("?"))
        with_hedge = sum(
            1
            for r in respostas
            if any(m in r.lower() for m in ("costuma", "geralmente", "normalmente é"))
        )
        print(
            f"  {rotulo}: termina com pergunta {com_pergunta}/{len(respostas)} | "
            f"hedge de invencao {with_hedge}/{len(respostas)}"
        )

    turno_um = pares[0]
    print(
        f"\n  turno 1 (saudacao enlatada): "
        f"{'IDENTICO' if turno_um[0]['resposta'] == turno_um[1]['resposta'] else 'MUDOU — investigue'}"
    )


if __name__ == "__main__":
    raise SystemExit(main())
