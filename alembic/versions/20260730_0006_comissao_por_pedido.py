"""comissao da plataforma gravada por pedido

Revision ID: 20260730_0006
Revises: 20260730_0005
Create Date: 2026-07-30

A comissao passa a ser congelada NO PEDIDO, no momento da criacao, em vez
de calculada no fechamento do mes. O motivo e simples: o percentual do
restaurante pode mudar amanha, o cupom pode ser estornado, o produto pode
mudar de preco. Recalcular depois produz um numero diferente do que valia
quando o pedido aconteceu, e cobranca que muda sozinha vira discussao com
o lojista.

Tres colunas em vez de uma: `commission_amount` sozinho nao explica de onde
saiu. Com base e percentual gravados, o extrato e conferivel linha a linha
sem refazer conta nenhuma.

Pedidos anteriores a esta revisao ficam com comissao zero. Nao ha o que
recuperar — o percentual por restaurante nem existia — e inventar um valor
retroativo seria pior que assumir o zero.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260730_0006"
down_revision: Union[str, None] = "20260730_0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Percentual POR RESTAURANTE, nao constante global: e negociado caso a
    # caso e muda com o tempo.
    op.add_column(
        "restaurant_settings",
        sa.Column(
            "platform_commission_percent",
            sa.Numeric(5, 2),
            nullable=False,
            server_default=sa.text("10.00"),
        ),
    )
    op.create_check_constraint(
        "ck_restaurant_settings_commission_percent",
        "restaurant_settings",
        "platform_commission_percent >= 0 AND platform_commission_percent <= 100",
    )

    op.add_column(
        "orders",
        sa.Column("commission_percent", sa.Numeric(5, 2), nullable=False, server_default=sa.text("0")),
    )
    op.add_column(
        "orders",
        sa.Column("commission_base_amount", sa.Numeric(12, 2), nullable=False, server_default=sa.text("0")),
    )
    op.add_column(
        "orders",
        sa.Column("commission_amount", sa.Numeric(12, 2), nullable=False, server_default=sa.text("0")),
    )

    # O relatorio filtra por restaurante e recorta por data de criacao.
    op.create_index(
        "ix_orders_restaurant_created_at",
        "orders",
        ["restaurant_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_orders_restaurant_created_at", table_name="orders")
    op.drop_column("orders", "commission_amount")
    op.drop_column("orders", "commission_base_amount")
    op.drop_column("orders", "commission_percent")
    op.drop_constraint(
        "ck_restaurant_settings_commission_percent",
        "restaurant_settings",
        type_="check",
    )
    op.drop_column("restaurant_settings", "platform_commission_percent")
