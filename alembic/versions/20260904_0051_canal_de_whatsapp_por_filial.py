"""canal de whatsapp por filial: o numero, a janela de 24h e o que foi enviado

Revision ID: 20260904_0051
Revises: 20260904_0050
Create Date: 2026-09-04

## O que nao existia

Nada. O sistema nao falava com o cliente depois que ele fechava o pedido — o
acompanhamento era o link do `tracking_token`, e quem nao abrisse o link nao
sabia de nada.

## O numero e por FILIAL, com queda no restaurante

`branch_id` nulo e a linha do RESTAURANTE: e ela que toda filial sem numero
proprio usa. E o regime da armadilha 35 — `NULL` significa "herda", e so
`NULL` —, e ele resolve os dois casos com o mesmo desenho: o Junior, que tem
DOIS numeros (um por filial, na mesma Business Manager), cadastra duas linhas
com `branch_id`; o restaurante de uma loja so cadastra uma linha com
`branch_id` nulo e funciona como "um numero por restaurante".

## Por que TABELA, e nao coluna em `branches` + `restaurant_settings`

A semantica de heranca daria certo com duas colunas. A pergunta INVERSA nao:
o webhook chega com um `phone_number_id` e precisa achar a filial. Espalhado
em duas tabelas isso vira dois SELECT e, pior, **nao existe UNIQUE que
atravesse duas tabelas** — o mesmo numero cadastrado nos dois lugares
rotearia para dois destinos, sem erro nenhum, ate o dia em que os dois
divergissem.

## As tres travas do canal, e a do meio e a que quase nao existiu

- `uq_whatsapp_channels_phone_number_id` — um numero, um destino;
- `uq_whatsapp_channels_branch_id` — uma filial, um numero;
- `ux_whatsapp_channels_restaurant_default`, indice unico **PARCIAL** sobre
  `restaurant_id` `WHERE branch_id IS NULL`.

A terceira parece redundante com a segunda e nao e: no Postgres **`NULL` e
distinto de `NULL`**, entao `UNIQUE (branch_id)` deixa passar duas linhas de
restaurante do MESMO restaurante. Com duas, a filial sem numero herdaria uma
das duas — a que o `ORDER BY` escolhesse —, e o lojista veria o aviso sair
ora de um numero, ora de outro.

## A FK composta

`(restaurant_id, branch_id) -> branches (restaurant_id, id)`, contra
`uq_branches_restaurant_id`. E o mesmo cinto do cardapio por filial
(armadilha 36): sem ela daria para pendurar a filial de um restaurante
debaixo de outro, e o webhook daquele numero entregaria pedido da loja
errada.

Com `branch_id` nulo ela **nao e conferida** (`MATCH SIMPLE`, o default do
Postgres para FK com coluna nula). Isso nao e brecha: e exatamente o que a
linha de restaurante precisa, e `restaurant_id` continua tendo FK propria.

## A janela de 24h guarda TELEFONE, e por isso nasce com prazo

`whatsapp_contact_windows` nao pende de `customers` — o telefone que escreve
para a loja pode nao ter conta nenhuma. Armadilha 38: tabela nova que guarde
rastro de gente e nao penda de `customers` nasce com prazo, e o prazo E o
mecanismo de exclusao. Quem apaga e `scripts/cleanup_idempotency_keys.py`.

O TEXTO da mensagem recebida nao e guardado. Nao ha quem o leia nesta rodada,
e guarda-lo seria dado pessoal a mais com prazo a mais.

## Os dois CHECK

`ck_whatsapp_messages_kind` e `ck_whatsapp_messages_status` espelham
`WHATSAPP_MESSAGE_KINDS` e `WHATSAPP_MESSAGE_STATUSES`, em
`src/core/constants.py`, e estao registrados em `scripts/espelhos_de_enum.py`
— armadilha 15.

## Nenhum indice a mais

`whatsapp_messages` le por `order_id` (primeira coluna do UNIQUE com `kind`)
e por `wamid` (UNIQUE proprio); `whatsapp_contact_windows` le por
`(channel_id, phone_e164)`, que e o UNIQUE dela. O expurgo da janela varre
`window_expires_at` sem indice de proposito: sao dezenas de linhas por
restaurante, e um indice a mais e custo em TODA escrita para economizar um
seq scan de tabela pequena.

Todos estritos, sem `if_not_exists`: as tres tabelas nascem aqui, nao ha
colisao possivel e a permissividade so esconderia erro (armadilha 4).
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "20260904_0051"
down_revision = "20260904_0050"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "whatsapp_channels",
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
        # NULO = o numero do restaurante, a queda de quem nao tem o seu.
        sa.Column("branch_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("waba_id", sa.Text(), nullable=False),
        sa.Column("phone_number_id", sa.Text(), nullable=False),
        sa.Column("display_phone_number", sa.Text(), nullable=False),
        # Fernet, chave em settings.WHATSAPP_TOKEN_ENCRYPTION_KEY.
        sa.Column("access_token_encrypted", sa.Text(), nullable=False),
        sa.Column(
            "is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("phone_number_id", name="uq_whatsapp_channels_phone_number_id"),
        sa.UniqueConstraint("branch_id", name="uq_whatsapp_channels_branch_id"),
        # A filial e do restaurante que a linha diz. Nao conferida quando
        # `branch_id` e nulo, que e o caso da linha de restaurante.
        sa.ForeignKeyConstraint(
            ["restaurant_id", "branch_id"],
            ["branches.restaurant_id", "branches.id"],
            name="fk_whatsapp_channels_branch",
            ondelete="CASCADE",
        ),
    )
    # A trava que o UNIQUE nao da: no Postgres NULL e distinto de NULL, entao
    # sem isto o mesmo restaurante teria duas quedas.
    op.create_index(
        "ux_whatsapp_channels_restaurant_default",
        "whatsapp_channels",
        ["restaurant_id"],
        unique=True,
        postgresql_where=sa.text("branch_id IS NULL"),
    )

    op.create_table(
        "whatsapp_contact_windows",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "channel_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("whatsapp_channels.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Digitos com DDI, sem `+`. E dado pessoal: ver o prazo, acima.
        sa.Column("phone_e164", sa.Text(), nullable=False),
        sa.Column("window_expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "channel_id", "phone_e164", name="uq_whatsapp_contact_windows_channel_phone"
        ),
    )

    op.create_table(
        "whatsapp_messages",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "order_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("orders.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Sem ON DELETE: apagar um canal apagaria o registro de que o cliente
        # foi avisado. Numero que sai de operacao e desativado.
        sa.Column(
            "channel_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("whatsapp_channels.id"),
            nullable=False,
        ),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        # Nulo quando a chamada falhou antes de a Meta responder.
        sa.Column("wamid", sa.Text(), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("order_id", "kind", name="uq_whatsapp_messages_order_kind"),
        sa.UniqueConstraint("wamid", name="uq_whatsapp_messages_wamid"),
        sa.CheckConstraint(
            "kind = ANY (ARRAY['order_accepted'::text, 'order_out_for_delivery'::text, "
            "'order_delivered'::text])",
            name="ck_whatsapp_messages_kind",
        ),
        sa.CheckConstraint(
            "status = ANY (ARRAY['sent'::text, 'delivered'::text, 'read'::text, "
            "'failed'::text])",
            name="ck_whatsapp_messages_status",
        ),
    )


def downgrade() -> None:
    # As tres nascem nesta revisao, entao o DROP nao leva historico de
    # ninguem embora. O que ele leva sao os TOKENS cadastrados: voltar aqui
    # significa cadastrar cada numero de novo, com o token da Meta em maos.
    #
    # A ordem e a inversa da criacao por causa das FKs: as mensagens apontam
    # para o canal.
    op.drop_table("whatsapp_messages")
    op.drop_table("whatsapp_contact_windows")
    op.drop_index(
        "ux_whatsapp_channels_restaurant_default", table_name="whatsapp_channels"
    )
    op.drop_table("whatsapp_channels")
