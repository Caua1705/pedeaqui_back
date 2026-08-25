"""cartao salvo do cliente, por restaurante

Revision ID: 20260825_0040
Revises: 20260825_0039
Create Date: 2026-08-25

## O que estas duas tabelas guardam, e o que elas NAO guardam

Nao ha coluna de numero de cartao, de CVV nem de validade que sirva para
cobrar. O que se guarda e o `provider_card_id` — um identificador OPACO,
gerado pelo Mercado Pago, que so significa alguma coisa dentro da conta que
o emitiu — mais bandeira e ultimos quatro digitos, que existem para a pessoa
reconhecer o cartao na tela.

O numero do cartao e digitado no formulario do SDK do Mercado Pago, no
NAVEGADOR, e vai direto para eles. Este backend nunca o ve. Se um dia
aparecer uma coluna de PAN aqui, o perimetro de PCI do projeto mudou e a
integracao saiu do padrao de tokenizacao.

## Por que o cartao pende do RESTAURANTE, e nao so do cliente

A credencial do Mercado Pago e do lojista (`restaurant_payment_credentials`):
a cobranca nasce na conta dele e o dinheiro cai na conta dele. O "customer"
do Mercado Pago, e os cartoes pendurados nele, vivem DENTRO daquela conta. O
`card_id` salvo no Junior da Picanha nao e cobravel pela conta de outro
restaurante, e nem sequer e legivel por ela.

**Consequencia aceita, e sabida:** no dia em que o split entrar e as
cobrancas passarem a nascer numa conta de marketplace, estes ids param de
valer e os clientes recadastram o cartao. Nao ha migracao possivel — os
cartoes nao sao nossos para mover.

## Por que `environment` esta na chave do perfil

Mesmo motivo de `restaurant_payment_credentials`: a conta de teste e a de
producao sao contas DIFERENTES. Um customer criado com a credencial de teste
nao existe para a de producao, e cobrar o `card_id` de uma na outra da 404 do
gateway no meio do checkout. Sem esta coluna, virar `MERCADOPAGO_ENVIRONMENT`
faria o backend tentar cobrar cartoes que a conta ativa nunca viu — e o erro
apareceria como recusa de pagamento, nao como configuracao errada.

Quando o ambiente vira, os perfis do ambiente antigo simplesmente deixam de
ser encontrados: a lista do cliente vem vazia e ele recadastra. **Nao ha
backfill e nao pode haver.**

## O CHECK dos quatro digitos

`last_four_digits ~ '^[0-9]{4}$'` nao esta ali por elegancia de schema. Ele
e a ultima barreira contra a coluna virar depositario acidental do numero
inteiro — um `INSERT` que passasse o PAN por engano falha no banco, alto, em
vez de gravar 16 digitos numa coluna chamada "ultimos quatro" e so ser
descoberto num dump.

## As duas tabelas nascem VAZIAS, e os indices sao estritos

Nenhuma das duas existe em producao, entao nao ha colisao de nome possivel
e `if_not_exists` seria permissividade escondendo erro (armadilha 4). Os
UNIQUE saem como constraint da propria tabela, criados junto com ela.

## Exclusao de conta

`customer_payment_profiles.customer_id` e `ON DELETE CASCADE`, e os cartoes
cascateiam do perfil. Mas a anonimizacao da LGPD **nao faz DELETE em
`customers`** (ver docs/lgpd-fase2-exclusao-de-conta.md), entao o CASCADE
nunca dispara por esse caminho: quem apaga as linhas e
`CustomerAnonymizationService._delete_saved_cards`, explicitamente. O CASCADE
fica para o DELETE de restaurante e como rede de seguranca.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260825_0040"
down_revision = "20260825_0039"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "customer_payment_profiles",
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
        sa.Column(
            "restaurant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("restaurants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("environment", sa.Text(), nullable=False),
        sa.Column("provider_customer_id", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "environment IN ('test', 'production')",
            name="ck_customer_payment_profiles_environment",
        ),
        sa.UniqueConstraint(
            "customer_id",
            "restaurant_id",
            "environment",
            name="uq_customer_payment_profiles_customer_restaurant_environment",
        ),
    )

    op.create_table(
        "customer_saved_cards",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "payment_profile_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("customer_payment_profiles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider_card_id", sa.Text(), nullable=False),
        sa.Column("brand", sa.Text(), nullable=False),
        sa.Column("last_four_digits", sa.Text(), nullable=False),
        sa.Column("expiration_month", sa.Integer(), nullable=True),
        sa.Column("expiration_year", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        # A ultima barreira contra o PAN inteiro cair nesta coluna.
        sa.CheckConstraint(
            "last_four_digits ~ '^[0-9]{4}$'",
            name="ck_customer_saved_cards_last_four_digits",
        ),
        # Salvar duas vezes o mesmo cartao devolve o mesmo id do gateway; sem
        # este UNIQUE a tela mostraria o cartao em duplicata.
        sa.UniqueConstraint(
            "payment_profile_id",
            "provider_card_id",
            name="uq_customer_saved_cards_profile_card",
        ),
    )

    # A lista de cartoes de um perfil e consultada em todo checkout com
    # cartao. Tabela criada nesta revisao: indice estrito, sem if_not_exists.
    op.create_index(
        "ix_customer_saved_cards_payment_profile_id",
        "customer_saved_cards",
        ["payment_profile_id"],
    )


def downgrade() -> None:
    # Ordem inversa da criacao: os cartoes pendem do perfil por FK.
    #
    # E o que este downgrade NAO faz: apagar os cartoes na conta do Mercado
    # Pago de cada lojista. Voltar a revisao esquece os `card_id` daqui e
    # eles ficam pendurados la, sem referencia nenhuma. Quem voltar em
    # producao com dado dentro precisa saber disso — nao ha como recuperar
    # esses ids depois do DROP.
    op.drop_index("ix_customer_saved_cards_payment_profile_id", table_name="customer_saved_cards")
    op.drop_table("customer_saved_cards")
    op.drop_table("customer_payment_profiles")
