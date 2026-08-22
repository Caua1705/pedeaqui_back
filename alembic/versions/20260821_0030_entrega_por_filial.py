"""frete gratis, pausa da entrega e prazo por faixa de distancia

Revision ID: 20260821_0030
Revises: 20260821_0029
Create Date: 2026-08-21

Tres mudancas de entrega, e elas caem em TRES regimes diferentes. A pergunta
que separa os regimes e a mesma da revisao `20260818_0025`: *um padrao do
restaurante responde alguma pergunta aqui?*

## 1. Frete gratis acima de X — termo comercial, HERDA

`free_delivery_enabled` + `free_delivery_min_order_value`, nos dois lados,
nullable, `NULL` = herda.

E campanha de MARCA. "Acima de R$ 60 a entrega e por nossa conta" e escrito
uma vez para a rede, e mudar 60 para 80 continua sendo UMA edicao — o mesmo
argumento que colocou `min_order_value` e a taxa de servico neste regime.

**Por que sao DUAS colunas, e nao so o valor.** Sem o booleano, a filial nao
teria como recusar a campanha da marca: `NULL` significa "herda", e nao ha
numero que signifique "desligado" — `0` seria "gratis sempre", o oposto. E
exatamente a forma do par `service_fee_enabled` + `service_fee_amount`, que
existe por essa mesma razao. A loja de Messejana, a 12 km de tudo, desliga
com `free_delivery_enabled = false` e continua herdando o resto.

**O default RESOLVIDO e desligado**, ao contrario da taxa de servico (que
`resolve_branch_operation` resolve como ligada quando os dois lados sao
nulos). A assimetria e deliberada e e a armadilha 11 de novo: taxa de servico
ligada sem valor cadastrado cobra zero, que nao machuca ninguem; frete gratis
ligado por omissao **da entrega de graca em nome de um lojista que nao pediu**.

## 2. Pausar a entrega — estado do dia, NAO herda

`delivery_paused_until` (timestamptz) + `delivery_pause_reason` (texto).

**Por que nao bastava `accepts_delivery`.** Ele resolve "pausar sem apagar
configuracao" — e o que ele NAO resolve e o "temporariamente". Uma pausa
manual precisa de alguem que lembre de desfaze-la, e o dia em que ela e usada
(chuva as 19h, entregador que sumiu) e exatamente o dia em que ninguem lembra.
O resultado e a loja aberta na manha seguinte sem aceitar entrega, e o sintoma
e a ausencia de pedido — que nao acende alarme nenhum.

Por isso a pausa e um PRAZO, e nao uma chave: ela se desfaz sozinha. Quem
quer desligar a entrega sem prazo continua usando `accepts_delivery`, que e a
chave estrutural ("este quiosque nao entrega, ponto"). As duas convivem: uma e
o dia, a outra e o negocio.

O teto de 24h vive no schema, e nao aqui: pausa longa e `accepts_delivery`
com passos a mais, e deixar as duas fazerem a mesma coisa apaga a diferenca
que este paragrafo acabou de justificar.

## 3. Prazo por faixa de distancia — tabela propria, da FILIAL

`branch_delivery_time_bands`. Nao herda: a faixa mede o tempo entre a porta
DAQUELA loja e a porta do cliente, e duas lojas da mesma marca em pontas
opostas da cidade nao tem faixa nenhuma em comum.

**As faixas sao TETOS, e nao intervalos.** So `max_distance_km` e gravado; a
faixa que vale e a primeira, em ordem crescente, cujo teto alcanca a
distancia. Guardar tambem o piso permitiria configurar `0-5` e `6-10` e
deixar o endereco de 5.4 km sem faixa nenhuma — um buraco que so aparece no
endereco de um cliente especifico, num dia especifico. Com teto, buraco nao
existe: o UNIQUE `(branch_id, max_distance_km)` e a ordenacao dao a cobertura
inteira de graca.

Endereco alem do ultimo teto nao cai em faixa nenhuma, e isso e um estado
valido: vale o comportamento de hoje (prazo do Google). A distancia maxima
que a filial atende continua sendo `delivery_max_distance_km`, que e outra
coisa e nao se mistura.

## 4. `orders.delivery_fee_waived`

Quanto de taxa a regra de frete gratis deixou de cobrar NESTE pedido.

Nao ha relatorio que a leia hoje, e a coluna existe assim mesmo por um motivo
que nao vale para codigo: **dado nao capturado na escrita nao se recupera
depois.** Com so `delivery_fee = 0` gravado, "quanto essa campanha me custou
em agosto" nao tem resposta — nao da para saber quanto a rota teria cobrado
num pedido que ja passou. O primeiro relatorio de frete gratis vai precisar
exatamente disto, e ele nao pode ser escrito retroativamente.

`NOT NULL DEFAULT 0` porque zero e a verdade sobre todo pedido anterior a
esta revisao: nenhum deles teve taxa perdoada.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "20260821_0030"
down_revision: Union[str, None] = "20260821_0029"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Teto de minutos de uma faixa, o mesmo do tempo de preparo
# (MAX_PREP_TIME_MINUTES): dez horas cobre qualquer operacao real e impede o
# dedo que digita 6000.
MAX_BAND_MINUTES = 600


def upgrade() -> None:
    # --- 1. Frete gratis: o par ligado/valor, dos dois lados ---------------
    for tabela in ("restaurant_settings", "branches"):
        op.add_column(tabela, sa.Column("free_delivery_enabled", sa.Boolean(), nullable=True))
        op.add_column(
            tabela,
            sa.Column("free_delivery_min_order_value", sa.Numeric(10, 2), nullable=True),
        )

    # Nenhum UPDATE: nulo nos dois lados e "ninguem configurou", que e a
    # verdade sobre a plataforma inteira neste minuto. Um default ligado daria
    # entrega de graca em nome de quem nao pediu.

    # --- 2. Pausa temporaria da entrega ------------------------------------
    op.add_column(
        "branches",
        sa.Column("delivery_paused_until", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.add_column("branches", sa.Column("delivery_pause_reason", sa.Text(), nullable=True))

    # --- 3. Faixas de prazo por distancia ----------------------------------
    op.create_table(
        "branch_delivery_time_bands",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "branch_id",
            postgresql.UUID(as_uuid=True),
            # ON DELETE CASCADE: filial nao e apagada neste projeto (e
            # desativada), mas se um dia for, uma faixa de prazo orfa nao tem
            # leitura possivel — ao contrario de um pedido, que e historico.
            sa.ForeignKey("branches.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("max_distance_km", sa.Numeric(6, 2), nullable=False),
        sa.Column("delivery_time_min", sa.Integer(), nullable=False),
        sa.Column("delivery_time_max", sa.Integer(), nullable=False),
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
        sa.CheckConstraint(
            "max_distance_km > 0", name="ck_branch_delivery_time_bands_distance"
        ),
        sa.CheckConstraint(
            f"delivery_time_min BETWEEN 0 AND {MAX_BAND_MINUTES}",
            name="ck_branch_delivery_time_bands_min",
        ),
        sa.CheckConstraint(
            f"delivery_time_max BETWEEN 0 AND {MAX_BAND_MINUTES}",
            name="ck_branch_delivery_time_bands_max",
        ),
        sa.CheckConstraint(
            "delivery_time_max >= delivery_time_min",
            name="ck_branch_delivery_time_bands_range",
        ),
    )
    # Dois tetos iguais na mesma filial nao sao ambiguidade de leitura (a
    # ordenacao escolheria um dos dois em silencio) — sao duas respostas
    # diferentes para a mesma distancia, e a que vale mudaria entre consultas.
    op.create_unique_constraint(
        "uq_branch_delivery_time_bands_branch_distance",
        "branch_delivery_time_bands",
        ["branch_id", "max_distance_km"],
    )
    # A leitura e sempre "as faixas desta filial, em ordem de teto" — que e
    # exatamente este indice. Estrito, sem `if_not_exists`: a tabela nasce
    # nesta revisao (armadilha 4).
    op.create_index(
        "ix_branch_delivery_time_bands_branch_distance",
        "branch_delivery_time_bands",
        ["branch_id", "max_distance_km"],
    )

    # --- 4. Quanto de frete foi perdoado, por pedido -----------------------
    op.add_column(
        "orders",
        sa.Column(
            "delivery_fee_waived",
            sa.Numeric(10, 2),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )


def downgrade() -> None:
    op.drop_column("orders", "delivery_fee_waived")

    op.drop_index(
        "ix_branch_delivery_time_bands_branch_distance",
        table_name="branch_delivery_time_bands",
    )
    op.drop_constraint(
        "uq_branch_delivery_time_bands_branch_distance",
        "branch_delivery_time_bands",
        type_="unique",
    )
    op.drop_table("branch_delivery_time_bands")

    op.drop_column("branches", "delivery_pause_reason")
    op.drop_column("branches", "delivery_paused_until")

    for tabela in ("branches", "restaurant_settings"):
        op.drop_column(tabela, "free_delivery_min_order_value")
        op.drop_column(tabela, "free_delivery_enabled")
