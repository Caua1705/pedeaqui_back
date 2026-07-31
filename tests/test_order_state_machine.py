"""Maquina de estados do pedido.

O bug que motivou o grafo: `update_order_status` so conferia se o status
existia na lista. Dava para ir de `cancelled` para `completed` — o pedido
era faturado depois de o cupom ja ter sido estornado, e o cliente levava o
desconto duas vezes.

Aqui tambem mora a regra central da Fase 2: pedido com pagamento online nao
entra na cozinha antes de o gateway confirmar.
"""

import unittest

from fastapi import HTTPException

from src.core.constants import ORDER_STATUSES, PAYMENT_STATUSES
from src.services.order_state_machine import (
    ORDER_STATUS_TRANSITIONS,
    PAYMENT_STATUS_TRANSITIONS,
    TERMINAL_ORDER_STATUSES,
    ensure_order_transition_allowed,
    ensure_payment_allows_order_status,
    ensure_payment_transition_allowed,
    payment_history_status,
)


class GraphShapeTests(unittest.TestCase):
    def test_every_status_of_the_platform_is_in_the_graph(self):
        self.assertEqual(set(ORDER_STATUS_TRANSITIONS), set(ORDER_STATUSES))

    def test_every_payment_status_of_the_platform_is_in_the_graph(self):
        self.assertEqual(set(PAYMENT_STATUS_TRANSITIONS), set(PAYMENT_STATUSES))

    def test_graph_has_no_destination_outside_the_platform_statuses(self):
        for origin, destinations in ORDER_STATUS_TRANSITIONS.items():
            for destination in destinations:
                with self.subTest(origin=origin, destination=destination):
                    self.assertIn(destination, ORDER_STATUSES)

    def test_completed_and_cancelled_are_terminal(self):
        self.assertIn("completed", TERMINAL_ORDER_STATUSES)
        self.assertIn("cancelled", TERMINAL_ORDER_STATUSES)


class OrderTransitionTests(unittest.TestCase):
    def test_happy_path_of_a_delivery_order(self):
        path = ["pending", "accepted", "preparing", "ready", "out_for_delivery", "completed"]
        for current, new in zip(path, path[1:]):
            with self.subTest(transition=f"{current}->{new}"):
                ensure_order_transition_allowed(current, new, "delivery")

    def test_pickup_order_goes_from_ready_straight_to_completed(self):
        ensure_order_transition_allowed("ready", "completed", "pickup")

    def test_pickup_order_never_goes_out_for_delivery(self):
        with self.assertRaises(HTTPException) as raised:
            ensure_order_transition_allowed("ready", "out_for_delivery", "pickup")
        self.assertEqual(raised.exception.status_code, 409)

    def test_cancelled_order_cannot_be_completed(self):
        # O caso concreto: pedido cancelado (cupom ja estornado) sendo
        # faturado depois.
        with self.assertRaises(HTTPException) as raised:
            ensure_order_transition_allowed("cancelled", "completed", "delivery")
        self.assertEqual(raised.exception.status_code, 409)

    def test_no_terminal_status_moves_anywhere(self):
        for terminal in TERMINAL_ORDER_STATUSES:
            for destination in ORDER_STATUSES:
                if destination == terminal:
                    continue
                with self.subTest(origin=terminal, destination=destination):
                    with self.assertRaises(HTTPException):
                        ensure_order_transition_allowed(terminal, destination, "delivery")

    def test_going_back_is_refused(self):
        for current, new in (("preparing", "accepted"), ("completed", "pending"), ("ready", "preparing")):
            with self.subTest(transition=f"{current}->{new}"):
                with self.assertRaises(HTTPException):
                    ensure_order_transition_allowed(current, new, "delivery")

    def test_repeating_the_current_status_is_refused(self):
        with self.assertRaises(HTTPException) as raised:
            ensure_order_transition_allowed("accepted", "accepted", "delivery")
        self.assertEqual(raised.exception.status_code, 409)

    def test_unknown_status_in_the_database_does_not_invent_a_transition(self):
        with self.assertRaises(HTTPException):
            ensure_order_transition_allowed("em_analise", "accepted", "delivery")


class PaymentGateTests(unittest.TestCase):
    def test_online_order_is_not_accepted_before_the_gateway_confirms(self):
        with self.assertRaises(HTTPException) as raised:
            ensure_payment_allows_order_status("accepted", "pending")
        self.assertEqual(raised.exception.status_code, 409)

    def test_paid_online_order_can_be_accepted(self):
        ensure_payment_allows_order_status("accepted", "paid")

    def test_pay_on_delivery_order_can_be_accepted_right_away(self):
        ensure_payment_allows_order_status("accepted", "on_delivery")

    def test_failed_payment_blocks_the_whole_kitchen_flow(self):
        for kitchen_status in ("accepted", "preparing", "ready", "out_for_delivery", "completed"):
            with self.subTest(status=kitchen_status):
                with self.assertRaises(HTTPException):
                    ensure_payment_allows_order_status(kitchen_status, "failed")

    def test_cancelling_and_rejecting_never_depend_on_payment(self):
        # E a saida para o pagamento que nunca chegou: precisa funcionar em
        # qualquer estado de pagamento.
        for payment_status in PAYMENT_STATUSES:
            for order_status in ("cancelled", "rejected"):
                with self.subTest(payment=payment_status, order=order_status):
                    ensure_payment_allows_order_status(order_status, payment_status)


class PaymentTransitionTests(unittest.TestCase):
    def test_pending_payment_can_be_confirmed_or_refused(self):
        ensure_payment_transition_allowed("pending", "paid")
        ensure_payment_transition_allowed("pending", "failed")

    def test_refused_payment_can_be_retried(self):
        ensure_payment_transition_allowed("failed", "pending")

    def test_paid_can_only_go_to_refunded(self):
        ensure_payment_transition_allowed("paid", "refunded")
        for destination in ("pending", "failed", "on_delivery"):
            with self.subTest(destination=destination):
                with self.assertRaises(HTTPException):
                    ensure_payment_transition_allowed("paid", destination)

    def test_pay_on_delivery_never_changes(self):
        for destination in ("pending", "paid", "failed", "refunded"):
            with self.subTest(destination=destination):
                with self.assertRaises(HTTPException):
                    ensure_payment_transition_allowed("on_delivery", destination)

    def test_refunded_is_terminal(self):
        for destination in ("paid", "pending", "failed"):
            with self.subTest(destination=destination):
                with self.assertRaises(HTTPException):
                    ensure_payment_transition_allowed("refunded", destination)

    def test_payment_events_are_prefixed_in_the_history(self):
        # Sem prefixo, "paid" no historico se confundiria com um status
        # operacional do pedido.
        self.assertEqual(payment_history_status("paid"), "payment:paid")


if __name__ == "__main__":
    unittest.main()
