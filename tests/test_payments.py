"""Pagamento: criacao da cobranca e webhook do gateway.

O Mercado Pago ainda nao esta plugado. O que estes testes exercitam e toda
a estrutura em volta dele — assinatura, idempotencia, maquina de estados e
o efeito no pedido — usando o provider "sandbox", que e uma implementacao
de verdade sem chamada externa.

Quando o Mercado Pago entrar, estes testes continuam valendo: o que muda e
so o conteudo das tres funcoes de src/integrations/payment_gateway.py.
"""

import json
import unittest
import uuid
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

from src.core.config import settings
from src.integrations.payment_gateway import (
    PaymentProviderNotConfiguredError,
    PaymentProviderUnknownError,
    PaymentWebhookPayloadError,
    create_payment,
    parse_webhook_event,
    sign_sandbox_payload,
    verify_webhook_signature,
)
from src.services.idempotency_service import IdempotencyService
from src.services.payment_service import PaymentService


WEBHOOK_SECRET = "segredo-de-teste"


class FakeDb:
    def __init__(self):
        self.events = []

    def commit(self):
        self.events.append("commit")

    def rollback(self):
        self.events.append("rollback")


class FakeIdempotencyRepository:
    def __init__(self):
        self.rows = {}
        self.completed = []

    def reserve(self, *, scope, key, request_fingerprint, expires_at):
        if (scope, key) in self.rows:
            return None
        record_id = uuid.uuid4()
        self.rows[(scope, key)] = SimpleNamespace(
            id=record_id,
            scope=scope,
            key=key,
            request_fingerprint=request_fingerprint,
            status="in_progress",
            response_body=None,
        )
        return record_id

    def get(self, *, scope, key):
        return self.rows.get((scope, key))

    def complete(self, *, record_id, response_body, order_id):
        self.completed.append((record_id, response_body, order_id))
        for row in self.rows.values():
            if row.id == record_id:
                row.status = "completed"
                row.response_body = response_body


class FakeOrderRepository:
    def __init__(self, order):
        self.order = order
        self.history = []

    def get_order_by_tracking_token(self, restaurant_id, tracking_token):
        if self.order.tracking_token != tracking_token:
            return None
        return self.order

    def get_order_by_provider_payment(self, provider, provider_payment_id):
        if (self.order.payment_provider, self.order.provider_payment_id) != (
            provider,
            provider_payment_id,
        ):
            return None
        return self.order

    def attach_payment_intent(self, order, *, provider, provider_payment_id):
        order.payment_provider = provider
        order.provider_payment_id = provider_payment_id
        order.payment_status = "pending"

    def update_payment_status(self, order, payment_status, paid_at=None):
        order.payment_status = payment_status
        if paid_at is not None:
            order.paid_at = paid_at

    def create_status_history(self, history):
        self.history.append(history)


def make_order(**overrides):
    values = {
        "id": uuid.uuid4(),
        "restaurant_id": uuid.uuid4(),
        "order_number": 1234,
        "tracking_token": "token-do-pedido",
        "total": Decimal("93.00"),
        "payment_method": "pix",
        "payment_flow": "online",
        "payment_status": "pending",
        "payment_provider": None,
        "provider_payment_id": None,
        "paid_at": None,
        "status": "pending",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def build_service(order, idempotency_repository=None):
    service = PaymentService(FakeDb())
    service.order_repository = FakeOrderRepository(order)
    service.restaurant_service = SimpleNamespace(
        get_active_restaurant=lambda slug: SimpleNamespace(id=order.restaurant_id)
    )
    service.idempotency_service.repository = idempotency_repository or FakeIdempotencyRepository()
    return service


def webhook_body(payment_id, status, event_id="evt-1"):
    return json.dumps(
        {"event_id": event_id, "payment_id": payment_id, "status": status},
        separators=(",", ":"),
    ).encode("utf-8")


def signed_headers(raw_body, secret=WEBHOOK_SECRET):
    return {"X-Webhook-Signature": sign_sandbox_payload(raw_body, secret)}


class GatewayContractTests(unittest.TestCase):
    """O contrato que a implementacao do Mercado Pago vai ter que cumprir."""

    def test_sandbox_payment_id_is_derived_from_the_order(self):
        order_id = uuid.uuid4()

        first = create_payment(
            provider="sandbox", order_id=order_id, amount=Decimal("10.00"),
            payment_method="pix", description="Pedido #1",
        )
        second = create_payment(
            provider="sandbox", order_id=order_id, amount=Decimal("10.00"),
            payment_method="pix", description="Pedido #1",
        )

        # Chamar duas vezes para o mesmo pedido nao pode gerar duas cobrancas.
        self.assertEqual(first.provider_payment_id, second.provider_payment_id)

    def test_mercadopago_is_a_documented_hole_and_not_a_silent_success(self):
        # O ponto de extensao precisa FALHAR alto enquanto nao existir. Se
        # devolvesse algo plausivel, um pedido ficaria eternamente pendente
        # sem ninguem perceber.
        with self.assertRaises(PaymentProviderNotConfiguredError):
            create_payment(
                provider="mercadopago", order_id=uuid.uuid4(), amount=Decimal("10.00"),
                payment_method="pix", description="Pedido #1",
            )

    def test_unknown_provider_is_rejected(self):
        with self.assertRaises(PaymentProviderUnknownError):
            create_payment(
                provider="picpay", order_id=uuid.uuid4(), amount=Decimal("10.00"),
                payment_method="pix", description="Pedido #1",
            )

    def test_signature_is_calculated_over_the_raw_body(self):
        raw_body = webhook_body("sandbox-1", "paid")

        self.assertTrue(
            verify_webhook_signature(
                provider="sandbox", raw_body=raw_body,
                headers=signed_headers(raw_body), secret=WEBHOOK_SECRET,
            )
        )

    def test_tampered_body_breaks_the_signature(self):
        raw_body = webhook_body("sandbox-1", "paid")
        headers = signed_headers(raw_body)
        tampered = webhook_body("sandbox-1", "refunded")

        self.assertFalse(
            verify_webhook_signature(
                provider="sandbox", raw_body=tampered, headers=headers, secret=WEBHOOK_SECRET,
            )
        )

    def test_signature_header_is_case_insensitive(self):
        raw_body = webhook_body("sandbox-1", "paid")
        headers = {"x-webhook-signature": sign_sandbox_payload(raw_body, WEBHOOK_SECRET)}

        self.assertTrue(
            verify_webhook_signature(
                provider="sandbox", raw_body=raw_body, headers=headers, secret=WEBHOOK_SECRET,
            )
        )

    def test_without_a_secret_nothing_is_accepted(self):
        raw_body = webhook_body("sandbox-1", "paid")

        with self.assertRaises(PaymentProviderNotConfiguredError):
            verify_webhook_signature(
                provider="sandbox", raw_body=raw_body,
                headers=signed_headers(raw_body), secret=None,
            )

    def test_event_is_translated_to_the_platform_vocabulary(self):
        event = parse_webhook_event(provider="sandbox", raw_body=webhook_body("sandbox-9", "paid"))

        self.assertEqual(event.provider_payment_id, "sandbox-9")
        self.assertEqual(event.payment_status, "paid")
        self.assertEqual(event.event_id, "evt-1")

    def test_status_outside_the_platform_list_is_refused(self):
        raw_body = json.dumps({"event_id": "e", "payment_id": "p", "status": "approved"}).encode()

        with self.assertRaises(PaymentWebhookPayloadError):
            parse_webhook_event(provider="sandbox", raw_body=raw_body)


class StartPaymentTests(unittest.TestCase):
    def test_online_order_gets_a_payment_id(self):
        order = make_order()
        service = build_service(order)

        with patch.object(settings, "PAYMENT_PROVIDER", "sandbox"):
            response = service.start_online_payment("junior", "token-do-pedido")

        self.assertEqual(response.provider, "sandbox")
        self.assertEqual(order.provider_payment_id, f"sandbox-{order.id}")
        self.assertEqual(order.payment_status, "pending")

    def test_gateway_is_called_outside_the_open_transaction(self):
        # A leitura e fechada antes do I/O externo: segurar a conexao
        # durante a chamada ao gateway trava o pool inteiro quando ele
        # demora.
        order = make_order()
        service = build_service(order)

        with patch.object(settings, "PAYMENT_PROVIDER", "sandbox"):
            service.start_online_payment("junior", "token-do-pedido")

        self.assertEqual(service.db.events, ["commit", "commit"])

    def test_pay_on_delivery_order_has_nothing_to_charge(self):
        order = make_order(payment_flow="delivery", payment_status="on_delivery")
        service = build_service(order)

        with self.assertRaises(HTTPException) as raised:
            service.start_online_payment("junior", "token-do-pedido")

        self.assertEqual(raised.exception.status_code, 409)

    def test_already_paid_order_is_not_charged_again(self):
        order = make_order(payment_status="paid")
        service = build_service(order)

        with self.assertRaises(HTTPException) as raised:
            service.start_online_payment("junior", "token-do-pedido")

        self.assertEqual(raised.exception.status_code, 409)

    def test_wrong_token_does_not_find_the_order(self):
        service = build_service(make_order())

        with self.assertRaises(HTTPException) as raised:
            service.start_online_payment("junior", "chute")

        self.assertEqual(raised.exception.status_code, 404)

    def test_gateway_without_credentials_answers_503(self):
        order = make_order()
        service = build_service(order)

        with patch.object(settings, "PAYMENT_PROVIDER", "mercadopago"):
            with self.assertRaises(HTTPException) as raised:
                service.start_online_payment("junior", "token-do-pedido")

        self.assertEqual(raised.exception.status_code, 503)


class WebhookTests(unittest.TestCase):
    def setUp(self):
        self.order = make_order(payment_provider="sandbox", provider_payment_id="sandbox-1")
        self.patcher = patch.object(settings, "PAYMENT_WEBHOOK_SECRET", WEBHOOK_SECRET)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def _handle(self, service, raw_body, headers=None):
        # `is None` e nao `or`: headers={} significa "sem assinatura", e e
        # exatamente um dos casos testados.
        if headers is None:
            headers = signed_headers(raw_body)
        return service.handle_webhook("sandbox", raw_body, headers)

    def test_confirmed_payment_marks_the_order_as_paid(self):
        service = build_service(self.order)
        raw_body = webhook_body("sandbox-1", "paid")

        result = self._handle(service, raw_body)

        self.assertEqual(result["status"], "processed")
        self.assertEqual(self.order.payment_status, "paid")
        self.assertIsNotNone(self.order.paid_at)

    def test_payment_does_not_accept_the_order_by_itself(self):
        # Pagar libera o lojista para aceitar; nao aceita por ele.
        service = build_service(self.order)

        self._handle(service, webhook_body("sandbox-1", "paid"))

        self.assertEqual(self.order.status, "pending")

    def test_payment_event_is_recorded_in_the_history_with_its_author(self):
        service = build_service(self.order)

        self._handle(service, webhook_body("sandbox-1", "paid"))

        entry = service.order_repository.history[0]
        self.assertEqual(entry.status, "payment:paid")
        self.assertEqual(entry.changed_by, "gateway:sandbox")

    def test_invalid_signature_is_a_401_and_changes_nothing(self):
        service = build_service(self.order)
        raw_body = webhook_body("sandbox-1", "paid")

        with self.assertRaises(HTTPException) as raised:
            self._handle(service, raw_body, headers={"X-Webhook-Signature": "chute"})

        self.assertEqual(raised.exception.status_code, 401)
        self.assertEqual(self.order.payment_status, "pending")

    def test_missing_signature_is_a_401(self):
        service = build_service(self.order)

        with self.assertRaises(HTTPException) as raised:
            self._handle(service, webhook_body("sandbox-1", "paid"), headers={})

        self.assertEqual(raised.exception.status_code, 401)

    def test_resent_event_does_not_duplicate_the_history(self):
        # Gateways reenviam a mesma notificacao ate receber 2xx.
        shared = FakeIdempotencyRepository()
        first = build_service(self.order, shared)
        raw_body = webhook_body("sandbox-1", "paid")

        self._handle(first, raw_body)
        second = build_service(self.order, shared)
        result = self._handle(second, raw_body)

        self.assertEqual(len(first.order_repository.history), 1)
        self.assertEqual(len(second.order_repository.history), 0)
        self.assertEqual(result["status"], "already_applied")

    def test_two_different_events_for_the_same_payment_both_apply(self):
        shared = FakeIdempotencyRepository()
        paid = build_service(self.order, shared)
        self._handle(paid, webhook_body("sandbox-1", "paid", event_id="evt-1"))

        refunded = build_service(self.order, shared)
        self._handle(refunded, webhook_body("sandbox-1", "refunded", event_id="evt-2"))

        self.assertEqual(self.order.payment_status, "refunded")

    def test_impossible_transition_is_ignored_without_error(self):
        # "pending" chegando depois de "paid": reenviar nao conserta, entao
        # respondemos 2xx e registramos no log em vez de deixar o gateway
        # tentando por horas.
        self.order.payment_status = "paid"
        service = build_service(self.order)

        result = self._handle(service, webhook_body("sandbox-1", "pending"))

        self.assertEqual(result["status"], "ignored")
        self.assertEqual(self.order.payment_status, "paid")

    def test_webhook_for_an_unknown_payment_is_ignored(self):
        service = build_service(self.order)

        result = self._handle(service, webhook_body("sandbox-outro", "paid"))

        self.assertEqual(result["reason"], "unknown_payment")

    def test_malformed_body_is_ignored(self):
        service = build_service(self.order)
        raw_body = b"nao sou json"

        result = self._handle(service, raw_body)

        self.assertEqual(result["reason"], "payload")

    def test_webhook_scope_isolates_the_restaurant(self):
        # A chave de idempotencia do webhook e o id do evento, que vem do
        # gateway. Dois restaurantes com gateways diferentes podem repetir
        # o mesmo id; o escopo separa.
        scope_a = IdempotencyService.build_scope(
            restaurant_id=uuid.uuid4(),
            route="POST /payments/webhooks/{provider}",
            requester="gateway:sandbox",
        )
        scope_b = IdempotencyService.build_scope(
            restaurant_id=uuid.uuid4(),
            route="POST /payments/webhooks/{provider}",
            requester="gateway:sandbox",
        )
        self.assertNotEqual(scope_a, scope_b)


if __name__ == "__main__":
    unittest.main()
