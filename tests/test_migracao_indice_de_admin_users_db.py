"""A duplicata de índice em `admin_users`, contra um Postgres de verdade.

A fixture do banco aplica `alembic/schema_baseline.sql` — a foto de produção,
onde os DOIS índices existem — e depois `upgrade head`. Então este arquivo
prova exatamente o que a revisão 0036 precisa provar: partindo do estado de
produção, sobra um índice sobre `restaurant_id`, e é o canônico.

Só há teste contra banco porque o defeito é do banco. A revisão não muda
nenhuma linha de código do app: um `CREATE INDEX IF NOT EXISTS` que casa por
NOME não tem como ser conferido sem um Postgres com o nome antigo dentro.
"""

import pytest
from sqlalchemy import text


pytestmark = pytest.mark.db


def indices_de_restaurant_id(db) -> list[str]:
    linhas = db.execute(
        text(
            """
            SELECT indexname
            FROM pg_indexes
            WHERE schemaname = 'public'
              AND tablename = 'admin_users'
              AND indexdef LIKE '%(restaurant_id)'
            ORDER BY indexname
            """
        )
    ).scalars().all()
    return list(linhas)


def test_sobra_um_indice_so_sobre_restaurant_id(db):
    assert indices_de_restaurant_id(db) == ["ix_admin_users_restaurant_id"]


def test_o_nome_legado_nao_existe_mais(db):
    """O `idx_` é o que a criação à mão deixou, antes de qualquer migração.

    Enquanto ele existir junto do `ix_`, o Postgres mantém os dois em toda
    escrita em `admin_users` sem servir a nenhuma consulta a mais — e nada
    falha, que é o motivo de ter durado de 26/07 a 23/08/2026.
    """
    assert "idx_admin_users_restaurant_id" not in indices_de_restaurant_id(db)
