"""exclusao de conta: marca de anonimizacao

Revision ID: 20260816_0024
Revises: 20260815_0023
Create Date: 2026-08-16

## Uma coluna, e so

`docs/lgpd-fase2-exclusao-de-conta.md`, secao 3. A exclusao de conta e feita
por ANONIMIZACAO — a linha de `customers` sobrevive, porque
`coupon_redemptions.customer_id` e NOT NULL e um DELETE falharia para toda
pessoa que ja usou cupom. Nada mais no schema muda.

## Por que nao basta `is_active = false`

`is_active` ja significa outra coisa: conta suspensa, que e reversivel e
mantem os dados. Sem marca propria, "esta conta foi anonimizada" e "esta
conta esta bloqueada" viram o mesmo estado, e a primeira pergunta de qualquer
auditoria de LGPD — quantas exclusoes voce atendeu, e quando — deixa de ter
resposta.

## Nullable sem default

Linha antiga fica `NULL`, que e a verdade: aquelas contas nao foram
anonimizadas. Um default (`now()`) marcaria a base inteira como excluida.

## O que esta revisao deliberadamente NAO faz

- **nao** relaxa o `NOT NULL` de `customers.email`/`phone`. A liberacao para
  recadastro sai de um valor SENTINELA derivado do id, nao de NULL: relaxar
  os dois obrigaria a conferir todo caminho que le esses campos sem checar
  nulo, e ha pelo menos um em fluxo de dinheiro
  (`PaymentService._resolve_payer_email`);
- **nao** mexe no `NOT NULL` de `coupon_redemptions.customer_id`. Ele e o que
  impede o DELETE, e impedir o DELETE e o desenho, nao o obstaculo.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "20260816_0024"
down_revision: Union[str, None] = "20260815_0023"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "customers",
        sa.Column("anonymized_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )


def downgrade() -> None:
    # Derruba so a MARCA. Os dados anonimizados nao voltam por aqui — eles
    # foram sobrescritos, nao arquivados.
    op.drop_column("customers", "anonymized_at")
