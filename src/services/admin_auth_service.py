"""Autenticacao de lojista.

Substitui a X-API-Key compartilhada das rotas /admin. A diferenca que
importa nao e o formato do credencial: e que a chave unica nao dizia QUEM
estava chamando nem de qual restaurante, entao nao havia como filtrar nada
por tenant. O token carrega restaurant_id, e e dele que as rotas passam a
tirar o escopo.
"""

import logging
import time
import uuid
from datetime import timedelta
from time import perf_counter

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from src.core.config import settings
from src.models.admin_user_model import AdminUser
from src.repositories.admin_user_repository import AdminUserRepository
from src.schemas.admin_auth_schema import (
    AdminLoginRequest,
    AdminLoginResponse,
    AdminUserResponse,
)
from src.utils.normalization import normalize_email
from src.utils.security import (
    TokenExpiredError,
    TokenInvalidError,
    admin_auth_secret,
    create_signed_token,
    decode_signed_token,
    verify_password,
)


logger = logging.getLogger("uvicorn.error")

ADMIN_TOKEN_PURPOSE = "admin_access"
# Piso de latencia para que o tempo de resposta nao diga se o e-mail existe,
# mesmo padrao ja adotado em forgot_password na Fase 0.
LOGIN_MIN_SECONDS = 0.4
INVALID_CREDENTIALS = "Credenciais invalidas"


class AdminAuthService:
    def __init__(self, db: Session):
        self.db = db
        self.repository = AdminUserRepository(db)

    def login(self, payload: AdminLoginRequest) -> AdminLoginResponse:
        started_at = perf_counter()
        admin_user = self.repository.get_by_email(normalize_email(payload.email))
        # verify_password roda mesmo sem usuario para nao vazar a existencia
        # do e-mail pela diferenca de tempo do bcrypt.
        password_hash = admin_user.password_hash if admin_user else None
        password_ok = verify_password(payload.password, password_hash)

        if not admin_user or not password_ok:
            self._pad_latency(started_at)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=INVALID_CREDENTIALS,
            )
        if not admin_user.is_active:
            self._pad_latency(started_at)
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Usuario inativo",
            )

        logger.info(
            "[AdminAuth] login admin_user_id=%s restaurant_id=%s role=%s",
            admin_user.id,
            admin_user.restaurant_id,
            admin_user.role,
        )
        return AdminLoginResponse(
            access_token=self.create_access_token(admin_user),
            token_type="bearer",
            admin_user=AdminUserResponse.model_validate(admin_user),
        )

    @staticmethod
    def create_access_token(admin_user: AdminUser) -> str:
        return create_signed_token(
            subject=str(admin_user.id),
            purpose=ADMIN_TOKEN_PURPOSE,
            expires_delta=timedelta(minutes=settings.ADMIN_ACCESS_TOKEN_MINUTES),
            extra={
                "type": "admin",
                # Informativos para o painel. A autorizacao NAO se apoia
                # neles: get_admin_from_token recarrega o usuario do banco,
                # senao um lojista desativado ou movido de restaurante
                # continuaria valendo ate o token expirar.
                "restaurant_id": str(admin_user.restaurant_id),
                "role": admin_user.role,
            },
            secret=admin_auth_secret(),
        )

    def get_admin_from_token(self, token: str) -> AdminUser:
        try:
            payload = decode_signed_token(
                token, ADMIN_TOKEN_PURPOSE, secret=admin_auth_secret()
            )
        except TokenExpiredError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expirado"
            ) from exc
        except TokenInvalidError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Token invalido"
            ) from exc

        if payload.get("type") != "admin":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Token invalido"
            )

        try:
            admin_user_id = uuid.UUID(payload["sub"])
        except (KeyError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Token invalido"
            ) from exc

        admin_user = self.repository.get_by_id(admin_user_id)
        if admin_user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Token invalido"
            )
        if not admin_user.is_active:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="Usuario inativo"
            )
        return admin_user

    @staticmethod
    def _pad_latency(started_at: float) -> None:
        remaining = LOGIN_MIN_SECONDS - (perf_counter() - started_at)
        if remaining > 0:
            time.sleep(remaining)
