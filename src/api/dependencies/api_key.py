import hmac

from fastapi import Header, HTTPException, status

from src.core.config import settings


def validate_internal_api_key(x_api_key: str | None = Header(default=None)) -> None:
    # compare_digest evita vazar o prefixo correto da chave por timing.
    # Comparado em bytes porque compare_digest so aceita str ASCII.
    if not x_api_key or not hmac.compare_digest(
        x_api_key.encode("utf-8"),
        settings.INTERNAL_API_KEY.encode("utf-8"),
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Chave interna inválida",
        )
