"""entregadores: cadastro, atribuicao e a taxa que a loja paga por corrida

Revision ID: 20260903_0045
Revises: 20260902_0044
Create Date: 2026-09-03

## O que nao existia

Nada sobre quem LEVA o pedido. `out_for_delivery` e `completed` eram escritos
so pelo painel, por quem estivesse no balcao, e "quanto devo ao motoboy este
mes" nao tinha resposta em tabela nenhuma.

Tres mudancas, em dois regimes.

## 1. A taxa do entregador — em `branches`, SEM heranca

`courier_fee_base` e `courier_fee_per_km`, nullable, `NULL` = nao configurado.

Mesmo regime das cinco colunas do frete do CLIENTE (`delivery_base_fee`,
`delivery_fee_per_km`, piso, teto e raio): so da filial, sem padrao no
restaurante. O motoboy do Centro e o da Aldeota nao sao pagos pelo mesmo
acordo, e um padrao da marca nao responderia pergunta nenhuma aqui.

A referencia (Cardapio Web) poe a taxa do entregador como campo da AREA DE
ENTREGA, ao lado do que o cliente paga. Este projeto nao tem area: tem raio,
com formula. A taxa do entregador espelha a formula — base mais por-km — em
vez de inventar uma tabela de regioes que so ela usaria. Motoboy pago por
corrida e `base` preenchida e `per_km = 0`.

**Nenhum UPDATE, e nulo nao vira zero.** Nulo e "ninguem configurou", que e a
verdade sobre toda filial neste minuto. Zero seria "o motoboy trabalha de
graca" gravado em nome de um lojista que nunca abriu a tela — e zero e um
numero que SOMA no historico que o dono usa para pagar.

## 2. `couriers` — o cadastro, da FILIAL

Nome, telefone, ativo, e as duas credenciais em hash. O entregador NAO e
`admin_user`: nao tem tela no painel, nao tem Bearer, nao alcanca `/admin`.

- `access_link_hash`: sha-256 sem chave de um token de 256 bits que vai no
  link (a mesma disciplina do `tracking_token`, e pelo mesmo motivo — a
  entrada nao e adivinhavel, e nao existe variavel de ambiente cuja perda
  mate todo link de uma vez). UNIQUE: um link abre um cadastro so.
- `access_code_hash`: HMAC de um codigo de 6 digitos com o PROPRIO link como
  chave. Seis digitos precisam de chave contra forca bruta num dump, e o
  dump nao tem o link.
- os dois nulos = acesso nunca gerado ou revogado. Regenerar troca os dois.

**Excluir e `deleted_at`.** `courier_assignments` referencia esta tabela, e o
historico de corridas tem que sobreviver ao motoboy que saiu: e o que o dono
usa para pagar. O UNIQUE de telefone por filial e PARCIAL (`deleted_at IS
NULL`) por isso: o motoboy que saiu e voltou e recadastrado, e o cadastro
antigo continua excluido com o historico dele.

Da filial, e nao do restaurante, pelo criterio do resto do sistema (setor de
impressao, agente, forma de pagamento): objeto fisico da loja. Quem serve
duas lojas tem dois cadastros.

## 3. `courier_assignments` — a corrida, com a taxa CONGELADA

Uma linha por atribuicao. `unassigned_at` nulo = ativa. O indice parcial
unico em `order_id WHERE unassigned_at IS NULL` e o que garante que um pedido
esta nas maos de UM motoboy: sem ele, dois sairiam com o mesmo pedido na
lista e o historico somaria a taxa duas vezes.

`courier_fee_snapshot` e a taxa calculada NA ATRIBUICAO, da configuracao da
filial sobre a distancia que o pedido ja tinha (`orders.delivery_distance_km`).
Mesma disciplina de `unit_price_snapshot`: mudar a taxa amanha nao muda a
corrida de ontem. Nulo = "sem taxa configurada", nunca zero (ver 1).

Tabela propria e nao coluna em `orders`: a reatribuicao se perde numa coluna,
a taxa tem autor e instante proprios, e `orders` e a tabela mais sensivel do
sistema — cada coluna nela e um schema de resposta a mais para tocar.

Os indices sao estritos, sem `if_not_exists`: as tabelas nascem nesta
revisao (armadilha 4). Os dois de `branches` tambem, porque sao CHECKs com
nome novo sobre colunas novas.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "20260903_0045"
down_revision: Union[str, None] = "20260902_0044"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- 1. A taxa do entregador, na filial --------------------------------
    op.add_column("branches", sa.Column("courier_fee_base", sa.Numeric(10, 2), nullable=True))
    op.add_column(
        "branches", sa.Column("courier_fee_per_km", sa.Numeric(10, 2), nullable=True)
    )
    op.create_check_constraint(
        "ck_branches_courier_fee_base", "branches", "courier_fee_base >= 0"
    )
    op.create_check_constraint(
        "ck_branches_courier_fee_per_km", "branches", "courier_fee_per_km >= 0"
    )

    # --- 2. O cadastro -----------------------------------------------------
    op.create_table(
        "couriers",
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
            "branch_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("branches.id"),
            nullable=False,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("phone", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("access_link_hash", sa.Text(), nullable=True),
        sa.Column("access_code_hash", sa.Text(), nullable=True),
        sa.Column("access_generated_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.TIMESTAMP(timezone=True), nullable=True),
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
    )
    op.create_unique_constraint(
        "uq_couriers_access_link_hash", "couriers", ["access_link_hash"]
    )
    # Telefone unico por filial, so entre os NAO excluidos: o motoboy que saiu
    # e voltou e um cadastro novo, e o antigo fica com o historico dele.
    op.create_index(
        "ux_couriers_branch_phone_ativos",
        "couriers",
        ["branch_id", "phone"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    # A listagem do painel e sempre "os entregadores desta filial".
    op.create_index("ix_couriers_branch_id", "couriers", ["branch_id"])

    # --- 3. A corrida ------------------------------------------------------
    op.create_table(
        "courier_assignments",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "order_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("orders.id"),
            nullable=False,
        ),
        sa.Column(
            "courier_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("couriers.id"),
            nullable=False,
        ),
        sa.Column(
            "assigned_by_admin_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("admin_users.id"),
            nullable=True,
        ),
        sa.Column(
            "assigned_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("unassigned_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "unassigned_by_admin_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("admin_users.id"),
            nullable=True,
        ),
        sa.Column("courier_fee_snapshot", sa.Numeric(10, 2), nullable=True),
        sa.Column("distance_km_snapshot", sa.Numeric(10, 2), nullable=True),
        sa.CheckConstraint(
            "courier_fee_snapshot >= 0", name="ck_courier_assignments_fee"
        ),
    )
    # Um pedido nas maos de UM motoboy por vez. Parcial, porque as fechadas
    # sao historico e o mesmo pedido pode ter varias.
    op.create_index(
        "ux_courier_assignments_order_ativa",
        "courier_assignments",
        ["order_id"],
        unique=True,
        postgresql_where=sa.text("unassigned_at IS NULL"),
    )
    # A lista do entregador ("os meus, abertos") e o historico dele.
    op.create_index(
        "ix_courier_assignments_courier_id",
        "courier_assignments",
        ["courier_id", "assigned_at"],
    )


def downgrade() -> None:
    # As atribuicoes sao historico de pagamento; o downgrade as perde. E o
    # preco de voltar, e por isso ele nao roda sozinho no entrypoint.
    op.drop_table("courier_assignments")
    op.drop_table("couriers")
    op.drop_constraint("ck_branches_courier_fee_per_km", "branches", type_="check")
    op.drop_constraint("ck_branches_courier_fee_base", "branches", type_="check")
    op.drop_column("branches", "courier_fee_per_km")
    op.drop_column("branches", "courier_fee_base")
