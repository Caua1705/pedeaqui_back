"""o cpf sai do cadastro: coluna nullable e valores anulados

Revision ID: 20260812_0019
Revises: 20260812_0018
Create Date: 2026-08-12

## Por que o CPF sai

Levantamento da frente 5 (LGPD, `docs/lgpd-proposta.md`): depois do cadastro,
o CPF aparecia em exatamente dois lugares do codigo — a checagem de cadastro
repetido (`_registration_conflict`) e a resposta de `/customers/me`. **Nao ia
para o Mercado Pago, nao emitia nota, nao entrava em relatorio.**

Ou seja: a plataforma exigia, guardava para sempre e em texto puro o
documento de identidade de todo cliente para cumprir uma funcao que e-mail e
telefone — os dois ja UNIQUE na mesma tabela — cumprem igual. E o oposto da
minimizacao (Art. 6, III), e o custo de um vazamento com CPF e de outra ordem
que um com e-mail.

Confirmado com o produto que nao ha plano de nota fiscal. Se um dia houver, o
CPF volta pedido no checkout de quem quer nota — nao no cadastro de todo
mundo.

## Por que a coluna FICA, nullable, em vez de ser derrubada

`DROP COLUMN` e irreversivel e nao da segunda chance: se aparecer um uso que
o levantamento nao viu, a informacao ja foi. Anular os valores entrega a
minimizacao inteira — o dado deixa de existir — mantendo a possibilidade de
descobrir o erro antes de fechar a porta.

A coluna vazia deve ser derrubada numa revisao proxima, depois de um ciclo
confirmando que nada a le. Enquanto ela existir, o modelo a mapeia como
nullable e NADA a escreve.

## O UNIQUE fica

No Postgres, varios NULL convivem sob um indice UNIQUE — ele nao atrapalha a
anulacao em massa. E se o CPF um dia voltar, volta com a garantia de unicidade
que ja tinha, sem precisar reconstruir indice sobre a tabela cheia.

## O UPDATE e a parte destrutiva

Sao 3 clientes na base hoje, e a decisao de anular os existentes foi tomada
explicitamente. `downgrade` devolve a coluna a NOT NULL, mas **nao devolve os
CPFs**: eles nao existem mais em lugar nenhum.

O preenchimento do downgrade sai do proprio `id` da linha, e nao de uma
string fixa: a coluna e UNIQUE, entao `SET cpf = ''` em todas as linhas
violaria a constraint e o downgrade morreria no meio. O valor derivado do
uuid e distinto por linha e obviamente nao e um CPF — que e exatamente o que
se quer de um marcador de "este dado foi apagado".
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "20260812_0019"
down_revision: Union[str, None] = "20260812_0018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Nullable ANTES do UPDATE: na outra ordem o UPDATE bate no NOT NULL.
    op.alter_column("customers", "cpf", existing_type=sa.Text(), nullable=True)
    op.execute("UPDATE customers SET cpf = NULL")


def downgrade() -> None:
    # Os CPFs nao voltam — foram apagados, nao arquivados. O marcador sai do
    # id da propria linha porque a coluna e UNIQUE: um valor fixo para todo
    # mundo violaria a constraint e mataria o downgrade no meio.
    op.execute(
        "UPDATE customers SET cpf = substr(replace(id::text, '-', ''), 1, 11) "
        "WHERE cpf IS NULL"
    )
    op.alter_column("customers", "cpf", existing_type=sa.Text(), nullable=False)
