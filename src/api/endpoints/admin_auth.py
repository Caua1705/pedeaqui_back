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
)
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
