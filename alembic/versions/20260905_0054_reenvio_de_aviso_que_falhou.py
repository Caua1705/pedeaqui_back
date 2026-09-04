"""reenvio de aviso que falhou: a decisao gravada, e nao o sintoma

Revision ID: 20260905_0054
Revises: 20260905_0053
Create Date: 2026-09-05

## O buraco que ela fecha

Ate aqui, um aviso que falhou virava linha `failed` em `whatsapp_messages` e
**ninguem retentava**. Um timeout da Meta no instante do aceite custava o
aviso inteiro daquele pedido — e o cliente nao tinha como saber, porque o
unico registro era uma linha de tabela que ninguem le.

## Por que uma COLUNA e nao um `WHERE status = 'failed'`

Porque nem toda falha se conserta repetindo, e a resposta a "repetir tem
chance?" **ja tem dono**: `WhatsAppSendError.retryable`, que sai do TIPO da
excecao e nunca do codigo de erro da Meta (armadilha 49).

Uma varredura que lesse `status='failed'` e decidisse pelo `error_code`
estaria respondendo a mesma pergunta num segundo lugar, com um vocabulario
que nao e feito para isso — e no dia em que os dois divergissem, o aviso
`132001` (template nao aprovado) seria retentado a cada dois minutos ate a
validade acabar, sem chance nenhuma, porque quem aprova template sou eu e
nao a maquina.

Entao a linha guarda **a decisao**, tomada la onde o tipo da excecao existe:

    next_attempt_at IS NOT NULL   ha retentativa marcada, e para quando
    next_attempt_at IS NULL       nao ha — e o estado de todo `sent` e de
                                  toda falha permanente

`attempts` conta as tentativas. Nao e um segundo teto — o teto e o relogio,
e dois limites que podem discordar sao piores que um. Ele existe para a
pergunta que se faz no dia do piloto: o aviso saiu de primeira ou na setima?
Sem ele, "o reenvio esta servindo para alguma coisa?" nao tem resposta.

## O indice e PARCIAL de proposito

A varredura pergunta so pelas linhas com retentativa marcada, e elas sao
poucas: quase toda linha da tabela e `sent`, com `next_attempt_at` nulo. Um
indice cheio guardaria a tabela inteira para responder por uma franja dela.

## O downgrade perde as retentativas marcadas

Nao ha o que preservar: as colunas somem, e o codigo antigo nao retenta
nada mesmo. O que fica sao linhas `failed` que ninguem mais vai retomar — que
e exatamente o estado de antes desta revisao.
"""

import sqlalchemy as sa
from alembic import op


revision = "20260905_0054"
down_revision = "20260905_0053"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "whatsapp_messages",
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "whatsapp_messages",
        sa.Column("next_attempt_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_whatsapp_messages_next_attempt",
        "whatsapp_messages",
        ["next_attempt_at"],
        postgresql_where=sa.text("next_attempt_at IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_whatsapp_messages_next_attempt", table_name="whatsapp_messages")
    op.drop_column("whatsapp_messages", "next_attempt_at")
    op.drop_column("whatsapp_messages", "attempts")
