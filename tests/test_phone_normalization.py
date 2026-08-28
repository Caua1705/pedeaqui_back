"""Telefone do pedido: normalizado na escrita e na comparacao.

O bug que estes testes travam: o telefone do pedido guest era gravado cru em
`customer_phone_snapshot`, mas a consulta publica compara por igualdade exata.
Quem digitava "(85) 99999-9999" no checkout nao achava o proprio pedido
depois procurando por "85999999999" — e o escopo de idempotencia, que ja
normalizava o mesmo campo, discordava do que tinha sido gravado.

A regra agora e unica: tudo que escreve ou compara telefone passa por
`normalize_digits`.

Nota da Fase 2: a consulta publica por (order_number, telefone) foi
REMOVIDA — era enumeravel, ver tests/test_order_tracking.py. O telefone
continua sendo normalizado na escrita do snapshot e no escopo da
idempotencia, que sao os pontos que sobraram.
"""

import unittest
import uuid
from decimal import Decimal
from types import SimpleNamespace

from pydantic import ValidationError

from src.schemas.order_schema import CreateOrderRequest, CustomerInput
from src.services.order_service import OrderService


FORMATTED = "(85) 99999-9999"
DIGITS = "85999999999"


class CustomerInputPhoneTests(unittest.TestCase):
    def test_formatted_phone_is_stored_as_digits(self):
        for raw in (FORMATTED, DIGITS, "85 9 9999-9999", " 85999999999 "):
            with self.subTest(raw=raw):
                self.assertEqual(CustomerInput(name="Ana", phone=raw).phone, DIGITS)

    def test_phone_without_enough_digits_is_rejected(self):
        # Passava no min_length=8 por causa da pontuacao e virava um snapshot
        # que nenhuma consulta encontraria.
        for raw in ("--------", "(85) 9", "() - - - -"):
            with self.subTest(raw=raw):
                with self.assertRaises(ValidationError):
                    CustomerInput(name="Ana", phone=raw)


class CreateOrderPhoneTests(unittest.TestCase):
    def test_snapshot_is_written_as_digits_for_guest(self):
        db = FakeDb()
        service, payload = build_order_service(db, uuid.uuid4(), phone=FORMATTED)

        service.create_order("junior", payload, None)

        self.assertEqual(service.order_repository.orders[0].customer_phone_snapshot, DIGITS)

    def test_snapshot_is_written_as_digits_for_legacy_account(self):
        # Conta antiga cujo `customers.phone` ficou gravado formatado: o
        # snapshot do pedido tem que sair em digitos mesmo assim.
        db = FakeDb()
        service, payload = build_order_service(db, uuid.uuid4())
        customer = SimpleNamespace(id=uuid.uuid4(), name="Ana", phone=FORMATTED)

        service.create_order("junior", payload, customer)

        self.assertEqual(service.order_repository.orders[0].customer_phone_snapshot, DIGITS)

    def test_idempotency_scope_matches_the_stored_snapshot(self):
        db = FakeDb()
        restaurant_id = uuid.uuid4()
        service, payload = build_order_service(db, restaurant_id, phone=FORMATTED)

        scope = service._idempotency_scope(restaurant_id, payload, None)

        self.assertIn(f"phone:{DIGITS}", scope)


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
        self.orders = []

    def create_order(self, order):
        order.id = uuid.uuid4()
        order.order_number = 123
        order.created_at = None
        order.updated_at = None
        order.items = []
        order.status_history = []
        self.orders.append(order)

    def create_order_items(self, items):
        for item in items:
            item.id = uuid.uuid4()

    def create_order_item_options(self, options):
        pass

    def create_status_history(self, history):
        pass


def build_order_service(db, restaurant_id, phone=DIGITS):
    branch = SimpleNamespace(
        id=uuid.uuid4(),
        # As tres chaves da operacao sao da FILIAL desde a revisao
        # 20260818_0025; os seis campos nulos sao "herda o padrao do
        # restaurante", que e como toda filial nasce.
        is_open=True,
        accepts_delivery=True,
        accepts_pickup=True,
        delivery_paused_until=None,
        delivery_pause_reason=None,
        min_order_value=None,
        service_fee_enabled=None,
        service_fee_amount=None,
        estimated_delivery_time_min=None,
        estimated_delivery_time_max=None,
        default_delivery_fee=None,
        free_delivery_enabled=None,
        free_delivery_min_order_value=None,
    )
    product_id = uuid.uuid4()
    product = SimpleNamespace(
        id=product_id,
        code="P1",
        catalog_key=None,
        name="Picanha",
        description=None,
        price=Decimal("50.00"),
        option_groups=[],
    )

    service = OrderService(db)
    service.restaurant_service = SimpleNamespace(
        get_active_restaurant=lambda slug: SimpleNamespace(id=restaurant_id)
    )
    service.branch_repository = SimpleNamespace(
        get_active_by_id_and_restaurant=lambda branch_id, restaurant: branch,
        # A filial aceita dinheiro na entrega: e o que o payload usa.
        list_enabled_payment_methods=lambda branch: [
            SimpleNamespace(method_type="cash", payment_flow="delivery"),
        ],
    )
    # Filial aberta agora: a validacao de horario e feita pelo
    # BranchHoursService, e o pedido nao chega ao banco sem ela.
    service.branch_hours_service = SimpleNamespace(
        ensure_branch_is_open=lambda branch: None
    )
    service.menu_repository = SimpleNamespace(
        get_settings=lambda restaurant: SimpleNamespace(
            min_order_value=Decimal("0"),
            service_fee_enabled=False,
            service_fee_amount=Decimal("0"),
            estimated_delivery_time_min=None,
            estimated_delivery_time_max=None,
            default_delivery_fee=None,
            free_delivery_enabled=None,
            free_delivery_min_order_value=None,
            platform_commission_percent=Decimal("10.00"),
        )
    )
    service.product_repository = SimpleNamespace(
        list_active_by_ids=lambda restaurant, ids: [product]
    )
    service.order_repository = FakeOrderRepository(db)
    # Sem cupom nenhum no corpo, `create_order` ainda pergunta ao
    # CouponService se ha campanha automatica (28/08/2026). O service de
    # verdade sobre o `FakeDb` deste teste estoura num `scalars` que ninguem
    # montou — e o erro nao teria nada a ver com telefone, que e o que este
    # arquivo testa.
    service.coupon_service = SimpleNamespace(auto_apply_for_order=lambda **kwargs: None)

    payload = CreateOrderRequest.model_validate({
        "branch_id": str(branch.id),
        "order_type": "pickup",
        "payment_method": "cash",
        "customer": {"name": "Ana", "phone": phone},
        "items": [{"product_id": str(product_id), "quantity": 1}],
    })
    return service, payload


if __name__ == "__main__":
    unittest.main()
