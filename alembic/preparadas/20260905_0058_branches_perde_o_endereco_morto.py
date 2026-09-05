"""branches perde as cinco colunas de endereco que ninguem le nem escreve

Revision ID: 20260905_0058
Revises: 20260905_0056
Create Date: 2026-09-05

`Revises` aqui e MARCADOR, e nao uma revisao de verdade: e o head do dia em que
esta foi escrita. Quem tirar do `preparadas/` acerta o valor para o head daquele
momento (passo 2 do `LEIA-ME.md`) — e a `20260905_0057`, que tambem esta
esperando decisao, pode muito bem estar na frente ate la.

**ESCRITA E NAO APLICADA.** Mora em `alembic/preparadas/`, que o Alembic nao le.
Ver `alembic/preparadas/LEIA-ME.md` e a armadilha 53. O plano esta em
`docs/modelo-de-dados.md`, secao "PENDENTE: quebrar a tabela `branches`".

Esta e a **etapa A** da divisao de `branches`, e a unica das duas que nao
depende de decisao nenhuma: ela nao move dado, nao cria tabela e nao muda
comportamento. So tira lixo.

---

## O que sai, e por que e lixo e nao dado

`branches` tem **treze** colunas de endereco, e elas nao sao treze campos: sao
dois conjuntos dizendo a mesma coisa, mais lat/lng.

| Conjunto | Colunas | Estado |
|---|---|---|
| **vivo** | `address`, `neighborhood`, `city`, `state`, `zipcode` | e o que `AdminBranchUpdate` grava e o que `_build_address` le. As quatro primeiras sao `NOT NULL` |
| **morto** | `address_street`, `address_neighborhood`, `address_city`, `address_state`, `address_zipcode` | resto do schema pre-Alembic. **Nenhuma revisao as toca e nada em `src/` as le ou escreve** |
| exceção | `address_number` | **FICA.** Nao tem par vivo, e a unica fonte do numero da casa |
| coordenada | `latitude`, `longitude` | origem do calculo de rota, sem par |

Ate 2026 o conjunto morto **vencia na leitura**: `_build_address` resolvia
`branch.address_street or branch.address`. Numa filial com essas colunas
preenchidas, o lojista corrigia o endereco no painel, o painel exibia o valor
novo (ele le `branch.address`) e o app do cliente continuava mostrando o antigo
— sem erro, sem log, sem tela onde conferir. A leitura ja foi consertada; o que
sobrou foram cinco colunas orfas esperando esta revisao.

**Conferido em 05/09/2026:** as unicas ocorrencias de `branch.address_*` em
`src/` sao `branch.address_number` (em `RestaurantService._build_address`) e as
declaracoes do model. Todo o resto dos `address_street`/`address_city`/... que
o grep encontra e de **`orders`**, que tem colunas homonimas e vivissimas — o
snapshot do endereco de entrega. **Nao confunda as duas tabelas ao conferir**: e
o jeito mais facil de achar que esta revisao apaga o endereco dos pedidos.

## Por que largar `address_number` sozinha nao da

Ela nao sobrescreve ninguem: nao existe `branches.number` nem campo de numero em
`AdminBranchUpdate`. Largar apagaria o numero da casa do endereco publico de
toda filial que o tenha preenchido, sem nada para por no lugar. Quem for tirar a
sexta precisa antes decidir **onde o numero passa a ser escrito** — e isso e
mudanca de painel, nao de schema.

## O codigo vai JUNTO, e nao antes nem depois

`src/models/branch_model.py` mapeia as cinco. Com as colunas fora do banco e o
model ainda mapeando, **todo `SELECT` de filial quebra** — o SQLAlchemy pede
coluna que nao existe. E com o model largando antes, `scripts/
divergencias_orm_schema.py` passa a contar cinco "coluna que o ORM nao mapeia".

Um commit so, com o `git mv` desta revisao e as cinco linhas fora do model.

## Downgrade

Recria as cinco como `TEXT NULL`, que e exatamente o que elas sao hoje. **O
conteudo nao volta**, e isso e o que se aceita ao aplicar: elas nao tinham
escritor havia anos, e o que estava la dentro era a copia velha que causou o
defeito acima. Se o valor de alguma importar, ele precisa ser copiado para o
conjunto vivo ANTES — e isso e trabalho de dado, nao de migracao.

## Custo de aplicar

`DROP COLUMN` no Postgres nao reescreve a tabela: ele marca o atributo como
apagado no catalogo. `ACCESS EXCLUSIVE` por milissegundos, sem varredura.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260905_0058"
down_revision: Union[str, None] = "20260905_0056"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


#: As cinco com par vivo. `address_number` NAO esta aqui, de proposito.
COLUNAS_MORTAS = (
    "address_street",
    "address_neighborhood",
    "address_city",
    "address_state",
    "address_zipcode",
)


def upgrade() -> None:
    for coluna in COLUNAS_MORTAS:
        op.drop_column("branches", coluna)


def downgrade() -> None:
    for coluna in COLUNAS_MORTAS:
        op.add_column("branches", sa.Column(coluna, sa.Text(), nullable=True))
