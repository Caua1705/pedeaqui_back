"""cria idempotency_keys

Revision ID: 20260726_0002
Revises: 20260726_0001
Create Date: 2026-07-26
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260726_0002"
down_revision: Union[str, None] = "20260726_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "idempotency_keys",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        # Valor cru do header Idempotency-Key enviado pelo cliente.
        sa.Column("key", sa.Text(), nullable=False),
        # restaurante + rota + identidade de quem chamou. Ver
        # src/services/idempotency_service.py:build_scope.
        sa.Column("scope", sa.Text(), nullable=False),
        # sha256 do corpo canonicalizado, para detectar reuso da mesma chave
        # com payload diferente.
        sa.Column("request_fingerprint", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("response_body", postgresql.JSONB(), nullable=True),
        sa.Column(
            "order_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("orders.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('in_progress', 'completed')",
            name="ck_idempotency_keys_status",
        ),
    )

    # A garantia central: duas requisicoes com a mesma chave no mesmo escopo
    # nao podem coexistir. O INSERT ... ON CONFLICT DO NOTHING se apoia neste
    # indice, e e ele que serializa requisicoes concorrentes.
    op.create_unique_constraint(
        "uq_idempotency_keys_scope_key",
        "idempotency_keys",
        ["scope", "key"],
    )

    # Usado apenas pela rotina de limpeza (scripts/cleanup_idempotency_keys.py).
    op.create_index(
        "ix_idempotency_keys_expires_at",
        "idempotency_keys",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_idempotency_keys_expires_at", table_name="idempotency_keys")
    op.drop_constraint(
        "uq_idempotency_keys_scope_key",
        "idempotency_keys",
        type_="unique",
    )
    op.drop_table("idempotency_keys")
