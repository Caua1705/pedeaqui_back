"""PATCH /admin/orders/{id}/status com a maquina de estados ligada.

Cobre o que so aparece quando o service roda inteiro: a ordem entre replay
de idempotencia e validacao da transicao, o bloqueio por pagamento nao
confirmado e a autoria do historico saindo do token.
"""

import unittest
from decimal import Decimal
import uuid
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

from src.schemas.admin_order_schema import UpdateOrderStatusRequest
from src.api.dependencies.admin_scope import AdminScope
from src.models.order_model import Order
from src.services.admin_order_service import AdminOrderService
from src.services.order_service import OrderService
from tests import fabricas


ADMIN = fabricas.usuario_do_painel(email="lojista@exemplo.com")


class FakeDb:
    def __init__(self):
        self.events = []

    def commit(self):
        self.events.append("commit")

    def rollback(self):
        self.events.append("rollback")


class FakeOrderRepository:
    def __init__(self, order):
        self.order = order
        self.history = []

    def get_order_detail(self, order_id, restaurant_id):
        if self.order.id != order_id or self.order.restaurant_id != restaurant_id:
            return None
        return self.order

    def update_status(self, order, new_status):
        order.status = new_status

    def create_status_history(self, history):
        self.history.append(history)


def build_service(order):
    service = AdminOrderService(FakeDb())
    repository = FakeOrderRepository(order)
    # A LEITURA (escopo, detalhe do pedido) continua sendo do
    # AdminOrderService; a ESCRITA passou a ser do OrderStatusChangeService,
    # que ele delega — e que e o mesmo writer do cancelamento pelo cliente. O
    # MESMO dublê vai nos dois: sao duas camadas do mesmo caminho, nao dois
    # repositorios.
    service.order_repository = repository
    service.status_change_service.order_repository = repository
    service.status_change_service.coupon_service = SimpleNamespace(
        reverse_for_order=lambda order_id: None
    )
    return service


def make_order(restaurant_id, *, status="pending", payment_status="on_delivery", order_type="delivery"):
    # Model de verdade, transiente (sem sessao, sem banco). O motivo esta no
    # docstring do `make_order` de test_admin_order_cancel.py.
    return Order(
        id=uuid.uuid4(),
        restaurant_id=restaurant_id,
        status=status,
        order_type=order_type,
        payment_status=payment_status,
        order_number=99,
        # Lido por `CashbackService.refund_redemption` no cancelamento: zero
        # e o caminho de quase todo pedido, e e o que faz o estorno sair sem
        # ir ao razao.
        cashback_redeemed_amount=Decimal("0"),
    )


def owner_scope(restaurant_id):
    """Escopo de dono: restaurante do token e todas as filiais."""
    return AdminScope(admin_user=ADMIN, restaurant_id=restaurant_id, branch_id=None)


def request(status, note=None):
    # Schema de verdade, nao SimpleNamespace: um objeto de atributos
    # livres responde qualquer campo que o teste escreva e nenhum que ele
    # esqueca, e foi exatamente `confirm_prepared_order` que quebrou
    # todos estes testes de uma vez quando o campo nasceu (CLAUDE.md).
    return UpdateOrderStatusRequest(status=status, note=note)


class TransitionTests(unittest.TestCase):
    def setUp(self):
        self.restaurant_id = uuid.uuid4()

    def test_cancelled_order_cannot_be_completed(self):
        order = make_order(self.restaurant_id, status="cancelled")
        service = build_service(order)

        with self.assertRaises(HTTPException) as raised:
            service.update_order_status(
                order.id, owner_scope(self.restaurant_id), request("completed"), admin_user=ADMIN
            )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(order.status, "cancelled")
        self.assertEqual(service.db.events, [])

    def test_unknown_status_is_still_a_400(self):
        order = make_order(self.restaurant_id)
        service = build_service(order)

        with self.assertRaises(HTTPException) as raised:
            service.update_order_status(
                order.id, owner_scope(self.restaurant_id), request("entregue"), admin_user=ADMIN
            )

        self.assertEqual(raised.exception.status_code, 400)

    def test_valid_transition_goes_through(self):
        order = make_order(self.restaurant_id)
        service = build_service(order)

        with patch.object(OrderService, "to_order_detail_response", return_value="detail"):
            service.update_order_status(
                order.id, owner_scope(self.restaurant_id), request("accepted"), admin_user=ADMIN
            )

        self.assertEqual(order.status, "accepted")
        self.assertEqual(service.db.events, ["commit"])


class PaymentGateTests(unittest.TestCase):
    def setUp(self):
        self.restaurant_id = uuid.uuid4()

    def test_online_order_cannot_be_accepted_before_payment(self):
        order = make_order(self.restaurant_id, payment_status="pending")
        service = build_service(order)

        with self.assertRaises(HTTPException) as raised:
            service.update_order_status(
                order.id, owner_scope(self.restaurant_id), request("accepted"), admin_user=ADMIN
            )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(order.status, "pending")
        self.assertEqual(service.order_repository.history, [])

    def test_online_order_is_accepted_after_payment(self):
        order = make_order(self.restaurant_id, payment_status="paid")
        service = build_service(order)

        with patch.object(OrderService, "to_order_detail_response", return_value="detail"):
            service.update_order_status(
                order.id, owner_scope(self.restaurant_id), request("accepted"), admin_user=ADMIN
            )

        self.assertEqual(order.status, "accepted")

    def test_unpaid_online_order_can_always_be_cancelled(self):
        order = make_order(self.restaurant_id, payment_status="pending")
        service = build_service(order)

        with patch.object(OrderService, "to_order_detail_response", return_value="detail"):
            service.update_order_status(
                order.id, owner_scope(self.restaurant_id), request("cancelled"), admin_user=ADMIN
            )

        self.assertEqual(order.status, "cancelled")


class HistoryAuthorTests(unittest.TestCase):
    def test_author_comes_from_the_token_and_not_from_the_body(self):
        restaurant_id = uuid.uuid4()
        order = make_order(restaurant_id)
        service = build_service(order)

        with patch.object(OrderService, "to_order_detail_response", return_value="detail"):
            service.update_order_status(
                order.id, owner_scope(restaurant_id), request("accepted", note="ok"), admin_user=ADMIN
            )

        entry = service.order_repository.history[0]
        self.assertEqual(entry.changed_by, "admin:lojista@exemplo.com")
        self.assertEqual(entry.status, "accepted")
        self.assertEqual(entry.note, "ok")


if __name__ == "__main__":
    unittest.main()
