"""Injeta o lojista autenticado nas rotas /admin.

Toda rota /admin passa a receber um AdminUser, e o `restaurant_id` dele e a
unica fonte de escopo aceita. Nenhuma rota deve confiar em restaurante vindo
do path ou do corpo sem confrontar com este valor — era exatamente essa
confianca que permitia ler pedido de outro restaurante.
"""

import uuid

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from src.api.dependencies.database import get_db
from src.models.admin_user_model import AdminUser
from src.services.admin_auth_service import AdminAuthService


bearer_scheme = HTTPBearer(auto_error=False)


def get_current_admin(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> AdminUser:
    if not credentials or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Token ausente"
        )
    return AdminAuthService(db).get_admin_from_token(credentials.credentials)


def ensure_restaurant_scope(admin_user: AdminUser, restaurant_id: uuid.UUID) -> None:
    """Confere que o restaurante pedido na URL e o do token.

    404 em vez de 403 de proposito: um 403 confirmaria que aquele
    restaurant_id existe, e um lojista nao precisa saber quem mais esta na
    plataforma. Para ele, o que nao e dele simplesmente nao existe.
    """
    if admin_user.restaurant_id != restaurant_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Restaurante nao encontrado"
        )
