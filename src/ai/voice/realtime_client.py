"""Cliente da Realtime API da OpenAI: emite credencial efemera e desliga chamada.

E o unico arquivo que fala com a OpenAI com a CHAVE MESTRA. Ela nunca sai
daqui: o que vai para o navegador e um segredo de curta duracao, preso a uma
sessao ja configurada — modelo, voz, instrucoes e ferramentas sao fixados
neste lado e o cliente nao troca nenhum deles.

SOBRE O TTL DO SEGREDO, que e o erro classico deste desenho: ele e a janela
para ABRIR a conexao, e NAO o teto da conversa. Uma sessao aberta continua
faturando muito depois de o segredo expirar. Quem limita a duracao e o teto
gravado em `ai_voice_sessions` mais os fechamentos do lado do cliente; ver
`src/ai/voice/session_service.py`.

O que protege a emissao, e onde cada peca mora:

    login de cliente          `src/api/voice.py`, get_current_customer
    rate limit por IP         `src/api/voice.py`, VOICE_SESSION_RATE_LIMIT
    cota por cliente e loja   `session_service.py`
    voz ligada no restaurante `session_service.py`
    chave mestra global       `VOICE_ENABLED`, em `main.py`

O que continua faltando, e nao depende deste arquivo: **projeto OpenAI
separado com teto de gasto proprio.** E a unica barreira que continua valendo
quando o codigo acima tiver bug.
"""

import logging
import uuid

import httpx
from fastapi import HTTPException, status

from src.core.config import settings
from src.ai.voice.voice_prompt import instructions_for


logger = logging.getLogger("uvicorn.error")

URL_CLIENT_SECRETS = "https://api.openai.com/v1/realtime/client_secrets"
# Desliga uma chamada em curso, com a chave MESTRA. Documentado em
# developers.openai.com/api/reference — POST, sem corpo, 200 quando a OpenAI
# comeca a encerrar. Ver `hangup_call` para o que ele exige.
URL_HANGUP = "https://api.openai.com/v1/realtime/calls/{call_id}/hangup"
TIMEOUT_SEGUNDOS = 10

# A ferramenta que o modelo do navegador pode chamar. Declarada AQUI, no
# servidor, e nao no HTML: e o servidor que decide o que o modelo pode fazer.
# Se a lista viajasse no javascript, quem abrisse o console escolheria as
# proprias ferramentas.
SEARCH_TOOL = {
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
            },
            # Opcional, e FORA de `required` de proposito: obrigatorio, o
            # modelo teria de inventar um teto toda vez que o cliente nao
            # pedisse nenhum, e o cardapio inteiro passaria a ser filtrado por
            # um numero que ninguem disse.
            "preco_maximo": {
                "type": "number",
                "description": (
                    "Teto de preco em reais, SO quando o cliente disser um. "
                    "Exemplos: 'algo ate 50 reais' -> 50; 'o mais barato' -> "
                    "nao preencher."
                ),
            },
        },
        "required": ["consulta"],
        "additionalProperties": False,
    },
}


def issue_client_secret(
    restaurant_id: uuid.UUID,
    restaurant_context: str,
    branch_context: str,
) -> dict:
    """Pede a OpenAI um segredo de curta duracao para o navegador usar.

    A chave mestra (`OPENAI_API_KEY`) nunca sai daqui. O que vai para o
    navegador e o `value` devolvido — um segredo que so serve para abrir uma
    sessao de Realtime ja configurada: modelo, voz, instrucoes e ferramentas
    sao fixados NESTE lado e o cliente nao consegue trocar nenhum deles.

    A LOJA entra nas instrucoes AQUI, e nao no navegador, pelo mesmo motivo
    que todo o resto: a pagina e do cliente e quem quiser edita. Instrucao de
    loja escolhida pelo front seria o modelo falando do cardapio que o
    proprio cliente mandou dizer que era o dele.
    """
    corpo = {
        "session": {
            "type": "realtime",
            "model": settings.VOICE_MODEL,
            "instructions": instructions_for(restaurant_context, branch_context),
            "audio": {"output": {"voice": settings.VOICE_NAME}},
            "tools": [SEARCH_TOOL],
            "tool_choice": "auto",
        }
    }

    try:
        resposta = httpx.post(
            URL_CLIENT_SECRETS,
            headers={
                "Authorization": f"Bearer {settings.OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json=corpo,
            timeout=TIMEOUT_SEGUNDOS,
        )
    except httpx.HTTPError as erro:
        logger.warning("[Voz] emissao falhou na rede: %s", erro)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Nao consegui falar com a OpenAI",
        )

    if resposta.status_code >= 400:
        # O corpo do erro da OpenAI vai INTEIRO para o log e NAO para o
        # cliente: ele as vezes ecoa pedaco do que foi enviado, e o que foi
        # enviado inclui as instrucoes.
        logger.warning(
            "[Voz] emissao recusada | status=%d | corpo=%s",
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
        "[Voz] credencial emitida | restaurant_id=%s | modelo=%s | expira_em=%s",
        restaurant_id,
        settings.VOICE_MODEL,
        emitida.get("expires_at"),
    )
    return emitida


def hangup_call(call_id: str) -> bool:
    """Encerra uma chamada em curso pelo SERVIDOR. Devolve se conseguiu.

    ===================================================================
    O QUE ISTO ALCANCA, E O QUE NAO ALCANCA
    ===================================================================

    `call_id` so existe no cabecalho `Location` da resposta que a OpenAI da ao
    NAVEGADOR quando ele cria a chamada. O servidor nao participa dessa
    requisicao e nao ha, na documentacao, outra forma de descobrir o
    identificador de uma chamada WebRTC que ele nao originou.

    Consequencia, dita sem rodeio: **isto vale contra cliente que coopera, e
    nao contra cliente hostil.** Quem editar o javascript para nao reportar o
    `call_id` fica com uma sessao que o servidor nao sabe desligar. O que
    barra esse caso nao e este arquivo — e o controle na EMISSAO: login, cota
    e rate limit.

    Falha aqui nao levanta. Quem chama esta varrendo sessoes vencidas, e uma
    OpenAI fora do ar nao pode derrubar a emissao de outra sessao.
    """
    try:
        resposta = httpx.post(
            URL_HANGUP.format(call_id=call_id),
            headers={"Authorization": f"Bearer {settings.OPENAI_API_KEY}"},
            timeout=TIMEOUT_SEGUNDOS,
        )
    except httpx.HTTPError as erro:
        logger.warning("[Voz] desligamento falhou na rede | call_id=%s | %s", call_id, erro)
        return False

    # 404 conta como sucesso: a chamada ja tinha acabado, que e o estado que
    # se queria. Insistir nela em toda varredura seria barulho eterno no log.
    if resposta.status_code == 404:
        logger.info("[Voz] chamada ja estava encerrada | call_id=%s", call_id)
        return True

    if resposta.status_code >= 400:
        logger.warning(
            "[Voz] desligamento recusado | call_id=%s | status=%d | corpo=%s",
            call_id,
            resposta.status_code,
            resposta.text[:300],
        )
        return False

    logger.info("[Voz] chamada desligada pelo servidor | call_id=%s", call_id)
    return True
