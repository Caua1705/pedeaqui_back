"""adiciona estrutura de pagamento em orders

Revision ID: 20260730_0004
Revises: 20260726_0003
Create Date: 2026-07-30

Colunas de pagamento do pedido. O gateway ainda nao esta plugado: o que
esta revisao cria e o lugar onde a confirmacao vai ser gravada.

Pedidos que ja existem no banco foram todos pagos na entrega (nao havia
outro caminho), entao o backfill os marca como `on_delivery`. Se algum dia
essa premissa mudar, mude o backfill junto.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260730_0004"
down_revision: Union[str, None] = "20260726_0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("payment_flow", sa.Text(), nullable=True))
    # Entra nullable para o backfill rodar sobre as linhas existentes e so
    # depois vira NOT NULL. Fazer o contrario quebraria a migracao em
    # qualquer banco com pedido gravado.
    op.add_column("orders", sa.Column("payment_status", sa.Text(), nullable=True))
    op.add_column(
        "orders",
        sa.Column("paid_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.add_column("orders", sa.Column("payment_provider", sa.Text(), nullable=True))
    op.add_column("orders", sa.Column("provider_payment_id", sa.Text(), nullable=True))

    op.execute(
        """
        UPDATE orders
           SET payment_status = 'on_delivery',
               payment_flow = 'delivery'
         WHERE payment_status IS NULL
        """
    )

    op.alter_column(
        "orders",
        "payment_status",
        nullable=False,
        server_default=sa.text("'on_delivery'"),
    )

    op.create_check_constraint(
        "ck_orders_payment_status",
        "orders",
        "payment_status IN ('on_delivery', 'pending', 'paid', 'failed', 'refunded')",
    )
    op.create_check_constraint(
        "ck_orders_payment_flow",
        "orders",
        "payment_flow IS NULL OR payment_flow IN ('online', 'delivery')",
    )

    # O webhook chega com o id do pagamento no gateway e precisa achar o
    # pedido por ele. O indice e unico porque dois pedidos nao podem
    # reivindicar o mesmo pagamento — seria dinheiro contado duas vezes.
    # IF NOT EXISTS porque `orders` e do baseline: veio dos .sql aplicados a
    # mao, onde um indice com este nome pode ter sido criado antes do Alembic.
    op.create_index(
        "uq_orders_provider_payment",
        "orders",
        ["payment_provider", "provider_payment_id"],
        unique=True,
        postgresql_where=sa.text("provider_payment_id IS NOT NULL"),
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index("uq_orders_provider_payment", table_name="orders", if_exists=True)
    op.drop_constraint("ck_orders_payment_flow", "orders", type_="check")
    op.drop_constraint("ck_orders_payment_status", "orders", type_="check")
    op.drop_column("orders", "provider_payment_id")
    op.drop_column("orders", "payment_provider")
    op.drop_column("orders", "paid_at")
    op.drop_column("orders", "payment_status")
    op.drop_column("orders", "payment_flow")
