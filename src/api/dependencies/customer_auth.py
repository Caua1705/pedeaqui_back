from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from src.api.dependencies.database import get_db
from src.models.customer_model import Customer
from src.services.auth_service import AuthService
from src.utils.security import TokenExpiredError, TokenInvalidError


bearer_scheme = HTTPBearer(auto_error=False)


def get_current_customer(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> Customer:
    if not credentials or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token ausente")
    return AuthService(db).get_customer_from_token_or_error(credentials.credentials)


def get_optional_current_customer(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> Customer | None:
    if not credentials or credentials.scheme.lower() != "bearer":
        return None
    try:
        customer = AuthService(db).get_customer_from_token(credentials.credentials)
    except (TokenExpiredError, TokenInvalidError):
        return None
    if not customer or not customer.is_active:
        return None
    return customer
