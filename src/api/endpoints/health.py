from fastapi import APIRouter

from src.core.config import settings
from src.schemas.common_schema import HealthResponse


router = APIRouter(prefix="/health", tags=["health"])


@router.get("", response_model=HealthResponse)
def health_check() -> HealthResponse:
    return HealthResponse(status="ok", app=settings.APP_NAME)
