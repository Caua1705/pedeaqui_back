"""O encanamento da suíte `db`, testado antes de qualquer teste confiar nele.

São quatro coisas que, se falharem, fazem todo teste de repositório falhar
junto e por um motivo que não é o dele.
"""

import pytest
from sqlalchemy import text


pytestmark = pytest.mark.db


class TestOSchemaDeTeste:
    def test_a_versao_do_postgres_e_a_mesma_de_producao(self, db):
        """PG 17 não é preferência: `gen_random_uuid()` sem extensão, o
        comportamento de collation no ILIKE (armadilha 31) e o planejador
        mudam entre majors."""
        versao = db.execute(text("SHOW server_version_num")).scalar_one()
        assert int(versao) // 10_000 == 17

    def test_as_tabelas_do_pedido_existem(self, db):
        tabelas = db.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public'"
            )
        ).scalars().all()
        assert {"orders", "order_items", "order_item_options", "products"} <= set(tabelas)

    def test_a_sequence_de_order_number_veio_junto(self, db):
        """O que o `Base.metadata.create_all()` teria perdido — armadilha 24.
        Sem ela, todo INSERT de pedido no teste falharia por `order_number`
        nulo, e o motivo estaria a três camadas de distância."""
        existe = db.execute(
            text("SELECT to_regclass('public.orders_order_number_seq') IS NOT NULL")
        ).scalar_one()
        assert existe

    def test_o_alembic_esta_em_head(self, db):
        """Se o stamp e o upgrade não tivessem rodado, o schema seria a foto
        crua e uma revisão nova nunca seria exercitada aqui."""
        revisao = db.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        assert revisao == "20260810_0012"


class TestOIsolamentoEntreTestes:
    """A fixture `db` desfaz tudo. Estes dois testes existem em par: o
    primeiro suja o banco, o segundo confere que a sujeira não chegou nele."""

    def test_grava_um_restaurante(self, db):
        db.execute(
            text(
                "INSERT INTO restaurants (id, name, slug) "
                "VALUES (gen_random_uuid(), 'Rastro de Teste', 'rastro-de-teste')"
            )
        )
        db.commit()
        assert self._quantos_rastros(db) == 1

    def test_o_restaurante_do_teste_anterior_nao_esta_aqui(self, db):
        assert self._quantos_rastros(db) == 0

    @staticmethod
    def _quantos_rastros(db) -> int:
        return db.execute(
            text("SELECT count(*) FROM restaurants WHERE slug = 'rastro-de-teste'")
        ).scalar_one()
