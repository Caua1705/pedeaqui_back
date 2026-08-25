"""Pagamento: criacao da cobranca e webhook do gateway.

A estrutura em volta do gateway — assinatura, idempotencia, maquina de
estados, resolucao de credencial e o efeito no pedido — e exercitada aqui
usando o provider "sandbox", uma implementacao de verdade sem chamada
externa. As chamadas HTTP de verdade ao Mercado Pago (create_payment,
verify_webhook_signature e parse_webhook_event para provider="mercadopago")
tem testes proprios em tests/test_mercadopago_gateway.py, com o gateway
mocado — aqui o que importa e que o PaymentService resolve e passa a
credencial CERTA, nao o formato da chamada HTTP em si.
"""

import hashlib
import hmac
import json
import time
import unittest
import uuid
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

from src.core.config import settings
from src.integrations.payment_gateway import (
    PaymentGatewayCredentialError,
    PaymentGatewayError,
    PaymentGatewayUnavailableError,
    PaymentIntent,
    PaymentProviderNotConfiguredError,
    PaymentProviderUnknownError,
    PaymentWebhookPayloadError,
    create_payment,
    parse_webhook_event,
    sign_sandbox_payload,
    verify_webhook_signature,
)
from src.schemas.payment_schema import CardPaymentPayload, StartPaymentRequest
from src.services.idempotency_service import IdempotencyService
from src.services.payment_refund_service import PaymentRefundService
from src.services.order_state_machine import PAYMENT_STATUSES_THAT_RELEASE_ORDER
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

    def attach_payment_intent(
        self, order, *, provider, provider_payment_id, payment_status="pending"
    ):
        order.payment_provider = provider
        order.provider_payment_id = provider_payment_id
        order.payment_status = payment_status

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
        # Zero na esmagadora maioria dos pedidos: so estorno mexe nele, e
        # estorno parcial e o unico caso em que ele muda sem `payment_status`
        # mudar junto.
        "refunded_amount": Decimal("0"),
        "status": "pending",
        # None = pedido de convidado, o caso mais comum nos testes. Ver
        # PaymentService._resolve_payer_email.
        "customer_id": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class FakeCustomerRepository:
    def __init__(self, customer=None):
        self.customer = customer

    def get_by_id(self, customer_id):
        if self.customer is not None and self.customer.id == customer_id:
            return self.customer
        return None


def build_service(order, idempotency_repository=None, credential=None, customer=None):
    service = PaymentService(FakeDb())
    service.order_repository = FakeOrderRepository(order)
    service.restaurant_service = SimpleNamespace(
        get_active_restaurant=lambda slug: SimpleNamespace(id=order.restaurant_id)
    )
    service.idempotency_service.repository = idempotency_repository or FakeIdempotencyRepository()
    # None por padrao: restaurante sem credencial cadastrada, o mesmo estado
    # de qualquer restaurante novo antes de rodar o script de cadastro.
    service.payment_credential_service = SimpleNamespace(
        get_active_credential=lambda restaurant_id: credential
    )
    service.customer_repository = FakeCustomerRepository(customer)
    return service


def webhook_body(payment_id, status, event_id="evt-1"):
    return json.dumps(
        {"event_id": event_id, "payment_id": payment_id, "status": status},
        separators=(",", ":"),
    ).encode("utf-8")


def signed_headers(raw_body, secret=WEBHOOK_SECRET):
    return {"X-Webhook-Signature": sign_sandbox_payload(raw_body, secret)}


def mercadopago_webhook_body(data_id, event_id="evt-1"):
    return json.dumps(
        {"id": event_id, "data": {"id": data_id}}, separators=(",", ":")
    ).encode("utf-8")


def mercadopago_signed_headers(data_id, secret, request_id="req-1", ts=None):
    # Mesmo manifest de src.integrations.payment_gateway._verify_mercadopago_signature,
    # calculado a mao aqui (ver tests/test_mercadopago_gateway.py para a
    # cobertura do formato em si) — o que estes testes provam e QUAL
    # segredo o PaymentService usa, nao o formato da assinatura.
    ts = ts or str(int(time.time()))
    manifest = f"id:{str(data_id).lower()};request-id:{request_id};ts:{ts};"
    v1 = hmac.new(secret.encode("utf-8"), manifest.encode("utf-8"), hashlib.sha256).hexdigest()
    return {"x-signature": f"ts={ts},v1={v1}", "x-request-id": request_id}


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

    def test_mercadopago_without_credential_fails_loudly_not_silently(self):
        # Sem access_token (restaurante sem credencial cadastrada para o
        # ambiente ativo) tem que FALHAR alto. Se devolvesse algo plausivel,
        # um pedido ficaria eternamente pendente sem ninguem perceber. A
        # chamada HTTP de verdade ao Mercado Pago e testada em
        # tests/test_mercadopago_gateway.py.
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
        # credential=None (padrao de build_service): restaurante sem
        # credencial cadastrada para o ambiente ativo.
        service = build_service(order)

        with patch.object(settings, "PAYMENT_PROVIDER", "mercadopago"):
            with self.assertRaises(HTTPException) as raised:
                service.start_online_payment("junior", "token-do-pedido")

        self.assertEqual(raised.exception.status_code, 503)

    def test_charge_is_created_with_the_restaurant_own_credential(self):
        # BLOCO G: a cobranca tem que sair em nome da conta do restaurante
        # do pedido, nunca de uma credencial global.
        order = make_order()
        credential = SimpleNamespace(access_token="token-do-junior-da-picanha")
        service = build_service(order, credential=credential)

        with patch.object(settings, "PAYMENT_PROVIDER", "mercadopago"), patch(
            "src.services.payment_service.create_payment"
        ) as mocked_create_payment:
            mocked_create_payment.return_value = PaymentIntent(
                provider="mercadopago",
                provider_payment_id="mp-1",
                payment_status="pending",
            )
            service.start_online_payment("junior", "token-do-pedido")

        _, kwargs = mocked_create_payment.call_args
        self.assertEqual(kwargs["access_token"], "token-do-junior-da-picanha")
        # application_fee opcional e hoje vazio: nao ha contrato de split
        # de pagamento assinado com nenhum restaurante ainda.
        self.assertIsNone(kwargs["application_fee"])

    def test_payer_email_uses_the_logged_in_customer_email(self):
        customer_id = uuid.uuid4()
        order = make_order(customer_id=customer_id)
        customer = SimpleNamespace(id=customer_id, email="cliente@exemplo.com")
        credential = SimpleNamespace(access_token="token-do-junior-da-picanha")
        service = build_service(order, credential=credential, customer=customer)

        with patch.object(settings, "PAYMENT_PROVIDER", "mercadopago"), patch(
            "src.services.payment_service.create_payment"
        ) as mocked_create_payment:
            mocked_create_payment.return_value = PaymentIntent(
                provider="mercadopago",
                provider_payment_id="mp-1",
                payment_status="pending",
            )
            service.start_online_payment("junior", "token-do-pedido")

        _, kwargs = mocked_create_payment.call_args
        self.assertEqual(kwargs["payer_email"], "cliente@exemplo.com")

    def test_payer_email_falls_back_to_a_synthetic_address_for_guests(self):
        # customer_id=None (padrao de make_order): pedido de convidado, sem
        # e-mail cadastrado em lugar nenhum.
        order = make_order(order_number=4321)
        credential = SimpleNamespace(access_token="token-do-junior-da-picanha")
        service = build_service(order, credential=credential)

        with patch.object(settings, "PAYMENT_PROVIDER", "mercadopago"), patch(
            "src.services.payment_service.create_payment"
        ) as mocked_create_payment:
            mocked_create_payment.return_value = PaymentIntent(
                provider="mercadopago",
                provider_payment_id="mp-1",
                payment_status="pending",
            )
            service.start_online_payment("junior", "token-do-pedido")

        _, kwargs = mocked_create_payment.call_args
        self.assertEqual(kwargs["payer_email"], "pedido-4321@pederapidex.com")

    def test_retry_of_a_pending_charge_asks_for_the_same_one(self):
        # Cliente clicou de novo em "pagar" com o pix ainda em aberto. Nao
        # ha cobranca a substituir: a chave de idempotencia tem que se
        # repetir para o gateway devolver o MESMO pix, em vez de abrir um
        # segundo QR code para o mesmo pedido.
        order = make_order(payment_status="pending", provider_payment_id="mp-1")
        credential = SimpleNamespace(access_token="token-do-junior-da-picanha")
        service = build_service(order, credential=credential)

        with patch.object(settings, "PAYMENT_PROVIDER", "mercadopago"), patch(
            "src.services.payment_service.create_payment"
        ) as mocked_create_payment:
            mocked_create_payment.return_value = PaymentIntent(
                provider="mercadopago",
                provider_payment_id="mp-1",
                payment_status="pending",
            )
            service.start_online_payment("junior", "token-do-pedido")

        _, kwargs = mocked_create_payment.call_args
        self.assertIsNone(kwargs["previous_payment_id"])

    def test_retry_after_a_refused_charge_asks_for_a_new_one(self):
        # Cobranca recusada nao se retenta, se SUBSTITUI. Sem informar qual
        # cobranca esta sendo trocada, a chave se repetiria e o gateway
        # devolveria a propria cobranca RECUSADA — o pedido ficaria sem
        # nenhuma forma de ser pago.
        order = make_order(
            payment_status="failed",
            payment_provider="mercadopago",
            provider_payment_id="mp-recusada",
        )
        credential = SimpleNamespace(access_token="token-do-junior-da-picanha")
        service = build_service(order, credential=credential)

        with patch.object(settings, "PAYMENT_PROVIDER", "mercadopago"), patch(
            "src.services.payment_service.create_payment"
        ) as mocked_create_payment:
            mocked_create_payment.return_value = PaymentIntent(
                provider="mercadopago",
                provider_payment_id="mp-2",
                payment_status="pending",
            )
            service.start_online_payment("junior", "token-do-pedido")

        _, kwargs = mocked_create_payment.call_args
        self.assertEqual(kwargs["previous_payment_id"], "mp-recusada")
        self.assertEqual(order.provider_payment_id, "mp-2")
        self.assertEqual(order.payment_status, "pending")

    def test_payer_email_is_not_resolved_for_the_sandbox_provider(self):
        # O sandbox nao usa e-mail nenhum: nao ha por que gastar uma consulta
        # ao customer_repository so para descartar o resultado.
        order = make_order()
        service = build_service(order)
        service.customer_repository = SimpleNamespace(
            get_by_id=lambda customer_id: self.fail("nao deveria consultar customer_repository")
        )

        with patch.object(settings, "PAYMENT_PROVIDER", "sandbox"):
            service.start_online_payment("junior", "token-do-pedido")


class CardPaymentTests(unittest.TestCase):
    """Cartao: o veredito vem no PROPRIO POST, e nao por webhook."""

    def _card_payload(self):
        return StartPaymentRequest(
            card=CardPaymentPayload(token="tok-123", payment_method_id="master")
        )

    def _customer(self):
        return SimpleNamespace(id=uuid.uuid4(), email="cliente@exemplo.com")

    def _service_with_card_order(self, customer=None, **order_overrides):
        order = make_order(payment_method="credit_card", **order_overrides)
        credential = SimpleNamespace(access_token="token-do-junior-da-picanha")
        return order, build_service(order, credential=credential, customer=customer)

    def _create_returning(self, status_gateway, payment_status, detail=None):
        return PaymentIntent(
            provider="mercadopago",
            provider_payment_id="mp-card-1",
            payment_status=payment_status,
            raw_status=status_gateway,
            raw_status_detail=detail,
        )

    def test_approved_card_marks_the_order_as_paid_without_any_webhook(self):
        order, service = self._service_with_card_order(customer=self._customer())

        with patch.object(settings, "PAYMENT_PROVIDER", "mercadopago"), patch(
            "src.services.payment_service.create_payment"
        ) as mocked:
            mocked.return_value = self._create_returning("approved", "paid")
            response = service.start_online_payment(
                "junior", "token-do-pedido", self._card_payload(), self._customer()
            )

        self.assertEqual(response.payment_status, "paid")
        self.assertEqual(order.payment_status, "paid")
        # Sem isto o pedido ficaria pago sem hora de pagamento.
        self.assertIsNotNone(order.paid_at)

    def test_rejected_card_marks_the_order_as_failed_not_pending(self):
        # O bug que este teste existe para impedir: `rejected` dentro de um
        # HTTP 201 nao e excecao, e ignorar a resposta deixava o pedido
        # `pending` para sempre — sem nunca alcancar o caminho de nova
        # tentativa, que so existe a partir de `failed`.
        order, service = self._service_with_card_order(customer=self._customer())

        with patch.object(settings, "PAYMENT_PROVIDER", "mercadopago"), patch(
            "src.services.payment_service.create_payment"
        ) as mocked:
            mocked.return_value = self._create_returning(
                "rejected", "failed", detail="cc_rejected_insufficient_amount"
            )
            response = service.start_online_payment(
                "junior", "token-do-pedido", self._card_payload(), self._customer()
            )

        self.assertEqual(response.payment_status, "failed")
        self.assertEqual(order.payment_status, "failed")
        # O motivo atravessa para o front: "saldo insuficiente" e "CVV errado"
        # pedem coisas diferentes do cliente.
        self.assertEqual(response.status_detail, "cc_rejected_insufficient_amount")

    def test_card_in_analysis_does_not_release_the_order(self):
        order, service = self._service_with_card_order(customer=self._customer())

        with patch.object(settings, "PAYMENT_PROVIDER", "mercadopago"), patch(
            "src.services.payment_service.create_payment"
        ) as mocked:
            mocked.return_value = self._create_returning("in_process", "in_review")
            response = service.start_online_payment(
                "junior", "token-do-pedido", self._card_payload(), self._customer()
            )

        self.assertEqual(response.payment_status, "in_review")
        self.assertNotIn("in_review", PAYMENT_STATUSES_THAT_RELEASE_ORDER)

    def test_synchronous_verdict_is_recorded_in_the_history(self):
        order, service = self._service_with_card_order(customer=self._customer())

        with patch.object(settings, "PAYMENT_PROVIDER", "mercadopago"), patch(
            "src.services.payment_service.create_payment"
        ) as mocked:
            mocked.return_value = self._create_returning("approved", "paid")
            service.start_online_payment(
                "junior", "token-do-pedido", self._card_payload(), self._customer()
            )

        entry = service.order_repository.history[0]
        self.assertEqual(entry.status, "payment:paid")
        self.assertEqual(entry.changed_by, "gateway:mercadopago")

    def test_pix_does_not_write_history_on_creation(self):
        # O contrario do teste acima: pix nasce `pending` e quem escreve o
        # historico e o webhook. Uma linha aqui seria evento de dinheiro que
        # nao aconteceu.
        order = make_order()
        service = build_service(order)

        with patch.object(settings, "PAYMENT_PROVIDER", "sandbox"):
            response = service.start_online_payment("junior", "token-do-pedido")

        self.assertEqual(response.payment_status, "pending")
        self.assertEqual(service.order_repository.history, [])

    def test_guest_cannot_pay_with_card(self):
        # O e-mail sintetico de convidado e inocuo no pix e caro no cartao:
        # ele entra na analise antifraude do gateway.
        _, service = self._service_with_card_order()

        with patch.object(settings, "PAYMENT_PROVIDER", "mercadopago"), patch(
            "src.services.payment_service.create_payment"
        ) as mocked:
            with self.assertRaises(HTTPException) as captured:
                service.start_online_payment(
                    "junior", "token-do-pedido", self._card_payload(), None
                )

        self.assertEqual(captured.exception.status_code, 401)
        self.assertEqual(captured.exception.detail["code"], "login_required")
        # E o gateway nunca chega a ser chamado.
        mocked.assert_not_called()

    def test_card_without_a_browser_token_is_refused_before_the_gateway(self):
        _, service = self._service_with_card_order(customer=self._customer())

        with patch.object(settings, "PAYMENT_PROVIDER", "mercadopago"), patch(
            "src.services.payment_service.create_payment"
        ) as mocked:
            with self.assertRaises(HTTPException) as captured:
                service.start_online_payment(
                    "junior", "token-do-pedido", None, self._customer()
                )

        self.assertEqual(captured.exception.status_code, 400)
        self.assertEqual(captured.exception.detail["code"], "card_token_required")
        mocked.assert_not_called()

    def test_logged_in_payer_email_wins_over_a_guest_order(self):
        # O pedido nasceu de convidado e a pessoa entrou na conta antes de
        # pagar: o e-mail de verdade existe mesmo com customer_id nulo.
        customer = self._customer()
        _, service = self._service_with_card_order(customer=customer)

        with patch.object(settings, "PAYMENT_PROVIDER", "mercadopago"), patch(
            "src.services.payment_service.create_payment"
        ) as mocked:
            mocked.return_value = self._create_returning("approved", "paid")
            service.start_online_payment(
                "junior", "token-do-pedido", self._card_payload(), customer
            )

        _, kwargs = mocked.call_args
        self.assertEqual(kwargs["payer_email"], "cliente@exemplo.com")

    def test_sandbox_refuses_card_instead_of_faking_it(self):
        # A armadilha que este teste fecha: o sandbox ignorava payment_method
        # e devolvia intent valido para cartao. O webhook marcava como pago e
        # a COMANDA IMPRIMIA — o fluxo inteiro parecendo funcionar sem
        # dinheiro nenhum ter existido.
        with self.assertRaises(PaymentProviderNotConfiguredError):
            create_payment(
                provider="sandbox",
                order_id=uuid.uuid4(),
                amount=Decimal("10.00"),
                payment_method="credit_card",
                description="Pedido #1",
            )


class PaymentConfigTests(unittest.TestCase):
    """A chave publica que o front precisa — e o que ela NAO pode levar junto."""

    def test_public_key_of_the_restaurant_is_served(self):
        order = make_order()
        credential = SimpleNamespace(
            public_key="TEST-public-do-junior",
            access_token="token-secretissimo",
            webhook_secret="segredo-do-webhook",
        )
        service = build_service(order, credential=credential)

        with patch.object(settings, "PAYMENT_PROVIDER", "mercadopago"):
            config = service.get_payment_config("junior")

        self.assertEqual(config.public_key, "TEST-public-do-junior")
        self.assertTrue(config.card_enabled)

    def test_nothing_encrypted_ever_reaches_the_response(self):
        order = make_order()
        credential = SimpleNamespace(
            public_key="TEST-public-do-junior",
            access_token="token-secretissimo",
            webhook_secret="segredo-do-webhook",
        )
        service = build_service(order, credential=credential)

        with patch.object(settings, "PAYMENT_PROVIDER", "mercadopago"):
            corpo = service.get_payment_config("junior").model_dump_json()

        self.assertNotIn("token-secretissimo", corpo)
        self.assertNotIn("segredo-do-webhook", corpo)

    def test_restaurant_without_credential_does_not_offer_card(self):
        # `card_enabled` responde ANTES de o cliente digitar o cartao, em vez
        # de depois, com um 503.
        service = build_service(make_order())

        with patch.object(settings, "PAYMENT_PROVIDER", "mercadopago"):
            config = service.get_payment_config("junior")

        self.assertIsNone(config.public_key)
        self.assertFalse(config.card_enabled)

    def test_sandbox_never_offers_card(self):
        credential = SimpleNamespace(public_key="nao-deveria-sair", access_token="x")
        service = build_service(make_order(), credential=credential)

        with patch.object(settings, "PAYMENT_PROVIDER", "sandbox"):
            config = service.get_payment_config("junior")

        self.assertFalse(config.card_enabled)
        self.assertIsNone(config.public_key)


class PartialRefundTests(unittest.TestCase):
    """Estorno parcial: o pagamento continua `paid` e so o VALOR muda."""

    def setUp(self):
        self.patcher = patch.object(settings, "PAYMENT_WEBHOOK_SECRET", WEBHOOK_SECRET)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def _paid_order(self, **overrides):
        return make_order(
            payment_provider="sandbox",
            provider_payment_id="sandbox-1",
            payment_status="paid",
            **overrides,
        )

    def _refund_body(self, amount, event_id="evt-refund"):
        return json.dumps(
            {
                "event_id": event_id,
                "payment_id": "sandbox-1",
                "status": "paid",
                "refunded_amount": amount,
            },
            separators=(",", ":"),
        ).encode("utf-8")

    def test_partial_refund_is_recorded_even_though_the_status_does_not_change(self):
        order = self._paid_order()
        service = build_service(order)
        raw_body = self._refund_body("20.00")

        with self.assertLogs("uvicorn.error", level="WARNING") as captured:
            result = service.handle_webhook("sandbox", raw_body, signed_headers(raw_body))

        self.assertEqual(result["status"], "processed")
        self.assertEqual(order.refunded_amount, Decimal("20.00"))
        # O pagamento NAO vira `refunded`: parte do dinheiro ficou.
        self.assertEqual(order.payment_status, "paid")
        self.assertIn("estorno parcial registrado", "\n".join(captured.output))

    def test_partial_refund_writes_its_own_history_entry(self):
        order = self._paid_order()
        service = build_service(order)
        raw_body = self._refund_body("20.00")

        service.handle_webhook("sandbox", raw_body, signed_headers(raw_body))

        entry = service.order_repository.history[0]
        self.assertEqual(entry.status, "payment:partially_refunded")

    def test_the_same_refund_notification_twice_is_not_a_second_refund(self):
        # O gateway reenvia ate receber 2xx. O valor e o TOTAL ja devolvido,
        # nao um incremento, entao comparar com o gravado basta.
        order = self._paid_order()
        service = build_service(order)
        raw_body = self._refund_body("20.00")

        service.handle_webhook("sandbox", raw_body, signed_headers(raw_body))
        result = service.handle_webhook("sandbox", raw_body, signed_headers(raw_body))

        self.assertEqual(result["status"], "already_applied")
        self.assertEqual(order.refunded_amount, Decimal("20.00"))
        self.assertEqual(len(service.order_repository.history), 1)

    def test_a_second_larger_refund_is_applied(self):
        order = self._paid_order()
        service = build_service(order)

        primeiro = self._refund_body("20.00")
        service.handle_webhook("sandbox", primeiro, signed_headers(primeiro))
        segundo = self._refund_body("35.00", event_id="evt-refund-2")
        service.handle_webhook("sandbox", segundo, signed_headers(segundo))

        self.assertEqual(order.refunded_amount, Decimal("35.00"))

    def test_a_paid_webhook_without_refund_stays_already_applied(self):
        # O caminho normal nao pode virar escrita: um webhook repetido de
        # pagamento confirmado nao mexe em nada.
        order = self._paid_order()
        service = build_service(order)
        raw_body = webhook_body("sandbox-1", "paid")

        result = service.handle_webhook("sandbox", raw_body, signed_headers(raw_body))

        self.assertEqual(result["status"], "already_applied")
        self.assertEqual(order.refunded_amount, Decimal("0"))
        self.assertEqual(service.order_repository.history, [])

    def test_commission_columns_are_never_touched_by_a_refund(self):
        # Decisao tomada: a plataforma cobra sobre a venda que aconteceu.
        # A coluna existe para a decisao CONTRARIA continuar possivel.
        order = self._paid_order()
        order.commission_amount = Decimal("9.30")
        order.commission_base_amount = Decimal("93.00")
        service = build_service(order)
        raw_body = self._refund_body("20.00")

        service.handle_webhook("sandbox", raw_body, signed_headers(raw_body))

        self.assertEqual(order.commission_amount, Decimal("9.30"))
        self.assertEqual(order.commission_base_amount, Decimal("93.00"))


class StartPaymentErrorTests(unittest.TestCase):
    """O que o CLIENTE recebe quando a cobranca nao sai.

    Antes daqui todo erro do gateway virava 503 com uma mensagem interna, e
    o frontend mostrava "erro interno" para tudo. Sao situacoes diferentes
    para quem esta esperando o pix: o gateway fora do ar por um minuto pede
    "tentar de novo"; restaurante sem credencial cadastrada, nao.
    """

    def _failing_service(self, exception):
        order = make_order()
        credential = SimpleNamespace(access_token="token-do-junior-da-picanha")
        service = build_service(order, credential=credential)

        with patch.object(settings, "PAYMENT_PROVIDER", "mercadopago"), patch(
            "src.services.payment_service.create_payment", side_effect=exception
        ):
            with self.assertRaises(HTTPException) as raised:
                service.start_online_payment("junior", "token-do-pedido")
        return raised.exception

    def test_unstable_gateway_asks_the_client_to_try_again(self):
        exc = self._failing_service(
            PaymentGatewayUnavailableError("Mercado Pago com erro interno (status 500)")
        )

        self.assertEqual(exc.status_code, 503)
        self.assertEqual(exc.detail["code"], "gateway_unavailable")
        self.assertTrue(exc.detail["retryable"])
        self.assertIn("Tente de novo", exc.detail["message"])

    def test_refused_charge_does_not_ask_the_client_to_try_again(self):
        # 400/422: eles entenderam e recusaram. Voltar depois com a mesma
        # cobranca da no mesmo — mandar o cliente insistir e enganacao.
        exc = self._failing_service(
            PaymentGatewayError(
                "Mercado Pago recusou a requisicao (status 400, code=4051)",
                provider_error_code="4051",
            )
        )

        self.assertEqual(exc.status_code, 502)
        self.assertEqual(exc.detail["code"], "payment_rejected")
        self.assertFalse(exc.detail["retryable"])
        # A referencia do provedor atravessa para poder ser citada num
        # chamado de suporte.
        self.assertEqual(exc.detail["provider_error_code"], "4051")

    def test_rejected_credential_is_definitive_for_the_client(self):
        # Token inválido ou revogado: quem resolve e o lojista, no painel
        # dele. O cliente insistindo nao troca credencial nenhuma.
        exc = self._failing_service(
            PaymentGatewayCredentialError("Mercado Pago recusou a credencial (status 401)")
        )

        self.assertEqual(exc.status_code, 503)
        self.assertEqual(exc.detail["code"], "payment_unavailable")
        self.assertFalse(exc.detail["retryable"])

    def test_restaurant_without_credential_is_definitive_too(self):
        # credential=None: restaurante que ainda nao rodou o script de
        # cadastro. Nunca chegamos a falar com o gateway.
        order = make_order()
        service = build_service(order)

        with patch.object(settings, "PAYMENT_PROVIDER", "mercadopago"):
            with self.assertRaises(HTTPException) as raised:
                service.start_online_payment("junior", "token-do-pedido")

        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(raised.exception.detail["code"], "payment_unavailable")
        self.assertFalse(raised.exception.detail["retryable"])

    def test_the_gateway_own_message_never_reaches_the_client(self):
        # O texto cru do gateway pode ecoar o e-mail de quem pagou (eles
        # devolvem o valor recusado na mensagem). Ele fica no log.
        exc = self._failing_service(
            PaymentGatewayError("cliente@exemplo.com recusado pelo Mercado Pago")
        )

        self.assertNotIn("cliente@exemplo.com", str(exc.detail))
        self.assertNotIn("Mercado Pago", exc.detail["message"])

    def test_the_reason_is_logged_even_though_it_is_not_answered(self):
        # Tirar o motivo tecnico da resposta so vale se ele nao se perder.
        order = make_order()
        credential = SimpleNamespace(access_token="token-do-junior-da-picanha")
        service = build_service(order, credential=credential)

        with patch.object(settings, "PAYMENT_PROVIDER", "mercadopago"), patch(
            "src.services.payment_service.create_payment",
            side_effect=PaymentGatewayUnavailableError("status 500, code=internal_error"),
        ):
            with self.assertLogs("uvicorn.error", level="WARNING") as captured:
                with self.assertRaises(HTTPException):
                    service.start_online_payment("junior", "token-do-pedido")

        logged = "\n".join(captured.output)
        self.assertIn("code=gateway_unavailable", logged)
        self.assertIn("internal_error", logged)


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

    def test_payment_confirmed_on_a_rejected_order_is_refunded(self):
        # A corrida real: o lojista recusa enquanto o pagamento esta em voo,
        # e o pedido termina `rejected` + `paid`. Ate 25/08/2026 isto era so
        # um warning — o dinheiro do cliente ficava na conta do restaurante,
        # e o extrato nao denunciava, porque `rejected` ja esta fora da
        # comissao. Hoje dispara o MESMO estorno do cancelamento pelo painel,
        # da outra ponta da mesma corrida.
        order = make_order(
            payment_provider="sandbox",
            provider_payment_id="sandbox-1",
            status="rejected",
        )
        service = build_service(order)

        with patch.object(PaymentRefundService, "refund_terminal_order") as refund:
            self._handle(service, webhook_body("sandbox-1", "paid"))

        refund.assert_called_once_with(order.id, order.restaurant_id)
        # O pagamento e gravado assim mesmo: recusar a escrita esconderia o
        # dinheiro que de fato entrou — e e justamente ele que sera devolvido.
        self.assertEqual(order.payment_status, "paid")

    def test_payment_confirmed_on_a_cancelled_order_is_refunded(self):
        order = make_order(
            payment_provider="sandbox",
            provider_payment_id="sandbox-1",
            status="cancelled",
        )
        service = build_service(order)

        with patch.object(PaymentRefundService, "refund_terminal_order") as refund:
            self._handle(service, webhook_body("sandbox-1", "paid"))

        refund.assert_called_once_with(order.id, order.restaurant_id)

    def test_payment_confirmed_on_a_live_order_is_never_refunded(self):
        # A propriedade mais perigosa desta frente inteira: pagar um pedido
        # NORMAL nao pode devolver o dinheiro. `pending` nao e estado
        # terminal, e a checagem que separa os dois casos e uma linha so.
        service = build_service(self.order)

        with patch.object(PaymentRefundService, "refund_terminal_order") as refund:
            with self.assertLogs("uvicorn.error", level="INFO") as captured:
                self._handle(service, webhook_body("sandbox-1", "paid"))

        refund.assert_not_called()
        # E o caminho normal continua sem gerar o aviso de dinheiro parado:
        # um warning que sai em todo pedido pago deixa de ser lido no dia em
        # que importar.
        self.assertNotIn("sem estorno automatico", "\n".join(captured.output))

    def test_refund_on_a_terminal_order_is_not_the_same_alarm(self):
        # Estorno em pedido cancelado e o desfecho CERTO, nao dinheiro parado.
        order = make_order(
            payment_provider="sandbox",
            provider_payment_id="sandbox-1",
            status="cancelled",
            payment_status="paid",
        )
        service = build_service(order)

        with self.assertLogs("uvicorn.error", level="INFO") as captured:
            self._handle(service, webhook_body("sandbox-1", "refunded"))

        self.assertNotIn("sem estorno automatico", "\n".join(captured.output))

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


class MercadopagoWebhookWiringTests(unittest.TestCase):
    """O Mercado Pago nao manda o status no webhook, so o data.id — dai o
    PaymentService precisar achar o PEDIDO (e por ele, o restaurante) antes
    de saber qual credencial usar na consulta de status. Aqui a assinatura e
    parse_webhook_event sao mocados: o que se prova e a ORDEM das chamadas e
    QUAL credencial chega em parse_webhook_event, nao o formato da chamada
    HTTP em si (isso e tests/test_mercadopago_gateway.py).
    """

    def test_credential_of_the_order_restaurant_is_passed_to_parse_webhook_event(self):
        order = make_order(payment_provider="mercadopago", provider_payment_id="mp-1")
        credential = SimpleNamespace(
            access_token="token-do-junior-da-picanha",
            webhook_secret="segredo-do-webhook-do-junior",
        )
        service = build_service(order, credential=credential)

        with patch(
            "src.services.payment_service.verify_webhook_signature", return_value=True
        ), patch(
            "src.services.payment_service.extract_provider_payment_id", return_value="mp-1"
        ), patch(
            "src.services.payment_service.parse_webhook_event"
        ) as mocked_parse:
            mocked_parse.return_value = SimpleNamespace(
                event_id="evt-1",
                provider_payment_id="mp-1",
                payment_status="paid",
                raw_status="approved",
                refunded_amount=Decimal("0"),
            )
            result = service.handle_webhook("mercadopago", b"{}", {})

        self.assertEqual(result["status"], "processed")
        _, kwargs = mocked_parse.call_args
        self.assertEqual(kwargs["access_token"], "token-do-junior-da-picanha")

    def test_without_a_registered_credential_the_webhook_answers_503(self):
        # credential=None (padrao de build_service): restaurante sem
        # credencial cadastrada. parse_webhook_event AQUI E O DE VERDADE
        # (nao mocado) — e ele quem recusa por falta de access_token.
        order = make_order(payment_provider="mercadopago", provider_payment_id="mp-1")
        service = build_service(order)

        with patch(
            "src.services.payment_service.verify_webhook_signature", return_value=True
        ), patch(
            "src.services.payment_service.extract_provider_payment_id", return_value="mp-1"
        ):
            with self.assertRaises(HTTPException) as raised:
                service.handle_webhook("mercadopago", b"{}", {})

        self.assertEqual(raised.exception.status_code, 503)


class MercadopagoWebhookSignatureOrderingTests(unittest.TestCase):
    """A assinatura do Mercado Pago passou a ser por restaurante — precisa
    achar o pedido (e por ele, o restaurante e o segredo dele) antes de dar
    para conferir a assinatura. Estes testes travam essa ordem: a assinatura
    e conferida com o segredo DO RESTAURANTE DO PEDIDO (nao um global, nao o
    de outro restaurante), e nada que fale de verdade com o Mercado Pago
    (parse_webhook_event) acontece sem uma assinatura valida. verify_webhook_signature
    e parse_webhook_event AQUI SAO OS DE VERDADE (nao mocados) exceto onde
    dito — o formato da assinatura em si e coberto em
    tests/test_mercadopago_gateway.py.
    """

    def test_signature_is_checked_with_the_order_restaurant_own_secret(self):
        order = make_order(payment_provider="mercadopago", provider_payment_id="mp-1")
        credential = SimpleNamespace(
            access_token="token-do-junior-da-picanha",
            webhook_secret="segredo-do-junior",
        )
        service = build_service(order, credential=credential)
        raw_body = mercadopago_webhook_body("mp-1")
        headers = mercadopago_signed_headers("mp-1", "segredo-do-junior")

        with patch("src.services.payment_service.parse_webhook_event") as mocked_parse:
            mocked_parse.return_value = SimpleNamespace(
                event_id="evt-1",
                provider_payment_id="mp-1",
                payment_status="paid",
                raw_status="approved",
                refunded_amount=Decimal("0"),
            )
            result = service.handle_webhook("mercadopago", raw_body, headers)

        self.assertEqual(result["status"], "processed")

    def test_another_restaurants_secret_does_not_validate_this_one(self):
        order = make_order(payment_provider="mercadopago", provider_payment_id="mp-1")
        credential = SimpleNamespace(
            access_token="token-do-junior-da-picanha",
            webhook_secret="segredo-do-junior",
        )
        service = build_service(order, credential=credential)
        raw_body = mercadopago_webhook_body("mp-1")
        # Assinado com o segredo de OUTRO restaurante: bater com o webhook
        # secret GLOBAL antigo teria passado, com o de cada um nao.
        headers = mercadopago_signed_headers("mp-1", "segredo-de-outro-restaurante")

        with self.assertRaises(HTTPException) as raised:
            service.handle_webhook("mercadopago", raw_body, headers)

        self.assertEqual(raised.exception.status_code, 401)

    def test_credential_without_webhook_secret_yet_answers_503(self):
        # Credencial cadastrada so com access_token (o caso do Junior antes
        # deste campo existir) — sem segredo nao ha como verificar, e a
        # resposta e a mesma de qualquer credencial ausente.
        order = make_order(payment_provider="mercadopago", provider_payment_id="mp-1")
        credential = SimpleNamespace(access_token="token-do-junior-da-picanha", webhook_secret=None)
        service = build_service(order, credential=credential)
        raw_body = mercadopago_webhook_body("mp-1")
        headers = mercadopago_signed_headers("mp-1", "qualquer-coisa")

        with self.assertRaises(HTTPException) as raised:
            service.handle_webhook("mercadopago", raw_body, headers)

        self.assertEqual(raised.exception.status_code, 503)

    def test_unknown_payment_never_reaches_signature_verification(self):
        # Sem pedido correspondente nao ha restaurante, e sem restaurante
        # nao ha segredo nenhum para conferir — a resposta e ignored ANTES
        # de qualquer tentativa de verificar assinatura, gastando so um
        # SELECT local que nao achou nada.
        order = make_order(payment_provider="mercadopago", provider_payment_id="mp-1")
        service = build_service(order)
        raw_body = mercadopago_webhook_body("mp-outro")
        headers = mercadopago_signed_headers("mp-outro", "nao-importa")

        with patch("src.services.payment_service.verify_webhook_signature") as mocked_verify:
            result = service.handle_webhook("mercadopago", raw_body, headers)

        mocked_verify.assert_not_called()
        self.assertEqual(result["reason"], "unknown_payment")

    def test_unknown_provider_is_a_404_before_touching_the_database(self):
        order = make_order(payment_provider="mercadopago", provider_payment_id="mp-1")
        service = build_service(order)

        with self.assertRaises(HTTPException) as raised:
            service.handle_webhook("picpay", b"{}", {})

        self.assertEqual(raised.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
