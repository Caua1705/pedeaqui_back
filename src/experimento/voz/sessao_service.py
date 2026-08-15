"""Emite a credencial efemera que o NAVEGADOR usa para falar com a OpenAI.

===========================================================================
ISTO NAO PODE IR PARA PRODUCAO ASSIM. LEIA ANTES DE COPIAR.
===========================================================================

A rota que chama este servico e ABERTA: sem login, sem cota, sem teto de
duracao e sem registro de consumo. Cada credencial emitida abre uma sessao de
audio faturada na conta da OpenAI do projeto, e nada aqui limita quantas.

Um `curl` em laco contra este endpoint gera credenciais indefinidamente. Nao
existe, neste arquivo, nenhuma barreira contra isso — de proposito, porque o
objetivo do experimento e descobrir se a coisa presta, e cota antes disso e
trabalho jogado fora se a resposta for nao.

O que falta, e que nao e opcional para virar produto:

1. **Identidade.** Cliente autenticado (`get_current_customer`). Um minuto de
   audio anonimo e dinheiro sem ninguem a quem cobrar nem a quem bloquear.
2. **Cota** por cliente, por restaurante e global, debitada NA EMISSAO pelo
   custo maximo possivel da sessao. Debitar no fim nunca cobra de quem nunca
   fecha a sessao.
3. **Teto de duracao dentro da sessao.** O TTL do segredo abaixo e a janela
   para ABRIR a conexao, e nao o teto da conversa: uma sessao aberta continua
   faturando muito depois de o segredo expirar. Confundir os dois e o erro
   classico deste desenho.
4. **Livro-razao** das emissoes e conciliacao diaria com a fatura. Sem isso
   nao ha como saber que uma sessao faturada foi emitida por voce.
5. **Projeto OpenAI separado, com teto de gasto proprio.** E a unica barreira
   que continua valendo quando o codigo acima tem bug.

O que existe hoje como unica protecao: a rota so sobe com
`EXPERIMENTO_VOZ_ENABLED=true`, e o padrao e desligado.
"""

import logging
import uuid

import httpx
from fastapi import HTTPException, status

from src.core.config import settings
from src.experimento.voz.prompt_de_voz import instrucoes_para


logger = logging.getLogger("uvicorn.error")

URL_DE_EMISSAO = "https://api.openai.com/v1/realtime/client_secrets"
MODELO_DE_VOZ = "gpt-realtime-mini"
VOZ = "marin"
TIMEOUT_SEGUNDOS = 10

# A ferramenta que o modelo do navegador pode chamar. Declarada AQUI, no
# servidor, e nao no HTML: e o servidor que decide o que o modelo pode fazer.
# Se a lista viajasse no javascript, quem abrisse o console escolheria as
# proprias ferramentas.
FERRAMENTA_DE_BUSCA = {
    "type": "function",
    "name": "buscar_no_cardapio",
    "description": (
        "Busca produtos no cardapio deste restaurante. Use SEMPRE antes de "
        "falar de qualquer produto, preco ou ingrediente. Os produtos "
        "encontrados aparecem na tela do cliente automaticamente."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "consulta": {
                "type": "string",
                "description": (
                    "O que o cliente quer, em palavras dele. "
                    "Exemplos: 'sobremesa de chocolate', 'algo vegetariano', "
                    "'a picanha'."
                ),
            }
        },
        "required": ["consulta"],
        "additionalProperties": False,
    },
}


def emitir_credencial_efemera(restaurant_id: uuid.UUID, restaurant_context: str) -> dict:
    """Pede a OpenAI um segredo de curta duracao para o navegador usar.

    A chave mestra (`OPENAI_API_KEY`) nunca sai daqui. O que vai para o
    navegador e o `value` devolvido — um segredo que so serve para abrir uma
    sessao de Realtime ja configurada: modelo, voz, instrucoes e ferramentas
    sao fixados NESTE lado e o cliente nao consegue trocar nenhum deles.
    """
    corpo = {
        "session": {
            "type": "realtime",
            "model": MODELO_DE_VOZ,
            "instructions": instrucoes_para(restaurant_context),
            "audio": {"output": {"voice": VOZ}},
            "tools": [FERRAMENTA_DE_BUSCA],
            "tool_choice": "auto",
        }
    }

    try:
        resposta = httpx.post(
            URL_DE_EMISSAO,
            headers={
                "Authorization": f"Bearer {settings.OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json=corpo,
            timeout=TIMEOUT_SEGUNDOS,
        )
    except httpx.HTTPError as erro:
        logger.warning("[Experimento voz] emissao falhou na rede: %s", erro)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Nao consegui falar com a OpenAI",
        )

    if resposta.status_code >= 400:
        # O corpo do erro da OpenAI vai INTEIRO para o log e NAO para o
        # cliente: ele as vezes ecoa pedaco do que foi enviado, e o que foi
        # enviado inclui as instrucoes.
        logger.warning(
            "[Experimento voz] emissao recusada | status=%d | corpo=%s",
            resposta.status_code,
            resposta.text[:800],
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="A OpenAI recusou a emissao da credencial",
        )

    emitida = resposta.json()
    # `restaurant_id` aqui e o unico ponto em que uma sessao de voz futura fica
    # ligada a um restaurante: depois desta linha a conversa acontece entre o
    # navegador e a OpenAI, e o backend nao ve mais nada. Esta linha e o
    # comeco de qualquer conciliacao de custo que venha a existir.
    logger.info(
        "[Experimento voz] credencial emitida | restaurant_id=%s | modelo=%s | expira_em=%s",
        restaurant_id,
        MODELO_DE_VOZ,
        emitida.get("expires_at"),
    )
    return emitida
