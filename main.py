from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware

from slowapi.errors import RateLimitExceeded

from src.api import chat
from src.api.middleware.body_size import BodySizeLimitMiddleware
from src.api.rate_limit import limiter, rate_limit_exceeded_handler
from src.api.validation_errors import log_contract_validation_error
from src.api.endpoints import (
    admin_auth,
    admin_menu,
    admin_orders,
    admin_reports,
    auth,
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

    "http://localhost:5500",
    "http://127.0.0.1:5500",

    "http://localhost:5501",
    "http://127.0.0.1:5501",

    "http://localhost:5173",
    "http://127.0.0.1:5173",
]

app.add_middleware(
    BodySizeLimitMiddleware,
    max_body_bytes=settings.MAX_REQUEST_BODY_BYTES,
)

# Adicionado por ultimo para ficar na camada mais externa: assim ate as
# respostas de erro (413, 429) saem com os cabecalhos de CORS.
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(restaurants.router)
app.include_router(menu.router)
app.include_router(auth.router)
app.include_router(customers.router)
app.include_router(delivery.router)
app.include_router(orders.router)
app.include_router(payments.router)
app.include_router(coupons.router)
app.include_router(coupons.admin_router)
app.include_router(admin_auth.router)
app.include_router(admin_orders.router)
app.include_router(admin_menu.router)
app.include_router(admin_reports.router)
app.include_router(chat.router)
