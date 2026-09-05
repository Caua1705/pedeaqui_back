"""a tarifa de entrega sai de branches para uma tabela 1:1 opcional

Revision ID: 20260905_0059
Revises: 20260905_0058
Create Date: 2026-09-05

**ESCRITA E NAO APLICADA, e esta ESPERA DECISAO** — ao contrario da `0058`, que
so tira lixo. Mora em `alembic/preparadas/`, que o Alembic nao le (armadilha
53). A proposta inteira, com tamanho e com o que eu recomendo, esta em
`docs/modelo-de-dados.md`, secao "PENDENTE: quebrar a tabela `branches`".

Esta e a **etapa B**, e a unica das quatro divisoes candidatas que eu
recomendaria fazer. As outras tres estao naquela secao, com o motivo de nao.

---

## O problema que ela resolve, e nao e o numero de colunas

`branches` tem 50 colunas, e o incomodo obvio e esse. O incomodo que CUSTA e
outro: **quatro regimes de nulo convivem ali com a mesma cara**, e a coluna nao
diz a qual pertence.

| Regime | Colunas | `NULL` significa |
|---|---|---|
| estado do dia | `is_open`, `accepts_delivery`, `accepts_pickup`, … | (sao `NOT NULL`) |
| **termo comercial** | `min_order_value`, `service_fee_*`, `estimated_delivery_time_*`, `default_delivery_fee`, `free_delivery_*`, `receipt_footer_message` | **"herda de `restaurant_settings`"** (armadilha 35) |
| **tarifa de entrega** | `delivery_base_fee`, `delivery_fee_per_km`, `delivery_min_fee`, `delivery_max_fee`, `delivery_max_distance_km` | **"nao cobra por esse eixo"** — nao herda nada |
| taxa do entregador | `courier_fee_base`, `courier_fee_per_km` | "nao configurado" — nao herda nada |

Os dois do meio sao o par perigoso: `default_delivery_fee` nulo **herda** e
`delivery_base_fee` nulo **nao herda**, os dois nulos, os dois `NUMERIC`, os dois
com `delivery` no nome, um do lado do outro na mesma tabela. Nada no schema
separa os dois, e a armadilha 35 existe porque confundi-los ja custou caro.

Separar a tarifa numa tabela propria transforma essa distincao em algo que se ve
sem ler o `branch_operation.py`: **o que herda ficou em `branches`, o que nao
herda saiu.**

## O que a tabela ganha, e o que ela cobra

**Ganha:** 7 colunas a menos em `branches` (50 → 38, contando a `0058`), e um
lugar so para "quanto esta loja cobra para entregar" — que hoje esta partido
entre a regra por km e a taxa do entregador, com cinco e duas colunas.

**Cobra:** a linha e OPCIONAL, entao "sem linha" vira um estado novo. Ele e
igual a "todas as colunas nulas", que ja e o estado de hoje da maioria das
filiais — mas quem consultar por `JOIN` em vez de `LEFT JOIN` passa a perder
filiais inteiras, sem erro. **Toda consulta e `LEFT JOIN`.**

Foi essa contrapartida que reprovou as outras tres divisoes candidatas: para o
estado do dia e as copias de impressao, que sao `NOT NULL`, a linha ausente seria
um estado que hoje nao existe; para o termo comercial, ela seria um TERCEIRO
estado ao lado de "sobrescrito" e "herda" — exatamente a confusao que a armadilha
35 registra.

## A COPIA e a ordem, e ela so quebra com dado dentro

`INSERT ... SELECT` antes dos `DROP COLUMN`, na mesma transacao. Invertido, a
copia leria colunas que ja nao existem — e num banco vazio a copia e no-op e a
ordem errada passaria verde na suite inteira. E a mesma licao da revisao
`20260820_0026` (armadilha 36).

E a copia **so cria linha para filial que tem algum valor**: criar uma linha de
nulos para toda filial encheria a tabela de linhas que nao dizem nada, e
transformaria "sem linha" — o estado que a maioria tem — em algo que nunca
acontece, deixando o caminho `LEFT JOIN` sem cobertura ate o dia em que
acontecesse.

## O codigo vai JUNTO

`Branch` perde as sete colunas e ganha o `relationship`; `AdminBranchUpdate`,
`admin_settings_service`, `delivery_estimate_service` e `courier_delivery_service`
passam a ler pela nova tabela. Com o schema mudado e o model velho, todo `SELECT`
de filial quebra.

O tamanho esta na proposta: **7 arquivos de `src/`, 4 de teste**, medido em
05/09/2026.

## Downgrade

Recria as sete colunas em `branches`, copia de volta e derruba a tabela. **Nao
ha perda**, porque nada aqui e apagado: as duas direcoes copiam antes de
derrubar.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260905_0059"
down_revision: Union[str, None] = "20260905_0058"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


#: As sete que mudam de casa, com o tipo que elas tem hoje em `branches`.
COLUNAS = (
    ("delivery_base_fee", sa.Numeric()),
    ("delivery_fee_per_km", sa.Numeric()),
    ("delivery_min_fee", sa.Numeric()),
    ("delivery_max_fee", sa.Numeric()),
    ("delivery_max_distance_km", sa.Numeric()),
    ("courier_fee_base", sa.Numeric(10, 2)),
    ("courier_fee_per_km", sa.Numeric(10, 2)),
)

_NOMES = tuple(nome for nome, _ in COLUNAS)
_LISTA = ", ".join(_NOMES)
#: Filial "tem tarifa" quando QUALQUER uma das sete esta preenchida. Linha de
#: nulos nao e criada — ver o cabecalho.
_TEM_ALGUM_VALOR = " OR ".join(f"{nome} IS NOT NULL" for nome in _NOMES)


def upgrade() -> None:
    op.create_table(
        "branch_delivery_pricing",
        sa.Column(
            "branch_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("branches.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        *[sa.Column(nome, tipo, nullable=True) for nome, tipo in COLUNAS],
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

    # ANTES dos DROP, e na mesma transacao. Invertido, leria coluna que ja nao
    # existe — e num banco vazio isto e no-op, entao a ordem errada passaria
    # verde na suite inteira e derrubaria o deploy no Junior.
    op.execute(
        sa.text(
            f"INSERT INTO branch_delivery_pricing (branch_id, {_LISTA}) "
            f"SELECT id, {_LISTA} FROM branches WHERE {_TEM_ALGUM_VALOR}"
        )
    )

    for nome in _NOMES:
        op.drop_column("branches", nome)


def downgrade() -> None:
    for nome, tipo in COLUNAS:
        op.add_column("branches", sa.Column(nome, tipo, nullable=True))

    atribuicoes = ", ".join(f"{nome} = p.{nome}" for nome in _NOMES)
    op.execute(
        sa.text(
            f"UPDATE branches AS b SET {atribuicoes} "
            "FROM branch_delivery_pricing AS p WHERE p.branch_id = b.id"
        )
    )

    op.drop_table("branch_delivery_pricing")
