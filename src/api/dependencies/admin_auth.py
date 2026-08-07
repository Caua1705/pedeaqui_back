"""Injeta o lojista autenticado nas rotas /admin.

Toda rota /admin recebe um AdminUser, e o `restaurant_id` dele e a unica
fonte de escopo que existe: desde a Fase 3 nenhuma rota /admin aceita
restaurante no path ou no corpo, entao nao ha o que confrontar — era
exatamente essa confianca que permitia ler pedido de outro restaurante.

Quem quiser o escopo pronto (restaurante + filial) usa `get_admin_scope`,
em `admin_scope.py`. Esta dependencia continua exposta para as duas rotas
que so precisam da identidade do lojista, nao do recorte de dados: `GET
/admin/auth/me` e a emissao do ticket de stream.
"""

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
