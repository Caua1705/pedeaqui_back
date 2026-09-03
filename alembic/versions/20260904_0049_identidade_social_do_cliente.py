"""identidade social do cliente: o `sub` do Google ligado a conta

Revision ID: 20260904_0049
Revises: 20260904_0048
Create Date: 2026-09-04

## O que nao existia

Nada ligando uma conta do app a um provedor de identidade. O unico jeito de
entrar era e-mail mais senha (`AuthService.login`), e "entrar com Google" nao
tinha onde guardar de QUEM e aquela conta do lado do Google.

## O VINCULO E PELO `sub`, NUNCA PELO E-MAIL

`provider_user_id` guarda o `sub` do `id_token` — o identificador que o Google
promete estavel e nunca reutilizado para aquela conta. O e-mail **muda**: a
pessoa troca de endereco no Google e continua sendo a mesma pessoa. Um vinculo
por e-mail transformaria essa troca em **perda da conta**: historico, cashback
e endereco ficariam presos a um id que ninguem mais alcanca.

O `UNIQUE (provider, provider_user_id)` e o que faz isso valer: uma conta do
Google aponta para UM cliente, sempre.

## E o e-mail do Google NAO e gravado aqui

Ele nao entra nem como conveniencia de suporte. Nao serve para o vinculo (e o
`sub` que serve), e uma copia dele nesta tabela seria dado pessoal numa
segunda tabela, fora de `customers` — exatamente o que `delete_codes_of`
existe para limpar em `email_verification_codes` e `password_reset_codes`. Uma
copia a menos e um passo a menos que pode ser esquecido na exclusao de conta.

O que fica e o par (provider, sub) mais os dois relogios. `sub` e opaco: nao
diz nome, nem e-mail, nem nada sobre a pessoa fora do Google.

## Por que NAO ha UNIQUE (customer_id, provider)

Duas contas do Google apontando para o mesmo cliente e um estado LEGITIMO: a
pessoa que tem o Gmail pessoal e o do trabalho, e que confirmou os dois por
codigo. Um UNIQUE ali transformaria a segunda ligacao — que passou pela
confirmacao inteira — num `IntegrityError` no meio de um fluxo que deu certo.

O que nao pode acontecer e o contrario (uma conta do Google servindo dois
clientes), e disso cuida o UNIQUE de cima.

## `ON DELETE CASCADE`, e ele nao e o mecanismo de exclusao

A FK cascateia porque uma identidade sem cliente nao significa nada. Mas
`DELETE FROM customers` **nao acontece neste sistema** — a exclusao de conta e
anonimizacao, por causa de `coupon_redemptions` (ver o cabecalho de
`CustomerAnonymizationService`). Quem apaga estas linhas e o passo
`_delete_social_identities`, e `scripts/alcance_da_anonimizacao.py` cobra que
ele exista.

Se ele nao existisse, o estrago seria em dois andares: a conta anonimizada
guardaria um ponteiro para o cadastro da pessoa num sistema de terceiro, e
quem excluisse a conta e voltasse pelo Google cairia no caso "sub conhecido" —
logado numa conta `is_active=False`, **403 para sempre e sem como se
recadastrar pelo Google.**

## O CHECK de `provider`

`ck_customer_social_identities_provider` espelha `SOCIAL_AUTH_PROVIDERS`, em
`src/core/constants.py`, e esta registrado em `scripts/espelhos_de_enum.py`.
E a armadilha 15: provedor que entre no banco e nao na constante e recusado no
schema; na constante e nao no banco, morre no INSERT.

Um valor so hoje (`google`), e o CHECK existe justamente por isso — e a
segunda linha que precisa das duas listas concordando, e ela chega quando
alguem quiser Apple.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "20260904_0049"
down_revision = "20260904_0048"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "customer_social_identities",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "customer_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("customers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("provider_user_id", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        # Nulo ate o primeiro login POR ESTE provedor. A ligacao acontece
        # numa requisicao e o login na seguinte (caso b: o codigo de
        # confirmacao volta, a identidade e ligada, e so ai o JWT sai).
        sa.Column("last_login_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint(
            "provider = ANY (ARRAY['google'::text])",
            name="ck_customer_social_identities_provider",
        ),
    )
    # O vinculo. Uma conta do provedor aponta para UM cliente, sempre.
    op.create_unique_constraint(
        "uq_customer_social_identities_provider_user",
        "customer_social_identities",
        ["provider", "provider_user_id"],
    )
    # "Quais provedores esta conta tem?" — a leitura de `GET /customers/me`,
    # da exportacao de dados e do passo da exclusao de conta.
    op.create_index(
        "ix_customer_social_identities_customer_id",
        "customer_social_identities",
        ["customer_id"],
    )


def downgrade() -> None:
    # A tabela nasce nesta revisao, entao o DROP nao leva historico de
    # ninguem embora — o que ele leva sao as ligacoes: depois de voltar,
    # entrar com Google recomeca do zero e cai no caso (b) para quem ja tinha
    # conta, que e o caminho seguro (codigo por e-mail antes de religar).
    op.drop_index(
        "ix_customer_social_identities_customer_id",
        table_name="customer_social_identities",
    )
    op.drop_table("customer_social_identities")
