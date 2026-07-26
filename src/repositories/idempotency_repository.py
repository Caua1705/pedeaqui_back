import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from src.models.idempotency_key_model import (
    IDEMPOTENCY_COMPLETED,
    IDEMPOTENCY_IN_PROGRESS,
    IdempotencyKey,
)


class IdempotencyRepository:
    def __init__(self, db: Session):
        self.db = db

    def reserve(
        self,
        *,
        scope: str,
        key: str,
        request_fingerprint: str,
        expires_at: datetime,
    ) -> uuid.UUID | None:
        """Tenta reservar a chave. Devolve o id se pegou, None se ja existia.

        `ON CONFLICT DO NOTHING` faz o trabalho pesado de concorrencia: se
        outra transacao inseriu a mesma (scope, key) e ainda nao terminou, o
        Postgres bloqueia aqui ate ela commitar ou abortar. Se commitou,
        voltamos None e o chamador le a linha dela; se abortou, a reserva e
        nossa. Nao ha janela entre checar e inserir.
        """
        stmt = (
            pg_insert(IdempotencyKey)
            .values(
                scope=scope,
                key=key,
                request_fingerprint=request_fingerprint,
                status=IDEMPOTENCY_IN_PROGRESS,
                expires_at=expires_at,
            )
            .on_conflict_do_nothing(constraint="uq_idempotency_keys_scope_key")
            .returning(IdempotencyKey.id)
        )
        return self.db.execute(stmt).scalar_one_or_none()

    def get(self, *, scope: str, key: str) -> IdempotencyKey | None:
        stmt = select(IdempotencyKey).where(
            IdempotencyKey.scope == scope,
            IdempotencyKey.key == key,
        )
        return self.db.scalar(stmt)

    def complete(
        self,
        *,
        record_id: uuid.UUID,
        response_body: dict[str, Any],
        order_id: uuid.UUID | None,
    ) -> None:
        stmt = (
            update(IdempotencyKey)
            .where(IdempotencyKey.id == record_id)
            .values(
                status=IDEMPOTENCY_COMPLETED,
                response_body=response_body,
                order_id=order_id,
            )
        )
        self.db.execute(stmt)

    def delete_expired(self, *, now: datetime) -> int:
        stmt = delete(IdempotencyKey).where(IdempotencyKey.expires_at <= now)
        return self.db.execute(stmt).rowcount or 0
