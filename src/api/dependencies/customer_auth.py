from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from src.api.dependencies.database import get_db
from src.models.customer_model import Customer
from src.services.auth_service import AuthService
from src.utils.security import TokenExpiredError, TokenInvalidError


def _extract_bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token


def get_current_customer(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> Customer:
    token = _extract_bearer_token(authorization)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token ausente")
    return AuthService(db).get_customer_from_token_or_error(token)


def get_optional_current_customer(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> Customer | None:
    token = _extract_bearer_token(authorization)
    if not token:
        return None
    try:
        customer = AuthService(db).get_customer_from_token(token)
    except (TokenExpiredError, TokenInvalidError):
        return None
    if not customer or not customer.is_active:
        return None
    return customer
