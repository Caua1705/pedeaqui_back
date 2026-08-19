"""a operacao passa a ser da filial

Revision ID: 20260818_0025
Revises: 20260816_0024
Create Date: 2026-08-18

## O problema que esta revisao fecha

`restaurant_settings.is_open` e o botao "fechar agora" — a pausa manual das
21h, quando acabou o gas ou a fila cresceu. Ele valia para o restaurante
INTEIRO: fechar a filial do Centro fechava tambem a da Aldeota, e nao havia
como fechar so uma. Com o cardapio virando por filial (passo 3), "cardapio
independente com fechar-agora compartilhado" nao descreve nenhum dia de
operacao real.

O mesmo valia para `accepts_delivery` / `accepts_pickup`: o quiosque de
shopping que so faz retirada desligava a entrega da rede toda.

## O que vai para a filial, e em que regime

Duas classes de campo, e a diferenca e o que decide o regime:

**Estado do dia — SO da filial, NOT NULL, sem heranca.**
`is_open`, `accepts_delivery`, `accepts_pickup`. Sao o que alguem no balcao
aperta durante o expediente. Um padrao do restaurante para eles nao responde
pergunta nenhuma: "o restaurante esta fechado mas esta filial esta aberta"
nao e um estado que a operacao consiga ler.

**Termo comercial — da filial COM heranca, nullable, NULL = herda.**
`min_order_value`, `service_fee_enabled`, `service_fee_amount`,
`estimated_delivery_time_min`, `estimated_delivery_time_max`,
`default_delivery_fee`. Sao preco negociado da marca. O lojista que abre a
quinta loja nao redigita a taxa de servico cinco vezes, e mudar 0,99 para
1,49 continua sendo UMA edicao — mas a filial que precisar divergir escreve o
proprio valor e ele passa a valer so ali.

`default_delivery_fee` nao estava no pedido original e entrou por estar na
mesma situacao: e a taxa de contingencia usada quando a regra por km da
filial nao pode ser aplicada (Google fora do ar), e essa regra por km JA e da
filial. Filial perto e filial longe caindo na mesma taxa fixa e o mesmo
defeito, mais discreto.

## O que NAO se move, e por que

- `platform_commission_percent` e `voice_enabled`: sao da PLATAFORMA, nao do
  lojista (armadilha 17). Nao viram configuracao de filial porque nao sao
  configuracao de loja nenhuma.
- `payment_methods` (jsonb): ja e dado morto. Quem manda em forma de
  pagamento e `branch_payment_methods`, que ja e por filial; o jsonb so e
  ecoado em `/menu` e pode discordar do que a filial de fato aceita. Remove-lo
  muda contrato publico e fica para quem mexer no `/menu` do passo 3.

## A copia de dados NAO e opcional

As tres colunas nascem `NOT NULL DEFAULT true` e depois o UPDATE copia o
valor que o restaurante tinha para todas as filiais dele. Sem esse UPDATE, um
restaurante que estava FECHADO (`is_open = false`) reabriria sozinho no
`alembic upgrade head` — a loja passa a aceitar pedido que ninguem esta la
para produzir, e nada no log denuncia.

`COALESCE(..., true)` porque as colunas de origem sao nullable e a leitura
antiga tratava NULL como "aceita" (`if settings.is_open is False`).

Restaurante SEM linha em `restaurant_settings` tem o LEFT JOIN vazio e fica
com o default `true`, que e exatamente o que valia para ele antes.

## O downgrade perde informacao, e nao ha como nao perder

N filiais voltam para uma coluna so. A volta usa o valor da filial
PRINCIPAL (`is_main`), ou da primeira em ordem alfabetica quando nao houver —
a mesma regra de `BranchRepository.get_default_branch`. Quem descer esta
revisao com filiais divergentes perde a divergencia; nao existe resposta
melhor com uma coluna.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "20260818_0025"
down_revision: Union[str, None] = "20260816_0024"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Estado do dia: NOT NULL, sem heranca, copiado do restaurante na subida.
COLUNAS_DE_ESTADO = ("is_open", "accepts_delivery", "accepts_pickup")

# A copia, como constante para `tests/test_migracao_operacao_por_filial_db.py`
# poder executa-la contra um Postgres de verdade. E a unica parte desta
# revisao que pode dar errado em silencio: um erro aqui nao levanta excecao,
# so reabre lojas que estavam fechadas.
SQL_COPIA_O_ESTADO_DO_DIA = """
UPDATE branches
   SET is_open          = COALESCE(restaurant_settings.is_open, true),
       accepts_delivery = COALESCE(restaurant_settings.accepts_delivery, true),
       accepts_pickup   = COALESCE(restaurant_settings.accepts_pickup, true)
  FROM restaurant_settings
 WHERE restaurant_settings.restaurant_id = branches.restaurant_id
"""


def upgrade() -> None:
    for coluna in COLUNAS_DE_ESTADO:
        op.add_column(
            "branches",
            sa.Column(coluna, sa.Boolean(), nullable=False, server_default=sa.true()),
        )

    op.add_column("branches", sa.Column("min_order_value", sa.Numeric(10, 2), nullable=True))
    op.add_column("branches", sa.Column("service_fee_enabled", sa.Boolean(), nullable=True))
    op.add_column("branches", sa.Column("service_fee_amount", sa.Numeric(10, 2), nullable=True))
    op.add_column("branches", sa.Column("estimated_delivery_time_min", sa.Integer(), nullable=True))
    op.add_column("branches", sa.Column("estimated_delivery_time_max", sa.Integer(), nullable=True))
    op.add_column("branches", sa.Column("default_delivery_fee", sa.Numeric(10, 2), nullable=True))

    # A copia. Sem ela, restaurante fechado reabre no deploy.
    op.execute(SQL_COPIA_O_ESTADO_DO_DIA)

    # As seis colunas de termo comercial NAO sao copiadas: NULL na filial
    # significa "herda", e herdar e o estado correto para todas as filiais
    # que existem hoje. Copiar deixaria cada filial com uma copia congelada
    # do valor do restaurante, e a proxima edicao do padrao nao chegaria a
    # nenhuma delas.

    for coluna in COLUNAS_DE_ESTADO:
        op.drop_column("restaurant_settings", coluna)


def downgrade() -> None:
    for coluna in COLUNAS_DE_ESTADO:
        op.add_column(
            "restaurant_settings",
            sa.Column(coluna, sa.Boolean(), nullable=True, server_default=sa.true()),
        )

    op.execute(
        """
        UPDATE restaurant_settings
           SET is_open          = filial_padrao.is_open,
               accepts_delivery = filial_padrao.accepts_delivery,
               accepts_pickup   = filial_padrao.accepts_pickup
          FROM (
                SELECT DISTINCT ON (restaurant_id)
                       restaurant_id, is_open, accepts_delivery, accepts_pickup
                  FROM branches
                 WHERE is_active IS TRUE
                 ORDER BY restaurant_id, is_main DESC NULLS LAST, name ASC
               ) AS filial_padrao
         WHERE filial_padrao.restaurant_id = restaurant_settings.restaurant_id
        """
    )

    op.drop_column("branches", "default_delivery_fee")
    op.drop_column("branches", "estimated_delivery_time_max")
    op.drop_column("branches", "estimated_delivery_time_min")
    op.drop_column("branches", "service_fee_amount")
    op.drop_column("branches", "service_fee_enabled")
    op.drop_column("branches", "min_order_value")
    for coluna in COLUNAS_DE_ESTADO:
        op.drop_column("branches", coluna)
