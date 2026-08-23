from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware

from slowapi.errors import RateLimitExceeded

from src.api import chat
from src.api.middleware.body_size import BodySizeLimitMiddleware
from src.api.middleware.rate_limit_state import RateLimitStateMiddleware
from src.api.rate_limit import limiter, rate_limit_exceeded_handler
from src.api.validation_errors import log_contract_validation_error
from src.api.endpoints import (
    admin_auth,
    admin_cashback,
    admin_customers,
    admin_menu,
    admin_orders,
    admin_printing,
    admin_reports,
    admin_reviews,
    admin_settings,
    admin_users,
    auth,
    branches,
    coupons,
    customers,
    delivery,
    health,
    menu,
    orders,
    payments,
    restaurants,
)
from src.core.config import settings
from src.core.startup_checks import validate_settings


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Falha aqui derruba o boot com mensagem explicita, em vez de deixar a
    # API subir e recusar silenciosamente todo pedido de entrega.
    validate_settings(settings)
    yield


# Em producao /docs, /redoc e /openapi.json ficam desligados: o schema
# completo entrega a superficie de ataque inteira a quem so achou o dominio.
_docs_enabled = settings.api_docs_enabled

app = FastAPI(
    title=settings.APP_NAME,
    description="Backend API for Rapidex white-label restaurant ordering.",
    version="0.1.0",
    docs_url="/docs" if _docs_enabled else None,
    redoc_url="/redoc" if _docs_enabled else None,
    openapi_url="/openapi.json" if _docs_enabled else None,
    lifespan=lifespan,
)

app.add_exception_handler(RequestValidationError, log_contract_validation_error)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)

origins = [
    "https://pederapidex.com",
    "https://www.pederapidex.com",

    # Painel do lojista. Origem separada da loja porque e outro app: e para
    # ela que vao as rotas /admin, inclusive o EventSource do stream de
    # pedidos, que so abre se a origem estiver aqui.
    "https://admin.pederapidex.com",

    "http://localhost:5500",
    "http://127.0.0.1:5500",

    "http://localhost:5501",
    "http://127.0.0.1:5501",

    "http://localhost:5173",
    "http://127.0.0.1:5173",
]

# Previews do painel na Vercel. Nao da para listar: o subdominio muda a cada
# deploy — por commit (`rapidex-admin-<hash>-...`) e por branch
# (`rapidex-admin-git-<branch>-...`).
#
# O escopo `cauas-projects-3c9f6aea` faz parte do padrao de proposito. Sem
# ele, `.*\.vercel\.app` aceitaria qualquer app hospedado na Vercel — de
# qualquer pessoa — chamando a API com credenciais.
#
# O `$` no fim tambem nao e decoracao: o Starlette de hoje compara com
# `fullmatch`, mas versoes antigas usavam `match`, e ai um
# `...vercel.app.atacante.com` passaria.
origin_regex = r"^https://rapidex-admin-(git-)?[a-z0-9-]+-cauas-projects-3c9f6aea\.vercel\.app$"

app.add_middleware(
    BodySizeLimitMiddleware,
    max_body_bytes=settings.MAX_REQUEST_BODY_BYTES,
    max_upload_bytes=settings.MAX_IMAGE_UPLOAD_BYTES,
)

# Precisa rodar antes da rota, para o atributo existir quando o wrapper do
# @limiter.limit for le-lo. Ver o modulo para o 500 que isto evita.
app.add_middleware(RateLimitStateMiddleware)

# Adicionado por ultimo para ficar na camada mais externa: assim ate as
# respostas de erro (413, 429) saem com os cabecalhos de CORS.
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_origin_regex=origin_regex,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(restaurants.router)
app.include_router(branches.router)
app.include_router(menu.router)
app.include_router(auth.router)
app.include_router(customers.router)
app.include_router(delivery.router)
app.include_router(orders.router)
app.include_router(payments.router)
app.include_router(coupons.router)
app.include_router(coupons.admin_router)
app.include_router(coupons.template_router)
app.include_router(admin_auth.router)
app.include_router(admin_users.router)
app.include_router(admin_orders.router)
app.include_router(admin_menu.router)
app.include_router(admin_printing.router)
app.include_router(admin_settings.router)
app.include_router(admin_cashback.router)
app.include_router(admin_customers.router)
app.include_router(admin_reports.router)
app.include_router(admin_reviews.router)
app.include_router(chat.router)

# Atendimento por voz. `VOICE_ENABLED` e a CHAVE MESTRA: desligada, as rotas
# nao existem — nem no /docs — e nenhum restaurante tem voz, por mais que a
# coluna `restaurant_settings.voice_enabled` diga o contrario.
#
# Ligada, ela nao acende a voz para ninguem sozinha: cada restaurante ainda
# precisa da propria habilitacao. Ver `src/ai/voice/session_service.py`.
#
# O import fica aqui dentro, e nao no topo: desligado, o pacote de voz nem
# chega a ser carregado.
if settings.VOICE_ENABLED:
    from src.api import voice as voice_router

    app.include_router(voice_router.router)
