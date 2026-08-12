from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from src.api.dependencies.admin_auth import get_current_admin
from src.api.dependencies.database import get_db
from src.api.rate_limit import ADMIN_LOGIN_RATE_LIMIT, limiter
from src.models.admin_user_model import AdminUser
from src.schemas.admin_auth_schema import (
    AdminLoginRequest,
    AdminLoginResponse,
    AdminUserResponse,
    ChangeAdminPasswordRequest,
)
from src.schemas.auth_schema import MessageResponse
from src.services.admin_auth_service import AdminAuthService


router = APIRouter(prefix="/admin/auth", tags=["admin auth"])


@router.post("/login", response_model=AdminLoginResponse)
@limiter.limit(ADMIN_LOGIN_RATE_LIMIT)
def login(
    request: Request,
    payload: AdminLoginRequest,
    db: Session = Depends(get_db),
) -> AdminLoginResponse:
    return AdminAuthService(db).login(payload)


@router.get("/me", response_model=AdminUserResponse)
def me(admin_user: AdminUser = Depends(get_current_admin)) -> AdminUserResponse:
    return AdminUserResponse.model_validate(admin_user)


@router.patch("/password", response_model=MessageResponse)
def change_password(
    payload: ChangeAdminPasswordRequest,
    admin_user: AdminUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
) -> MessageResponse:
    """Troca da propria senha, e a unica forma de revogar os proprios tokens.

    Sem esta rota, a senha do lojista so mudava por quem tem acesso ao
    servidor (`scripts/create_admin_user.py`) — e o `config.ini` do agente de
    impressao guarda essa senha em texto puro na maquina do balcao.

    Trocar a senha grava `password_changed_at` e derruba TODO token emitido
    antes: a sessao do painel, a de outros navegadores e o ticket do stream
    SSE. Quem estiver com o painel aberto volta para a tela de login, e o
    agente de impressao instalado com `email`/`password` refaz o login
    sozinho — o instalado com `token =` fixo para de imprimir ate alguem colar
    um token novo, que e o motivo de a instalacao com senha ser a recomendada.
    """
    return AdminAuthService(db).change_password(admin_user, payload)
