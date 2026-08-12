"""Rate limiting por IP nas rotas publicas.

Biblioteca: slowapi (port do Flask-Limiter para Starlette/FastAPI, em cima
do pacote `limits`).

Por que ela:
- e decorator por rota, entao da para ter limites diferentes em login,
  criacao de pedido e /chat sem tocar no Traefik;
- o mesmo codigo roda com storage em memoria (1 worker) ou em Redis so
  setando REDIS_URL, que ja e uma variavel do projeto. Quando a API escalar
  para varios workers/containers o limite continua valendo sem reescrita;
- dependencia pequena e sincrona, compativel com os endpoints `def` que o
  projeto usa (fastapi-limiter, a alternativa mais citada, exige Redis
  obrigatorio e handlers async).

---------------------------------------------------------------------------
DE ONDE SAI O IP, E POR QUE NAO DA PARA ACREDITAR NO CLIENTE
---------------------------------------------------------------------------

O balde do limite e o IP do cliente, e atras de um proxy o socket peer e o
proprio proxy — o que colocaria todo mundo no mesmo balde. Por isso o IP sai
de um CABECALHO (`RATE_LIMIT_CLIENT_IP_HEADER`).

O perigo esta ai: cabecalho e texto que o cliente tambem consegue mandar. Se
o proxy REPASSA o que chegou em vez de SOBRESCREVER, o atacante escolhe o
proprio balde e o limite vira decoracao — foi o que a auditoria de 12/08/2026
mediu em producao. O cabecalho so vale se o proxy o reescrever em TODA
requisicao. Com Cloudflare na frente, o cabecalho com essa propriedade e
`cf-connecting-ip`: o proprio Cloudflare o sobrescreve na borda e o valor que
o cliente mandar e descartado. `x-real-ip` so tem essa propriedade se o
Traefik estiver configurado para reescreve-lo.

Duas consequencias no codigo:

1. Com o cabecalho configurado e AUSENTE, nao caimos no socket peer. Cair no
   peer seria dar a todos os clientes o mesmo balde SEM ninguem perceber —
   silencioso e do lado errado. Em vez disso o balde vira `SHARED_BUCKET_KEY`,
   um balde unico e explicito: o sintoma de proxy mal configurado passa a ser
   429 no primeiro minuto de trafego, e nao seis meses de forca bruta livre.
   Errar para o lado de limitar demais e barato; o outro lado ja custou caro.

2. Valor que nao e um IP valido nao vira balde. Sem isso, um atacante que
   controle o cabecalho gera um balde novo por requisicao mandando lixo, e o
   limite deixa de existir mesmo com o proxy correto.
"""

import ipaddress
import logging

from fastapi import Request
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from starlette.responses import JSONResponse

from src.core.config import settings


logger = logging.getLogger("uvicorn.error")


# Limites por rota publica.
LOGIN_RATE_LIMIT = "10/minute"
# Login de lojista: mesma exposicao a forca bruta do login de cliente, e a
# conta vale mais (le todos os pedidos do restaurante).
ADMIN_LOGIN_RATE_LIMIT = "10/minute;60/hour"
FORGOT_PASSWORD_RATE_LIMIT = "5/minute;20/hour"
PUBLIC_ORDER_LOOKUP_RATE_LIMIT = "30/minute"
CREATE_ORDER_RATE_LIMIT = "10/minute;60/hour"
# Criar cobranca chama o gateway, que cobra por requisicao e tem limite
# proprio. Um pouco mais folgado que criar pedido porque o cliente pode
# legitimamente tentar de novo depois de um cartao recusado.
START_PAYMENT_RATE_LIMIT = "15/minute;60/hour"
CHAT_RATE_LIMIT = "20/minute;200/hour"
CHAT_FEEDBACK_RATE_LIMIT = "30/minute"

# Cadastro. Uma pessoa cria uma conta; um IP compartilhado (wi-fi do salao,
# CGNAT de operadora movel) pode legitimamente criar algumas. Vinte por hora
# cobre isso com folga e ainda assim fecha a porta de inflar a base — e cada
# cadastro dispara um e-mail pelo Resend, que tem cota paga.
REGISTER_RATE_LIMIT = "5/minute;20/hour"
# Reenvio de codigo. O AuthService ja tem cooldown e teto POR E-MAIL; este
# limite fecha o buraco que sobra, que e varrer MUITOS e-mails a partir de um
# IP so — cada tentativa e um e-mail enviado.
RESEND_EMAIL_CODE_RATE_LIMIT = "5/minute;20/hour"
# Conferencia do codigo de verificacao de e-mail. `attempts_count` ja limita
# a forca bruta de UM codigo; o que falta e o atacante trocando de e-mail a
# cada tentativa. Mais folgado que os outros porque errar o codigo digitando
# e comum e a rota nao leva a tomada de conta.
VERIFY_EMAIL_CODE_RATE_LIMIT = "10/minute;100/hour"
# Conferencia do codigo de RECUPERACAO. Mesma forma da anterior e teto por
# hora bem menor: aqui acertar o codigo entrega a conta.
VERIFY_RESET_CODE_RATE_LIMIT = "10/minute;60/hour"
# Troca efetiva da senha com o reset_token na mao. O token tem 256 bits e 15
# minutos de vida, entao o limite aqui e contencao de ruido, nao a defesa
# principal.
RESET_PASSWORD_RATE_LIMIT = "10/minute;60/hour"


# Balde unico usado quando o cabecalho confiavel nao chega. Nao e um IP de
# proposito: nenhum cliente consegue cair nele por acidente, e ele aparece
# inteiro no log.
SHARED_BUCKET_KEY = "sem-ip-confiavel"


def _valid_ip(value: str) -> str | None:
    """O primeiro salto do cabecalho, se for mesmo um IP.

    `X-Forwarded-For` pode vir como lista (`cliente, proxy1, proxy2`), e o
    primeiro item e o cliente. `CF-Connecting-IP` e `X-Real-Ip` trazem um
    valor so, e o `split` os devolve intactos.
    """
    first_hop = value.split(",")[0].strip()
    try:
        return str(ipaddress.ip_address(first_hop))
    except ValueError:
        return None


def client_ip(request: Request) -> str:
    header_name = settings.RATE_LIMIT_CLIENT_IP_HEADER.strip().lower()

    # Sem cabecalho configurado: e o modo "API exposta sem proxy na frente",
    # documentado no .env.example. Ai o socket peer E o cliente.
    if not header_name:
        return get_remote_address(request)

    raw = request.headers.get(header_name)
    if not raw:
        logger.warning(
            "[Rate limit] cabecalho %s ausente; usando balde compartilhado. "
            "O proxy na frente precisa preenche-lo em TODA requisicao.",
            header_name,
        )
        return SHARED_BUCKET_KEY

    address = _valid_ip(raw)
    if address is None:
        logger.warning(
            "[Rate limit] cabecalho %s com valor que nao e IP; usando balde "
            "compartilhado.",
            header_name,
        )
        return SHARED_BUCKET_KEY

    return address


limiter = Limiter(
    key_func=client_ip,
    enabled=settings.RATE_LIMIT_ENABLED,
    storage_uri=settings.REDIS_URL or "memory://",
    # ESTA linha e a degradacao de verdade, e a ausencia dela derrubou a API.
    #
    # Com o storage fora do ar (Redis recusando AUTH, por exemplo), o
    # `limiter.hit` levanta DENTRO de `__evaluate_limits` — antes da linha que
    # grava `request.state.view_rate_limit`. Com `swallow_errors` sozinho, o
    # slowapi engolia a excecao, a requisicao seguia SEM LIMITE NENHUM, e o
    # wrapper do decorator entao lia `request.state.view_rate_limit` para
    # injetar os cabecalhos e estourava AttributeError: 500 em toda rota
    # limitada.
    #
    # Com o fallback ligado, o mesmo `except` marca o storage como morto,
    # troca para MemoryStorage e REAVALIA o mesmo limite da rota. O limite
    # continua valendo (agora por processo), `view_rate_limit` e gravado, e o
    # slowapi ainda reconfere o Redis periodicamente, com backoff, para voltar
    # sozinho quando ele levantar.
    in_memory_fallback_enabled=True,
    # Rede de seguranca de ultima instancia, para o caso de a propria memoria
    # falhar. Sozinha ela NAO degrada — so silencia. Ver acima.
    swallow_errors=True,
)


async def rate_limit_exceeded_handler(
    request: Request,
    exc: RateLimitExceeded,
) -> JSONResponse:
    response = JSONResponse(
        status_code=429,
        content={"detail": "Muitas requisições. Tente novamente em instantes."},
    )
    # `getattr` com default porque `view_rate_limit` so existe se a avaliacao
    # do limite tiver chegado ao fim. `_inject_headers` trata None sozinho
    # (nao injeta nada), entao o 429 sai sem os cabecalhos informativos em vez
    # de virar 500 — que era o defeito do dia 12/08.
    current_limit = getattr(request.state, "view_rate_limit", None)
    return request.app.state.limiter._inject_headers(response, current_limit)
