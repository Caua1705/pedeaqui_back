"""credenciais de pagamento por restaurante

Revision ID: 20260801_0008
Revises: 20260730_0007
Create Date: 2026-08-01

Ate aqui, a credencial do Mercado Pago era uma variavel global do .env
(MERCADOPAGO_ACCESS_TOKEN): valia para a plataforma inteira, e uma cobranca
nunca sabia em nome de qual restaurante estava sendo criada. Isso so
funciona enquanto existir UM restaurante de verdade; com mais de um, a
plataforma cobraria todo mundo na mesma conta.

Esta tabela guarda a credencial POR RESTAURANTE. Duas colunas, tratamento
bem diferente:

  - `public_key` nao e segredo (o proprio Mercado Pago manda expor no
    frontend), fica em texto puro.
  - `access_token` e a credencial da CONTA DO RESTAURANTE — dinheiro de
    terceiro passa por ela. Fica cifrada (Fernet, ver src/utils/crypto.py);
    a chave de cifra mora so no .env, nunca no banco.

Uma linha por (restaurante, ambiente) em vez de duas colunas de token no
restaurante: o lojista cadastra a de teste primeiro e a de producao depois,
sem uma sobrescrever a outra, e o ambiente ativo e uma variavel global
(MERCADOPAGO_ENVIRONMENT) — trocar de teste para producao nao mexe em
banco nem em codigo.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260801_0008"
down_revision: Union[str, None] = "20260730_0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "restaurant_payment_credentials",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "restaurant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("restaurants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("environment", sa.Text(), nullable=False),
        sa.Column("public_key", sa.Text(), nullable=False),
        sa.Column("access_token_encrypted", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_check_constraint(
        "ck_restaurant_payment_credentials_environment",
        "restaurant_payment_credentials",
        "environment IN ('test', 'production')",
    )
    # Uma credencial por (restaurante, ambiente): cadastrar de novo para o
    # mesmo ambiente e um UPDATE, nao uma segunda linha concorrendo.
    op.create_unique_constraint(
        "uq_restaurant_payment_credentials_restaurant_environment",
        "restaurant_payment_credentials",
        ["restaurant_id", "environment"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_restaurant_payment_credentials_restaurant_environment",
        "restaurant_payment_credentials",
        type_="unique",
    )
    op.drop_constraint(
        "ck_restaurant_payment_credentials_environment",
        "restaurant_payment_credentials",
        type_="check",
    )
    op.drop_table("restaurant_payment_credentials")
