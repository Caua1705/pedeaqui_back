"""indices para as consultas do painel do lojista

Revision ID: 20260806_0010
Revises: 20260801_0009
Create Date: 2026-08-06

Nenhuma tabela nova: a Fase 3 escreve em tabelas que ja existem
(categories, products, product_option_groups, product_options, branches,
branch_business_hours, branch_payment_methods, restaurant_settings). O que
falta e indice para as consultas que passam a existir.

Tres delas rodam o dia inteiro:

1. A listagem de pedidos do painel, agora com filtro por filial e por
   periodo, ordenada por data.
2. O stream SSE, que a cada poll pergunta "o que mudou desde X" em orders e
   em order_status_history. Sem indice por created_at isso e um seq scan a
   cada poucos segundos, por painel aberto.
3. A listagem de clientes (BLOCO D), que agrupa orders por telefone dentro
   de um restaurante.

Sem restricao UNIQUE nova de proposito: o schema de producao veio dos .sql
aplicados a mao e pode ter slug repetido de antes. Duplicidade de slug e
barrada no service (AdminMenuService), onde da para responder 409 com
mensagem em vez de estourar IntegrityError.
"""

from typing import Sequence, Union

from alembic import op


revision: str = "20260806_0010"
down_revision: Union[str, None] = "20260801_0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Cobre a listagem do painel: filtra por restaurante (+ filial) e ordena
    # por created_at desc. A ordem das colunas segue a do WHERE.
    op.create_index(
        "ix_orders_restaurant_branch_created_at",
        "orders",
        ["restaurant_id", "branch_id", "created_at"],
    )
    # Cursor do SSE: "pedidos criados depois de X neste restaurante".
    op.create_index(
        "ix_orders_restaurant_created_at",
        "orders",
        ["restaurant_id", "created_at"],
    )
    # Cursor do SSE do outro lado: mudancas de status desde X. A juncao com
    # orders para achar o restaurante e feita por order_id.
    op.create_index(
        "ix_order_status_history_created_at",
        "order_status_history",
        ["created_at"],
    )
    # Agrupamento de clientes por telefone dentro do restaurante (BLOCO D).
    op.create_index(
        "ix_orders_restaurant_customer_phone",
        "orders",
        ["restaurant_id", "customer_phone_snapshot"],
    )
    # Listagem de produtos do painel, que mostra ativos e inativos por
    # categoria — o indice do cardapio publico nao serve porque aquele
    # assume is_active.
    op.create_index(
        "ix_products_restaurant_category",
        "products",
        ["restaurant_id", "category_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_products_restaurant_category", table_name="products")
    op.drop_index("ix_orders_restaurant_customer_phone", table_name="orders")
    op.drop_index("ix_order_status_history_created_at", table_name="order_status_history")
    op.drop_index("ix_orders_restaurant_created_at", table_name="orders")
    op.drop_index("ix_orders_restaurant_branch_created_at", table_name="orders")
