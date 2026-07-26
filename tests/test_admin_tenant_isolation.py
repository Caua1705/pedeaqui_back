"""Isolamento entre restaurantes nas rotas /admin.

O que da para provar sem banco: que o restaurant_id do token chega ao
repositorio em toda leitura e escrita, que o service recusa quando o
restaurante da URL nao e o do token, e que a resposta e 404 (nao 403) para
nao confirmar a existencia de recurso alheio.

O que fica para os testes de integracao da Fase 4: que o WHERE
restaurant_id de fato filtra no Postgres. Aqui o repositorio e um fake — ele
prova que o parametro foi passado, nao que a query esta correta. Sao coisas
diferentes e so a segunda pega um erro de SQL.
"""

import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

from src.api.dependencies.admin_auth import ensure_restaurant_scope
from src.services.admin_order_service import AdminOrderService
from src.services.order_service import OrderService


class FakeDb:
    def __init__(self):
        self.events = []

    def commit(self):
        self.events.append("commit")

    def rollback(self):
        self.events.append("rollback")


class TenantScopedOrderRepository:
    """Repositorio que respeita o filtro por restaurante, como o SQL real.

    Guarda os pedidos por (order_id, restaurant_id) e so devolve quando os
    dois batem — e o comportamento do WHERE id = :id AND restaurant_id = :r.
    """

    def __init__(self, orders):
        self.orders = orders
        self.calls = []

    def get_order_detail(self, order_id, restaurant_id):
        self.calls.append((order_id, restaurant_id))
        order = self.orders.get(order_id)
        if order is None or order.restaurant_id != restaurant_id:
            return None
        return order

    def list_orders_by_restaurant(self, restaurant_id, status=None, limit=50, offset=0):
        self.calls.append(("list", restaurant_id))
        return [
            order for order in self.orders.values()
            if order.restaurant_id == restaurant_id
        ]

    def update_status(self, order, new_status):
        order.status = new_status

    def create_status_history(self, history):
        pass


def make_order(restaurant_id):
    return SimpleNamespace(
        id=uuid.uuid4(),
        restaurant_id=restaurant_id,
        status="pending",
        order_number=1,
        customer_name_snapshot="Cliente",
        customer_phone_snapshot="85999999999",
        order_type="delivery",
        total=10,
        created_at=None,
    )


class ReadIsolationTests(unittest.TestCase):
    def setUp(self):
        self.db = FakeDb()
        self.restaurant_a = uuid.uuid4()
        self.restaurant_b = uuid.uuid4()
        self.order_of_b = make_order(self.restaurant_b)
        self.repository = TenantScopedOrderRepository({self.order_of_b.id: self.order_of_b})

        self.service = AdminOrderService(self.db)
        self.service.order_repository = self.repository

    def test_admin_cannot_read_order_of_another_restaurant(self):
        with self.assertRaises(HTTPException) as raised:
            self.service.get_order_detail(self.order_of_b.id, self.restaurant_a)

        self.assertEqual(raised.exception.status_code, 404)

    def test_error_does_not_reveal_that_the_order_exists(self):
        with self.assertRaises(HTTPException) as unknown:
            self.service.get_order_detail(uuid.uuid4(), self.restaurant_a)
        with self.assertRaises(HTTPException) as foreign:
            self.service.get_order_detail(self.order_of_b.id, self.restaurant_a)

        # Pedido inexistente e pedido de outro restaurante tem que ser
        # indistinguiveis, senao a rota vira um oraculo de UUIDs validos.
        self.assertEqual(unknown.exception.status_code, foreign.exception.status_code)
        self.assertEqual(unknown.exception.detail, foreign.exception.detail)

    def test_owner_restaurant_reads_its_own_order(self):
        with patch.object(OrderService, "to_order_detail_response", return_value="detail"):
            result = self.service.get_order_detail(self.order_of_b.id, self.restaurant_b)

        self.assertEqual(result, "detail")

    def test_restaurant_id_is_always_forwarded_to_the_repository(self):
        with self.assertRaises(HTTPException):
            self.service.get_order_detail(self.order_of_b.id, self.restaurant_a)

        self.assertEqual(self.repository.calls, [(self.order_of_b.id, self.restaurant_a)])


class WriteIsolationTests(unittest.TestCase):
    def setUp(self):
        self.db = FakeDb()
        self.restaurant_a = uuid.uuid4()
        self.restaurant_b = uuid.uuid4()
        self.order_of_b = make_order(self.restaurant_b)
        self.repository = TenantScopedOrderRepository({self.order_of_b.id: self.order_of_b})

        self.service = AdminOrderService(self.db)
        self.service.order_repository = self.repository
        self.service.coupon_service = SimpleNamespace(reverse_for_order=lambda order_id: None)

    def test_admin_cannot_change_status_of_another_restaurants_order(self):
        with self.assertRaises(HTTPException) as raised:
            self.service.update_order_status(
                self.order_of_b.id,
                self.restaurant_a,
                SimpleNamespace(status="accepted", changed_by="invasor", note=None),
            )

        self.assertEqual(raised.exception.status_code, 404)
        # O que mais importa: o pedido alheio nao foi tocado e nada foi
        # commitado.
        self.assertEqual(self.order_of_b.status, "pending")
        self.assertEqual(self.db.events, [])

    def test_owner_restaurant_changes_its_own_order(self):
        with patch.object(OrderService, "to_order_detail_response", return_value="detail"):
            self.service.update_order_status(
                self.order_of_b.id,
                self.restaurant_b,
                SimpleNamespace(status="accepted", changed_by="lojista", note=None),
            )

        self.assertEqual(self.order_of_b.status, "accepted")
        self.assertEqual(self.db.events, ["commit"])


class ListIsolationTests(unittest.TestCase):
    def test_listing_with_a_slug_of_another_restaurant_is_refused(self):
        db = FakeDb()
        restaurant_a = uuid.uuid4()
        restaurant_b = uuid.uuid4()

        service = AdminOrderService(db)
        service.order_repository = TenantScopedOrderRepository({})
        # O slug da URL resolve para o restaurante B; o token e do A.
        service.restaurant_service = SimpleNamespace(
            get_active_restaurant=lambda slug: SimpleNamespace(id=restaurant_b)
        )

        with self.assertRaises(HTTPException) as raised:
            service.list_orders(restaurant_slug="restaurante-b", restaurant_id=restaurant_a)

        self.assertEqual(raised.exception.status_code, 404)

    def test_listing_its_own_slug_works(self):
        db = FakeDb()
        restaurant_a = uuid.uuid4()
        order = make_order(restaurant_a)

        service = AdminOrderService(db)
        service.order_repository = TenantScopedOrderRepository({order.id: order})
        service.restaurant_service = SimpleNamespace(
            get_active_restaurant=lambda slug: SimpleNamespace(id=restaurant_a)
        )

        result = service.list_orders(restaurant_slug="restaurante-a", restaurant_id=restaurant_a)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].id, order.id)


class CouponScopeTests(unittest.TestCase):
    def test_scope_check_refuses_another_restaurant(self):
        admin_user = SimpleNamespace(restaurant_id=uuid.uuid4())

        with self.assertRaises(HTTPException) as raised:
            ensure_restaurant_scope(admin_user, uuid.uuid4())

        self.assertEqual(raised.exception.status_code, 404)

    def test_scope_check_accepts_its_own_restaurant(self):
        restaurant_id = uuid.uuid4()
        admin_user = SimpleNamespace(restaurant_id=restaurant_id)

        self.assertIsNone(ensure_restaurant_scope(admin_user, restaurant_id))


if __name__ == "__main__":
    unittest.main()
