"""codigo para excluir a conta: a prova de quem nao tem senha

Revision ID: 20260904_0050
Revises: 20260904_0049
Create Date: 2026-09-04

## Por que ela existe

`DELETE /customers/me` exige a senha atual, e a conta criada pelo "entrar com
Google" (revisao 0049) **nao tem uma**: `password_hash` nasce com um segredo
sorteado e jogado fora, marcado com o prefixo `!`. Sem esta tabela, essa pessoa
so conseguiria exercer o direito de exclusao depois de DEFINIR uma senha por
`/auth/forgot-password` — um desvio de tres telas num caminho de LGPD.

O codigo de seis digitos prova o mesmo que a senha provaria: que quem esta
pedindo tem acesso ao e-mail da conta.

## POR QUE UMA TABELA PROPRIA, E NAO UMA COLUNA EM `email_verification_codes`

Este e o ponto inteiro da revisao, e ele nao e de organizacao.

Ha tres fluxos consumindo codigo de seis digitos: verificar e-mail (cadastro),
ligar a conta do Google (o caso b da 0049) e agora apagar a conta. **Um codigo
que sirva a mais de um deles e um codigo que faz a coisa errada.** O estrago
tem lado: um codigo pedido para "confirmar sua conta do Google" aceito pela
exclusao apagaria a conta de quem so queria entrar — e nao ha desfazer.

Com tabelas separadas isso deixa de ser um `if` que alguem pode remover e
passa a ser impossivel por construcao: `latest_unused_email_code` consulta
`email_verification_codes`, a exclusao consulta esta, e nenhuma das duas
enxerga a linha da outra.

A alternativa era uma coluna `purpose` na tabela de verificacao. Ela obrigaria
a mexer em `latest_unused_email_code` — que e o fluxo de e-mail existente, o
que nao se toca nesta frente — e uma consulta que esquecesse o filtro voltaria
a misturar os tres, em silencio.

## A forma e a de `email_verification_codes`, de proposito

Mesmas colunas, menos as duas do token de reset (que so `password_reset_codes`
usa). Repeticao clara vence abstracao esperta: uma tabela generica de "codigos"
com discriminador seria um `WHERE` a mais em toda consulta, e o dia em que
alguem esquecesse esse `WHERE` e exatamente o dia que esta revisao existe para
nao ter.

`code_hash` e HMAC com `EMAIL_CODE_SECRET`, como os outros dois: seis digitos
sao um milhao de possibilidades, e a chave e o que os torna caros num dump.

## Retencao e exclusao

A linha guarda o e-mail em TEXTO PURO, como as outras duas. Ela entra em
`CustomerRepository.delete_codes_created_before` (a varredura diaria do
container `limpeza`) e em `delete_codes_of` (o passo
`_delete_verification_codes` da propria exclusao de conta) — e
`scripts/alcance_da_anonimizacao.py` cobra o segundo.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "20260904_0050"
down_revision = "20260904_0049"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "account_deletion_codes",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "customer_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("customers.id"),
            nullable=False,
        ),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("code_hash", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column(
            "attempts_count", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "resend_count", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("used_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    # "o ultimo codigo nao usado deste e-mail" e a unica leitura da tabela, e
    # ela roda no caminho de uma pessoa esperando a tela responder.
    op.create_index(
        "ix_account_deletion_codes_email_created_at",
        "account_deletion_codes",
        ["email", "created_at"],
    )
    # O DELETE da exclusao de conta e o da retencao passam por aqui.
    op.create_index(
        "ix_account_deletion_codes_customer_id",
        "account_deletion_codes",
        ["customer_id"],
    )


def downgrade() -> None:
    # A tabela nasce nesta revisao e o que ela guarda vive dez minutos: voltar
    # nao perde historico de ninguem. O que volta e a conta sem senha nao
    # conseguir se excluir sozinha.
    op.drop_index(
        "ix_account_deletion_codes_customer_id", table_name="account_deletion_codes"
    )
    op.drop_index(
        "ix_account_deletion_codes_email_created_at",
        table_name="account_deletion_codes",
    )
    op.drop_table("account_deletion_codes")
