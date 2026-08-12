"""O encanamento da suíte `db`, testado antes de qualquer teste confiar nele.

São quatro coisas que, se falharem, fazem todo teste de repositório falhar
junto e por um motivo que não é o dele.
"""

from pathlib import Path

import pytest
from alembic.script import ScriptDirectory
from sqlalchemy import text


pytestmark = pytest.mark.db

RAIZ_DO_REPOSITORIO = Path(__file__).resolve().parent.parent


def _head_do_repositorio() -> str:
    """A revisão mais nova em `alembic/versions/`, lida pelo próprio Alembic.

    `ScriptDirectory` direto, sem `Config`: passar pelo `alembic.ini` só para
    descobrir o head faria este teste carregar o `prepend_sys_path` do arquivo
    e emitir o DeprecationWarning do Alembic — ruído que não é dele.
    """
    return ScriptDirectory(str(RAIZ_DO_REPOSITORIO / "alembic")).get_current_head()


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

    def test_a_automacao_de_rls_veio_no_baseline(self, db):
        """O gatilho `ensure_rls`, e não só a função que ele chama.

        O `pg_dump` que gerou o `schema_baseline.sql` trouxe
        `rls_auto_enable()` e deixou o `CREATE EVENT TRIGGER` para trás. Sem
        ele, tabela criada por revisão futura nasce sem RLS aqui e com RLS em
        produção — e o teste passa verde contra um schema que não é o de lá.
        """
        gatilho = db.execute(
            text(
                "SELECT evtenabled FROM pg_event_trigger WHERE evtname = 'ensure_rls'"
            )
        ).scalar_one_or_none()
        assert gatilho is not None, "o baseline não criou o event trigger ensure_rls"
        assert gatilho == "O", "o event trigger ensure_rls existe mas está desabilitado"

    def test_tabela_nova_nasce_com_rls(self, db):
        """A prova de que o gatilho funciona, e não só existe."""
        db.execute(text("CREATE TABLE prova_de_rls (id int)"))
        ligada = db.execute(
            text("SELECT relrowsecurity FROM pg_class WHERE relname = 'prova_de_rls'")
        ).scalar_one()
        db.execute(text("DROP TABLE prova_de_rls"))
        assert ligada is True

    def test_o_alembic_esta_em_head(self, db):
        """Se o stamp e o upgrade não tivessem rodado, o schema seria a foto
        crua e uma revisão nova nunca seria exercitada aqui.

        O head sai do `alembic/versions/` em vez de ser um literal: com o
        número escrito à mão, toda revisão nova quebrava este teste — e o
        reflexo de "atualizar o número para o que o banco devolveu" faz o
        teste dizer só que o banco concorda consigo mesmo.
        """
        revisao = db.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        assert revisao == _head_do_repositorio()


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
