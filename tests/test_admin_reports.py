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
from tests import fabricas


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
        by_hour=None,
        by_weekday_hour=None,
        by_neighborhood=None,
        non_delivery_count=0,
        recency=None,
        recency_by_period=None,
        redeemed=None,
        redeemed_by_period=None,
        earned=None,
        earned_by_period=None,
        durations=None,
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
        self.by_hour = by_hour or []
        self.by_weekday_hour = by_weekday_hour or []
        self.by_neighborhood = by_neighborhood or []
        self.non_delivery_count = non_delivery_count
        self.default_recency = recency or self._empty_recency()
        self.recency_by_period = recency_by_period or {}
        self.default_redeemed = redeemed or {
            "redeemed_total": Decimal("0"),
            "orders_with_redeem_count": 0,
        }
        self.redeemed_by_period = redeemed_by_period or {}
        self.default_earned = earned if earned is not None else Decimal("0")
        self.earned_by_period = earned_by_period or {}
        self.durations = durations or self._empty_durations()

    @staticmethod
    def _empty_recency():
        return {
            "customers_count": 0,
            "new_customers_count": 0,
            "returning_customers_count": 0,
            "new_revenue_total": Decimal("0"),
            "returning_revenue_total": Decimal("0"),
        }

    @staticmethod
    def _empty_durations():
        vazio = {"median": None, "p90": None, "average": None, "orders_count": 0}
        return {
            "orders_count": 0,
            "accept": dict(vazio),
            "prep": dict(vazio),
            "delivery": dict(vazio),
            "late_orders_count": 0,
            "late_orders_base_count": 0,
        }

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

    # --- as quatro consultas de 05/09/2026 ---------------------------------

    def sales_by_hour(self, restaurant_id, start_at, end_at, branch_id=None):
        self.calls.append(("sales_by_hour", start_at, end_at))
        self.branches_asked.append(branch_id)
        return self.by_hour

    def sales_by_weekday_hour(self, restaurant_id, start_at, end_at, branch_id=None):
        self.branches_asked.append(branch_id)
        return self.by_weekday_hour

    def sales_by_neighborhood(self, restaurant_id, start_at, end_at, branch_id=None):
        self.calls.append(("sales_by_neighborhood", start_at, end_at))
        self.branches_asked.append(branch_id)
        return self.by_neighborhood

    def count_non_delivery_orders(self, restaurant_id, start_at, end_at, branch_id=None):
        self.branches_asked.append(branch_id)
        return self.non_delivery_count

    def customers_by_recency(self, restaurant_id, start_at, end_at, branch_id=None):
        self.calls.append(("customers_by_recency", start_at, end_at))
        self.branches_asked.append(branch_id)
        return self.recency_by_period.get(start_at.date(), self.default_recency)

    def cashback_redeemed_totals(self, restaurant_id, start_at, end_at, branch_id=None):
        self.branches_asked.append(branch_id)
        return self.redeemed_by_period.get(start_at.date(), self.default_redeemed)

    def cashback_earned_total(self, restaurant_id, start_at, end_at, branch_id=None):
        self.branches_asked.append(branch_id)
        return self.earned_by_period.get(start_at.date(), self.default_earned)

    def operation_durations(self, restaurant_id, start_at, end_at, branch_id=None):
        self.calls.append(("operation_durations", start_at, end_at))
        self.branches_asked.append(branch_id)
        return self.durations


class FakeCashbackRuleRepository:
    """Dublê de COLABORADOR (repositorio), nao de dado: o que ele devolve
    sao instancias reais de `CashbackRule`, montadas pela fabrica."""

    def __init__(self, da_filial=None, do_restaurante=None):
        self.da_filial = da_filial
        self.do_restaurante = do_restaurante
        self.perguntas = []

    def get_rules_for_branch(self, restaurant_id, branch_id):
        self.perguntas.append(("filial", branch_id))
        return self.da_filial, self.do_restaurante

    def list_restaurant_rules(self, restaurant_ids):
        self.perguntas.append(("restaurante", tuple(restaurant_ids)))
        if self.do_restaurante is None:
            return {}
        return {restaurant_ids[0]: self.do_restaurante}


def build_service(repository, rule_repository=None):
    service = AdminReportService.__new__(AdminReportService)
    service.report_repository = repository
    service.cashback_rule_repository = rule_repository or FakeCashbackRuleRepository()
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

    **O que estes testes mediam mudou em 04/09/2026, e o motivo importa.**
    Eles conferiam a FORMA do SQL — `NOT IN` de um lado, `IN OR =` do outro —
    porque enquanto o faturamento era escrito por exclusao a forma ERA a
    regra: ler as duas expressoes lado a lado era o que provava que uma
    negava a outra.

    Com as duas listas positivas (armadilha 47), a forma deixou de provar
    isso, e insistir nela travaria justamente o conserto. O que ficou no
    lugar e mais forte: os STATUS que cada lado nomeia, conferidos contra as
    constantes, mais a garantia de que nenhum dos dois lados voltou a negar.
    A particao em si (nada de fora, nada nos dois) mora em
    `tests/test_particao_dos_status.py`.
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

    def test_each_side_names_exactly_its_own_statuses(self):
        """O SQL de cada lado nomeia a lista que ele diz nomear.

        E o que impede um dos dois de ser editado sozinho: a constante e o
        predicado deixam de concordar aqui, e nao seis meses depois num
        numero do painel que ninguem consegue explicar.
        """
        from src.core.constants import ORDER_STATUSES, PAYMENT_STATUSES
        from src.repositories.order_repository import (
            BILLABLE_ORDER_STATUSES,
            BILLABLE_PAYMENT_STATUSES,
            NON_BILLABLE_ORDER_STATUSES,
            NON_BILLABLE_PAYMENT_STATUSES,
            billable_order_conditions,
            excluded_order_conditions,
        )

        start = datetime(2026, 7, 1, tzinfo=timezone.utc)
        end = datetime(2026, 8, 1, tzinfo=timezone.utc)
        billable = self._rendered(billable_order_conditions(RESTAURANT_ID, start, end))
        excluded = self._rendered(excluded_order_conditions(RESTAURANT_ID, start, end))

        def nomeados(sql, vocabulario):
            return {valor for valor in vocabulario if f"'{valor}'" in sql}

        self.assertEqual(nomeados(billable, ORDER_STATUSES), set(BILLABLE_ORDER_STATUSES))
        self.assertEqual(nomeados(excluded, ORDER_STATUSES), set(NON_BILLABLE_ORDER_STATUSES))
        self.assertEqual(
            nomeados(billable, PAYMENT_STATUSES), set(BILLABLE_PAYMENT_STATUSES)
        )
        self.assertEqual(
            nomeados(excluded, PAYMENT_STATUSES), set(NON_BILLABLE_PAYMENT_STATUSES)
        )

    def test_neither_side_decides_by_exclusion(self):
        """Nenhum dos dois lados volta a ser escrito por negacao.

        E a armadilha 47 sobre o WHERE que decide COMISSAO: enquanto era
        `NOT IN` + `<>`, um `payment_status` novo nascia faturavel e o
        restaurante pagava por ele sem ninguem ter decidido. O `OR` do lado
        excluido continua sendo cobrado — sem ele, os dois lados deixariam de
        se somar no total de pedidos do periodo.
        """
        from src.repositories.order_repository import (
            billable_order_conditions,
            excluded_order_conditions,
        )

        start = datetime(2026, 7, 1, tzinfo=timezone.utc)
        end = datetime(2026, 8, 1, tzinfo=timezone.utc)
        billable = self._rendered(billable_order_conditions(RESTAURANT_ID, start, end))
        excluded = self._rendered(excluded_order_conditions(RESTAURANT_ID, start, end))

        for rendered in (billable, excluded):
            self.assertNotIn("NOT IN", rendered)
            self.assertNotIn("!=", rendered)
            self.assertNotIn("<>", rendered)
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


# ---------------------------------------------------------------------------
# As quatro rotas de 05/09/2026
# ---------------------------------------------------------------------------


class SalesByHourTests(unittest.TestCase):
    """As 24 horas SEMPRE, e o mapa dia x hora que NAO e preenchido.

    A assimetria entre os dois e a decisao do relatorio, e ela e o unico
    lugar onde ele poderia mentir: a hora 3 sem venda existiu de verdade; a
    segunda-feira de um recorte de quarta a sexta nao existiu.
    """

    def test_devolve_as_24_horas_mesmo_com_venda_em_uma_so(self):
        repository = FakeReportRepository(by_hour=[(19, 3, Decimal("300.00"))])

        report = build_service(repository).sales_by_hour(
            RESTAURANT_ID, date(2026, 7, 1), date(2026, 7, 7)
        )

        self.assertEqual(len(report.hours), 24)
        self.assertEqual([item.hour for item in report.hours], list(range(24)))
        self.assertEqual(report.hours[19].orders_count, 3)
        self.assertEqual(report.hours[19].revenue_total, Decimal("300.00"))

    def test_hora_sem_venda_vem_zerada_e_nao_omitida(self):
        """Sem isto o grafico ligaria 11h direto em 13h e o vale sumiria."""
        repository = FakeReportRepository(by_hour=[(11, 1, Decimal("50.00"))])

        report = build_service(repository).sales_by_hour(
            RESTAURANT_ID, date(2026, 7, 1), date(2026, 7, 1)
        )

        self.assertEqual(report.hours[12].orders_count, 0)
        self.assertEqual(report.hours[12].revenue_total, Decimal("0.00"))

    def test_o_total_e_a_soma_das_horas(self):
        repository = FakeReportRepository(
            by_hour=[(11, 2, Decimal("100.00")), (19, 3, Decimal("300.50"))]
        )

        report = build_service(repository).sales_by_hour(
            RESTAURANT_ID, date(2026, 7, 1), date(2026, 7, 7)
        )

        self.assertEqual(report.orders_count, 5)
        self.assertEqual(report.revenue_total, Decimal("400.50"))

    def test_o_mapa_dia_x_hora_NAO_e_preenchido_com_zero(self):
        """A assimetria com `hours`, e o motivo dela.

        As 24 horas existem em todo dia; um dia da semana pode nao estar no
        periodo. Preencher faria a resposta afirmar que a loja nao vendeu
        numa segunda que o recorte nem contem."""
        repository = FakeReportRepository(
            by_weekday_hour=[(2, 19, 3, Decimal("300.00"))]
        )

        report = build_service(repository).sales_by_hour(
            RESTAURANT_ID, date(2026, 7, 1), date(2026, 7, 1)
        )

        self.assertEqual(len(report.weekday_hours), 1)
        celula = report.weekday_hours[0]
        self.assertEqual(celula.weekday, 2)
        self.assertEqual(celula.hour, 19)

    def test_periodo_vazio_ainda_devolve_as_24_zeradas(self):
        report = build_service(FakeReportRepository()).sales_by_hour(
            RESTAURANT_ID, date(2026, 7, 1), date(2026, 7, 31)
        )

        self.assertEqual(len(report.hours), 24)
        self.assertEqual(report.revenue_total, Decimal("0.00"))
        self.assertEqual(report.weekday_hours, [])

    def test_o_recorte_de_filial_chega_as_duas_consultas(self):
        repository = FakeReportRepository()
        filial = uuid.uuid4()

        build_service(repository).sales_by_hour(
            RESTAURANT_ID, date(2026, 7, 1), date(2026, 7, 7), branch_id=filial
        )

        self.assertTrue(repository.branches_asked)
        self.assertTrue(all(pedida == filial for pedida in repository.branches_asked))


class NeighborhoodsTests(unittest.TestCase):
    def test_ticket_medio_e_fatia_por_bairro(self):
        repository = FakeReportRepository(
            by_neighborhood=[
                ("Aldeota", "Fortaleza", 4, Decimal("400.00")),
                ("Centro", "Fortaleza", 2, Decimal("100.00")),
            ]
        )

        report = build_service(repository).neighborhoods_report(
            RESTAURANT_ID, date(2026, 7, 1), date(2026, 7, 31)
        )

        self.assertEqual(report.revenue_total, Decimal("500.00"))
        self.assertEqual(report.orders_count, 6)
        self.assertEqual(report.neighborhoods[0].average_ticket, Decimal("100.00"))
        self.assertEqual(report.neighborhoods[0].revenue_share_percent, Decimal("80.00"))
        self.assertEqual(report.neighborhoods[1].average_ticket, Decimal("50.00"))

    def test_bairro_nulo_continua_nulo_e_nao_vira_outro(self):
        """Mesma regra do `payment_method` nulo: o pedido existe, o dinheiro
        entrou, e ninguem anotou onde. "Outro" seria um bairro de verdade."""
        repository = FakeReportRepository(
            by_neighborhood=[(None, None, 1, Decimal("70.00"))]
        )

        report = build_service(repository).neighborhoods_report(
            RESTAURANT_ID, date(2026, 7, 1), date(2026, 7, 31)
        )

        self.assertIsNone(report.neighborhoods[0].neighborhood)
        self.assertIsNone(report.neighborhoods[0].city)

    def test_a_retirada_e_publicada_a_parte_e_nao_somada(self):
        """O total daqui NAO bate com o do resumo, e a diferenca precisa ser
        explicavel sem abrir o codigo."""
        repository = FakeReportRepository(
            by_neighborhood=[("Aldeota", "Fortaleza", 4, Decimal("400.00"))],
            non_delivery_count=9,
        )

        report = build_service(repository).neighborhoods_report(
            RESTAURANT_ID, date(2026, 7, 1), date(2026, 7, 31)
        )

        self.assertEqual(report.orders_count, 4)
        self.assertEqual(report.non_delivery_orders_count, 9)

    def test_periodo_sem_entrega_nenhuma_nao_divide_por_zero(self):
        report = build_service(FakeReportRepository()).neighborhoods_report(
            RESTAURANT_ID, date(2026, 7, 1), date(2026, 7, 31)
        )

        self.assertEqual(report.neighborhoods, [])
        self.assertEqual(report.revenue_total, Decimal("0.00"))


def recencia(novos, recorrentes, receita_nova="0", receita_recorrente="0"):
    return {
        "customers_count": novos + recorrentes,
        "new_customers_count": novos,
        "returning_customers_count": recorrentes,
        "new_revenue_total": Decimal(receita_nova),
        "returning_revenue_total": Decimal(receita_recorrente),
    }


class CustomersReportTests(unittest.TestCase):
    def test_novos_e_recorrentes_com_o_periodo_anterior_ao_lado(self):
        repository = FakeReportRepository(
            recency_by_period={
                date(2026, 7, 8): recencia(3, 7, "300.00", "700.00"),
                date(2026, 7, 1): recencia(1, 4),
            }
        )

        report = build_service(repository).customers_report(
            RESTAURANT_ID, date(2026, 7, 8), date(2026, 7, 14)
        )

        self.assertEqual(report.customers_count.current, Decimal("10"))
        self.assertEqual(report.customers_count.previous, Decimal("5"))
        self.assertEqual(report.new_customers_count.current, Decimal("3"))
        self.assertEqual(report.returning_customers_count.current, Decimal("7"))
        self.assertEqual(report.new_revenue_total, Decimal("300.00"))
        self.assertEqual(report.returning_revenue_total, Decimal("700.00"))

    def test_sem_cliente_no_periodo_anterior_a_variacao_e_nula(self):
        """Sair de zero para dez clientes nao e crescimento de 1000%."""
        repository = FakeReportRepository(
            recency_by_period={date(2026, 7, 8): recencia(10, 0)}
        )

        report = build_service(repository).customers_report(
            RESTAURANT_ID, date(2026, 7, 8), date(2026, 7, 14)
        )

        self.assertIsNone(report.customers_count.change_percent)

    def test_o_gerado_e_o_resgatado_sao_numeros_independentes(self):
        """Eles nao fecham entre si de proposito: o credito nasce na
        conclusao de um pedido e o resgate na criacao de outro."""
        repository = FakeReportRepository(
            earned_by_period={
                date(2026, 7, 8): Decimal("120.00"),
                date(2026, 7, 1): Decimal("80.00"),
            },
            redeemed_by_period={
                date(2026, 7, 8): {
                    "redeemed_total": Decimal("45.00"),
                    "orders_with_redeem_count": 6,
                },
                date(2026, 7, 1): {
                    "redeemed_total": Decimal("15.00"),
                    "orders_with_redeem_count": 2,
                },
            },
        )

        report = build_service(repository).customers_report(
            RESTAURANT_ID, date(2026, 7, 8), date(2026, 7, 14)
        )

        self.assertEqual(report.cashback.earned_total.current, Decimal("120.00"))
        self.assertEqual(report.cashback.earned_total.previous, Decimal("80.00"))
        self.assertEqual(report.cashback.redeemed_total.current, Decimal("45.00"))
        self.assertEqual(report.cashback.orders_with_redeem_count, 6)

    def test_configured_e_falso_sem_regra_nenhuma(self):
        """O estado de fabrica: `cashback_rules.enabled` nasce falso em todo
        restaurante. Sem este campo, "R$ 0,00 resgatados" nao distingue
        "ninguem usa" de "ninguem ligou"."""
        report = build_service(FakeReportRepository()).customers_report(
            RESTAURANT_ID, date(2026, 7, 1), date(2026, 7, 31)
        )

        self.assertFalse(report.cashback.configured)

    def test_configured_e_verdadeiro_com_regra_do_restaurante_ligada(self):
        regras = FakeCashbackRuleRepository(do_restaurante=fabricas.regra_de_cashback())

        report = build_service(FakeReportRepository(), regras).customers_report(
            RESTAURANT_ID, date(2026, 7, 1), date(2026, 7, 31)
        )

        self.assertTrue(report.cashback.configured)

    def test_regra_desligada_nao_conta_como_configurada(self):
        regras = FakeCashbackRuleRepository(
            do_restaurante=fabricas.regra_de_cashback(enabled=False)
        )

        report = build_service(FakeReportRepository(), regras).customers_report(
            RESTAURANT_ID, date(2026, 7, 1), date(2026, 7, 31)
        )

        self.assertFalse(report.cashback.configured)

    def test_sem_branch_id_so_a_regra_do_RESTAURANTE_responde(self):
        """Uma filial que sobrescreveu a campanha nao torna a REDE
        configurada — e a leitura certa para quem olha o total da rede."""
        regras = FakeCashbackRuleRepository(do_restaurante=fabricas.regra_de_cashback())

        build_service(FakeReportRepository(), regras).customers_report(
            RESTAURANT_ID, date(2026, 7, 1), date(2026, 7, 31)
        )

        self.assertEqual([tipo for tipo, _ in regras.perguntas], ["restaurante"])

    def test_com_branch_id_a_regra_da_filial_entra_na_heranca(self):
        regras = FakeCashbackRuleRepository(
            da_filial=fabricas.regra_de_cashback(enabled=False),
            do_restaurante=fabricas.regra_de_cashback(),
        )
        filial = uuid.uuid4()

        report = build_service(FakeReportRepository(), regras).customers_report(
            RESTAURANT_ID, date(2026, 7, 1), date(2026, 7, 31), branch_id=filial
        )

        # A da filial VENCE a do restaurante, e ela esta desligada — a mesma
        # heranca do checkout, e nao um `or` entre as duas.
        self.assertFalse(report.cashback.configured)
        self.assertEqual(regras.perguntas, [("filial", filial)])


class OperationsReportTests(unittest.TestCase):
    def test_os_minutos_saem_com_uma_casa(self):
        repository = FakeReportRepository(
            durations={
                "orders_count": 12,
                "accept": {
                    "median": Decimal("2.5"),
                    "p90": Decimal("9.25"),
                    "average": Decimal("3.333333"),
                    "orders_count": 12,
                },
                "prep": {
                    "median": Decimal("18"),
                    "p90": Decimal("35"),
                    "average": Decimal("21"),
                    "orders_count": 10,
                },
                "delivery": {
                    "median": None,
                    "p90": None,
                    "average": None,
                    "orders_count": 0,
                },
                "late_orders_count": 2,
                "late_orders_base_count": 8,
            }
        )

        report = build_service(repository).operations_report(
            RESTAURANT_ID, date(2026, 7, 1), date(2026, 7, 31)
        )

        self.assertEqual(report.accept_minutes.median, Decimal("2.5"))
        self.assertEqual(report.accept_minutes.p90, Decimal("9.2"))
        self.assertEqual(report.accept_minutes.average, Decimal("3.3"))
        self.assertEqual(report.prep_minutes.median, Decimal("18.0"))

    def test_estagio_sem_pedido_nenhum_vem_NULO_e_nao_zero(self):
        """"Mediana de 0 min" afirmaria um aceite instantaneo. O que houve
        foi nao ter havido aceite nenhum."""
        report = build_service(FakeReportRepository()).operations_report(
            RESTAURANT_ID, date(2026, 7, 1), date(2026, 7, 31)
        )

        self.assertIsNone(report.accept_minutes.median)
        self.assertIsNone(report.prep_minutes.p90)
        self.assertIsNone(report.delivery_minutes.average)
        self.assertEqual(report.delivery_minutes.orders_count, 0)

    def test_cada_bloco_traz_o_proprio_numero_de_pedidos(self):
        """Nem todo pedido passa por todos os estagios: retirada nao tem
        entrega, e pedido aceito e cancelado nao tem preparo."""
        repository = FakeReportRepository(
            durations={
                "orders_count": 30,
                "accept": {"median": Decimal("2"), "p90": Decimal("5"),
                           "average": Decimal("3"), "orders_count": 30},
                "prep": {"median": Decimal("20"), "p90": Decimal("40"),
                         "average": Decimal("22"), "orders_count": 28},
                "delivery": {"median": Decimal("15"), "p90": Decimal("30"),
                             "average": Decimal("17"), "orders_count": 19},
                "late_orders_count": 0,
                "late_orders_base_count": 28,
            }
        )

        report = build_service(repository).operations_report(
            RESTAURANT_ID, date(2026, 7, 1), date(2026, 7, 31)
        )

        self.assertEqual(report.orders_count, 30)
        self.assertEqual(report.accept_minutes.orders_count, 30)
        self.assertEqual(report.prep_minutes.orders_count, 28)
        self.assertEqual(report.delivery_minutes.orders_count, 19)

    def test_a_taxa_de_atraso_sai_sobre_quem_TINHA_prazo_prometido(self):
        """O denominador NAO e `prep_minutes.orders_count`: pedido com
        preparo medido e sem prazo prometido nao pode ser julgado atrasado, e
        conta-lo embaixo faria a tela subestimar o atraso."""
        repository = FakeReportRepository(
            durations={
                "orders_count": 100,
                "accept": {"median": None, "p90": None, "average": None, "orders_count": 0},
                "prep": {"median": Decimal("20"), "p90": Decimal("40"),
                         "average": Decimal("22"), "orders_count": 100},
                "delivery": {"median": None, "p90": None, "average": None, "orders_count": 0},
                "late_orders_count": 5,
                "late_orders_base_count": 20,
            }
        )

        report = build_service(repository).operations_report(
            RESTAURANT_ID, date(2026, 7, 1), date(2026, 7, 31)
        )

        self.assertEqual(report.late_orders_base_count, 20)
        # 5 em 20, e nao 5 em 100.
        self.assertEqual(report.late_orders_percent, Decimal("25.00"))

    def test_sem_denominador_a_taxa_e_nula_e_nao_zero(self):
        report = build_service(FakeReportRepository()).operations_report(
            RESTAURANT_ID, date(2026, 7, 1), date(2026, 7, 31)
        )

        self.assertEqual(report.late_orders_count, 0)
        self.assertIsNone(report.late_orders_percent)

    def test_o_recorte_de_filial_chega_ao_repositorio(self):
        repository = FakeReportRepository()
        filial = uuid.uuid4()

        build_service(repository).operations_report(
            RESTAURANT_ID, date(2026, 7, 1), date(2026, 7, 7), branch_id=filial
        )

        self.assertEqual(repository.branches_asked, [filial])


class PeriodoDasQuatroTests(unittest.TestCase):
    """As quatro validam periodo como as seis anteriores.

    Sem isto, um recorte de anos varre a tabela inteira em quatro rotas
    novas — e o teto de 92 dias existiria so nas antigas.
    """

    def test_periodo_invertido_e_recusado_nas_quatro(self):
        service = build_service(FakeReportRepository())
        for metodo in (
            service.sales_by_hour,
            service.neighborhoods_report,
            service.customers_report,
            service.operations_report,
        ):
            with self.subTest(metodo=metodo.__name__):
                with self.assertRaises(HTTPException) as raised:
                    metodo(RESTAURANT_ID, date(2026, 7, 31), date(2026, 7, 1))
                self.assertEqual(raised.exception.status_code, 400)

    def test_periodo_longo_demais_e_recusado_nas_quatro(self):
        service = build_service(FakeReportRepository())
        for metodo in (
            service.sales_by_hour,
            service.neighborhoods_report,
            service.customers_report,
            service.operations_report,
        ):
            with self.subTest(metodo=metodo.__name__):
                with self.assertRaises(HTTPException) as raised:
                    metodo(RESTAURANT_ID, date(2026, 1, 1), date(2026, 12, 31))
                self.assertEqual(raised.exception.status_code, 400)

    def test_o_fuso_da_operacao_vale_nas_quatro(self):
        for metodo_nome, chamada in (
            ("sales_by_hour", "sales_by_hour"),
            ("neighborhoods_report", "sales_by_neighborhood"),
            ("customers_report", "customers_by_recency"),
            ("operations_report", "operation_durations"),
        ):
            with self.subTest(metodo=metodo_nome):
                repository = FakeReportRepository()
                getattr(build_service(repository), metodo_nome)(
                    RESTAURANT_ID, date(2026, 7, 1), date(2026, 7, 1)
                )

                nome, start_at, end_at = next(
                    linha for linha in repository.calls if linha[0] == chamada
                )
                self.assertEqual(start_at.utcoffset(), timedelta(hours=-3))
                self.assertEqual(start_at.day, 1)
                self.assertEqual(end_at.day, 2)
