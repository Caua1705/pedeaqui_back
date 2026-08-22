"""identificador de origem no pedido e funil do cardapio

Revision ID: 20260822_0031
Revises: 20260821_0030
Create Date: 2026-08-22

Ate aqui a plataforma so enxergava PEDIDO. Poucos pedidos tinham dois
diagnosticos opostos — ninguem entrou no cardapio, ou entrou e nao comprou —
e nenhuma forma de distinguir os dois. Desenho completo em
`docs/funil-e-origem.md`.

## Sao DUAS coisas, com naturezas diferentes

**`orders.source_snapshot` e atributo da VENDA.** Fica para sempre, como
todo snapshot do pedido: o lojista pergunta em marco quanto o ima de
geladeira vendeu em janeiro.

**`menu_events` e TELEMETRIA de quem nao comprou.** Vence em 90 dias
(`menu_event_service.menu_event_retention_cutoff`).

Misturar as duas quebra exatamente isso: com a origem so na tabela de
evento, o relatorio de origem morreria junto com o primeiro expurgo.

## Por que `menu_events` nao tem `customer_id` — e `orders` nao tem sessao

Os quatro degraus se contam por `(filial, dia, origem)`, e o quinto (o
pedido) sai de `orders` pelo mesmo recorte. **Desenhar o funil nao exige
ligar sessao a pedido.** Se exigisse, o rastro de navegacao passaria a estar
amarrado a uma linha com nome, telefone e endereco.

Consequencia assumida: a exclusao de conta nao alcanca `menu_events`, porque
nao ha por onde. E a mesma situacao de `ai_feedback` e do comentario de
`order_reviews`, e a resposta e a mesma das duas — a RETENCAO e o mecanismo
de exclusao desta tabela, nao uma faxina de disco.

## A coluna em `orders` entra SEM indice, de proposito

`ALTER TABLE ... ADD COLUMN ... NOT NULL DEFAULT 'direct'` nao reescreve a
tabela no PG 11+: o default fica no catalogo e as linhas antigas o leem de
la. Custa milissegundos mesmo com `orders` grande.

`CREATE INDEX` custaria o oposto. O `docker-entrypoint.sh` roda
`alembic upgrade head` com a API FORA DO AR (armadilha 5), e um indice sobre
a maior tabela do banco nesse ponto e a operacao parada enquanto ele
constroi. Os relatorios ja filtram por restaurante, filial e periodo — que os
indices existentes cobrem — e a origem entra como predicado adicional sobre
um conjunto ja pequeno. Se um dia ela precisar de indice proprio, ele nasce a
mao com `CREATE INDEX CONCURRENTLY` e um `alembic stamp` em seguida.

## Os dois indices de `menu_events`, e por que um deles e BRIN

O btree `(branch_id, occurred_at)` serve o relatorio. O expurgo varre por
data SEM filial e nao aproveita esse indice — a coluna lider nao bate.

O segundo e BRIN sobre `occurred_at`, e nao um btree, porque a tabela e
append-only e ordenada no tempo: e o caso de livro do BRIN. Ele ocupa
kilobytes onde o btree ocuparia centenas de megabytes, e o tamanho importa
porque esta tabela escreve muito mais do que le — todo indice e custo em
cada INSERT do caminho quente do cardapio.

Os dois sao ESTRITOS, sem `if_not_exists`: a tabela nasce nesta revisao,
entao nao ha colisao possivel e a permissividade so esconderia erro
(armadilha 4).
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "20260822_0031"
down_revision: Union[str, None] = "20260821_0030"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "menu_events",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "restaurant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("restaurants.id"),
            nullable=False,
        ),
        # NOT NULL: o cardapio e da filial (armadilha 36), e evento sem loja
        # nao responde nada. "As duas somadas" e um GROUP BY, nao um nulo.
        sa.Column(
            "branch_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("branches.id"),
            nullable=False,
        ),
        sa.Column("session_id", sa.Text(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        # Sem ON DELETE: nada some do cardapio, so desativa.
        sa.Column(
            "product_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("products.id"),
            nullable=True,
        ),
        sa.Column(
            "occurred_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        # Espelha MENU_EVENT_TYPES (core/constants.py). As duas listas mudam
        # JUNTAS — armadilha 15.
        sa.CheckConstraint(
            "event_type IN ('menu_view', 'product_view', 'cart_add', 'checkout_start')",
            name="ck_menu_events_event_type",
        ),
        # A filial tem que ser DAQUELE restaurante. Mesma amarra composta que
        # a revisao 20260820_0026 poe em `categories` e `products`, e pela
        # mesma licao (armadilha 13): quando a regra e "estas duas colunas nao
        # podem divergir", o lugar de escreve-la e o schema — a alternativa
        # seria um SELECT de conferencia por requisicao na rota mais quente da
        # API, que custa mais e ainda pode ser esquecido.
        #
        # Aproveita `uq_branches_restaurant_id`, o UNIQUE (restaurant_id, id)
        # que aquela revisao criou.
        sa.ForeignKeyConstraint(
            ["restaurant_id", "branch_id"],
            ["branches.restaurant_id", "branches.id"],
            name="fk_menu_events_branch_do_restaurante",
        ),
    )
    op.create_index(
        "ix_menu_events_branch_occurred_at",
        "menu_events",
        ["branch_id", "occurred_at"],
    )
    op.create_index(
        "ix_menu_events_occurred_at_brin",
        "menu_events",
        ["occurred_at"],
        postgresql_using="brin",
    )

    op.add_column(
        "orders",
        sa.Column(
            "source_snapshot",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'direct'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("orders", "source_snapshot")
    op.drop_index("ix_menu_events_occurred_at_brin", table_name="menu_events")
    op.drop_index("ix_menu_events_branch_occurred_at", table_name="menu_events")
    op.drop_table("menu_events")
