"""Clientes do painel (BLOCO D da Fase 3).

O teste que mais importa aqui e o primeiro: a consulta nao pode encostar na
tabela `customers`. A conta do cliente e global da plataforma, entao um
SELECT no cadastro entregaria ao lojista tambem quem nunca pediu na loja
dele — inclusive cliente de concorrente que usa a mesma plataforma. E
requisito de seguranca, nao de feature, e por isso esta escrito como teste
e nao so como comentario.
"""

import unittest
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy.dialects import postgresql

from src.api.dependencies.admin_scope import AdminScope
from src.repositories.admin_customer_repository import (
    AdminCustomerRepository,
    _build_customer_search_condition,
)
from src.schemas.admin_customer_schema import CustomerSegment
from src.services.admin_customer_service import AdminCustomerService


RESTAURANT_ID = uuid.uuid4()
NOW = datetime(2026, 8, 7, 12, tzinfo=timezone.utc)


def compile_sql(statement) -> str:
    """SQL como o Postgres vai receber.

    Compilar no dialeto certo importa: o DISTINCT ON e o FILTER usados aqui
    so existem no Postgres, e o dialeto generico os descarta silenciosamente.
    """
    return str(statement.compile(dialect=postgresql.dialect()))


def make_row(**overrides):
    values = {
        "customer_phone": "85999990000",
        "orders_count": 3,
        # Por padrao todo pedido e faturavel. O caso interessante — os dois
        # numeros DIFERENTES, que e o que o ticket medio erraria — tem teste
        # proprio em `TicketMedioTests`.
        "billable_orders_count": 3,
        "total_spent": Decimal("150.00"),
        "first_order_at": NOW,
        "last_order_at": NOW,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class RecordingCustomerRepository:
    def __init__(self, rows=(), names=None):
        self.rows = list(rows)
        self.names = names or {}
        self.list_kwargs = None
        self.count_kwargs = None
        self.name_phones = None

    def list_customers(self, **kwargs):
        self.list_kwargs = kwargs
        return self.rows

    def count_customers(self, **kwargs):
        self.count_kwargs = kwargs
        return len(self.rows)

    def get_latest_names(self, restaurant_id, phones):
        self.name_phones = phones
        return self.names


def build_service(repository):
    service = AdminCustomerService(SimpleNamespace())
    service.repository = repository
    return service


def scope(branch_id=None):
    return AdminScope(admin_user=None, restaurant_id=RESTAURANT_ID, branch_id=branch_id)


class CapturingDb:
    """Guarda o SELECT montado em vez de executa-lo.

    E o que permite conferir o SQL de verdade sem subir Postgres: o
    statement compilado e exatamente o que iria para o banco.
    """

    def __init__(self):
        self.statements = []

    def execute(self, statement):
        self.statements.append(statement)
        return SimpleNamespace(all=lambda: [])

    def scalar(self, statement):
        self.statements.append(statement)
        return 0


class DataSourceTests(unittest.TestCase):
    def test_the_query_never_reads_the_customers_table(self):
        db = CapturingDb()
        repository = AdminCustomerRepository(db)
        repository.list_customers(RESTAURANT_ID, search="maria")
        repository.count_customers(RESTAURANT_ID)
        repository.get_latest_names(RESTAURANT_ID, ["85999990000"])

        for statement in db.statements:
            sql = compile_sql(statement)
            self.assertIn("FROM orders", sql)
            # A conta do cliente e global: qualquer leitura de `customers`
            # aqui entregaria ao lojista quem nunca pediu na loja dele.
            self.assertNotIn("FROM customers", sql)
            self.assertNotIn("JOIN customers", sql)

    def test_total_spent_ignores_cancelled_and_rejected(self):
        db = CapturingDb()
        AdminCustomerRepository(db).list_customers(RESTAURANT_ID)

        sql = compile_sql(db.statements[0])
        # Pedido cancelado nao e dinheiro que entrou; somar faria o cliente
        # que mais desistiu parecer o melhor.
        self.assertIn("FILTER", sql.upper())

    def test_contract_does_not_expose_account_fields(self):
        from src.schemas.admin_customer_schema import AdminCustomerListItem

        # E-mail, CPF e o id de cadastro sao da conta global; o lojista e
        # dono do que o cliente informou ao pedir na loja dele.
        for field in ("email", "cpf", "customer_id", "birth_date"):
            self.assertNotIn(field, AdminCustomerListItem.model_fields)


class ListingTests(unittest.TestCase):
    def test_restaurant_comes_from_the_token(self):
        repository = RecordingCustomerRepository()
        build_service(repository).list_customers(scope())

        self.assertEqual(repository.list_kwargs["restaurant_id"], RESTAURANT_ID)
        self.assertEqual(repository.count_kwargs["restaurant_id"], RESTAURANT_ID)

    def test_branch_bound_user_is_filtered_even_without_asking(self):
        branch_id = uuid.uuid4()
        repository = RecordingCustomerRepository()
        build_service(repository).list_customers(scope(branch_id=branch_id))

        self.assertEqual(repository.list_kwargs["branch_id"], branch_id)

    def test_branch_filter_only_narrows(self):
        own_branch = uuid.uuid4()
        repository = RecordingCustomerRepository()
        with self.assertRaises(Exception) as raised:
            build_service(repository).list_customers(
                scope(branch_id=own_branch), branch_id=uuid.uuid4()
            )

        # Pedir a filial vizinha na querystring nao amplia o escopo.
        self.assertEqual(raised.exception.status_code, 404)

    def test_name_comes_from_the_most_recent_order(self):
        row = make_row(customer_phone="85999990000")
        repository = RecordingCustomerRepository(
            rows=[row], names={"85999990000": "Maria Silva"}
        )
        response = build_service(repository).list_customers(scope())

        self.assertEqual(response.items[0].customer_name, "Maria Silva")
        self.assertEqual(repository.name_phones, ["85999990000"])

    def test_pagination_envelope_is_filled(self):
        repository = RecordingCustomerRepository(rows=[make_row()])
        response = build_service(repository).list_customers(scope(), limit=10, offset=20)

        self.assertEqual((response.limit, response.offset, response.total), (10, 20, 1))

    def test_blank_search_becomes_none(self):
        repository = RecordingCustomerRepository()
        build_service(repository).list_customers(scope(), search="   ")

        self.assertIsNone(repository.list_kwargs["search"])

    def test_search_is_trimmed_and_capped(self):
        repository = RecordingCustomerRepository()
        build_service(repository).list_customers(scope(), search="  " + "a" * 500)

        self.assertEqual(len(repository.list_kwargs["search"]), 120)


class TicketMedioTests(unittest.TestCase):
    """O ticket medio, e a divisao que ele quase virou.

    `total_spent` NAO soma cancelado nem recusado, mas `orders_count` conta
    todos os pedidos. Dividir um pelo outro sub-reporta o ticket de todo
    cliente que ja cancelou alguma coisa — e o erro nao aparece em lugar
    nenhum: nao ha excecao, nao ha log, so um numero um pouco menor do que
    deveria, do lado de um total que esta certo.
    """

    def test_divide_pelos_pedidos_que_geraram_dinheiro(self):
        row = make_row(
            orders_count=5,
            billable_orders_count=3,
            total_spent=Decimal("150.00"),
        )
        response = build_service(RecordingCustomerRepository(rows=[row])).list_customers(scope())

        # 150 / 3, e nunca 150 / 5 = 30.
        self.assertEqual(response.items[0].average_ticket, 50.00)

    def test_os_dois_contadores_vao_no_contrato(self):
        """Sem `billable_orders_count` na resposta, a tela mostra tres numeros
        que nao fecham e o lojista abre chamado."""
        row = make_row(orders_count=5, billable_orders_count=3, total_spent=Decimal("150.00"))
        response = build_service(RecordingCustomerRepository(rows=[row])).list_customers(scope())

        item = response.items[0]
        self.assertEqual((item.orders_count, item.billable_orders_count), (5, 3))
        self.assertEqual(item.total_spent, 150.00)

    def test_so_cancelamento_nao_divide_por_zero(self):
        """Cliente que pediu duas vezes e cancelou as duas. E estado normal, e
        nao pode virar 500."""
        row = make_row(orders_count=2, billable_orders_count=0, total_spent=Decimal("0"))
        response = build_service(RecordingCustomerRepository(rows=[row])).list_customers(scope())

        self.assertEqual(response.items[0].average_ticket, 0.0)

    def test_arredonda_para_duas_casas(self):
        row = make_row(orders_count=3, billable_orders_count=3, total_spent=Decimal("100.00"))
        response = build_service(RecordingCustomerRepository(rows=[row])).list_customers(scope())

        # 33,333... — o contrato e float de duas casas, como o resto do schema.
        self.assertEqual(response.items[0].average_ticket, 33.33)


class SegmentoNaRespostaTests(unittest.TestCase):
    """A classificacao chega ao contrato. A REGRA em si tem arquivo proprio
    (`test_customer_segment.py`); o que se prova aqui e o encanamento."""

    def test_a_linha_sai_com_segmento_e_dias(self):
        agora = datetime.now(timezone.utc)
        row = make_row(
            orders_count=12,
            billable_orders_count=12,
            first_order_at=agora - timedelta(days=257),
            last_order_at=agora - timedelta(days=180),
        )
        response = build_service(RecordingCustomerRepository(rows=[row])).list_customers(scope())

        item = response.items[0]
        self.assertEqual(item.segment, CustomerSegment.PERDIDO)
        self.assertEqual(item.days_since_last_order, 180)

    def test_o_segmento_serializa_como_o_codigo_estavel(self):
        """O painel le a string do JSON, e nao o nome do membro do enum."""
        row = make_row(billable_orders_count=3)
        response = build_service(RecordingCustomerRepository(rows=[row])).list_customers(scope())

        self.assertIn(
            response.model_dump(mode="json")["items"][0]["segment"],
            {"novo", "ocasional", "fiel", "em_risco", "perdido"},
        )

    def test_a_pagina_inteira_e_classificada_no_mesmo_instante(self):
        """Uma pagina lida na virada do dia nao pode ter as primeiras linhas
        contra ontem e as ultimas contra hoje: a linha que mudasse de rotulo
        no meio seria impossivel de reproduzir depois."""
        agora = datetime.now(timezone.utc)
        na_fronteira = dict(
            orders_count=1,
            billable_orders_count=1,
            first_order_at=agora - timedelta(days=120, seconds=1),
            last_order_at=agora - timedelta(days=120, seconds=1),
        )
        rows = [make_row(customer_phone=f"8599999000{i}", **na_fronteira) for i in range(20)]
        response = build_service(RecordingCustomerRepository(rows=rows)).list_customers(scope())

        segmentos = {item.segment for item in response.items}
        self.assertEqual(len(segmentos), 1, segmentos)


class SearchConditionTests(unittest.TestCase):
    def test_digits_search_matches_the_phone(self):
        condition = str(_build_customer_search_condition("85999"))

        self.assertIn("customer_phone_snapshot", condition)

    def test_text_search_matches_the_name(self):
        condition = str(_build_customer_search_condition("dona maria"))

        self.assertIn("customer_name_snapshot", condition)
        self.assertIn("lower", condition.lower())

    def test_like_wildcards_are_escaped(self):
        condition = _build_customer_search_condition("100%")

        # Sem escapar, um "%" digitado sozinho listaria a base inteira.
        self.assertIn("\\%", condition.right.value)


if __name__ == "__main__":
    unittest.main()
