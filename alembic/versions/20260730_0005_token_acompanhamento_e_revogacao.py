"""token opaco de acompanhamento e revogacao de token de cliente

Revision ID: 20260730_0005
Revises: 20260730_0004
Create Date: 2026-07-30

Duas colunas de seguranca.

`orders.tracking_token`: a consulta publica de pedido era
`/orders/{order_number}?phone=...`, e `order_number` vem de uma sequence
GLOBAL — o pedido 5471 e vizinho do 5472. Com um telefone em maos dava
para varrer os numeros ao redor e extrair endereco residencial completo,
itens e historico. O token opaco corta a enumeracao: ele e sorteado, so o
criador do pedido recebe, e sem ele nao ha consulta publica.

Pedidos antigos recebem um token sorteado no banco (gen_random_bytes) para
a coluna poder ser NOT NULL. Ninguem tem esses tokens em maos, entao na
pratica os pedidos antigos deixam de ser consultaveis pela rota publica —
o que e exatamente o objetivo.

`customers.password_changed_at`: trocar a senha nao invalidava os JWT ja
emitidos. Eles valem 7 dias, entao quem tivesse um token roubado seguia
dentro da conta por ate uma semana depois de a vitima trocar a senha.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260730_0005"
down_revision: Union[str, None] = "20260730_0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("tracking_token", sa.Text(), nullable=True))
    op.execute(
        """
        UPDATE orders
           SET tracking_token = encode(gen_random_bytes(24), 'hex')
         WHERE tracking_token IS NULL
        """
    )
    op.alter_column("orders", "tracking_token", nullable=False)
    op.create_unique_constraint("uq_orders_tracking_token", "orders", ["tracking_token"])

    op.add_column(
        "customers",
        sa.Column("password_changed_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("customers", "password_changed_at")
    op.drop_constraint("uq_orders_tracking_token", "orders", type_="unique")
    op.drop_column("orders", "tracking_token")
