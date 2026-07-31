"""guarda a estimativa de entrega para reaproveitamento

Revision ID: 20260730_0007
Revises: 20260730_0006
Create Date: 2026-07-30

O cliente chama POST /delivery/estimate no checkout (geocode + rota, ambas
pagas no Google) e, minutos depois, POST /orders refazia as duas chamadas.
Custo dobrado por pedido, e a transacao do pedido ficava aberta durante um
I/O externo.

Esta tabela guarda o resultado ja calculado. O cliente devolve so o token; a
taxa, a distancia e o prazo saem daqui, nunca do corpo da requisicao — o
valor cobrado nao pode vir de quem paga.

TTL curto (padrao 15 min, DELIVERY_ESTIMATE_REUSE_TTL_SECONDS): a rota do
almoco nao vale para o jantar, e o preco do combustivel de amanha tambem
nao.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260730_0007"
down_revision: Union[str, None] = "20260730_0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "delivery_estimates",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("token", sa.Text(), nullable=False),
        sa.Column(
            "restaurant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("restaurants.id"),
            nullable=False,
        ),
        sa.Column(
            "branch_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("branches.id"),
            nullable=False,
        ),
        sa.Column(
            "customer_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("customers.id"),
            nullable=True,
        ),
        sa.Column("address_fingerprint", sa.Text(), nullable=False),
        sa.Column("distance_km", sa.Numeric(10, 2), nullable=True),
        sa.Column("travel_time_min", sa.Integer(), nullable=True),
        sa.Column("prep_time_min", sa.Integer(), nullable=True),
        sa.Column("prep_time_max", sa.Integer(), nullable=True),
        sa.Column("eta_min", sa.Integer(), nullable=True),
        sa.Column("eta_max", sa.Integer(), nullable=True),
        sa.Column("delivery_fee", sa.Numeric(12, 2), nullable=True),
        sa.Column("latitude", sa.Numeric(10, 7), nullable=True),
        sa.Column("longitude", sa.Numeric(10, 7), nullable=True),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
    )
    # O token e a chave de busca do pedido, e nao pode repetir.
    op.create_unique_constraint("uq_delivery_estimates_token", "delivery_estimates", ["token"])
    # Usado pela limpeza periodica (scripts/cleanup_idempotency_keys.py).
    op.create_index("ix_delivery_estimates_expires_at", "delivery_estimates", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_delivery_estimates_expires_at", table_name="delivery_estimates")
    op.drop_constraint("uq_delivery_estimates_token", "delivery_estimates", type_="unique")
    op.drop_table("delivery_estimates")
