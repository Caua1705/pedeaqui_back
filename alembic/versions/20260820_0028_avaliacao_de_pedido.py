"""avaliacao de pedido: nota, motivo do problema e comentario

Revision ID: 20260820_0028
Revises: 20260820_0027
Create Date: 2026-08-20

Atraso e pedido errado sao a reclamacao numero 1 do consumidor de delivery, e
ate aqui o restaurante so descobria quando o cliente reclamava em outro lugar.
Esta tabela e o unico canal de retorno direto.

## O que a tabela NAO tem, e por que

**Nao tem `branch_id` nem `restaurant_id`.** Os dois estao em `orders`, e a
consulta do painel ja precisa entrar la de qualquer jeito para trazer o
`order_number`. Repetir as colunas aqui criaria a possibilidade de a
avaliacao dizer uma filial e o pedido dizer outra — que e exatamente a classe
de divergencia que a revisao 20260820_0026 teve que fechar com FK composta
depois de o problema existir. Uma fonte da verdade, e o `JOIN` e por chave
primaria.

**Nao tem `customer_id`.** Pelo mesmo motivo: quem sabe de quem e o pedido e
`orders.customer_id`, e ele e NULO no pedido de convidado. Uma coluna aqui so
repetiria o nulo. O que isso custa para a LGPD esta em
`docs/avaliacao-de-pedido.md`.

## `rating` e smallint, nao numeric

Nota nao e dinheiro: nao ha centavo, nao ha arredondamento e a regra do
`Decimal` (e a armadilha 34) nao se aplica. O CHECK de 1 a 5 fica no banco
porque e ele que impede a nota 0 ou 11 chegar por um caminho que ninguem
previu.

## `problem_tag` e text com CHECK, e espelha uma constante

Mesmo formato de `cashback_transactions.type`. E o mesmo par de listas da
armadilha 15: `REVIEW_PROBLEM_TAGS` (core/constants.py) espelha este CHECK,
e as duas mudam JUNTAS. Se uma etiqueta entrar so no banco, o schema recusa
o valor na validacao antes de chegar la; se entrar so na constante, o INSERT
morre no CHECK.

Nulo e o normal: o campo so e perguntado quando a nota e baixa.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "20260820_0028"
down_revision: Union[str, None] = "20260820_0027"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "order_reviews",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "order_id",
            postgresql.UUID(as_uuid=True),
            # Sem ON DELETE: pedido nao e apagado neste projeto, e se um dia
            # for, uma avaliacao orfa e pior que o erro de FK que avisa.
            sa.ForeignKey("orders.id"),
            nullable=False,
        ),
        sa.Column("rating", sa.SmallInteger(), nullable=False),
        sa.Column("problem_tag", sa.Text(), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
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
        sa.CheckConstraint("rating BETWEEN 1 AND 5", name="ck_order_reviews_rating"),
        sa.CheckConstraint(
            "problem_tag IS NULL OR problem_tag IN "
            "('atrasou', 'veio_errado', 'veio_frio', 'faltou_item', 'qualidade', 'outro')",
            name="ck_order_reviews_problem_tag",
        ),
    )
    # Um pedido, uma avaliacao. E a regra de "uma vez so" escrita onde ela
    # nao depende de ninguem lembrar: sem isto, duas requisicoes simultaneas
    # do mesmo token gravariam duas notas para o mesmo pedido.
    op.create_unique_constraint("uq_order_reviews_order_id", "order_reviews", ["order_id"])
    # Serve as DUAS varreduras por data: o filtro de periodo do painel e o
    # expurgo do texto no container de limpeza.
    #
    # Estrito, sem `if_not_exists`: a tabela nasce nesta revisao, entao nao ha
    # colisao possivel e a permissividade so esconderia erro (armadilha 4).
    op.create_index("ix_order_reviews_created_at", "order_reviews", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_order_reviews_created_at", table_name="order_reviews")
    op.drop_constraint("uq_order_reviews_order_id", "order_reviews", type_="unique")
    op.drop_table("order_reviews")
