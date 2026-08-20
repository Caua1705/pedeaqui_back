import unittest
import uuid
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

from fastapi import HTTPException
from pydantic import ValidationError

from src.api.dependencies.admin_scope import AdminScope
from src.schemas.coupon_schema import CouponPreviewRequest
from src.schemas.order_schema import CreateOrderRequest
from src.services.admin_order_service import AdminOrderService
from src.services.coupon_service import CouponService
from src.services.order_service import OrderService


NOW = datetime(2026, 7, 22, 12, tzinfo=timezone.utc)


def make_coupon(**overrides):
    values = {
        "id": uuid.uuid4(),
        "restaurant_id": uuid.uuid4(),
        "code": "PROMO10",
        "title": "Promocao",
        "description": None,
        "discount_type": "fixed",
        "discount_value": Decimal("10.00"),
        "max_discount_amount": None,
        "min_order_value": Decimal("0.00"),
        "valid_from": NOW - timedelta(days=1),
        "valid_until": NOW + timedelta(days=1),
        "total_usage_limit": None,
        "usage_limit_per_customer": None,
        "cooldown_days": None,
        "first_order_only": False,
        "is_public": True,
        "is_active": True,
        "sort_order": 0,
        "created_at": NOW,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class FakeCouponRepository:
    def __init__(self, coupon):
        self.coupon = coupon
        self.total = 0
        self.customer_total = 0
        self.has_order = False
        self.last_applied_at = None
        self.redemptions = []
        self.lock_calls = 0

    def count_applied_total(self, coupon_id):
        return self.total

    def count_applied_redemptions_for_customer(self, coupon_id, customer_id):
        return self.customer_total

    def count_applied_by_customer(self, coupon_id, customer_id):
        return self.count_applied_redemptions_for_customer(coupon_id, customer_id)

    def get_last_applied_redemption_for_customer(self, coupon_id, customer_id):
        return self.last_applied_at

    def list_public_available(self, restaurant_id, now=None):
        return [self.coupon] if self.coupon.restaurant_id == restaurant_id else []

    def customer_has_valid_order(self, customer_id, restaurant_id):
        return self.has_order

    def get_by_id_and_restaurant(self, coupon_id, restaurant_id, for_update=False):
        if self.coupon.id == coupon_id and self.coupon.restaurant_id == restaurant_id:
            return self.coupon
        return None

    def get_by_code_and_restaurant(self, code, restaurant_id, for_update=False):
        if self.coupon.code.lower() == code.lower() and self.coupon.restaurant_id == restaurant_id:
            return self.coupon
        return None

    def lock_coupon(self, restaurant_id, coupon_id=None, coupon_code=None):
        self.lock_calls += 1
        if coupon_id is not None:
            return self.get_by_id_and_restaurant(coupon_id, restaurant_id)
        return self.get_by_code_and_restaurant(coupon_code, restaurant_id)

    def get_redemption_by_order_id(self, order_id):
        return next((item for item in self.redemptions if item.order_id == order_id), None)

    def create_redemption(self, **values):
        redemption = SimpleNamespace(**values, status="applied", reversed_at=None)
        self.redemptions.append(redemption)
        return redemption

    def reverse_redemption(self, redemption):
        if redemption.status == "applied":
            redemption.status = "reversed"
            redemption.reversed_at = NOW
        return redemption


class CouponServiceTests(unittest.TestCase):
    def setUp(self):
        self.coupon = make_coupon()
        self.customer = SimpleNamespace(id=uuid.uuid4())
        self.service = CouponService(SimpleNamespace())
        self.repository = FakeCouponRepository(self.coupon)
        self.service.repository = self.repository
        self.service.clock = lambda: NOW

    def evaluate(self, **overrides):
        return self.service.evaluate(
            self.coupon,
            restaurant_id=overrides.pop("restaurant_id", self.coupon.restaurant_id),
            subtotal=overrides.pop("subtotal", Decimal("100.00")),
            delivery_fee=overrides.pop("delivery_fee", Decimal("8.00")),
            customer=overrides.pop("customer", self.customer),
            require_public=overrides.pop("require_public", True),
            now=overrides.pop("now", NOW),
            **overrides,
        )

    def test_fixed_discount_is_capped_by_subtotal(self):
        self.coupon.discount_value = Decimal("120.00")
        self.assertEqual(self.evaluate().discount, Decimal("100.00"))

    def test_percent_discount(self):
        self.coupon.discount_type = "percent"
        self.coupon.discount_value = Decimal("12.50")
        self.assertEqual(self.evaluate().discount, Decimal("12.50"))

    def test_percent_discount_with_cap(self):
        self.coupon.discount_type = "percent"
        self.coupon.discount_value = Decimal("50.00")
        self.coupon.max_discount_amount = Decimal("20.00")
        self.assertEqual(self.evaluate().discount, Decimal("20.00"))

    def test_free_delivery(self):
        self.coupon.discount_type = "free_delivery"
        self.assertEqual(self.evaluate(delivery_fee=Decimal("13.789")).discount, Decimal("13.79"))

    def test_minimum_order_uses_product_subtotal(self):
        self.coupon.min_order_value = Decimal("50.00")
        result = self.evaluate(subtotal=Decimal("40.00"), delivery_fee=Decimal("20.00"))
        self.assertFalse(result.valid)
        self.assertEqual(result.missing_amount, Decimal("10.00"))

    def test_inactive_not_started_and_expired(self):
        self.coupon.is_active = False
        self.assertEqual(self.evaluate().reason, "inactive")
        self.coupon.is_active = True
        self.coupon.valid_from = NOW + timedelta(seconds=1)
        self.assertEqual(self.evaluate().reason, "not_started")
        self.coupon.valid_from = NOW - timedelta(days=2)
        self.coupon.valid_until = NOW - timedelta(seconds=1)
        self.assertEqual(self.evaluate().reason, "expired")

    def test_total_and_customer_limits_count_only_applied(self):
        self.coupon.total_usage_limit = 1
        self.repository.total = 1
        self.assertEqual(self.evaluate().reason, "total_limit_reached")
        self.repository.total = 0
        self.coupon.usage_limit_per_customer = 1
        self.repository.customer_total = 1
        self.assertEqual(self.evaluate().reason, "customer_limit_reached")

    def test_first_order_only(self):
        self.coupon.first_order_only = True
        self.repository.has_order = True
        self.assertEqual(self.evaluate().reason, "first_order_only")

    def test_anonymous_customer_is_marked_as_requiring_login(self):
        self.coupon.usage_limit_per_customer = 1
        result = self.evaluate(customer=None)
        self.assertFalse(result.valid)
        self.assertTrue(result.requires_login)
        self.assertEqual(result.reason, "login_required")

    def test_code_lookup_is_case_insensitive(self):
        found = self.service._find_coupon(
            self.coupon.restaurant_id,
            coupon_id=None,
            coupon_code="promo10",
            for_update=False,
        )
        self.assertIs(found, self.coupon)

    def test_only_one_coupon_selector_is_accepted(self):
        with self.assertRaises(ValidationError):
            CouponPreviewRequest.model_validate({
                "coupon_id": str(self.coupon.id),
                "coupon_code": self.coupon.code,
                "subtotal": "10.00",
                "delivery_fee": "0.00",
                "order_type": "pickup",
            })

    def test_coupon_from_another_restaurant_is_rejected(self):
        self.assertEqual(self.evaluate(restaurant_id=uuid.uuid4()).reason, "coupon_from_another_restaurant")
        with self.assertRaises(HTTPException):
            self.service._find_coupon(
                uuid.uuid4(), coupon_id=self.coupon.id, coupon_code=None, for_update=False
            )

    def test_preview_never_creates_redemption(self):
        restaurant = SimpleNamespace(id=self.coupon.restaurant_id)
        self.service.restaurant_service = SimpleNamespace(get_active_restaurant=lambda slug: restaurant)
        payload = CouponPreviewRequest(
            coupon_code="promo10", subtotal=Decimal("100"), delivery_fee=Decimal("8"), order_type="delivery"
        )
        response = self.service.preview("restaurant", payload, self.customer)
        self.assertTrue(response.valid)
        self.assertEqual(response.discount_amount, Decimal("10.00"))
        self.assertEqual(self.repository.redemptions, [])

    def test_redemption_is_idempotent_and_reversal_allows_reuse(self):
        order_id = uuid.uuid4()
        first = self.service.create_redemption(self.coupon, self.customer, order_id, Decimal("10"))
        second = self.service.create_redemption(self.coupon, self.customer, order_id, Decimal("10"))
        self.assertIs(first, second)
        self.assertEqual(len(self.repository.redemptions), 1)
        self.service.reverse_for_order(order_id)
        self.service.reverse_for_order(order_id)
        self.assertEqual(first.status, "reversed")
        self.repository.customer_total = 0
        self.coupon.usage_limit_per_customer = 1
        self.assertTrue(self.evaluate().valid)

    def test_last_coupon_attempts_are_serialized_and_rechecked(self):
        self.coupon.total_usage_limit = 1
        transaction_lock = threading.Lock()
        start = threading.Barrier(2)

        def attempt():
            start.wait()
            # Models the PostgreSQL row lock held until the transaction finishes.
            with transaction_lock:
                try:
                    self.service.lock_and_validate_for_order(
                        restaurant_id=self.coupon.restaurant_id,
                        coupon_id=self.coupon.id,
                        coupon_code=None,
                        subtotal=Decimal("100"),
                        delivery_fee=Decimal("0"),
                        customer=self.customer,
                    )
                except HTTPException as exc:
                    return exc.detail
                self.repository.total += 1
                return "applied"

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: attempt(), range(2)))

        self.assertCountEqual(results, ["applied", "total_limit_reached"])
        self.assertEqual(self.repository.lock_calls, 2)

    def test_order_payload_ignores_untrusted_totals_and_discount(self):
        payload = CreateOrderRequest.model_validate({
            "branch_id": str(uuid.uuid4()),
            "customer": {"name": "Ana", "phone": "8599999999"},
            "order_type": "pickup",
            "items": [{"product_id": str(uuid.uuid4()), "quantity": 1}],
            "subtotal": "0.01",
            "coupon_discount_amount": "9999.99",
            "discount_total": "9999.99",
            "total": "0.01",
        })
        dumped = payload.model_dump()
        for field in ("subtotal", "coupon_discount_amount", "discount_total", "total"):
            self.assertNotIn(field, dumped)

    def test_customer_without_previous_usage_can_use_cooldown_coupon(self):
        self.coupon.cooldown_days = 30
        self.repository.last_applied_at = None
        self.assertTrue(self.evaluate().valid)

    def test_cooldown_30_days_blocks_before_deadline(self):
        self.coupon.cooldown_days = 30
        self.repository.last_applied_at = datetime(2026, 6, 22, 15, tzinfo=timezone.utc)
        result = self.evaluate(now=datetime(2026, 7, 22, 14, 59, 59, tzinfo=timezone.utc))
        self.assertFalse(result.valid)
        self.assertEqual(result.reason, "cooldown_active")
        self.assertEqual(
            result.next_available_at,
            datetime(2026, 7, 22, 15, tzinfo=timezone.utc),
        )

    def test_cooldown_allows_usage_exactly_at_deadline(self):
        self.coupon.cooldown_days = 30
        self.repository.last_applied_at = datetime(2026, 6, 22, 15, tzinfo=timezone.utc)
        result = self.evaluate(now=datetime(2026, 7, 22, 15, tzinfo=timezone.utc))
        self.assertTrue(result.valid)
        self.assertIsNone(result.next_available_at)

    def test_unlimited_lifetime_usage_still_respects_cooldown(self):
        self.coupon.usage_limit_per_customer = None
        self.coupon.cooldown_days = 7
        self.repository.customer_total = 25
        self.repository.last_applied_at = NOW - timedelta(days=8)
        self.assertTrue(self.evaluate().valid)
        self.repository.last_applied_at = NOW - timedelta(days=6)
        self.assertEqual(self.evaluate().reason, "cooldown_active")

    def test_third_usage_allowed_and_fourth_blocked(self):
        self.coupon.usage_limit_per_customer = 3
        self.coupon.cooldown_days = 1
        self.repository.last_applied_at = NOW - timedelta(days=2)
        self.repository.customer_total = 2
        self.assertTrue(self.evaluate().valid)
        self.repository.customer_total = 3
        self.assertEqual(self.evaluate().reason, "customer_limit_reached")

    def test_reversed_redemption_is_ignored_for_count_and_cooldown(self):
        self.coupon.usage_limit_per_customer = 1
        self.coupon.cooldown_days = 30
        self.repository.customer_total = 0
        self.repository.last_applied_at = None
        self.assertTrue(self.evaluate().valid)

    def test_cancellation_uses_previous_still_applied_redemption(self):
        self.coupon.cooldown_days = 30
        # The most recent redemption was reversed; repository returns the prior applied use.
        self.repository.last_applied_at = NOW - timedelta(days=40)
        self.assertTrue(self.evaluate().valid)
        self.repository.last_applied_at = None
        self.assertTrue(self.evaluate().valid)

    def test_coupons_can_have_independent_cooldowns(self):
        self.repository.last_applied_at = NOW - timedelta(days=10)
        self.coupon.cooldown_days = 7
        self.assertTrue(self.evaluate().valid)
        another = make_coupon(restaurant_id=self.coupon.restaurant_id, cooldown_days=15)
        result = self.service.evaluate(
            another,
            restaurant_id=another.restaurant_id,
            subtotal=Decimal("100"),
            delivery_fee=Decimal("0"),
            customer=self.customer,
            require_public=True,
            now=NOW,
        )
        self.assertEqual(result.reason, "cooldown_active")

    def test_preview_exposes_cooldown_without_consuming_coupon(self):
        self.coupon.cooldown_days = 30
        self.repository.last_applied_at = NOW - timedelta(days=1)
        restaurant = SimpleNamespace(id=self.coupon.restaurant_id)
        self.service.restaurant_service = SimpleNamespace(get_active_restaurant=lambda slug: restaurant)
        response = self.service.preview(
            "restaurant",
            CouponPreviewRequest(
                coupon_id=self.coupon.id,
                subtotal=Decimal("100"),
                delivery_fee=Decimal("0"),
                order_type="pickup",
            ),
            self.customer,
        )
        self.assertFalse(response.valid)
        self.assertEqual(response.ineligibility_reason, "cooldown_active")
        self.assertEqual(response.next_available_at, NOW + timedelta(days=29))
        self.assertEqual(self.repository.redemptions, [])

    def test_available_keeps_cooldown_coupon_as_ineligible_for_compatibility(self):
        self.coupon.cooldown_days = 30
        self.repository.last_applied_at = NOW - timedelta(days=2)
        restaurant = SimpleNamespace(id=self.coupon.restaurant_id)
        self.service.restaurant_service = SimpleNamespace(get_active_restaurant=lambda slug: restaurant)
        response = self.service.get_available(
            "restaurant",
            subtotal=Decimal("100"),
            delivery_fee=Decimal("0"),
            order_type="pickup",
            customer=self.customer,
        )
        self.assertEqual(len(response.coupons), 1)
        item = response.coupons[0]
        self.assertFalse(item.eligible)
        self.assertEqual(item.ineligibility_reason, "cooldown_active")
        self.assertEqual(item.next_available_at, NOW + timedelta(days=28))

    def test_order_final_validation_rechecks_cooldown(self):
        self.coupon.cooldown_days = 30
        self.repository.last_applied_at = NOW - timedelta(minutes=1)
        with self.assertRaises(HTTPException) as raised:
            self.service.lock_and_validate_for_order(
                restaurant_id=self.coupon.restaurant_id,
                coupon_id=self.coupon.id,
                coupon_code=None,
                subtotal=Decimal("100"),
                delivery_fee=Decimal("0"),
                customer=self.customer,
            )
        self.assertEqual(raised.exception.detail, "cooldown_active")
        self.assertEqual(self.repository.redemptions, [])

    def test_concurrent_cooldown_attempts_allow_only_one(self):
        self.coupon.cooldown_days = 30
        transaction_lock = threading.Lock()
        start = threading.Barrier(2)

        def attempt():
            start.wait()
            with transaction_lock:
                try:
                    self.service.lock_and_validate_for_order(
                        restaurant_id=self.coupon.restaurant_id,
                        coupon_id=self.coupon.id,
                        coupon_code=None,
                        subtotal=Decimal("100"),
                        delivery_fee=Decimal("0"),
                        customer=self.customer,
                    )
                except HTTPException as exc:
                    return exc.detail
                self.repository.last_applied_at = NOW
                return "applied"

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: attempt(), range(2)))
        self.assertCountEqual(results, ["applied", "cooldown_active"])


class FakeDb:
    def __init__(self):
        self.events = []

    def commit(self):
        self.events.append("commit")

    def rollback(self):
        self.events.append("rollback")

    def refresh(self, value):
        pass


class FakeOrderRepository:
    def __init__(self, db):
        self.db = db
        self.order = None

    def create_order(self, order):
        order.id = uuid.uuid4()
        order.order_number = 123
        self.order = order
        self.db.events.append("order")

    def create_order_items(self, items):
        for item in items:
            item.id = uuid.uuid4()
        self.db.events.append("items")

    def create_order_item_options(self, options):
        self.db.events.append("options")

    def create_status_history(self, history):
        self.db.events.append("history")


class FakeOrderCouponService:
    def __init__(self, coupon, db):
        self.coupon = coupon
        self.db = db
        self.redemption = None

    def lock_and_validate_for_order(self, **kwargs):
        self.db.events.append("coupon_lock")
        return self.coupon, Decimal("10.00")

    def create_redemption(self, coupon, customer, order_id, discount):
        self.redemption = SimpleNamespace(
            coupon_id=coupon.id,
            customer_id=customer.id,
            order_id=order_id,
            discount_amount=discount,
            status="applied",
        )
        self.db.events.append("redemption")
        return self.redemption


class OrderCouponIntegrationTests(unittest.TestCase):
    def test_order_recalculates_total_saves_snapshot_and_redemption(self):
        db = FakeDb()
        restaurant = SimpleNamespace(id=uuid.uuid4())
        branch = SimpleNamespace(
        id=uuid.uuid4(),
        # As tres chaves da operacao sao da FILIAL desde a revisao
        # 20260818_0025; os seis campos nulos sao "herda o padrao do
        # restaurante", que e como toda filial nasce.
        is_open=True,
        accepts_delivery=True,
        accepts_pickup=True,
        min_order_value=None,
        service_fee_enabled=None,
        service_fee_amount=None,
        estimated_delivery_time_min=None,
        estimated_delivery_time_max=None,
        default_delivery_fee=None,
    )
        product_id = uuid.uuid4()
        product = SimpleNamespace(
            id=product_id,
            code="P1",
        catalog_key=None,
            name="Produto",
            description=None,
            price=Decimal("25.00"),
            option_groups=[],
        )
        coupon = make_coupon(restaurant_id=restaurant.id, code="SAVE10")
        customer = SimpleNamespace(id=uuid.uuid4(), name="Ana", phone="8599999999")
        payload = CreateOrderRequest.model_validate({
            "branch_id": str(branch.id),
            "order_type": "pickup",
            "payment_method": "cash",
            "coupon_code": "save10",
            "items": [{"product_id": str(product_id), "quantity": 4}],
            "subtotal": "1.00",
            "total": "1.00",
            "coupon_discount_amount": "99.00",
        })

        service = OrderService(db)
        service.restaurant_service = SimpleNamespace(get_active_restaurant=lambda slug: restaurant)
        service.branch_repository = SimpleNamespace(
            get_active_by_id_and_restaurant=lambda branch_id, restaurant_id: branch,
            list_enabled_payment_methods=lambda branch_id: [
                SimpleNamespace(method_type="cash", payment_flow="delivery"),
            ],
        )
        service.branch_hours_service = SimpleNamespace(
            ensure_branch_is_open=lambda branch_id: None
        )
        service.menu_repository = SimpleNamespace(
            get_settings=lambda restaurant_id: SimpleNamespace(
                min_order_value=Decimal("0"),
                service_fee_enabled=True,
                service_fee_amount=Decimal("3.00"),
                estimated_delivery_time_min=None,
                estimated_delivery_time_max=None,
                default_delivery_fee=None,
                platform_commission_percent=Decimal("10.00"),
            )
        )
        service.product_repository = SimpleNamespace(list_active_by_ids=lambda restaurant_id, ids: [product])
        service.order_repository = FakeOrderRepository(db)
        service.coupon_service = FakeOrderCouponService(coupon, db)

        response = service.create_order("restaurant", payload, customer)
        order = service.order_repository.order

        self.assertEqual(order.subtotal, Decimal("100.00"))
        self.assertEqual(order.coupon_code_snapshot, "SAVE10")
        self.assertEqual(order.coupon_discount_amount, Decimal("10.00"))
        self.assertEqual(order.discount_total, Decimal("10.00"))
        self.assertEqual(order.total, Decimal("93.00"))
        self.assertEqual(response.total, 93.0)
        self.assertEqual(service.coupon_service.redemption.order_id, order.id)
        self.assertLess(db.events.index("coupon_lock"), db.events.index("order"))
        self.assertLess(db.events.index("redemption"), db.events.index("commit"))

    def test_cancelled_order_reverses_redemption_in_same_transaction(self):
        db = FakeDb()
        restaurant_id = uuid.uuid4()
        order = SimpleNamespace(
            id=uuid.uuid4(),
            status="pending",
            order_type="delivery",
            payment_status="on_delivery",
        )

        class Repo:
            # get_order_detail passou a exigir restaurant_id na Fase 1 para
            # fechar o vazamento cross-tenant; ver AdminOrderService.
            def get_order_detail(self, order_id, restaurant):
                return order

            def update_status(self, current, new_status):
                current.status = new_status

            def create_status_history(self, history):
                pass

        reversed_orders = []
        service = AdminOrderService(db)
        service.order_repository = Repo()
        service.coupon_service = SimpleNamespace(reverse_for_order=lambda order_id: reversed_orders.append(order_id))

        from unittest.mock import patch
        with patch.object(OrderService, "to_order_detail_response", return_value="detail"):
            result = service.update_order_status(
                order.id,
                AdminScope(admin_user=None, restaurant_id=restaurant_id, branch_id=None),
                SimpleNamespace(status="cancelled", note=None),
                admin_user=SimpleNamespace(id=uuid.uuid4(), email="lojista@exemplo.com"),
            )

        self.assertEqual(result, "detail")
        self.assertEqual(reversed_orders, [order.id])
        self.assertEqual(db.events, ["commit"])


if __name__ == "__main__":
    unittest.main()
