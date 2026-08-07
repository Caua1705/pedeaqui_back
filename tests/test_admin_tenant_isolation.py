"""Isolamento entre restaurantes e entre filiais nas rotas /admin.

O que da para provar sem banco: que o restaurant_id do token chega ao
repositorio em toda leitura e escrita, que a filial do lojista restringe o
que ele ve, e que a resposta e 404 (nao 403) para nao confirmar a
existencia de recurso alheio.

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

from src.api.dependencies.admin_scope import AdminScope, build_admin_scope
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
    O mesmo vale para branch_id na listagem.
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

    def _matching(self, restaurant_id, branch_id):
        return [
            order for order in self.orders.values()
            if order.restaurant_id == restaurant_id
            and (branch_id is None or order.branch_id == branch_id)
        ]

    def list_orders_by_restaurant(
        self, restaurant_id, branch_id=None, status=None,
        start_at=None, end_at=None, search=None, limit=50, offset=0,
    ):
        self.calls.append(("list", restaurant_id, branch_id))
        return self._matching(restaurant_id, branch_id)

    def count_orders_by_restaurant(
        self, restaurant_id, branch_id=None, status=None,
        start_at=None, end_at=None, search=None,
    ):
        return len(self._matching(restaurant_id, branch_id))

    def count_orders_grouped_by_status(
        self, restaurant_id, branch_id=None, start_at=None, end_at=None, search=None,
    ):
        grouped = {}
        for order in self._matching(restaurant_id, branch_id):
            grouped[order.status] = grouped.get(order.status, 0) + 1
        return grouped

    def update_status(self, order, new_status):
        order.status = new_status

    def create_status_history(self, history):
        pass


# Lojista autenticado usado nas chamadas de escrita: `changed_by` deixou de
# vir do corpo e passou a sair do token (AdminOrderService._admin_signature).
ADMIN_USER = SimpleNamespace(id=uuid.uuid4(), email="lojista@exemplo.com")


def owner_scope(restaurant_id):
    return AdminScope(admin_user=ADMIN_USER, restaurant_id=restaurant_id, branch_id=None)


def branch_scope(restaurant_id, branch_id):
    return AdminScope(admin_user=ADMIN_USER, restaurant_id=restaurant_id, branch_id=branch_id)


def make_order(restaurant_id, branch_id=None):
    return SimpleNamespace(
        id=uuid.uuid4(),
        restaurant_id=restaurant_id,
        branch_id=branch_id or uuid.uuid4(),
        status="pending",
        payment_status="on_delivery",
        payment_method="cash",
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
            self.service.get_order_detail(self.order_of_b.id, owner_scope(self.restaurant_a))

        self.assertEqual(raised.exception.status_code, 404)

    def test_error_does_not_reveal_that_the_order_exists(self):
        with self.assertRaises(HTTPException) as unknown:
            self.service.get_order_detail(uuid.uuid4(), owner_scope(self.restaurant_a))
        with self.assertRaises(HTTPException) as foreign:
            self.service.get_order_detail(self.order_of_b.id, owner_scope(self.restaurant_a))

        # Pedido inexistente e pedido de outro restaurante tem que ser
        # indistinguiveis, senao a rota vira um oraculo de UUIDs validos.
        self.assertEqual(unknown.exception.status_code, foreign.exception.status_code)
        self.assertEqual(unknown.exception.detail, foreign.exception.detail)

    def test_owner_restaurant_reads_its_own_order(self):
        with patch.object(OrderService, "to_order_detail_response", return_value="detail"):
            result = self.service.get_order_detail(self.order_of_b.id, owner_scope(self.restaurant_b))

        self.assertEqual(result, "detail")

    def test_restaurant_id_is_always_forwarded_to_the_repository(self):
        with self.assertRaises(HTTPException):
            self.service.get_order_detail(self.order_of_b.id, owner_scope(self.restaurant_a))

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
                owner_scope(self.restaurant_a),
                SimpleNamespace(status="accepted", note=None),
                admin_user=ADMIN_USER,
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
                owner_scope(self.restaurant_b),
                SimpleNamespace(status="accepted", note=None),
                admin_user=ADMIN_USER,
            )

        self.assertEqual(self.order_of_b.status, "accepted")
        self.assertEqual(self.db.events, ["commit"])


class BranchIsolationTests(unittest.TestCase):
    """Escopo por filial: BLOCO A da Fase 3.

    Ate aqui `admin_users.branch_id` era gravado e ignorado (armadilha 9.10
    da arquitetura): quem fosse cadastrado como atendente de uma filial via
    os pedidos de todas.
    """

    def setUp(self):
        self.db = FakeDb()
        self.restaurant_id = uuid.uuid4()
        self.branch_a = uuid.uuid4()
        self.branch_b = uuid.uuid4()
        self.order_a = make_order(self.restaurant_id, self.branch_a)
        self.order_b = make_order(self.restaurant_id, self.branch_b)
        self.repository = TenantScopedOrderRepository({
            self.order_a.id: self.order_a,
            self.order_b.id: self.order_b,
        })

        self.service = AdminOrderService(self.db)
        self.service.order_repository = self.repository
        self.service.coupon_service = SimpleNamespace(reverse_for_order=lambda order_id: None)

    def test_attendant_only_lists_orders_of_its_own_branch(self):
        result = self.service.list_orders(branch_scope(self.restaurant_id, self.branch_a))

        self.assertEqual(result.total, 1)
        self.assertEqual([item.id for item in result.items], [self.order_a.id])

    def test_owner_lists_orders_of_every_branch(self):
        result = self.service.list_orders(owner_scope(self.restaurant_id))

        self.assertEqual(result.total, 2)

    def test_attendant_cannot_read_order_of_another_branch(self):
        with self.assertRaises(HTTPException) as raised:
            self.service.get_order_detail(
                self.order_b.id, branch_scope(self.restaurant_id, self.branch_a)
            )

        self.assertEqual(raised.exception.status_code, 404)

    def test_attendant_cannot_change_status_of_another_branch(self):
        with self.assertRaises(HTTPException) as raised:
            self.service.update_order_status(
                self.order_b.id,
                branch_scope(self.restaurant_id, self.branch_a),
                SimpleNamespace(status="accepted", note=None),
                admin_user=ADMIN_USER,
            )

        self.assertEqual(raised.exception.status_code, 404)
        self.assertEqual(self.order_b.status, "pending")
        self.assertEqual(self.db.events, [])

    def test_branch_filter_from_the_querystring_cannot_widen_the_scope(self):
        # O painel manda ?branch_id=... como filtro de tela. Um atendente
        # preso a filial A pedindo a filial B nao pode receber a lista de B.
        with self.assertRaises(HTTPException) as raised:
            self.service.list_orders(
                branch_scope(self.restaurant_id, self.branch_a), branch_id=self.branch_b
            )

        self.assertEqual(raised.exception.status_code, 404)

    def test_status_counts_respect_the_branch(self):
        counts = self.service.count_orders_by_status(
            branch_scope(self.restaurant_id, self.branch_a)
        )

        self.assertEqual(counts.total, 1)
        pending = next(item for item in counts.counts if item.status == "pending")
        self.assertEqual(pending.count, 1)


class ScopeRuleTests(unittest.TestCase):
    """A regra de qual filial cada papel enxerga, isolada da rota."""

    def test_owner_ignores_its_branch_and_sees_the_whole_restaurant(self):
        restaurant_id = uuid.uuid4()
        admin_user = SimpleNamespace(
            role="owner", restaurant_id=restaurant_id, branch_id=uuid.uuid4()
        )

        scope = build_admin_scope(admin_user)

        self.assertIsNone(scope.branch_id)
        self.assertTrue(scope.sees_all_branches)

    def test_manager_with_branch_is_restricted_to_it(self):
        branch_id = uuid.uuid4()
        admin_user = SimpleNamespace(
            role="manager", restaurant_id=uuid.uuid4(), branch_id=branch_id
        )

        scope = build_admin_scope(admin_user)

        self.assertEqual(scope.branch_id, branch_id)
        self.assertFalse(scope.sees_all_branches)

    def test_attendant_without_branch_sees_every_branch(self):
        admin_user = SimpleNamespace(
            role="attendant", restaurant_id=uuid.uuid4(), branch_id=None
        )

        scope = build_admin_scope(admin_user)

        self.assertIsNone(scope.branch_id)

    def test_resolve_branch_filter_keeps_the_requested_branch_when_allowed(self):
        branch_id = uuid.uuid4()
        scope = branch_scope(uuid.uuid4(), branch_id)

        self.assertEqual(scope.resolve_branch_filter(branch_id), branch_id)
        self.assertEqual(scope.resolve_branch_filter(None), branch_id)


if __name__ == "__main__":
    unittest.main()
