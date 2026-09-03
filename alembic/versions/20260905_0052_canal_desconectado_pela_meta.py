"""canal desconectado pela meta: o aviso que evita o silencio

Revision ID: 20260905_0052
Revises: 20260904_0051
Create Date: 2026-09-05

## O que nao existia

Nenhuma forma de saber que o lojista desconectou. Ele abre o aplicativo dele,
tira o acesso da Cloud API, e do nosso lado **nada muda**: o canal continua
`is_active`, o aviso continua sendo tentado, e a Meta responde erro de
autenticacao a cada pedido aceito. Ninguem descobre ate um cliente reclamar
que nao foi avisado.

A Meta avisa — `account_update` com `event: PARTNER_REMOVED` —, mas o aviso
nao tinha onde pousar.

## Por que DUAS colunas, e nao `is_active = false`

Sao dois estados diferentes com donos diferentes:

    is_active = false      EU desliguei este numero
    disconnected_at        A META/o LOJISTA tirou o acesso

Reaproveitar `is_active` responderia "parou" e perderia "por que" — e as duas
saidas sao opostas: a primeira se desfaz no nosso painel, a segunda so se
desfaz com o lojista reconectando. E o mesmo par de `branches.is_open`
(a pausa manual) contra o horario de funcionamento: sao duas checagens de
"fechado" que nao se substituem, e remover uma "porque a outra cobre" abre o
buraco de novo (armadilha 35).

**As duas sao conferidas JUNTAS, num lugar so** —
`whatsapp_repository._canal_utilizavel()`, com a forma SQL e a forma Python
lado a lado, pelo motivo da armadilha 54: a mesma pergunta feita em dois
lugares diverge no dia em que alguem mexe em um.

## `disconnect_reason` e TEXTO da Meta, e pode ser nulo

Vem de `disconnection_info.reason`, que a documentacao marca como
**condicional**: so aparece quando o lojista usava o aplicativo E a Cloud API.
Nulo aqui e "desconectou e nao disseram por que", que e um estado legitimo — e
nao um dado faltando.

## Sem indice

A leitura e sempre por `phone_number_id` ou por `branch_id`, que ja tem
UNIQUE, e a varredura por `waba_id` do `PARTNER_REMOVED` roda uma vez por
desconexao — que e um evento raro, sobre uma tabela de dezenas de linhas. Um
indice aqui seria custo em toda escrita para economizar um seq scan que
ninguem sente.
"""

import sqlalchemy as sa
from alembic import op


revision = "20260905_0052"
down_revision = "20260904_0051"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "whatsapp_channels",
        sa.Column("disconnected_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.add_column(
        "whatsapp_channels",
        sa.Column("disconnect_reason", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    # Voltar apaga a informacao de que o canal esta desconectado, e o codigo
    # antigo volta a tentar enviar por ele — sem erro nosso, com erro da Meta
    # em toda tentativa. E o estado de antes desta revisao; quem voltar
    # precisa saber que volta para o silencio.
    op.drop_column("whatsapp_channels", "disconnect_reason")
    op.drop_column("whatsapp_channels", "disconnected_at")
