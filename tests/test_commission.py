"""Comissao da plataforma.

Duas metades: o que e gravado no pedido (na criacao, congelado) e o que o
relatorio soma (nada e recalculado la).

Base = subtotal - desconto de cupom - cashback usado. Taxa de entrega, taxa
de servico e taxa do gateway NAO entram.
"""

import unittest
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

from fastapi import HTTPException

from src.schemas.order_schema import CreateOrderRequest
from src.services.admin_report_service import AdminReportService
from src.services.order_service import OrderService


class FakeDb:
    def commit(self):
        pass

    def rollback(self):
        pass

    def refresh(self, value):
        pass


class FakeOrderRepository:
    def __init__(self):
        self.orders = []

    def create_order(self, order):
        order.id = uuid.uuid4()
        order.order_number = 77
        self.orders.append(order)

    def create_order_items(self, items):
        for item in items:
            item.id = uuid.uuid4()

    def create_order_item_options(self, options):
        pass

    def create_status_history(self, history):
        pass


class FakeCouponService:
    """Cupom ja validado: aqui so interessa o desconto que ele produz."""

    def __init__(self, discount):
        self.discount = discount
        self.coupon = SimpleNamespace(id=uuid.uuid4(), code="SAVE")

    def lock_and_validate_for_order(self, **kwargs):
        return self.coupon, self.discount

    def create_redemption(self, *args, **kwargs):
        return None


def build_service(*, commission_percent=Decimal("10.00"), service_fee=Decimal("0"), coupon_discount=None):
    branch = SimpleNamespace(id=uuid.uuid4())
    product_id = uuid.uuid4()
    product = SimpleNamespace(
        id=product_id,
        code="P1",
        name="Picanha",
        description=None,
        price=Decimal("50.00"),
        option_groups=[],
    )

    service = OrderService(FakeDb())
    service.restaurant_service = SimpleNamespace(
        get_active_restaurant=lambda slug: SimpleNamespace(id=uuid.uuid4())
    )
    service.branch_repository = SimpleNamespace(
        get_active_by_id_and_restaurant=lambda branch_id, restaurant_id: branch,
        list_enabled_payment_methods=lambda branch_id: [
            SimpleNamespace(method_type="cash", payment_flow="delivery"),
        ],
    )
    service.branch_hours_service = SimpleNamespace(ensure_branch_is_open=lambda branch_id: None)
    service.menu_repository = SimpleNamespace(
        get_settings=lambda restaurant_id: SimpleNamespace(
            min_order_value=Decimal("0"),
            service_fee_enabled=service_fee > 0,
            service_fee_amount=service_fee,
            is_open=True,
            accepts_delivery=True,
            accepts_pickup=True,
            platform_commission_percent=commission_percent,
        )
    )
    service.product_repository = SimpleNamespace(
        list_active_by_ids=lambda restaurant_id, ids: [product]
    )
    service.order_repository = FakeOrderRepository()
    if coupon_discount is not None:
        service.coupon_service = FakeCouponService(coupon_discount)

    body = {
        "branch_id": str(branch.id),
        "order_type": "pickup",
        "payment_method": "cash",
        "customer": {"name": "Ana", "phone": "85999999999"},
        "items": [{"product_id": str(product_id), "quantity": 2}],
    }
    if coupon_discount is not None:
        body["coupon_code"] = "SAVE"
    return service, CreateOrderRequest.model_validate(body)


class CommissionOnCreationTests(unittest.TestCase):
    def test_commission_is_written_on_the_order(self):
        service, payload = build_service(commission_percent=Decimal("10.00"))

        service.create_order("junior", payload)

        order = service.order_repository.orders[0]
        self.assertEqual(order.subtotal, Decimal("100.00"))
        self.assertEqual(order.commission_percent, Decimal("10.00"))
        self.assertEqual(order.commission_base_amount, Decimal("100.00"))
        self.assertEqual(order.commission_amount, Decimal("10.00"))

    def test_percent_comes_from_the_restaurant_and_not_from_a_constant(self):
        service, payload = build_service(commission_percent=Decimal("7.50"))

        service.create_order("junior", payload)

        order = service.order_repository.orders[0]
        self.assertEqual(order.commission_percent, Decimal("7.50"))
        self.assertEqual(order.commission_amount, Decimal("7.50"))

    def test_service_fee_is_not_part_of_the_base(self):
        # A taxa de servico e repasse, nao receita da venda.
        service, payload = build_service(service_fee=Decimal("3.00"))

        service.create_order("junior", payload)

        order = service.order_repository.orders[0]
        self.assertEqual(order.service_fee, Decimal("3.00"))
        self.assertEqual(order.commission_base_amount, Decimal("100.00"))

    def test_coupon_discount_reduces_the_base(self):
        # O restaurante recebeu menos; cobrar comissao sobre o desconto
        # seria cobrar sobre dinheiro que ninguem pagou.
        service, payload = build_service(coupon_discount=Decimal("20.00"))
        customer = SimpleNamespace(id=uuid.uuid4(), name="Ana", phone="85999999999")

        service.create_order("junior", payload, customer)

        order = service.order_repository.orders[0]
        self.assertEqual(order.commission_base_amount, Decimal("80.00"))
        self.assertEqual(order.commission_amount, Decimal("8.00"))

    def test_discount_bigger_than_the_subtotal_does_not_make_the_base_negative(self):
        service, payload = build_service(coupon_discount=Decimal("150.00"))
        customer = SimpleNamespace(id=uuid.uuid4(), name="Ana", phone="85999999999")

        service.create_order("junior", payload, customer)

        order = service.order_repository.orders[0]
        self.assertEqual(order.commission_base_amount, Decimal("0.00"))
        self.assertEqual(order.commission_amount, Decimal("0.00"))

    def test_restaurant_without_settings_falls_back_to_the_platform_default(self):
        service, payload = build_service()
        service.menu_repository = SimpleNamespace(get_settings=lambda restaurant_id: None)

        service.create_order("junior", payload)

        order = service.order_repository.orders[0]
        self.assertEqual(order.commission_percent, Decimal("10.00"))
        self.assertEqual(order.commission_amount, Decimal("10.00"))

    def test_rounding_is_half_up_with_two_decimals(self):
        # 100.00 * 8.33% = 8.33
        service, payload = build_service(commission_percent=Decimal("8.33"))

        service.create_order("junior", payload)

        self.assertEqual(service.order_repository.orders[0].commission_amount, Decimal("8.33"))


class FakeReportRepository:
    def __init__(self, orders, excluded=0):
        self.orders = orders
        self.excluded = excluded
        self.bounds = None

    def list_orders_for_commission(self, restaurant_id, start_at, end_at):
        self.bounds = (start_at, end_at)
        return self.orders

    def count_excluded_from_commission(self, restaurant_id, start_at, end_at):
        return self.excluded


def report_order(commission_amount, base=Decimal("100.00"), percent=Decimal("10.00")):
    return SimpleNamespace(
        id=uuid.uuid4(),
        order_number=1,
        created_at=datetime(2026, 7, 15, 12, tzinfo=timezone.utc),
        status="completed",
        payment_status="paid",
        payment_method="pix",
        subtotal=Decimal("100.00"),
        coupon_discount_amount=Decimal("0.00"),
        cashback_redeemed_amount=Decimal("0.00"),
        commission_base_amount=base,
        commission_percent=percent,
        commission_amount=commission_amount,
        total=Decimal("103.00"),
    )


def build_report_service(repository):
    service = AdminReportService.__new__(AdminReportService)
    service.order_repository = repository
    return service


class CommissionReportTests(unittest.TestCase):
    def test_totals_are_the_sum_of_the_extract(self):
        repository = FakeReportRepository([
            report_order(Decimal("10.00")),
            report_order(Decimal("7.55")),
        ])
        service = build_report_service(repository)

        report = service.commission_report(uuid.uuid4(), date(2026, 7, 1), date(2026, 7, 31))

        self.assertEqual(report.orders_count, 2)
        self.assertEqual(report.commission_total, Decimal("17.55"))
        self.assertEqual(report.commission_base_total, Decimal("200.00"))
        self.assertEqual(len(report.orders), 2)

    def test_extract_carries_base_and_percent_of_each_order(self):
        repository = FakeReportRepository([report_order(Decimal("8.00"), base=Decimal("80.00"), percent=Decimal("10.00"))])
        service = build_report_service(repository)

        report = service.commission_report(uuid.uuid4(), date(2026, 7, 1), date(2026, 7, 31))

        item = report.orders[0]
        self.assertEqual(item.commission_base_amount, Decimal("80.00"))
        self.assertEqual(item.commission_percent, Decimal("10.00"))
        self.assertEqual(item.commission_amount, Decimal("8.00"))

    def test_excluded_orders_are_reported_as_a_number(self):
        repository = FakeReportRepository([report_order(Decimal("10.00"))], excluded=3)
        service = build_report_service(repository)

        report = service.commission_report(uuid.uuid4(), date(2026, 7, 1), date(2026, 7, 31))

        self.assertEqual(report.excluded_orders_count, 3)

    def test_empty_period_returns_zero_and_not_an_error(self):
        service = build_report_service(FakeReportRepository([]))

        report = service.commission_report(uuid.uuid4(), date(2026, 7, 1), date(2026, 7, 31))

        self.assertEqual(report.orders_count, 0)
        self.assertEqual(report.commission_total, Decimal("0.00"))

    def test_period_is_read_in_the_operation_timezone(self):
        # America/Fortaleza e UTC-3: o dia 1 comeca as 03:00 UTC. Sem isso,
        # tres horas de pedidos caem no dia errado do relatorio.
        repository = FakeReportRepository([])
        service = build_report_service(repository)

        service.commission_report(uuid.uuid4(), date(2026, 7, 1), date(2026, 7, 1))

        start_at, end_at = repository.bounds
        self.assertEqual(start_at.utcoffset(), timedelta(hours=-3))
        self.assertEqual(start_at.day, 1)
        # Fim exclusivo no comeco do dia seguinte, para nao perder pedido
        # feito 23:59:59.
        self.assertEqual(end_at.day, 2)

    def test_inverted_period_is_refused(self):
        service = build_report_service(FakeReportRepository([]))

        with self.assertRaises(HTTPException) as raised:
            service.commission_report(uuid.uuid4(), date(2026, 7, 31), date(2026, 7, 1))

        self.assertEqual(raised.exception.status_code, 400)

    def test_period_longer_than_the_limit_is_refused(self):
        service = build_report_service(FakeReportRepository([]))

        with self.assertRaises(HTTPException) as raised:
            service.commission_report(uuid.uuid4(), date(2026, 1, 1), date(2026, 12, 31))

        self.assertEqual(raised.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
