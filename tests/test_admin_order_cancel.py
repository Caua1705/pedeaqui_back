"""PATCH /admin/orders/{id}/cancel — cancelamento com motivo obrigatorio.

O que estes testes protegem:

1. O motivo e obrigatorio DE VERDADE: string vazia, espacos e um "x" nao
   passam. Um motivo simbolico responde a exigencia sem responder a
   pergunta que o suporte vai fazer daqui a uma semana.
2. O motivo chega em `order_status_history.note`, junto do lojista que
   cancelou (que sai do token, nunca do corpo).
3. A rota nao e um atalho para fora da maquina de estados: pedido em estado
   final continua respondendo 409, e cancelar continua estornando o cupom.
"""

import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from pydantic import ValidationError

from src.api.dependencies.admin_scope import AdminScope
from src.schemas.admin_order_schema import CancelOrderRequest
from src.services.admin_order_service import AdminOrderService
from src.services.order_service import OrderService


ADMIN = SimpleNamespace(id=uuid.uuid4(), email="lojista@exemplo.com")


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
    service.order_repository = FakeOrderRepository(order)
    service.reversed_coupons = []
    service.coupon_service = SimpleNamespace(
        reverse_for_order=service.reversed_coupons.append
    )
    return service


def make_order(restaurant_id, *, status="preparing", payment_status="on_delivery"):
    return SimpleNamespace(
        id=uuid.uuid4(),
        restaurant_id=restaurant_id,
        status=status,
        order_type="delivery",
        payment_status=payment_status,
        order_number=99,
    )


def owner_scope(restaurant_id):
    return AdminScope(admin_user=ADMIN, restaurant_id=restaurant_id, branch_id=None)


class ReasonContractTests(unittest.TestCase):
    def test_reason_is_required(self):
        with self.assertRaises(ValidationError):
            CancelOrderRequest()

    def test_blank_reason_is_refused(self):
        # `min_length` do Field mediria o texto cru e deixaria passar tres
        # espacos; a validacao corta antes de medir.
        for blank in ("", "   ", "\n\t "):
            with self.assertRaises(ValidationError):
                CancelOrderRequest(reason=blank)

    def test_symbolic_reason_is_refused(self):
        with self.assertRaises(ValidationError):
            CancelOrderRequest(reason="x")

    def test_reason_is_stored_without_the_surrounding_spaces(self):
        self.assertEqual(
            CancelOrderRequest(reason="  cliente desistiu  ").reason,
            "cliente desistiu",
        )

    def test_the_body_does_not_accept_a_status(self):
        # Aceitar status aqui faria desta rota o PATCH de status com outro
        # nome, e a obrigatoriedade do motivo viraria um `if` por status.
        self.assertNotIn("status", CancelOrderRequest.model_fields)


class CancellationTests(unittest.TestCase):
    def setUp(self):
        self.restaurant_id = uuid.uuid4()

    def test_the_reason_lands_in_the_history_with_the_author_from_the_token(self):
        order = make_order(self.restaurant_id)
        service = build_service(order)

        with patch.object(OrderService, "to_order_detail_response", return_value="detail"):
            service.cancel_order(
                order.id,
                owner_scope(self.restaurant_id),
                CancelOrderRequest(reason="acabou a costela"),
                admin_user=ADMIN,
            )

        entry = service.order_repository.history[0]
        self.assertEqual(order.status, "cancelled")
        self.assertEqual(entry.status, "cancelled")
        self.assertEqual(entry.note, "acabou a costela")
        self.assertEqual(entry.changed_by, "admin:lojista@exemplo.com")
        self.assertEqual(service.db.events, ["commit"])

    def test_cancelling_reverses_the_coupon(self):
        order = make_order(self.restaurant_id)
        service = build_service(order)

        with patch.object(OrderService, "to_order_detail_response", return_value="detail"):
            service.cancel_order(
                order.id,
                owner_scope(self.restaurant_id),
                CancelOrderRequest(reason="cliente desistiu"),
                admin_user=ADMIN,
            )

        # Mesmo estorno do PATCH de status: sem ele o cupom ficaria
        # consumido por um pedido que nao existe mais.
        self.assertEqual(service.reversed_coupons, [order.id])

    def test_a_finished_order_cannot_be_cancelled(self):
        order = make_order(self.restaurant_id, status="completed")
        service = build_service(order)

        with self.assertRaises(HTTPException) as raised:
            service.cancel_order(
                order.id,
                owner_scope(self.restaurant_id),
                CancelOrderRequest(reason="cliente reclamou"),
                admin_user=ADMIN,
            )

        # A rota nova nao e um atalho para fora da maquina de estados.
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(order.status, "completed")
        self.assertEqual(service.order_repository.history, [])

    def test_cancelling_twice_is_refused(self):
        order = make_order(self.restaurant_id, status="cancelled")
        service = build_service(order)

        with self.assertRaises(HTTPException) as raised:
            service.cancel_order(
                order.id,
                owner_scope(self.restaurant_id),
                CancelOrderRequest(reason="engano"),
                admin_user=ADMIN,
            )

        self.assertEqual(raised.exception.status_code, 409)

    def test_an_unpaid_online_order_can_be_cancelled(self):
        # Cancelar e justamente a saida para o pagamento que nunca chegou:
        # a trava de pagamento nao pode valer aqui.
        order = make_order(self.restaurant_id, status="pending", payment_status="pending")
        service = build_service(order)

        with patch.object(OrderService, "to_order_detail_response", return_value="detail"):
            service.cancel_order(
                order.id,
                owner_scope(self.restaurant_id),
                CancelOrderRequest(reason="pix nao foi pago"),
                admin_user=ADMIN,
            )

        self.assertEqual(order.status, "cancelled")

    def test_an_order_of_another_restaurant_is_not_found(self):
        order = make_order(uuid.uuid4())
        service = build_service(order)

        with self.assertRaises(HTTPException) as raised:
            service.cancel_order(
                order.id,
                owner_scope(self.restaurant_id),
                CancelOrderRequest(reason="engano"),
                admin_user=ADMIN,
            )

        self.assertEqual(raised.exception.status_code, 404)
        self.assertEqual(order.status, "preparing")


if __name__ == "__main__":
    unittest.main()
