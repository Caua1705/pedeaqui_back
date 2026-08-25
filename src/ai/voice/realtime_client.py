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
            # A ORDENACAO, e ela e a resposta ao superlativo (25/08/2026).
            # "Qual a bebida mais barata?" e "manda o mais caro do cardapio"
            # ficaram sem resposta numa sessao real, e nao por defeito de
            # prompt: a busca e por SIGNIFICADO, e os cinco mais parecidos com
            # "bebida" nao sao as cinco mais baratas.
            #
            # Fora de `required` pelo mesmo motivo do `preco_maximo`:
            # obrigatorio, o modelo teria de escolher uma ordem toda vez, e o
            # cardapio inteiro passaria a sair ordenado por preco sem ninguem
            # ter pedido.
            #
            # `_da_busca` contra `_da_loja` e a distincao que o modelo precisa
            # fazer, e por isso ela esta no NOME de cada valor em vez de num
            # segundo campo: "a bebida mais barata" tem assunto, "o mais caro
            # do cardapio" nao tem — e no segundo caso a busca por significado
            # nao ajuda, porque "cardapio" nao se parece com nada.
            "ordenar": {
                "type": "string",
                "enum": [
                    "mais_barato_da_busca",
                    "mais_caro_da_busca",
                    "mais_barato_da_loja",
                    "mais_caro_da_loja",
                ],
                "description": (
                    "SO quando o cliente pedir o mais barato ou o mais caro. "
                    "Use '_da_busca' quando ele disser DE QUE tipo ('a bebida "
                    "mais barata' -> consulta 'bebida', ordenar "
                    "'mais_barato_da_busca'). Use '_da_loja' quando for o "
                    "cardapio inteiro ('manda o mais caro' -> "
                    "'mais_caro_da_loja'), e ai a consulta e ignorada."
                ),
            },
        },
        "required": ["consulta"],
        "additionalProperties": False,
    },
}


# A SEGUNDA FERRAMENTA (25/08/2026), e ela existe pelo mesmo motivo estrutural
# que `ordenar`: ha pergunta de cardapio que a busca por SIGNIFICADO nao alcanca.
#
#     "manda o mais caro"      -> ordenacao, e nao similaridade
#     "quais sao as categorias?" -> listagem, e nao similaridade
#
# "Categorias" nao se parece com prato nenhum, entao subir o `top_k` so tornaria
# o acaso mais provavel. Perguntado isso numa sessao real, o atendente respondeu
# "tem pratos como arroz, com varios tipos, carnes e algumas opcoes de
# acompanhamentos" — um cardapio de churrascaria plausivel e inventado, que e o
# que um modelo produz quando nao ha o que ler.
#
# SEM PARAMETRO NENHUM, e isso e decisao. Um filtro ou um termo aqui seria mais
# uma chance de ele acertar a pergunta e errar o argumento — o modo de falha que
# o enum de `ordenar` foi desenhado para nao ter. A lista de categorias de uma
# loja e a mesma para qualquer forma de perguntar.
CATEGORIES_TOOL = {
    "type": "function",
    "name": "listar_categorias",
    "description": (
        "Lista os tipos de comida que esta loja tem, com quantos produtos ha "
        "em cada um. Use quando o cliente perguntar o que a loja tem, quais "
        "sao as categorias, ou o que da para pedir, SEM nomear um produto. "
        "Nao mostra nada na tela do cliente."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    },
}


# A ENTRADA DE AUDIO, e por que ela deixou de ser o padrao (24/08/2026). Sem
# esta secao a sessao roda no `server_vad` de fabrica — limiar 0,5 e nenhuma
# reducao de ruido — e a bancada mostrou, num ambiente real, o que isso
# produz: ruido de fundo virando fala, transcrito como frase solta em outro
# idioma ("Thank you.", "Bye!", "MBC ..."), ABRINDO TURNO PAGO, com o modelo
# respondendo a ninguem.
#
# O segundo estrago do mesmo falso positivo e mais caro que o turno: como
# `interrupt_response` nasce ligado, todo ruido detectado como comeco de fala
# CANCELA a resposta em curso. No log e `output_audio_buffer.cleared` dezenas
# de vezes numa sessao; no ouvido do cliente e a frase pela metade — "vinte e
# quatro e", "cinqu", "Certo, agora que voce ja".
#
# `far_field` porque o cliente fala com o CELULAR NA MAO, e nao com fone: o
# microfone pega a rua, a televisao e a mesa ao lado. `threshold` acima do
# padrao exige audio mais alto para abrir turno. E `silence_duration_ms` sobe
# de 500 para 700 porque a pausa curta do meio da frase fechava o turno antes
# da hora — custa 200 ms a mais no fim de cada fala, e latencia nao e o
# problema desta frente (a primeira palavra sai em 390-720 ms).
#
# Os tres numeros ficam AQUI, e nao em `settings`, pelo mesmo motivo das
# ferramentas: e o servidor que decide a sessao. Variavel de ambiente aqui
# seria producao rodando com um valor que nunca foi medido.
# AS VOZES QUE A REALTIME ACEITA, e a lista e FECHADA de proposito (24/08/2026).
#
# Ela existe porque `POST /voice/session` passou a aceitar a voz no corpo
# quando `VOICE_ALLOW_VOICE_OVERRIDE` esta ligada — e corpo de requisicao e
# texto que veio da rua. Sem a lista, "marim" digitado errado viaja ate a
# OpenAI, volta 400, e o cliente ve 502 depois de a rota ja ter varrido
# vencidas e conferido duas cotas.
#
# Escrita a mao e nao consultada: nao ha rota da OpenAI que liste vozes, e uma
# lista que se descobre sozinha seria uma chamada de rede a cada emissao para
# validar um campo de teste. Quando a OpenAI acrescentar voz, esta linha muda
# junto — e ate la o nome novo e recusado com 422, que e a falha barulhenta e
# barata.
#
# `VOICE_NAME` NAO e validado contra ela, e isso e decisao. Um nome novo da
# OpenAI posto no `.env` da VPS derrubaria a emissao inteira no boot por causa
# de uma lista desatualizada no nosso codigo — outage auto-infligida para
# proteger um campo de bancada. O padrao continua respondendo pela OpenAI: se
# estiver errado, o 400 dela aparece no log de `issue_client_secret`.
VOZES_DO_REALTIME = (
    "alloy",
    "ash",
    "ballad",
    "coral",
    "echo",
    "sage",
    "shimmer",
    "verse",
    "marin",
    "cedar",
)


AUDIO_INPUT = {
    "noise_reduction": {"type": "far_field"},
    "turn_detection": {
        "type": "server_vad",
        "threshold": 0.6,
        "silence_duration_ms": 700,
    },
}


def issue_client_secret(
    restaurant_id: uuid.UUID,
    restaurant_context: str,
    branch_context: str,
    voz: str | None = None,
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

    `voz` e a UNICA excecao a essa regra, e ela e de bancada: quando
    `VOICE_ALLOW_VOICE_OVERRIDE` esta ligada, a rota deixa o corpo escolher
    entre `VOZES_DO_REALTIME`. `None` — o caminho de producao — cai em
    `VOICE_NAME`. A escolha nao chega aqui sem passar pela lista fechada; ver
    `SessaoRequest` em `src/api/voice.py`.
    """
    voz_da_sessao = voz or settings.VOICE_NAME
    corpo = {
        "session": {
            "type": "realtime",
            "model": settings.VOICE_MODEL,
            "instructions": instructions_for(restaurant_context, branch_context),
            "audio": {
                "input": AUDIO_INPUT,
                "output": {"voice": voz_da_sessao},
            },
            "tools": [SEARCH_TOOL, CATEGORIES_TOOL],
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
    # `voz` na linha porque, com o seletor ligado, ela deixou de ser sempre a
    # mesma: sem isto nao ha como saber depois com qual voz uma sessao rodou.
    logger.info(
        "[Voz] credencial emitida | restaurant_id=%s | modelo=%s | voz=%s "
        "| expira_em=%s",
        restaurant_id,
        settings.VOICE_MODEL,
        voz_da_sessao,
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
