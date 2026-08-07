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
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy.dialects import postgresql

from src.api.dependencies.admin_scope import AdminScope
from src.repositories.admin_customer_repository import (
    AdminCustomerRepository,
    _build_customer_search_condition,
)
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
