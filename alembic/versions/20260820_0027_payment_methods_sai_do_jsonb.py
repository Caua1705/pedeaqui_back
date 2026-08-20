"""o payment_methods jsonb sai de restaurant_settings

Revision ID: 20260820_0027
Revises: 20260820_0026
Create Date: 2026-08-20

## O que sai

`restaurant_settings.payment_methods` (jsonb), a lista de formas de pagamento
do restaurante. Ela e **dado morto desde que `branch_payment_methods`
existe**: quem decide o que a loja aceita e aquela tabela, por filial, e e ela
que `POST /orders` consulta em `_resolve_payment_flow`.

O jsonb sobrevivia num lugar so — ecoado dentro de `settings` na resposta de
`GET /restaurants/{slug}/menu` — e ali ele era pior que inutil: podia
discordar do que a filial de fato aceita, e o app que acreditasse nele
mostraria ao cliente uma forma de pagamento que o pedido recusa com 400
(armadilha 15). A revisao 20260818_0025 deixou a remocao para ca de proposito,
por ser mudanca de contrato publico.

## Por que revisao separada da 0026

E o unico DROP irreversivel do passo, e nao tem nada a ver com cardapio por
filial. Separada, a decisao de descer a 0026 as 3h da manha e sobre uma coisa
so.

## O downgrade nao devolve o dado

Ele recria a coluna com o default que a tabela sempre teve
(`["pix","credit_card","debit_card"]` para linha nova; nulo para as que ja
existem). O conteudo anterior nao volta, e nao ha de onde: o valor de verdade
esta em `branch_payment_methods` desde antes desta revisao.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "20260820_0027"
down_revision: Union[str, None] = "20260820_0026"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column("restaurant_settings", "payment_methods")


def downgrade() -> None:
    op.add_column(
        "restaurant_settings",
        sa.Column("payment_methods", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
