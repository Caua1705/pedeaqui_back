"""adota o indice de admin_users.restaurant_id sob o nome canonico

Revision ID: 20260823_0036
Revises: 20260823_0035
Create Date: 2026-08-23

`idx_admin_users_restaurant_id` e `ix_admin_users_restaurant_id` existem os
DOIS em producao, sobre a mesma coluna. Os dois estao no
`alembic/schema_baseline.sql`, quer dizer: no banco de verdade.

Como chegaram la: a tabela `admin_users` foi criada fora de qualquer
migracao versionada, ja com o `idx_`. A revisao `20260726_0003`, que
reconciliou a tabela, criou o `ix_` com `CREATE INDEX IF NOT EXISTS` — e o
IF NOT EXISTS casa por NOME, nao por definicao. Para o Postgres nasceram
dois indices, nao um; ele mantem os dois em toda escrita em `admin_users` e
nenhuma consulta a mais e servida por isso.

Nada falha por causa disso, e e justamente por isso que durou: foi achado em
22/08/2026 rodando `scripts/audit_indexes.py`.

O conserto e o mesmo da `20260810_0012`, que fez esta adocao em
`order_items`: DROP do nome antigo e CREATE do canonico, na MESMA
transacao, DROP primeiro para nao manter as duas copias em disco ao mesmo
tempo. Ou a tabela termina com o indice canonico, ou termina como estava.

Revisao propria, e nao junto da `20260822_0032` (que fez a mesma adocao em
`cashback_transactions`): sao tabelas diferentes e motivos diferentes de
mexer, e um rollback de uma arrastaria a outra.

DEPLOY: `CREATE INDEX` sem CONCURRENTLY trava escrita na tabela enquanto
roda, e o entrypoint roda `alembic upgrade head` antes do Uvicorn. Aqui o
risco e pequeno de um jeito que nao era em `order_items`: `admin_users` tem
uma linha por pessoa do painel, nao uma por item de pedido. A construcao e
instantanea.
"""

from typing import Sequence, Union

from alembic import op


revision: str = "20260823_0036"
down_revision: Union[str, None] = "20260823_0035"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Nomes escritos como literal, e nao como constante do modulo, pelo mesmo
# motivo da 20260810_0012: `scripts/audit_indexes.py` descobre o que o
# Alembic e dono lendo os nomes das revisoes com expressao regular, e um
# nome vindo de variavel escapa dessa leitura — o indice desta revisao
# apareceria no relatorio como "sem dono", que e o oposto do que ela faz.


def upgrade() -> None:
    # IF EXISTS porque nem todo banco tem o nome antigo: um ambiente montado
    # depois do baseline so recebeu o `ix_` da 20260726_0003.
    op.drop_index("idx_admin_users_restaurant_id", table_name="admin_users", if_exists=True)
    # IF NOT EXISTS pela convencao da 20260806_0010: `admin_users` e do
    # baseline e o nome canonico ja pode estar ocupado — neste caso ele
    # esta, e o CREATE e no-op. E o DROP acima que faz o trabalho.
    op.create_index(
        "ix_admin_users_restaurant_id",
        "admin_users",
        ["restaurant_id"],
        if_not_exists=True,
    )


def downgrade() -> None:
    """Devolve a duplicata, nome antigo inclusive.

    Recriar `idx_admin_users_restaurant_id` parece errado — e o proprio
    defeito voltando —, mas o downgrade existe para desfazer um deploy, e
    desfazer significa devolver a tabela ao estado em que o codigo anterior
    a encontrou. Sair daqui com um indice a menos e uma mudanca que o
    downgrade nao pediu.

    Ao contrario da `20260810_0012`, este downgrade NAO derruba o nome
    canonico: la o `ix_` nasceu na propria revisao, aqui ele veio da
    `20260726_0003`. Derruba-lo aqui deixaria sem indice nenhum quem
    parasse entre as duas.
    """
    op.create_index(
        "idx_admin_users_restaurant_id",
        "admin_users",
        ["restaurant_id"],
        if_not_exists=True,
    )
