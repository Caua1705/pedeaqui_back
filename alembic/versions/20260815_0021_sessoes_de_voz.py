"""livro-razao das sessoes de voz

Revision ID: 20260815_0021
Revises: 20260814_0020
Create Date: 2026-08-15

## Por que uma tabela

Depois que a credencial efemera sai daqui, a conversa acontece entre o
NAVEGADOR e a OpenAI. O backend nao ve o audio, nao ve a transcricao e nao ve
o consumo. Sem um registro do que foi emitido, nao existe nem cota, nem
conciliacao de fatura, nem resposta para "quanto esse restaurante gastou".

Esta tabela e o unico ponto em que uma sessao faturada fica ligada a alguem.

## O que cada coluna existe para responder

`openai_call_id` e NULO ate o navegador reportar. Ele so aparece no cabecalho
`Location` da resposta que a OpenAI da ao NAVEGADOR quando a conexao de audio
e criada — o servidor nao participa dessa chamada e nao tem outro jeito
documentado de descobrir o identificador. Sem ele nao da para desligar a
sessao pelo servidor, e essa e a fronteira do que o BLOCO 2 alcanca: vale
contra cliente que coopera, nao contra cliente hostil.

`expires_at` e o teto gravado no momento da emissao, e nao um calculo feito na
leitura. Mudar `VOZ_DURACAO_MAXIMA_SEGUNDOS` depois nao pode reescrever o
contrato das sessoes que ja estavam de pe.

`customer_id` nasce nulo nesta revisao e passa a ser obrigatorio na pratica no
bloco de login — a coluna fica nullable para as linhas emitidas antes disso
nao virarem mentira.

## Os dois indices

`ix_ai_voice_sessions_customer_dia` serve a cota por cliente, que e uma
contagem por customer_id numa janela de tempo — a consulta mais frequente
depois desta tabela existir.

`ix_ai_voice_sessions_abertas` e PARCIAL, so sobre as linhas sem `ended_at`.
A varredura que desliga sessao vencida so olha para essas, e elas sao sempre
poucas; um indice sobre a tabela inteira cresceria para sempre para servir uma
consulta que enxerga um punhado de linhas.

Indices estritos, sem `if_not_exists`: a tabela nasce nesta revisao, nao ha
colisao possivel com nome criado a mao (armadilha 4).
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "20260815_0021"
down_revision: Union[str, None] = "20260814_0020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ai_voice_sessions",
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
            "customer_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("customers.id"),
            nullable=True,
        ),
        # Nulo ate o navegador reportar. Ver o docstring.
        sa.Column("openai_call_id", sa.Text(), nullable=True),
        sa.Column(
            "issued_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("ended_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("ended_reason", sa.Text(), nullable=True),
    )

    op.create_index(
        "ix_ai_voice_sessions_customer_dia",
        "ai_voice_sessions",
        ["customer_id", "issued_at"],
    )
    op.create_index(
        "ix_ai_voice_sessions_abertas",
        "ai_voice_sessions",
        ["expires_at"],
        postgresql_where=sa.text("ended_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_ai_voice_sessions_abertas", table_name="ai_voice_sessions")
    op.drop_index("ix_ai_voice_sessions_customer_dia", table_name="ai_voice_sessions")
    op.drop_table("ai_voice_sessions")
