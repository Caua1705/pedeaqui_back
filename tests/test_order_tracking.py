"""Consulta de pedido: token opaco no lugar do numero previsivel.

O problema: `/restaurants/{slug}/orders/{order_number}?phone=...` casava um
`order_number` que vem de uma SEQUENCE GLOBAL com o telefone do cliente. De
posse de um telefone, dava para varrer os numeros vizinhos e ler endereco
residencial completo, itens e historico. O pedido de numero 5471 tinha o
5472 do lado.
"""

import unittest
import uuid
from decimal import Decimal
from types import SimpleNamespace

from fastapi import HTTPException

from src.schemas.order_schema import CreateOrderRequest
from src.services.order_service import OrderService
from src.utils.security import generate_tracking_token, hash_tracking_token
from tests import fabricas


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
        order.order_number = 5471
        self.orders.append(order)

    def create_order_items(self, items):
        for item in items:
            item.id = uuid.uuid4()

    def create_order_item_options(self, options):
        pass

    def create_status_history(self, history):
        pass


class LookupRepository:
    """Guarda o que a consulta recebeu, para provar por onde ela filtra."""

    def __init__(self, orders_by_token=None, orders_by_customer=None):
        self.orders_by_token = orders_by_token or {}
        self.orders_by_customer = orders_by_customer or {}
        self.calls = []

    def get_order_by_tracking_token(self, restaurant_id, tracking_token):
        self.calls.append((restaurant_id, tracking_token))
        return self.orders_by_token.get(tracking_token)

    def get_order_detail_for_customer(self, order_id, customer_id):
        self.calls.append((order_id, customer_id))
        return self.orders_by_customer.get((order_id, customer_id))


def build_create_service():
    branch = fabricas.filial(is_open=True, accepts_delivery=True, accepts_pickup=True)
    product_id = uuid.uuid4()
    product = fabricas.produto(id=product_id, code="P1")

    service = OrderService(FakeDb())
    service.restaurant_service = SimpleNamespace(
        get_active_restaurant=lambda slug: fabricas.restaurante()
    )
    service.branch_repository = SimpleNamespace(
        get_active_by_id_and_restaurant=lambda branch_id, restaurant_id: branch,
        list_enabled_payment_methods=lambda branch_id: [
            fabricas.forma_de_pagamento(method_type="cash", payment_flow="delivery"),
        ],
    )
    service.branch_hours_service = SimpleNamespace(ensure_branch_is_open=lambda branch_id: None)
    service.menu_repository = SimpleNamespace(
        get_settings=lambda restaurant_id: fabricas.configuracoes(
            service_fee_enabled=False,
            service_fee_amount=Decimal("0"),
            platform_commission_percent=Decimal("10.00"),
        )
    )
    service.product_repository = SimpleNamespace(
        list_active_by_ids=lambda restaurant_id, ids: [product]
    )
    service.order_repository = FakeOrderRepository()

    payload = CreateOrderRequest.model_validate({
        "branch_id": str(branch.id),
        "order_type": "pickup",
        "payment_method": "cash",
        "customer": {"name": "Ana", "phone": "85999999999"},
        "items": [{"product_id": str(product_id), "quantity": 1}],
    })
    return service, payload


def build_lookup_service(repository, restaurant_id=None):
    service = OrderService(FakeDb())
    service.restaurant_service = SimpleNamespace(
        get_active_restaurant=lambda slug: SimpleNamespace(id=restaurant_id or uuid.uuid4())
    )
    service.order_repository = repository
    return service


class TokenGenerationTests(unittest.TestCase):
    def test_order_is_born_with_a_token_and_returns_it_to_the_creator(self):
        service, payload = build_create_service()

        response = service.create_order("junior", payload)

        stored = service.order_repository.orders[0]
        self.assertTrue(response.tracking_token)
        self.assertEqual(hash_tracking_token(response.tracking_token), stored.tracking_token_hash)

    def test_the_plaintext_token_is_never_written_to_the_order(self):
        """O que a coluna em claro fazia e nao faz mais.

        O pedido gravado nao tem o token: quem le a linha (dump, backup,
        replica, log de query) nao consegue abrir a consulta de
        acompanhamento daquele cliente.
        """
        service, payload = build_create_service()

        response = service.create_order("junior", payload)

        stored = service.order_repository.orders[0]
        self.assertFalse(hasattr(stored, "tracking_token"))
        self.assertNotIn(response.tracking_token, stored.tracking_token_hash)

    def test_two_orders_never_share_a_token(self):
        first_service, first_payload = build_create_service()
        second_service, second_payload = build_create_service()

        first = first_service.create_order("junior", first_payload)
        second = second_service.create_order("junior", second_payload)

        self.assertNotEqual(first.tracking_token, second.tracking_token)

    def test_token_is_long_enough_to_not_be_guessable(self):
        # 32 bytes de `secrets` viram 43 caracteres URL-safe.
        self.assertGreaterEqual(len(generate_tracking_token()), 40)


class PublicLookupTests(unittest.TestCase):
    def test_order_is_found_by_its_token(self):
        restaurant_id = uuid.uuid4()
        order = SimpleNamespace(
            id=uuid.uuid4(),
            tracking_token="token-do-pedido",
            items=[],
            status_history=[],
        )
        repository = LookupRepository(orders_by_token={"token-do-pedido": order})
        service = build_lookup_service(repository, restaurant_id)

        with _detail_response_as_identity():
            found = service.get_order_by_tracking_token("junior", "token-do-pedido")

        self.assertIs(found, order)
        self.assertEqual(repository.calls, [(restaurant_id, "token-do-pedido")])

    def test_wrong_token_is_a_404(self):
        service = build_lookup_service(LookupRepository())

        with self.assertRaises(HTTPException) as raised:
            service.get_order_by_tracking_token("junior", "chute")

        self.assertEqual(raised.exception.status_code, 404)

    def test_order_number_is_not_a_valid_key_anymore(self):
        # A defesa concreta contra a enumeracao: numero de pedido nao abre
        # mais nada.
        order = SimpleNamespace(id=uuid.uuid4(), tracking_token="segredo", items=[], status_history=[])
        service = build_lookup_service(LookupRepository(orders_by_token={"segredo": order}))

        with self.assertRaises(HTTPException):
            service.get_order_by_tracking_token("junior", "5471")

    def test_token_of_another_restaurant_does_not_leak(self):
        # O filtro por restaurant_id continua na consulta: o token e o
        # segredo, mas o escopo do tenant nao depende dele.
        repository = LookupRepository(orders_by_token={})
        service = build_lookup_service(repository, uuid.uuid4())

        with self.assertRaises(HTTPException):
            service.get_order_by_tracking_token("outro", "token-valido-em-outro-lugar")


class AuthenticatedLookupTests(unittest.TestCase):
    def test_customer_reads_his_own_order_without_any_token(self):
        customer = SimpleNamespace(id=uuid.uuid4())
        order_id = uuid.uuid4()
        order = SimpleNamespace(id=order_id, items=[], status_history=[])
        repository = LookupRepository(orders_by_customer={(order_id, customer.id): order})
        service = build_lookup_service(repository)

        with _detail_response_as_identity():
            found = service.get_customer_order(customer, order_id)

        self.assertIs(found, order)

    def test_order_of_another_customer_is_a_404(self):
        customer = SimpleNamespace(id=uuid.uuid4())
        other_order_id = uuid.uuid4()
        repository = LookupRepository(
            orders_by_customer={(other_order_id, uuid.uuid4()): SimpleNamespace()}
        )
        service = build_lookup_service(repository)

        with self.assertRaises(HTTPException) as raised:
            service.get_customer_order(customer, other_order_id)

        self.assertEqual(raised.exception.status_code, 404)


def _detail_response_as_identity():
    """Troca a serializacao por identidade.

    Os testes aqui sao sobre COMO o pedido e encontrado; montar o
    OrderDetailResponse inteiro exigiria um fake com trinta campos e nao
    provaria nada a mais.
    """
    from unittest.mock import patch

    return patch.object(OrderService, "to_order_detail_response", side_effect=lambda order: order)


if __name__ == "__main__":
    unittest.main()
