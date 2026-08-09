"""setores de impressao e vinculo do produto

Revision ID: 20260809_0011
Revises: 20260806_0010
Create Date: 2026-08-09

A impressao das comandas passa a ser montada aqui dentro
(`GET /admin/orders/{id}/print-jobs`), e para isso falta uma coisa no
schema: saber QUAL impressora prepara cada item.

Por que `printing_sectors` pende de FILIAL e nao de restaurante: a
impressora e um objeto fisico dentro de uma loja. "Cozinha" da unidade do
Centro e "Cozinha" da unidade da Aldeota sao duas maquinas diferentes,
ligadas a dois computadores diferentes. Pendurar o setor no restaurante
faria a comanda da Aldeota sair no Centro.

`products.printing_sector_id` e NULLABLE, e o nulo tem significado: o
produto nao gera via de producao. E o caso da lata de refrigerante, que sai
da geladeira do balcao e nao passa por nenhuma praca — imprimir uma comanda
para ela so gasta papel e treina a cozinha a ignorar comanda.

O produto continua sendo do restaurante (a tabela `products` nao tem
filial), entao um produto so pode apontar para o setor de UMA filial. Num
restaurante com varias lojas isso e uma limitacao real, e ela nao e
silenciosa: `AdminPrintingService` manda para uma via "SEM SETOR" todo item
cujo setor nao seja daquela filial, em vez de deixar o item sumir da
comanda.

UNIQUE em (branch_id, name) aqui, diferente do que a 0010 evitou: aquela
tabela ja existia em producao com dados aplicados a mao e podia ter
duplicidade de antes. Esta nasce vazia, entao a restricao e de graca. O
service ainda confere antes para responder 409 com mensagem em vez de
IntegrityError.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260809_0011"
down_revision: Union[str, None] = "20260806_0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "printing_sectors",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "branch_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("branches.id"),
            nullable=False,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        # NOT NULL com default nos dois: a listagem ordena por sort_order e
        # filtra por is_active sem tratar nulo, e um nulo aqui viraria setor
        # que nao aparece em lugar nenhum da tela.
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
        ),
    )
    # Cobre a listagem do painel ("setores desta filial, na ordem") e serve
    # de prefixo para a conferencia de escopo, que sempre passa por branch_id.
    op.create_index(
        "ix_printing_sectors_branch_sort_order",
        "printing_sectors",
        ["branch_id", "sort_order"],
    )
    op.create_unique_constraint(
        "uq_printing_sectors_branch_name",
        "printing_sectors",
        ["branch_id", "name"],
    )

    op.add_column(
        "products",
        sa.Column(
            "printing_sector_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("printing_sectors.id"),
            nullable=True,
        ),
    )
    # A aplicacao em massa por categoria ("todos os produtos de Bebidas vao
    # para o Bar") e a unica escrita que varre produto por setor; sem indice
    # ela e um seq scan na tabela inteira de produtos do banco.
    op.create_index(
        "ix_products_printing_sector",
        "products",
        ["printing_sector_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_products_printing_sector", table_name="products")
    op.drop_column("products", "printing_sector_id")
    op.drop_constraint(
        "uq_printing_sectors_branch_name", "printing_sectors", type_="unique"
    )
    op.drop_index("ix_printing_sectors_branch_sort_order", table_name="printing_sectors")
    op.drop_table("printing_sectors")
