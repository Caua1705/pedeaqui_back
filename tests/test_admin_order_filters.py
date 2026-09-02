"""Filtros da listagem de pedidos do painel (BLOCO A1 e A3 da Fase 3).

Tres coisas sutis moram aqui e cada uma ja mordeu alguem em algum projeto:

1. O recorte de datas e lido no fuso da OPERACAO, nao em UTC. Sem isso, tres
   horas de pedidos caem no dia errado e o lojista jura que sumiu venda.
2. O campo de busca aceita numero do pedido e nome no mesmo input, e os dois
   viram SQL diferente.
3. Os badges tem que mostrar zero, nao sumir, quando um status esvazia.
"""

import unittest
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

from fastapi import HTTPException

from src.api.dependencies.admin_scope import AdminScope
from src.core.constants import ORDER_STATUSES
from src.repositories.order_repository import _build_search_condition
from src.services.admin_order_service import MAX_LIST_DAYS, AdminOrderService
from tests import fabricas


class RecordingRepository:
    """Guarda os argumentos que chegaram, que e o que se quer provar aqui."""

    def __init__(self, orders=(), grouped=None):
        self.orders = list(orders)
        self.grouped = grouped or {}
        self.list_kwargs = None
        self.count_kwargs = None
        self.grouped_kwargs = None

    def list_orders_by_restaurant(self, **kwargs):
        self.list_kwargs = kwargs
        return self.orders

    def count_orders_by_restaurant(self, **kwargs):
        self.count_kwargs = kwargs
        return len(self.orders)

    def count_orders_grouped_by_status(self, **kwargs):
        self.grouped_kwargs = kwargs
        return self.grouped


def build_service(repository):
    service = AdminOrderService(SimpleNamespace())
    service.order_repository = repository
    return service


def owner_scope():
    return AdminScope(admin_user=None, restaurant_id=uuid.uuid4(), branch_id=None)


class PeriodFilterTests(unittest.TestCase):
    def test_dates_are_read_in_the_operation_timezone(self):
        repository = RecordingRepository()
        build_service(repository).list_orders(
            owner_scope(), start_date=date(2026, 8, 1), end_date=date(2026, 8, 1)
        )

        start_at = repository.list_kwargs["start_at"]
        # America/Fortaleza e UTC-3: a meia-noite do lojista sao 03:00 UTC.
        self.assertEqual(start_at.utcoffset().total_seconds(), -3 * 3600)
        self.assertEqual((start_at.hour, start_at.minute), (0, 0))

    def test_end_date_is_exclusive_on_the_next_day(self):
        repository = RecordingRepository()
        build_service(repository).list_orders(
            owner_scope(), start_date=date(2026, 8, 1), end_date=date(2026, 8, 1)
        )

        # O repositorio usa `<` nesse limite. Se fosse o fim do mesmo dia, o
        # pedido feito 23:59:59.7 ficaria de fora da propria data dele.
        self.assertEqual(repository.list_kwargs["end_at"].date(), date(2026, 8, 2))

    def test_only_one_side_of_the_period_is_allowed(self):
        repository = RecordingRepository()
        build_service(repository).list_orders(owner_scope(), start_date=date(2026, 8, 1))

        self.assertIsNotNone(repository.list_kwargs["start_at"])
        self.assertIsNone(repository.list_kwargs["end_at"])

    def test_inverted_period_is_refused(self):
        with self.assertRaises(HTTPException) as raised:
            build_service(RecordingRepository()).list_orders(
                owner_scope(), start_date=date(2026, 8, 10), end_date=date(2026, 8, 1)
            )

        self.assertEqual(raised.exception.status_code, 400)

    def test_period_longer_than_the_ceiling_is_refused(self):
        # Sem teto, um start_date=2020-01-01 varre a tabela inteira.
        with self.assertRaises(HTTPException) as raised:
            build_service(RecordingRepository()).list_orders(
                owner_scope(),
                start_date=date(2026, 1, 1),
                end_date=date(2026, 1, 1).replace(year=2028),
            )

        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn(str(MAX_LIST_DAYS), raised.exception.detail)


class SearchFilterTests(unittest.TestCase):
    def test_blank_search_becomes_none(self):
        repository = RecordingRepository()
        build_service(repository).list_orders(owner_scope(), search="   ")

        # String em branco viraria um ILIKE '%%' que casa com tudo e ainda
        # paga o custo do scan.
        self.assertIsNone(repository.list_kwargs["search"])

    def test_search_is_trimmed_and_capped(self):
        repository = RecordingRepository()
        build_service(repository).list_orders(owner_scope(), search="  " + "a" * 500)

        self.assertEqual(len(repository.list_kwargs["search"]), 120)

    def test_digits_search_matches_the_order_number_exactly(self):
        condition = str(_build_search_condition("5471"))

        # order_number e BigInteger: um ILIKE ali forcaria cast da coluna
        # inteira e perderia o indice.
        self.assertIn("orders.order_number", condition)
        self.assertNotIn("lower", condition.lower())

    def test_text_search_matches_the_customer_name(self):
        condition = str(_build_search_condition("dona maria"))

        self.assertIn("customer_name_snapshot", condition)
        self.assertIn("lower", condition.lower())

    def test_like_wildcards_in_the_search_are_escaped(self):
        condition = _build_search_condition("100%")

        # Sem escapar, um "%" digitado sozinho listaria a base inteira e um
        # cliente chamado "Ana_" nunca seria encontrado.
        value = condition.right.value
        self.assertIn("\\%", value)


class StatusFilterTests(unittest.TestCase):
    def test_unknown_status_is_refused_before_touching_the_database(self):
        repository = RecordingRepository()

        with self.assertRaises(HTTPException) as raised:
            build_service(repository).list_orders(owner_scope(), order_status="entregue")

        self.assertEqual(raised.exception.status_code, 400)
        self.assertIsNone(repository.list_kwargs)

    def test_status_counts_include_every_status_even_at_zero(self):
        repository = RecordingRepository(grouped={"pending": 3, "ready": 1})

        result = build_service(repository).count_orders_by_status(owner_scope())

        # O badge de "pendentes" nao pode sumir da tela quando chega a zero
        # — e justamente quando o lojista quer ver o zero.
        self.assertEqual([item.status for item in result.counts], list(ORDER_STATUSES))
        self.assertEqual(result.total, 4)

    def test_status_counts_do_not_filter_by_status(self):
        repository = RecordingRepository(grouped={})
        build_service(repository).count_orders_by_status(owner_scope())

        # Filtrar por status aqui zeraria todos os outros contadores.
        self.assertNotIn("status", repository.grouped_kwargs)


class PaginationTests(unittest.TestCase):
    def test_response_carries_the_total_of_the_filter(self):
        orders = [
            fabricas.pedido(
                order_number=index, customer_name_snapshot="Cliente",
                order_type="delivery", payment_method="cash",
                payment_status="on_delivery", total=Decimal("10.00"),
                # Instante FIXO, e nao `datetime.now()`: a lista nao le a hora
                # para nada, e a hora real so faz o teste depender do relogio.
                created_at=datetime(2026, 7, 15, 12, tzinfo=timezone.utc),
            )
            for index in range(3)
        ]
        repository = RecordingRepository(orders=orders)

        result = build_service(repository).list_orders(owner_scope(), limit=10, offset=20)

        # Sem `total` o painel nao consegue desenhar a paginacao: com a
        # pagina em maos nao da para saber se existe a proxima.
        self.assertEqual(result.total, 3)
        self.assertEqual((result.limit, result.offset), (10, 20))
        self.assertEqual(len(result.items), 3)

    def test_the_page_and_the_total_use_the_same_filter(self):
        repository = RecordingRepository()
        build_service(repository).list_orders(
            owner_scope(), order_status="pending", search="maria", start_date=date(2026, 8, 1)
        )

        # Se um dos dois esquecer um filtro, o lojista ve "1 de 40" numa
        # lista de 12 e para de confiar nos dois numeros.
        page = {key: value for key, value in repository.list_kwargs.items()
                if key not in {"limit", "offset"}}
        self.assertEqual(page, repository.count_kwargs)


if __name__ == "__main__":
    unittest.main()
