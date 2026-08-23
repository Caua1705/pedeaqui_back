"""relato de erro do lojista, com prazo de retencao

Revision ID: 20260823_0038
Revises: 20260823_0037
Create Date: 2026-08-23

O que isto substitui: "deu erro" no WhatsApp, seguido de quarenta minutos
tentando adivinhar qual erro, em que tela, em qual loja. A tabela guarda o
relato com o que o lojista sabe (o que ele estava fazendo, o log que a tela
capturou, a tela, opcionalmente o numero do pedido) e o que o TOKEN sabe
(usuario, restaurante, filial).

## A filial e o usuario NAO vem no corpo

Escopo de lojista sai do token, sempre. Um relato que dissesse de qual filial
ele e seria a primeira rota do painel a aceitar o contrario, e o dia em que
alguem copiar essa rota para escrever alguma coisa a regra ja teria sido
quebrada em outro lugar.

`branch_id` e NULAVEL porque o escopo tem esse buraco de propria: dono, e
qualquer usuario com `admin_users.branch_id` em nulo, enxerga todas as
filiais e nao esta em nenhuma. Nulo aqui significa "o relato nao aponta uma
loja", e nao "loja desconhecida".

## `order_number` e um numero solto, sem FK e sem conferencia

E deliberado, e e a peca que sustenta a decisao de redacao la em cima: com um
campo estruturado para apontar o pedido, o lojista nao precisa escrever "o
pedido do Joao, telefone 91 9…" no texto livre.

Sem FK porque ele NAO e um vinculo: e um numero que uma pessoa digitou
olhando para a tela, e pode vir errado. Recusar o relato porque o numero nao
existe seria recusa-lo exatamente na hora em que ele importa — e um relato
perdido custa mais que um numero que nao casa com pedido nenhum.

## A retencao E o mecanismo de exclusao (armadilha 38)

A tabela nao pende de `customers`, entao a exclusao de conta nunca vai
alcanca-la: o texto e escrito por um LOJISTA, sobre um cliente que nao tem
como saber que ele existe. Noventa dias, o mesmo prazo e o mesmo motivo do
`ai_feedback`, varridos pelo mesmo container `limpeza`. Quem sabe ate quando
e `admin_error_report_service.error_report_retention_cutoff`.

Esticar esse prazo "porque disco e barato" troca o mecanismo de exclusao por
espaco em disco. E o unico indice da tabela existe para essa varredura:
`created_at`.

## O que NAO e gravado

Credencial. `AdminErrorReportService` mascara `Authorization`, JWT,
`Idempotency-Key`, `tracking_token` e campos de senha ANTES do INSERT — um
token de painel colado num relato e um token de painel guardado em texto puro
numa tabela que ninguem audita. Nome, telefone e endereco que escaparem para
o texto livre ficam como o lojista escreveu, e quem os apaga e o prazo.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "20260823_0038"
down_revision: Union[str, None] = "20260823_0037"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "admin_error_reports",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "restaurant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("restaurants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Nulo = o relato nao aponta uma loja. Ver o docstring.
        sa.Column(
            "branch_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("branches.id", ondelete="SET NULL"),
            nullable=True,
        ),
        # `SET NULL` e nao `CASCADE`: o usuario que relatou pode ser desligado
        # depois, e o relato continua sendo o unico registro de um bug que
        # talvez ainda exista. Perder quem escreveu e aceitavel; perder o
        # relato nao.
        sa.Column(
            "admin_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("admin_users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("error_log", sa.Text(), nullable=True),
        sa.Column("screen", sa.Text(), nullable=True),
        sa.Column("order_number", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        # O schema ja recusa texto em branco, e o CHECK e a segunda porta: um
        # relato vazio nao e relato, e o custo de descobrir isso depois e uma
        # linha que ninguem consegue interpretar nem apagar com criterio.
        sa.CheckConstraint(
            "btrim(description) <> ''",
            name="ck_admin_error_reports_description_not_blank",
        ),
    )
    # O UNICO indice, e ele existe para a varredura da retencao. Nao ha
    # consulta por restaurante: quem le os relatos e
    # `scripts/error_reports.py`, que pega os ultimos N de todo mundo.
    # Indice que nenhuma consulta usa e custo em toda escrita (armadilha 4).
    op.create_index(
        "ix_admin_error_reports_created_at",
        "admin_error_reports",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_admin_error_reports_created_at", table_name="admin_error_reports")
    op.drop_table("admin_error_reports")
