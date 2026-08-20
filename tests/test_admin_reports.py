"""Relatorios de Desempenho do painel.

O que estes testes protegem, em ordem de importancia:

1. O fuso. Todo recorte e lido em America/Fortaleza, e o repositorio so ve
   instantes UTC ja convertidos.
2. O conjunto de pedidos. Faturamento e cancelamentos sao complementos
   exatos um do outro, pelo mesmo predicado do extrato de comissao.
3. As divisoes por zero. Periodo vazio, periodo anterior vazio e restaurante
   sem venda nenhuma sao o estado NORMAL de um restaurante novo — nenhum
   deles pode virar 500.
"""

import unittest
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from fastapi import HTTPException

from src.services.admin_report_service import AdminReportService


RESTAURANT_ID = uuid.uuid4()


class FakeReportRepository:
    """Devolve o que as consultas agregadas devolveriam.

    Guarda os limites recebidos em cada chamada para que os testes possam
    conferir a conversao de fuso sem um banco.
    """

    def __init__(
        self,
        totals=None,
        by_type=None,
        by_day=None,
        by_method=None,
        products=None,
        cancellations=None,
        cancellations_by_status=None,
        totals_by_period=None,
    ):
        self.default_totals = totals or self._empty_totals()
        self.totals_by_period = totals_by_period or {}
        self.by_type = by_type or []
        self.by_day = by_day or []
        self.by_method = by_method or []
        self.products = products or []
        self.cancellations = cancellations or {"orders_count": 0, "amount_total": Decimal("0")}
        self.cancellations_by_status_rows = cancellations_by_status or []
        self.calls = []
        # Toda consulta registra a filial que recebeu. E o que permite provar
        # que o recorte CHEGOU ao repositorio sem subir Postgres — e, no
        # resumo, que ele chegou tambem na consulta do periodo anterior.
        self.branches_asked = []

    @staticmethod
    def _empty_totals():
        return {
            "orders_count": 0,
            "revenue_total": Decimal("0"),
            "subtotal_total": Decimal("0"),
            "delivery_fee_total": Decimal("0"),
            "service_fee_total": Decimal("0"),
            "discount_total": Decimal("0"),
            "commission_total": Decimal("0"),
        }

    def sales_totals(self, restaurant_id, start_at, end_at, branch_id=None):
        self.calls.append(("sales_totals", start_at, end_at))
        self.branches_asked.append(branch_id)
        # Permite dar numeros diferentes ao periodo atual e ao anterior, que
        # e o que o resumo compara.
        return self.totals_by_period.get(start_at.date(), self.default_totals)

    def totals_by_order_type(self, restaurant_id, start_at, end_at, branch_id=None):
        self.branches_asked.append(branch_id)
        return self.by_type

    def sales_by_day(self, restaurant_id, start_at, end_at, branch_id=None):
        self.calls.append(("sales_by_day", start_at, end_at))
        self.branches_asked.append(branch_id)
        return self.by_day

    def totals_by_payment_method(self, restaurant_id, start_at, end_at, branch_id=None):
        self.branches_asked.append(branch_id)
        return self.by_method

    def top_products(self, restaurant_id, start_at, end_at, limit, branch_id=None):
        self.calls.append(("top_products", start_at, end_at, limit))
        self.branches_asked.append(branch_id)
        return self.products[:limit]

    def cancellation_totals(self, restaurant_id, start_at, end_at, branch_id=None):
        self.branches_asked.append(branch_id)
        return self.cancellations

    def cancellations_by_status(self, restaurant_id, start_at, end_at, branch_id=None):
        self.branches_asked.append(branch_id)
        return self.cancellations_by_status_rows


def build_service(repository):
    service = AdminReportService.__new__(AdminReportService)
    service.report_repository = repository
    return service


def totals(orders_count, revenue, **extra):
    base = FakeReportRepository._empty_totals()
    base.update(orders_count=orders_count, revenue_total=Decimal(revenue))
    base.update({key: Decimal(value) for key, value in extra.items()})
    return base


class PeriodTests(unittest.TestCase):
    def test_period_is_read_in_the_operation_timezone(self):
        # Mesma regra do extrato de comissao: America/Fortaleza e UTC-3, o
        # dia 1 comeca as 03:00 UTC. Sem isso, tres horas de pedidos caem no
        # dia errado do relatorio.
        repository = FakeReportRepository()
        build_service(repository).sales_by_day(
            RESTAURANT_ID, date(2026, 7, 1), date(2026, 7, 1)
        )

        _, start_at, end_at = repository.calls[0]
        self.assertEqual(start_at.utcoffset(), timedelta(hours=-3))
        self.assertEqual(start_at.day, 1)
        # Fim exclusivo no comeco do dia seguinte, para nao perder pedido
        # feito 23:59:59.
        self.assertEqual(end_at.day, 2)

    def test_inverted_period_is_refused(self):
        service = build_service(FakeReportRepository())

        with self.assertRaises(HTTPException) as raised:
            service.sales_summary(RESTAURANT_ID, date(2026, 7, 31), date(2026, 7, 1))

        self.assertEqual(raised.exception.status_code, 400)

    def test_period_longer_than_the_limit_is_refused(self):
        service = build_service(FakeReportRepository())

        with self.assertRaises(HTTPException) as raised:
            service.cancellations_report(RESTAURANT_ID, date(2026, 1, 1), date(2026, 12, 31))

        self.assertEqual(raised.exception.status_code, 400)

    def test_previous_period_is_the_same_length_immediately_before(self):
        # Comparar sete dias com "o mes passado" mediria a diferenca de
        # tamanho dos blocos, nao a do movimento.
        service = build_service(FakeReportRepository())

        report = service.sales_summary(RESTAURANT_ID, date(2026, 7, 8), date(2026, 7, 14))

        self.assertEqual(report.period.days, 7)
        self.assertEqual(report.previous_period.days, 7)
        self.assertEqual(report.previous_period.end_date, date(2026, 7, 7))
        self.assertEqual(report.previous_period.start_date, date(2026, 7, 1))


class SummaryTests(unittest.TestCase):
    def test_average_ticket_is_revenue_over_orders(self):
        repository = FakeReportRepository(totals=totals(4, "402.00"))
        report = build_service(repository).sales_summary(
            RESTAURANT_ID, date(2026, 7, 1), date(2026, 7, 31)
        )

        self.assertEqual(report.revenue_total, Decimal("402.00"))
        self.assertEqual(report.average_ticket, Decimal("100.50"))

    def test_empty_period_returns_zero_and_not_a_division_error(self):
        report = build_service(FakeReportRepository()).sales_summary(
            RESTAURANT_ID, date(2026, 7, 1), date(2026, 7, 31)
        )

        self.assertEqual(report.orders_count, 0)
        self.assertEqual(report.revenue_total, Decimal("0.00"))
        self.assertEqual(report.average_ticket, Decimal("0.00"))

    def test_change_percent_is_null_when_the_previous_period_was_zero(self):
        # Sair de zero para dez pedidos nao e "crescimento de 1000%".
        repository = FakeReportRepository(
            totals_by_period={
                date(2026, 7, 1): totals(10, "1000.00"),
                date(2026, 6, 1): totals(0, "0"),
            }
        )
        report = build_service(repository).sales_summary(
            RESTAURANT_ID, date(2026, 7, 1), date(2026, 7, 30)
        )

        self.assertEqual(report.revenue_comparison.current, Decimal("1000.00"))
        self.assertEqual(report.revenue_comparison.previous, Decimal("0.00"))
        self.assertEqual(report.revenue_comparison.change, Decimal("1000.00"))
        self.assertIsNone(report.revenue_comparison.change_percent)

    def test_change_percent_is_computed_when_there_is_a_base(self):
        repository = FakeReportRepository(
            totals_by_period={
                date(2026, 7, 1): totals(12, "1200.00"),
                date(2026, 6, 1): totals(10, "1000.00"),
            }
        )
        report = build_service(repository).sales_summary(
            RESTAURANT_ID, date(2026, 7, 1), date(2026, 7, 30)
        )

        self.assertEqual(report.revenue_comparison.change_percent, Decimal("20.00"))
        self.assertEqual(report.orders_count_comparison.change, Decimal("2"))

    def test_a_drop_produces_a_negative_change(self):
        repository = FakeReportRepository(
            totals_by_period={
                date(2026, 7, 1): totals(5, "500.00"),
                date(2026, 6, 1): totals(10, "1000.00"),
            }
        )
        report = build_service(repository).sales_summary(
            RESTAURANT_ID, date(2026, 7, 1), date(2026, 7, 30)
        )

        self.assertEqual(report.revenue_comparison.change, Decimal("-500.00"))
        self.assertEqual(report.revenue_comparison.change_percent, Decimal("-50.00"))

    def test_order_type_split_carries_the_share_of_revenue(self):
        repository = FakeReportRepository(
            totals=totals(4, "400.00"),
            by_type=[("delivery", 3, Decimal("300.00")), ("pickup", 1, Decimal("100.00"))],
        )
        report = build_service(repository).sales_summary(
            RESTAURANT_ID, date(2026, 7, 1), date(2026, 7, 31)
        )

        shares = {item.order_type: item.revenue_share_percent for item in report.order_types}
        self.assertEqual(shares["delivery"], Decimal("75.00"))
        self.assertEqual(shares["pickup"], Decimal("25.00"))

    def test_share_is_null_when_the_period_had_no_revenue(self):
        # Nao existe fatia de zero. "0%" faria a tela afirmar que aquele tipo
        # nao vendeu, quando nenhum vendeu.
        repository = FakeReportRepository(
            totals=totals(0, "0"),
            by_type=[("delivery", 0, Decimal("0"))],
        )
        report = build_service(repository).sales_summary(
            RESTAURANT_ID, date(2026, 7, 1), date(2026, 7, 31)
        )

        self.assertIsNone(report.order_types[0].revenue_share_percent)

    def test_breakdown_adds_up_to_the_revenue(self):
        # subtotal + entrega + servico - desconto = total
        repository = FakeReportRepository(
            totals=totals(
                2,
                "115.00",
                subtotal_total="100.00",
                delivery_fee_total="10.00",
                service_fee_total="10.00",
                discount_total="5.00",
            )
        )
        report = build_service(repository).sales_summary(
            RESTAURANT_ID, date(2026, 7, 1), date(2026, 7, 31)
        )

        recomposed = (
            report.breakdown.subtotal_total
            + report.breakdown.delivery_fee_total
            + report.breakdown.service_fee_total
            - report.breakdown.discount_total
        )
        self.assertEqual(recomposed, report.revenue_total)


class SalesByDayTests(unittest.TestCase):
    def test_days_without_sales_are_filled_with_zero(self):
        # O grafico precisa do vale desenhado; omitir o dia ligaria terca
        # direto em quinta.
        repository = FakeReportRepository(
            by_day=[
                (date(2026, 7, 1), 2, Decimal("200.00")),
                (date(2026, 7, 3), 1, Decimal("50.00")),
            ]
        )
        report = build_service(repository).sales_by_day(
            RESTAURANT_ID, date(2026, 7, 1), date(2026, 7, 3)
        )

        self.assertEqual([item.day.day for item in report.days], [1, 2, 3])
        self.assertEqual(report.days[1].orders_count, 0)
        self.assertEqual(report.days[1].revenue_total, Decimal("0.00"))

    def test_totals_are_the_sum_of_the_series(self):
        repository = FakeReportRepository(
            by_day=[
                (date(2026, 7, 1), 2, Decimal("200.00")),
                (date(2026, 7, 2), 1, Decimal("50.00")),
            ]
        )
        report = build_service(repository).sales_by_day(
            RESTAURANT_ID, date(2026, 7, 1), date(2026, 7, 2)
        )

        self.assertEqual(report.orders_count, 3)
        self.assertEqual(report.revenue_total, Decimal("250.00"))

    def test_a_single_day_period_returns_one_day(self):
        report = build_service(FakeReportRepository()).sales_by_day(
            RESTAURANT_ID, date(2026, 7, 5), date(2026, 7, 5)
        )

        self.assertEqual(len(report.days), 1)
        self.assertEqual(report.days[0].day, date(2026, 7, 5))


class PaymentMethodTests(unittest.TestCase):
    def test_shares_are_computed_over_the_listed_revenue(self):
        repository = FakeReportRepository(
            by_method=[("pix", 3, Decimal("300.00")), ("cash", 1, Decimal("100.00"))]
        )
        report = build_service(repository).payment_methods_report(
            RESTAURANT_ID, date(2026, 7, 1), date(2026, 7, 31)
        )

        self.assertEqual(report.revenue_total, Decimal("400.00"))
        self.assertEqual(report.payment_methods[0].revenue_share_percent, Decimal("75.00"))

    def test_orders_without_a_registered_method_stay_null(self):
        # Nulo e "nao registrado", que nao e a forma de pagamento "other".
        repository = FakeReportRepository(by_method=[(None, 2, Decimal("80.00"))])
        report = build_service(repository).payment_methods_report(
            RESTAURANT_ID, date(2026, 7, 1), date(2026, 7, 31)
        )

        self.assertIsNone(report.payment_methods[0].payment_method)
        self.assertEqual(report.payment_methods[0].orders_count, 2)


class ProductSalesTests(unittest.TestCase):
    def test_items_carry_quantity_and_revenue(self):
        product_id = uuid.uuid4()
        repository = FakeReportRepository(
            products=[(product_id, "Picanha", "picanha", 4, 9, Decimal("450.00"))]
        )
        report = build_service(repository).product_sales_report(
            RESTAURANT_ID, date(2026, 7, 1), date(2026, 7, 31)
        )

        item = report.products[0]
        self.assertEqual(item.product_id, product_id)
        self.assertEqual(item.product_name, "Picanha")
        self.assertEqual(item.quantity_total, 9)
        self.assertEqual(item.revenue_total, Decimal("450.00"))

    def test_product_removed_from_the_menu_keeps_the_snapshot_name(self):
        repository = FakeReportRepository(
            products=[(None, "Combo antigo", None, 1, 1, Decimal("30.00"))]
        )
        report = build_service(repository).product_sales_report(
            RESTAURANT_ID, date(2026, 7, 1), date(2026, 7, 31)
        )

        self.assertIsNone(report.products[0].product_id)
        self.assertEqual(report.products[0].product_name, "Combo antigo")

    def test_limit_is_passed_through_to_the_query(self):
        repository = FakeReportRepository(
            products=[(uuid.uuid4(), f"P{i}", None, 1, 1, Decimal("10.00")) for i in range(50)]
        )
        report = build_service(repository).product_sales_report(
            RESTAURANT_ID, date(2026, 7, 1), date(2026, 7, 31), limit=5
        )

        self.assertEqual(len(report.products), 5)
        self.assertEqual(repository.calls[0][3], 5)

    def test_response_warns_that_the_revenue_does_not_match_the_summary(self):
        # Receita de item nao desconta cupom nem cashback. Sem o aviso, quem
        # consome compara com /reports/summary e acha que ha um bug.
        report = build_service(FakeReportRepository()).product_sales_report(
            RESTAURANT_ID, date(2026, 7, 1), date(2026, 7, 31)
        )

        self.assertIn("summary", report.revenue_note)


class CancellationTests(unittest.TestCase):
    def test_rate_is_over_all_orders_and_not_only_the_billable_ones(self):
        # 12 cancelados e 100 faturados sao 12 em 112 pedidos feitos.
        repository = FakeReportRepository(
            totals=totals(100, "10000.00"),
            cancellations={"orders_count": 12, "amount_total": Decimal("900.00")},
        )
        report = build_service(repository).cancellations_report(
            RESTAURANT_ID, date(2026, 7, 1), date(2026, 7, 31)
        )

        self.assertEqual(report.orders_count, 12)
        self.assertEqual(report.billable_orders_count, 100)
        self.assertEqual(report.cancellation_rate_percent, Decimal("10.71"))

    def test_rate_is_null_when_there_was_no_order_at_all(self):
        report = build_service(FakeReportRepository()).cancellations_report(
            RESTAURANT_ID, date(2026, 7, 1), date(2026, 7, 31)
        )

        self.assertEqual(report.orders_count, 0)
        self.assertIsNone(report.cancellation_rate_percent)

    def test_refunded_order_appears_separated_from_a_rejected_one(self):
        # `completed` + `refunded` e a comida que saiu e o dinheiro que
        # voltou: caso operacional diferente de uma recusa.
        repository = FakeReportRepository(
            cancellations={"orders_count": 3, "amount_total": Decimal("300.00")},
            cancellations_by_status=[
                ("rejected", "on_delivery", 2, Decimal("200.00")),
                ("completed", "refunded", 1, Decimal("100.00")),
            ],
        )
        report = build_service(repository).cancellations_report(
            RESTAURANT_ID, date(2026, 7, 1), date(2026, 7, 31)
        )

        pairs = {(item.status, item.payment_status): item.orders_count for item in report.breakdown}
        self.assertEqual(pairs[("rejected", "on_delivery")], 2)
        self.assertEqual(pairs[("completed", "refunded")], 1)


class BillableFilterAgreementTests(unittest.TestCase):
    """Faturamento e cancelamento tem que ser complementos exatos.

    Testa o predicado SQL, nao o service: se um dia alguem mexer em um dos
    dois lados sem mexer no outro, o painel passa a dizer que o faturamento
    foi de X pedidos e o extrato de comissao que a base foi de outro numero,
    sem ninguem conseguir explicar a diferenca.
    """

    @staticmethod
    def _rendered(conditions):
        from sqlalchemy import and_
        from sqlalchemy.dialects import postgresql

        return str(
            and_(*conditions).compile(
                dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
            )
        )

    def test_both_sides_name_the_same_statuses(self):
        from src.repositories.order_repository import (
            NON_BILLABLE_ORDER_STATUSES,
            billable_order_conditions,
            excluded_order_conditions,
        )

        start = datetime(2026, 7, 1, tzinfo=timezone.utc)
        end = datetime(2026, 8, 1, tzinfo=timezone.utc)
        billable = self._rendered(billable_order_conditions(RESTAURANT_ID, start, end))
        excluded = self._rendered(excluded_order_conditions(RESTAURANT_ID, start, end))

        for order_status in NON_BILLABLE_ORDER_STATUSES:
            self.assertIn(order_status, billable)
            self.assertIn(order_status, excluded)
        self.assertIn("refunded", billable)
        self.assertIn("refunded", excluded)

    def test_one_side_negates_the_other(self):
        # NOT IN + <> de um lado, IN OR = do outro. Um pedido do periodo cai
        # em exatamente um dos dois conjuntos, que e o que permite somar
        # faturados e cancelados para achar o total de pedidos feitos.
        from src.repositories.order_repository import (
            billable_order_conditions,
            excluded_order_conditions,
        )

        start = datetime(2026, 7, 1, tzinfo=timezone.utc)
        end = datetime(2026, 8, 1, tzinfo=timezone.utc)
        billable = self._rendered(billable_order_conditions(RESTAURANT_ID, start, end))
        excluded = self._rendered(excluded_order_conditions(RESTAURANT_ID, start, end))

        self.assertIn("NOT IN", billable)
        self.assertIn("!=", billable)
        self.assertNotIn("NOT IN", excluded)
        self.assertIn(" OR ", excluded)

    def test_both_sides_read_the_same_period(self):
        from src.repositories.order_repository import (
            billable_order_conditions,
            excluded_order_conditions,
        )

        start = datetime(2026, 7, 1, tzinfo=timezone.utc)
        end = datetime(2026, 8, 1, tzinfo=timezone.utc)
        billable = self._rendered(billable_order_conditions(RESTAURANT_ID, start, end))
        excluded = self._rendered(excluded_order_conditions(RESTAURANT_ID, start, end))

        for rendered in (billable, excluded):
            # Inicio inclusivo, fim exclusivo nos dois lados.
            self.assertIn("created_at >=", rendered)
            self.assertIn("created_at <", rendered)
            self.assertNotIn("created_at <=", rendered)


if __name__ == "__main__":
    unittest.main()
