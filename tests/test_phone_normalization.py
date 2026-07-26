"""Telefone do pedido: normalizado na escrita e na comparacao.

O bug que estes testes travam: o telefone do pedido guest era gravado cru em
`customer_phone_snapshot`, mas a consulta publica compara por igualdade exata.
Quem digitava "(85) 99999-9999" no checkout nao achava o proprio pedido
depois procurando por "85999999999" — e o escopo de idempotencia, que ja
normalizava o mesmo campo, discordava do que tinha sido gravado.

A regra agora e unica: tudo que escreve ou compara telefone passa por
`normalize_digits`.
"""

import unittest
import uuid
from decimal import Decimal
from types import SimpleNamespace

from fastapi import HTTPException
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


class GetCustomerOrderPhoneTests(unittest.TestCase):
    def test_lookup_normalizes_before_comparing(self):
        service, repository = build_lookup_service()

        for queried in (FORMATTED, DIGITS, "85 99999 9999"):
            with self.subTest(queried=queried):
                repository.received = None
                with self.assertRaises(HTTPException):
                    service.get_customer_order("junior", 123, queried)
                self.assertEqual(repository.received, DIGITS)

    def test_order_saved_formatted_is_found_when_queried_with_digits(self):
        """O round-trip que o cliente faz: pede com telefone formatado e
        consulta depois com o telefone limpo."""
        db = FakeDb()
        restaurant_id = uuid.uuid4()
        service, payload = build_order_service(db, restaurant_id, phone=FORMATTED)
        service.create_order("junior", payload, None)
        stored = service.order_repository.orders[0]

        lookup, repository = build_lookup_service(restaurant_id=restaurant_id)
        repository.orders = {stored.customer_phone_snapshot: stored}

        found = lookup.get_customer_order("junior", 123, DIGITS)

        self.assertEqual(found.customer_phone_snapshot, DIGITS)


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


class FakeLookupRepository:
    """Guarda o telefone que chegou, para provar que veio normalizado."""

    def __init__(self):
        self.received = None
        self.orders = {}

    def get_order_by_number_and_phone(self, restaurant_id, order_number, phone):
        self.received = phone
        return self.orders.get(phone)


def build_order_service(db, restaurant_id, phone=DIGITS):
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

    service = OrderService(db)
    service.restaurant_service = SimpleNamespace(
        get_active_restaurant=lambda slug: SimpleNamespace(id=restaurant_id)
    )
    service.branch_repository = SimpleNamespace(
        get_active_by_id_and_restaurant=lambda branch_id, restaurant: branch
    )
    service.menu_repository = SimpleNamespace(
        get_settings=lambda restaurant: SimpleNamespace(
            min_order_value=Decimal("0"),
            service_fee_enabled=False,
            service_fee_amount=Decimal("0"),
        )
    )
    service.product_repository = SimpleNamespace(
        list_active_by_ids=lambda restaurant, ids: [product]
    )
    service.order_repository = FakeOrderRepository(db)

    payload = CreateOrderRequest.model_validate({
        "branch_id": str(branch.id),
        "order_type": "pickup",
        "customer": {"name": "Ana", "phone": phone},
        "items": [{"product_id": str(product_id), "quantity": 1}],
    })
    return service, payload


def build_lookup_service(restaurant_id=None):
    service = OrderService(FakeDb())
    service.restaurant_service = SimpleNamespace(
        get_active_restaurant=lambda slug: SimpleNamespace(
            id=restaurant_id or uuid.uuid4()
        )
    )
    repository = FakeLookupRepository()
    service.order_repository = repository
    return service, repository


if __name__ == "__main__":
    unittest.main()
