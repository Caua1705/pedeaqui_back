"""remove a frente de funil e origem do cardapio

Revision ID: 20260822_0033
Revises: 20260822_0032
Create Date: 2026-08-22

Desfaz a revisao `20260822_0031` inteira: a tabela `menu_events` e a coluna
`orders.source_snapshot`. A frente nao vai ser usada, e o que ela deixaria
para tras nao e neutro — `menu_events` e de longe a tabela que mais cresce no
banco (ela grava quem NAO comprou), e cada INSERT do caminho quente do
cardapio pagaria os dois indices dela para alimentar um relatorio que ninguem
abre.

**A 0031 NAO foi editada.** Ela ja foi aplicada em producao, e reescrever
revisao aplicada faz o historico do Alembic e o banco discordarem em silencio
(armadilha 33). O caminho e para frente, sempre — inclusive para desfazer.

## O que sai, e o que cada saida custa

**`menu_events`** sai inteira: modelo, CHECK, as duas FKs (a simples de
`restaurant_id` e a composta que amarrava filial ao restaurante), os dois
indices e a rota que a alimentava. O `DROP TABLE` leva junto tudo o que
pendia dela, e nao ha dado a preservar: a tabela e telemetria com prazo de 90
dias, nao registro de venda.

**`orders.source_snapshot`** sai com `DROP COLUMN`, que no Postgres e
operacao de catalogo — nao reescreve a tabela, custa milissegundos mesmo com
`orders` grande. E o mesmo motivo pelo qual a 0031 pode acrescenta-la com
`NOT NULL DEFAULT` sem travar a API.

**O dado da coluna nao volta.** Quem tiver origem gravada em producao a perde
aqui, e o `downgrade` devolve a coluna com `'direct'` em toda linha — nao ha
de onde reconstruir o rotulo, porque a outra metade (o evento) foi apagada
junto. Isso e aceitavel exatamente porque a frente nunca chegou a ser usada:
nenhum front manda `source`, entao toda linha em producao ja esta em
`'direct'`.

## O downgrade recria a 0031, e nao um esqueleto dela

Mesmas colunas, mesmo CHECK, mesmas FKs, mesmos dois indices — inclusive o
BRIN sobre `occurred_at`, que nao e detalhe: ele existe porque a tabela e
append-only e ordenada no tempo, e um btree ali custaria centenas de
megabytes na tabela que mais escreve do banco. Um downgrade que devolvesse a
tabela sem os indices certos deixaria o banco parecido com a 0031 e diferente
dela onde importa.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "20260822_0033"
down_revision: Union[str, None] = "20260822_0032"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column("orders", "source_snapshot")
    # Os indices caem com a tabela; drop explicito antes so criaria a chance
    # de um deles nao existir e derrubar a revisao no meio.
    op.drop_table("menu_events")


def downgrade() -> None:
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
        sa.Column(
            "branch_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("branches.id"),
            nullable=False,
        ),
        sa.Column("session_id", sa.Text(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
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
        sa.CheckConstraint(
            "event_type IN ('menu_view', 'product_view', 'cart_add', 'checkout_start')",
            name="ck_menu_events_event_type",
        ),
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
