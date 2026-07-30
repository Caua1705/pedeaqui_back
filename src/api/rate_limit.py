"""Rate limiting por IP nas rotas publicas.

Biblioteca: slowapi (port do Flask-Limiter para Starlette/FastAPI, em cima
do pacote `limits`).

Por que ela:
- e decorator por rota, entao da para ter limites diferentes em login,
  criacao de pedido e /chat sem tocar no Traefik;
- o mesmo codigo roda com storage em memoria (1 worker, que e o caso hoje)
  ou em Redis so setando REDIS_URL, que ja e uma variavel do projeto. Quando
  a API escalar para varios workers/containers o limite continua valendo sem
  reescrita;
- dependencia pequena e sincrona, compativel com os endpoints `def` que o
  projeto usa (fastapi-limiter, a alternativa mais citada, exige Redis
  obrigatorio e handlers async).

Atencao ao IP: atras do Traefik o socket peer e o proprio proxy, o que
colocaria todos os clientes no mesmo balde. A chave sai de
RATE_LIMIT_CLIENT_IP_HEADER (por padrao X-Real-Ip, que o Traefik sobrescreve
com o IP real do cliente), com fallback para o socket.
"""

from fastapi import Request
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from starlette.responses import JSONResponse

from src.core.config import settings


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


def client_ip(request: Request) -> str:
    header = settings.RATE_LIMIT_CLIENT_IP_HEADER.strip().lower()
    if header:
        value = request.headers.get(header)
        if value:
            # X-Forwarded-For pode vir com uma lista; o primeiro salto e o cliente.
            return value.split(",")[0].strip()
    return get_remote_address(request)


limiter = Limiter(
    key_func=client_ip,
    enabled=settings.RATE_LIMIT_ENABLED,
    storage_uri=settings.REDIS_URL or "memory://",
    # Se o Redis cair, a API continua respondendo sem limite em vez de
    # derrubar todas as rotas publicas.
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
    return request.app.state.limiter._inject_headers(
        response,
        request.state.view_rate_limit,
    )
