from fastapi import APIRouter

from src.core.config import settings
from src.schemas.common_schema import HealthResponse


router = APIRouter(prefix="/health", tags=["health"])


@router.get("", response_model=HealthResponse)
def health_check() -> HealthResponse:
    """Sinal de vida, com a versao da imagem junto.

    A rota NAO ganhou checagem de banco nem de Redis, e isso e deliberado: ela
    responde "o processo esta de pe e e este o codigo dele", nada mais. Uma
    rota de health que consulta dependencias vira candidata natural a
    `healthcheck` do compose — e ai o `start_period` passa a depender da soma
    dos timeouts do warmup, que e a armadilha 40 da skill.
    """
    return HealthResponse(
        status="ok", app=settings.APP_NAME, git_sha=settings.GIT_SHA
    )
