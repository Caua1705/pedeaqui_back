"""Integracao real com o Mercado Pago (create_payment, verify_webhook_signature
e parse_webhook_event para provider="mercadopago").

O gateway e SUBSTITUIDO por um FakeHttpxClient no lugar de httpx.Client —
nenhuma chamada de rede acontece aqui.

O que estes testes PROVAM:
- o que MANDAMOS para o Mercado Pago esta certo: URL, Authorization com o
  token do restaurante (nunca uma constante global), X-Idempotency-Key,
  payment_method_id=pix, e-mail do pagador, application_fee so quando
  informado.
- a resposta deles e traduzida certo: id/qr_code/ticket_url no create, e
  approved/rejected/cancelled/refunded/charged_back -> paid/failed/refunded
  no webhook (pending/in_process/authorized e qualquer status novo ->
  ignorado, nao aplica mudanca).
- os erros do gateway (timeout, rede, 401/403, 404, 5xx, 4xx generico,
  corpo que nao e JSON) viram excecoes especificas — nenhum deles cai
  silenciosamente em "deu certo".
- a assinatura do webhook (manifest `id:...;request-id:...;ts:...;` + HMAC
  comparado com hmac.compare_digest) bate com um valor calculado a mao no
  teste, corpo adulterado ou timestamp velho (>5min) sao recusados, e nada
  disso lanca excecao por corpo malformado (vira False, que e 401).

O que ISSO NAO PROVA — so uma chamada real (ou o simulador de pagamentos do
proprio painel do Mercado Pago) prova:
- que o corpo que montamos e aceito pela API deles hoje (campo renomeado,
  novo campo obrigatorio, mudanca de contrato);
- que a credencial de teste cadastrada de fato autentica;
- que o QR code/ticket_url gerados abrem e sao pagaveis num app de banco
  (para teste, isso normalmente passa pelo simulador de pagamentos do
  painel, nao por um pix de verdade);
- o webhook de producao chegando com o formato exato descrito na
  documentacao deles (headers, `x-signature`) — aqui simulamos o formato
  documentado, nao um payload capturado de producao.
"""

import hashlib
import hmac
import json
import time
import unittest
import uuid
from decimal import Decimal
from unittest.mock import patch

import httpx

from src.integrations.payment_gateway import (
    PaymentGatewayCredentialError,
    PaymentGatewayError,
    PaymentGatewayUnavailableError,
    PaymentNotFoundError,
    PaymentProviderNotConfiguredError,
    PaymentWebhookPayloadError,
    create_payment,
    extract_provider_payment_id,
    parse_webhook_event,
    verify_webhook_signature,
)


ACCESS_TOKEN = "TEST-token-do-junior-da-picanha"
WEBHOOK_SECRET = "segredo-do-webhook-mercadopago"
HTTPX_CLIENT_PATH = "src.integrations.payment_gateway.httpx.Client"


class FakeResponse:
    def __init__(self, status_code, json_body=None, json_error=False):
        self.status_code = status_code
        self._json_body = json_body
        self._json_error = json_error

    def json(self):
        if self._json_error:
            raise ValueError("corpo nao e JSON")
        return self._json_body


class FakeHttpxClient:
    """Substitui httpx.Client dentro de payment_gateway.py. Guarda as
    chamadas feitas para os testes conferirem URL/headers/corpo."""

    def __init__(self, response=None, exception=None):
        self._response = response
        self._exception = exception
        self.requests = []

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def request(self, method, url, json=None, headers=None):
        self.requests.append({"method": method, "url": url, "json": json, "headers": headers})
        if self._exception is not None:
            raise self._exception
        return self._response


def create_payment_body(
    order_id=None,
    access_token=ACCESS_TOKEN,
    payer_email="cliente@exemplo.com",
    payment_method="pix",
    application_fee=None,
    previous_payment_id=None,
):
    return dict(
        provider="mercadopago",
        order_id=order_id or uuid.uuid4(),
        amount=Decimal("93.00"),
        payment_method=payment_method,
        description="Pedido #1234",
        access_token=access_token,
        payer_email=payer_email,
        application_fee=application_fee,
        previous_payment_id=previous_payment_id,
    )


class CreatePaymentTests(unittest.TestCase):
    def test_success_sends_pix_body_and_maps_the_response(self):
        order_id = uuid.uuid4()
        fake_client = FakeHttpxClient(
            response=FakeResponse(
                201,
                {
                    "id": 123456789,
                    "status": "pending",
                    "point_of_interaction": {
                        "transaction_data": {
                            "qr_code": "00020126-copia-e-cola",
                            "ticket_url": "https://www.mercadopago.com.br/payments/123456789/ticket",
                        }
                    },
                },
            )
        )

        with patch(HTTPX_CLIENT_PATH, return_value=fake_client):
            intent = create_payment(**create_payment_body(order_id=order_id))

        self.assertEqual(intent.provider, "mercadopago")
        self.assertEqual(intent.provider_payment_id, "123456789")
        self.assertEqual(intent.qr_code, "00020126-copia-e-cola")
        self.assertEqual(
            intent.checkout_url, "https://www.mercadopago.com.br/payments/123456789/ticket"
        )

        self.assertEqual(len(fake_client.requests), 1)
        sent = fake_client.requests[0]
        self.assertEqual(sent["method"], "POST")
        self.assertEqual(sent["url"], "https://api.mercadopago.com/v1/payments")
        self.assertEqual(sent["headers"]["Authorization"], f"Bearer {ACCESS_TOKEN}")
        # A chave comeca pelo order_id para a tentativa ser achavel no painel
        # deles a partir do pedido; o sufixo e o que separa uma tentativa da
        # outra (ver IdempotencyKeyTests).
        self.assertTrue(sent["headers"]["X-Idempotency-Key"].startswith(f"{order_id}:"))
        self.assertEqual(sent["json"]["payment_method_id"], "pix")
        self.assertEqual(sent["json"]["payer"], {"email": "cliente@exemplo.com"})
        self.assertEqual(sent["json"]["transaction_amount"], 93.00)
        self.assertNotIn("application_fee", sent["json"])

    def test_application_fee_is_included_only_when_given(self):
        fake_client = FakeHttpxClient(
            response=FakeResponse(201, {"id": 1, "point_of_interaction": {"transaction_data": {}}})
        )

        with patch(HTTPX_CLIENT_PATH, return_value=fake_client):
            create_payment(**create_payment_body(application_fee=Decimal("1.50")))

        self.assertEqual(fake_client.requests[0]["json"]["application_fee"], 1.5)

    def test_missing_response_fields_do_not_crash_the_mapping(self):
        # Pix sempre traz point_of_interaction, mas nao custa nao quebrar
        # se um dia vier incompleto.
        fake_client = FakeHttpxClient(response=FakeResponse(201, {"id": 1}))

        with patch(HTTPX_CLIENT_PATH, return_value=fake_client):
            intent = create_payment(**create_payment_body())

        self.assertIsNone(intent.qr_code)
        self.assertIsNone(intent.checkout_url)

    def test_without_access_token_is_refused_not_silently_accepted(self):
        with self.assertRaises(PaymentProviderNotConfiguredError):
            create_payment(**create_payment_body(access_token=None))

    def test_without_payer_email_is_refused(self):
        with self.assertRaises(PaymentProviderNotConfiguredError):
            create_payment(**create_payment_body(payer_email=None))

    def test_unsupported_payment_method_is_refused(self):
        # Piloto so aceita pix. Cartao exige token gerado no frontend —
        # outra integracao, ainda nao existe.
        with self.assertRaises(PaymentProviderNotConfiguredError):
            create_payment(**create_payment_body(payment_method="credit_card"))

    def test_401_is_a_credential_error(self):
        fake_client = FakeHttpxClient(response=FakeResponse(401, {"message": "invalid token"}))

        with patch(HTTPX_CLIENT_PATH, return_value=fake_client):
            with self.assertRaises(PaymentGatewayCredentialError):
                create_payment(**create_payment_body())

    def test_403_is_a_credential_error(self):
        fake_client = FakeHttpxClient(response=FakeResponse(403, {"message": "forbidden"}))

        with patch(HTTPX_CLIENT_PATH, return_value=fake_client):
            with self.assertRaises(PaymentGatewayCredentialError):
                create_payment(**create_payment_body())

    def test_500_is_unavailable_not_a_credential_error(self):
        fake_client = FakeHttpxClient(response=FakeResponse(500, {"message": "internal error"}))

        with patch(HTTPX_CLIENT_PATH, return_value=fake_client):
            with self.assertRaises(PaymentGatewayUnavailableError):
                create_payment(**create_payment_body())

    def test_400_is_a_generic_gateway_error(self):
        # Requisicao NOSSA recusada (dado invalido) — nem credencial, nem
        # indisponibilidade, nem "nao encontrado".
        fake_client = FakeHttpxClient(response=FakeResponse(400, {"message": "bad request"}))

        with patch(HTTPX_CLIENT_PATH, return_value=fake_client):
            with self.assertRaises(PaymentGatewayError):
                create_payment(**create_payment_body())

    def test_400_error_message_does_not_leak_the_response_body(self):
        fake_client = FakeHttpxClient(
            response=FakeResponse(400, {"message": "cliente@exemplo.com invalido"})
        )

        with patch(HTTPX_CLIENT_PATH, return_value=fake_client):
            try:
                create_payment(**create_payment_body())
                self.fail("esperava PaymentGatewayError")
            except PaymentGatewayError as exc:
                self.assertNotIn("cliente@exemplo.com", str(exc))

    def test_timeout_is_unavailable(self):
        fake_client = FakeHttpxClient(exception=httpx.ConnectTimeout("timed out"))

        with patch(HTTPX_CLIENT_PATH, return_value=fake_client):
            with self.assertRaises(PaymentGatewayUnavailableError):
                create_payment(**create_payment_body())

    def test_network_error_is_unavailable(self):
        fake_client = FakeHttpxClient(exception=httpx.ConnectError("connection refused"))

        with patch(HTTPX_CLIENT_PATH, return_value=fake_client):
            with self.assertRaises(PaymentGatewayUnavailableError):
                create_payment(**create_payment_body())

    def test_non_json_response_is_unavailable(self):
        fake_client = FakeHttpxClient(response=FakeResponse(201, json_error=True))

        with patch(HTTPX_CLIENT_PATH, return_value=fake_client):
            with self.assertRaises(PaymentGatewayUnavailableError):
                create_payment(**create_payment_body())

    def test_access_token_is_never_in_the_raised_error_message(self):
        fake_client = FakeHttpxClient(response=FakeResponse(401, {"message": "invalid"}))

        with patch(HTTPX_CLIENT_PATH, return_value=fake_client):
            try:
                create_payment(**create_payment_body())
                self.fail("esperava PaymentGatewayCredentialError")
            except PaymentGatewayCredentialError as exc:
                self.assertNotIn(ACCESS_TOKEN, str(exc))


class IdempotencyKeyTests(unittest.TestCase):
    """Quando duas chamadas sao "a mesma cobranca" e quando nao sao.

    O header X-Idempotency-Key faz o Mercado Pago devolver a MESMA cobranca
    para a mesma chave. Isso protege um retry (timeout na ida, clique duplo)
    de virar duas cobrancas pix abertas — mas so vale como protecao se a
    chave se repetir EXATAMENTE nos casos em que a cobranca e a mesma.
    """

    def _key_for(self, **kwargs):
        fake_client = FakeHttpxClient(response=FakeResponse(201, {"id": 1}))
        with patch(HTTPX_CLIENT_PATH, return_value=fake_client):
            create_payment(**create_payment_body(**kwargs))
        return fake_client.requests[0]["headers"]["X-Idempotency-Key"]

    def test_same_charge_asked_twice_repeats_the_key(self):
        # O caso que a idempotencia existe para cobrir: retry do MESMO
        # pedido com o MESMO corpo devolve a mesma cobranca, nao uma segunda.
        order_id = uuid.uuid4()

        first = self._key_for(order_id=order_id)
        second = self._key_for(order_id=order_id)

        self.assertEqual(first, second)

    def test_key_is_scoped_to_the_order(self):
        first = self._key_for(order_id=uuid.uuid4())
        second = self._key_for(order_id=uuid.uuid4())

        self.assertNotEqual(first, second)

    def test_key_starts_with_the_order_id(self):
        # Para achar a tentativa no painel de "Atividade" deles a partir do
        # pedido, sem ter que recalcular hash nenhum.
        order_id = uuid.uuid4()

        self.assertTrue(self._key_for(order_id=order_id).startswith(f"{order_id}:"))

    def test_a_new_attempt_after_a_refused_charge_gets_a_new_key(self):
        # Cobranca recusada nao se retenta, se SUBSTITUI: reenviar a chave
        # da tentativa recusada devolveria a propria cobranca recusada de
        # volta, e o pedido nunca mais teria como ser pago.
        order_id = uuid.uuid4()

        first = self._key_for(order_id=order_id)
        second = self._key_for(order_id=order_id, previous_payment_id="mp-1")

        self.assertNotEqual(first, second)

    def test_each_refused_attempt_gets_its_own_key(self):
        order_id = uuid.uuid4()

        second = self._key_for(order_id=order_id, previous_payment_id="mp-1")
        third = self._key_for(order_id=order_id, previous_payment_id="mp-2")

        self.assertNotEqual(second, third)

    def test_a_changed_amount_gets_a_new_key(self):
        # Mesma chave com corpo diferente e CONFLITO de idempotencia, nao
        # retry — e conflito e uma das coisas que eles respondem com erro.
        order_id = uuid.uuid4()
        fake_client = FakeHttpxClient(response=FakeResponse(201, {"id": 1}))

        with patch(HTTPX_CLIENT_PATH, return_value=fake_client):
            create_payment(**create_payment_body(order_id=order_id))
            body = create_payment_body(order_id=order_id)
            body["amount"] = Decimal("120.00")
            create_payment(**body)

        first, second = (r["headers"]["X-Idempotency-Key"] for r in fake_client.requests)
        self.assertNotEqual(first, second)

    def test_a_changed_payer_email_gets_a_new_key(self):
        # O caso concreto: o cliente fez login entre uma tentativa e outra e
        # o payer.email deixou de ser o sintetico de convidado.
        order_id = uuid.uuid4()

        guest = self._key_for(order_id=order_id, payer_email="pedido-4321@pederapidex.com")
        logged_in = self._key_for(order_id=order_id, payer_email="cliente@exemplo.com")

        self.assertNotEqual(guest, logged_in)

    def test_the_key_never_carries_the_payer_email_in_the_clear(self):
        # O header vai para o gateway, mas tambem aparece em log de proxy e
        # em painel de observabilidade pelo caminho.
        key = self._key_for(payer_email="cliente@exemplo.com")

        self.assertNotIn("cliente@exemplo.com", key)


class ErrorLoggingTests(unittest.TestCase):
    """Um erro do Mercado Pago tem que ser DIAGNOSTICAVEL pelo nosso log.

    Antes daqui a linha de log tinha metodo, path, status e latencia — e
    "status=500" sozinho nao distingue instabilidade deles de `payer.email`
    recusado de chave de idempotencia em conflito. O codigo e a causa que
    eles mandam no corpo sao o que separa os tres.
    """

    def _call_and_capture_log(self, response):
        fake_client = FakeHttpxClient(response=response)
        with patch(HTTPX_CLIENT_PATH, return_value=fake_client):
            with self.assertLogs("uvicorn.error", level="WARNING") as captured:
                with self.assertRaises(PaymentGatewayError):
                    create_payment(**create_payment_body())
        return "\n".join(captured.output)

    def test_500_logs_the_code_the_message_and_the_cause(self):
        logged = self._call_and_capture_log(
            FakeResponse(
                500,
                {
                    "message": "internal server error",
                    "error": "internal_error",
                    "status": 500,
                    "cause": [{"code": 2062, "description": "invalid idempotency key"}],
                },
            )
        )

        self.assertIn("status=500", logged)
        self.assertIn("error=internal_error", logged)
        self.assertIn("code=2062", logged)
        self.assertIn("internal server error", logged)
        self.assertIn("invalid idempotency key", logged)

    def test_400_logs_the_cause_too(self):
        logged = self._call_and_capture_log(
            FakeResponse(
                400,
                {
                    "message": "bad request",
                    "error": "bad_request",
                    "cause": [{"code": 4051, "description": "payer.email must be valid"}],
                },
            )
        )

        self.assertIn("code=4051", logged)
        self.assertIn("payer.email must be valid", logged)

    def test_payer_email_is_masked_in_the_log(self):
        # Eles ecoam o valor recusado na mensagem. O e-mail do pagador e o
        # UNICO dado dele que mandamos — nao pode voltar para o log.
        logged = self._call_and_capture_log(
            FakeResponse(
                400,
                {
                    "message": "cliente@exemplo.com is not a valid email",
                    "error": "bad_request",
                    "cause": [{"code": 4051, "description": "payer.email cliente@exemplo.com invalid"}],
                },
            )
        )

        self.assertNotIn("cliente@exemplo.com", logged)
        self.assertIn("[email]", logged)
        # O que interessa depurar continua legivel.
        self.assertIn("code=4051", logged)
        self.assertIn("is not a valid email", logged)

    def test_payment_id_and_error_code_survive_the_masking(self):
        # Mascarar tudo que parece identificador levaria junto o id do
        # pagamento e o codigo do erro — que sao exatamente o que se le.
        logged = self._call_and_capture_log(
            FakeResponse(
                400,
                {
                    "message": "payment 123456789012 already exists",
                    "error": "bad_request",
                    "cause": [{"code": 2062, "description": "duplicated payment 123456789012"}],
                },
            )
        )

        self.assertIn("123456789012", logged)
        self.assertIn("code=2062", logged)

    def test_access_token_is_never_logged(self):
        logged = self._call_and_capture_log(
            FakeResponse(500, {"message": "internal error", "error": "internal_error"})
        )

        self.assertNotIn(ACCESS_TOKEN, logged)

    def test_very_long_description_is_truncated(self):
        logged = self._call_and_capture_log(
            FakeResponse(500, {"message": "x" * 5000, "error": "internal_error"})
        )

        self.assertNotIn("x" * 400, logged)
        self.assertIn("x" * 100, logged)

    def test_error_body_without_cause_still_logs_what_there_is(self):
        logged = self._call_and_capture_log(
            FakeResponse(500, {"message": "internal error", "error": "internal_error"})
        )

        self.assertIn("error=internal_error", logged)
        # Sem `cause`, o codigo especifico nao existe e o slug generico serve.
        self.assertIn("code=internal_error", logged)
        self.assertIn("cause=-", logged)

    def test_error_body_that_is_not_json_does_not_break_the_call(self):
        # Corpo de erro e justamente o que menos segue contrato. Nao poder
        # ler o motivo nao pode virar uma excecao diferente da devida.
        fake_client = FakeHttpxClient(response=FakeResponse(500, json_error=True))

        with patch(HTTPX_CLIENT_PATH, return_value=fake_client):
            with self.assertRaises(PaymentGatewayUnavailableError):
                create_payment(**create_payment_body())

    def test_error_body_that_is_a_list_does_not_break_the_call(self):
        fake_client = FakeHttpxClient(response=FakeResponse(400, ["erro"]))

        with patch(HTTPX_CLIENT_PATH, return_value=fake_client):
            with self.assertRaises(PaymentGatewayError):
                create_payment(**create_payment_body())

    def test_cause_as_a_single_object_is_read_the_same_way(self):
        logged = self._call_and_capture_log(
            FakeResponse(400, {"error": "bad_request", "cause": {"code": 3034, "description": "nope"}})
        )

        self.assertIn("code=3034", logged)

    def test_provider_error_code_is_carried_on_the_exception(self):
        # E o que o PaymentService mostra ao cliente como referencia: um
        # codigo do catalogo deles, nunca a mensagem crua.
        fake_client = FakeHttpxClient(
            response=FakeResponse(
                400,
                {"error": "bad_request", "cause": [{"code": 4051, "description": "payer.email invalid"}]},
            )
        )

        with patch(HTTPX_CLIENT_PATH, return_value=fake_client):
            try:
                create_payment(**create_payment_body())
                self.fail("esperava PaymentGatewayError")
            except PaymentGatewayError as exc:
                self.assertEqual(exc.provider_error_code, "4051")

    def test_provider_error_code_falls_back_to_the_generic_slug(self):
        fake_client = FakeHttpxClient(response=FakeResponse(500, {"error": "internal_error"}))

        with patch(HTTPX_CLIENT_PATH, return_value=fake_client):
            try:
                create_payment(**create_payment_body())
                self.fail("esperava PaymentGatewayUnavailableError")
            except PaymentGatewayUnavailableError as exc:
                self.assertEqual(exc.provider_error_code, "internal_error")

    def test_raised_message_never_carries_the_gateway_text(self):
        # A mensagem da excecao chega ao cliente. O texto deles pode ecoar o
        # e-mail do pagador; so o codigo pode atravessar.
        fake_client = FakeHttpxClient(
            response=FakeResponse(
                500,
                {"message": "cliente@exemplo.com recusado", "error": "internal_error"},
            )
        )

        with patch(HTTPX_CLIENT_PATH, return_value=fake_client):
            try:
                create_payment(**create_payment_body())
                self.fail("esperava PaymentGatewayUnavailableError")
            except PaymentGatewayUnavailableError as exc:
                self.assertNotIn("cliente@exemplo.com", str(exc))
                self.assertNotIn("recusado", str(exc))
                self.assertIn("internal_error", str(exc))


class ExtractProviderPaymentIdTests(unittest.TestCase):
    def test_reads_data_id_from_the_mercadopago_envelope(self):
        raw_body = json.dumps({"id": 999, "data": {"id": "123456789"}}).encode()

        self.assertEqual(
            extract_provider_payment_id(provider="mercadopago", raw_body=raw_body),
            "123456789",
        )

    def test_missing_data_id_is_a_payload_error(self):
        raw_body = json.dumps({"id": 999, "data": {}}).encode()

        with self.assertRaises(PaymentWebhookPayloadError):
            extract_provider_payment_id(provider="mercadopago", raw_body=raw_body)

    def test_malformed_json_is_a_payload_error(self):
        with self.assertRaises(PaymentWebhookPayloadError):
            extract_provider_payment_id(provider="mercadopago", raw_body=b"nao sou json")


class ParseWebhookEventMercadopagoTests(unittest.TestCase):
    def _envelope(self, event_id=999, data_id="123456789"):
        return json.dumps({"id": event_id, "data": {"id": data_id}}).encode()

    def test_approved_translates_to_paid_and_queries_the_right_url(self):
        fake_client = FakeHttpxClient(response=FakeResponse(200, {"status": "approved"}))

        with patch(HTTPX_CLIENT_PATH, return_value=fake_client):
            event = parse_webhook_event(
                provider="mercadopago", raw_body=self._envelope(), access_token=ACCESS_TOKEN
            )

        self.assertEqual(event.payment_status, "paid")
        self.assertEqual(event.provider_payment_id, "123456789")
        self.assertEqual(event.event_id, "999")
        self.assertEqual(event.raw_status, "approved")

        sent = fake_client.requests[0]
        self.assertEqual(sent["method"], "GET")
        self.assertEqual(sent["url"], "https://api.mercadopago.com/v1/payments/123456789")
        self.assertEqual(sent["headers"]["Authorization"], f"Bearer {ACCESS_TOKEN}")

    def test_rejected_translates_to_failed(self):
        fake_client = FakeHttpxClient(response=FakeResponse(200, {"status": "rejected"}))

        with patch(HTTPX_CLIENT_PATH, return_value=fake_client):
            event = parse_webhook_event(
                provider="mercadopago", raw_body=self._envelope(), access_token=ACCESS_TOKEN
            )

        self.assertEqual(event.payment_status, "failed")

    def test_cancelled_translates_to_failed(self):
        fake_client = FakeHttpxClient(response=FakeResponse(200, {"status": "cancelled"}))

        with patch(HTTPX_CLIENT_PATH, return_value=fake_client):
            event = parse_webhook_event(
                provider="mercadopago", raw_body=self._envelope(), access_token=ACCESS_TOKEN
            )

        self.assertEqual(event.payment_status, "failed")

    def test_refunded_translates_to_refunded(self):
        fake_client = FakeHttpxClient(response=FakeResponse(200, {"status": "refunded"}))

        with patch(HTTPX_CLIENT_PATH, return_value=fake_client):
            event = parse_webhook_event(
                provider="mercadopago", raw_body=self._envelope(), access_token=ACCESS_TOKEN
            )

        self.assertEqual(event.payment_status, "refunded")

    def test_charged_back_translates_to_refunded(self):
        fake_client = FakeHttpxClient(response=FakeResponse(200, {"status": "charged_back"}))

        with patch(HTTPX_CLIENT_PATH, return_value=fake_client):
            event = parse_webhook_event(
                provider="mercadopago", raw_body=self._envelope(), access_token=ACCESS_TOKEN
            )

        self.assertEqual(event.payment_status, "refunded")

    def test_pending_status_does_not_apply_any_change(self):
        # pix "pendente" (aguardando o pagador escanear e pagar): nao e
        # aprovado nem recusado, nao ha nada para o nosso lado aplicar
        # ainda. Ver PAYMENT_STATUSES — nao existe "pending" alcancavel a
        # partir de um webhook do Mercado Pago.
        fake_client = FakeHttpxClient(response=FakeResponse(200, {"status": "pending"}))

        with patch(HTTPX_CLIENT_PATH, return_value=fake_client):
            with self.assertRaises(PaymentWebhookPayloadError):
                parse_webhook_event(
                    provider="mercadopago", raw_body=self._envelope(), access_token=ACCESS_TOKEN
                )

    def test_in_process_status_does_not_apply_any_change(self):
        fake_client = FakeHttpxClient(response=FakeResponse(200, {"status": "in_process"}))

        with patch(HTTPX_CLIENT_PATH, return_value=fake_client):
            with self.assertRaises(PaymentWebhookPayloadError):
                parse_webhook_event(
                    provider="mercadopago", raw_body=self._envelope(), access_token=ACCESS_TOKEN
                )

    def test_unknown_status_does_not_apply_any_change(self):
        fake_client = FakeHttpxClient(response=FakeResponse(200, {"status": "algo_novo_que_nao_existe_hoje"}))

        with patch(HTTPX_CLIENT_PATH, return_value=fake_client):
            with self.assertRaises(PaymentWebhookPayloadError):
                parse_webhook_event(
                    provider="mercadopago", raw_body=self._envelope(), access_token=ACCESS_TOKEN
                )

    def test_without_access_token_is_refused_not_silently_accepted(self):
        with self.assertRaises(PaymentProviderNotConfiguredError):
            parse_webhook_event(provider="mercadopago", raw_body=self._envelope(), access_token=None)

    def test_404_is_a_not_found_error(self):
        fake_client = FakeHttpxClient(response=FakeResponse(404, {"message": "not found"}))

        with patch(HTTPX_CLIENT_PATH, return_value=fake_client):
            with self.assertRaises(PaymentNotFoundError):
                parse_webhook_event(
                    provider="mercadopago", raw_body=self._envelope(), access_token=ACCESS_TOKEN
                )

    def test_timeout_is_unavailable(self):
        fake_client = FakeHttpxClient(exception=httpx.ReadTimeout("timed out"))

        with patch(HTTPX_CLIENT_PATH, return_value=fake_client):
            with self.assertRaises(PaymentGatewayUnavailableError):
                parse_webhook_event(
                    provider="mercadopago", raw_body=self._envelope(), access_token=ACCESS_TOKEN
                )

    def test_envelope_without_event_id_is_a_payload_error(self):
        raw_body = json.dumps({"data": {"id": "123"}}).encode()

        with self.assertRaises(PaymentWebhookPayloadError):
            parse_webhook_event(provider="mercadopago", raw_body=raw_body, access_token=ACCESS_TOKEN)


class VerifyMercadopagoSignatureTests(unittest.TestCase):
    @staticmethod
    def _sign(data_id, request_id, ts, secret=WEBHOOK_SECRET):
        manifest = f"id:{data_id};request-id:{request_id};ts:{ts};"
        return hmac.new(secret.encode("utf-8"), manifest.encode("utf-8"), hashlib.sha256).hexdigest()

    def test_correctly_signed_notification_is_accepted(self):
        data_id = "123456789"
        request_id = "req-abc-123"
        ts = str(int(time.time()))
        v1 = self._sign(data_id, request_id, ts)
        raw_body = json.dumps({"data": {"id": data_id}}).encode()
        headers = {"x-signature": f"ts={ts},v1={v1}", "x-request-id": request_id}

        self.assertTrue(
            verify_webhook_signature(
                provider="mercadopago", raw_body=raw_body, headers=headers, secret=WEBHOOK_SECRET
            )
        )

    def test_header_names_are_case_insensitive(self):
        data_id = "123456789"
        request_id = "req-abc-123"
        ts = str(int(time.time()))
        v1 = self._sign(data_id, request_id, ts)
        raw_body = json.dumps({"data": {"id": data_id}}).encode()
        headers = {"X-Signature": f"ts={ts},v1={v1}", "X-Request-Id": request_id}

        self.assertTrue(
            verify_webhook_signature(
                provider="mercadopago", raw_body=raw_body, headers=headers, secret=WEBHOOK_SECRET
            )
        )

    def test_tampered_body_breaks_the_signature(self):
        data_id = "123456789"
        request_id = "req-abc-123"
        ts = str(int(time.time()))
        v1 = self._sign(data_id, request_id, ts)
        tampered_body = json.dumps({"data": {"id": "outro-payment-id"}}).encode()
        headers = {"x-signature": f"ts={ts},v1={v1}", "x-request-id": request_id}

        self.assertFalse(
            verify_webhook_signature(
                provider="mercadopago", raw_body=tampered_body, headers=headers, secret=WEBHOOK_SECRET
            )
        )

    def test_wrong_secret_breaks_the_signature(self):
        data_id = "123456789"
        request_id = "req-abc-123"
        ts = str(int(time.time()))
        v1 = self._sign(data_id, request_id, ts, secret="segredo-errado")
        raw_body = json.dumps({"data": {"id": data_id}}).encode()
        headers = {"x-signature": f"ts={ts},v1={v1}", "x-request-id": request_id}

        self.assertFalse(
            verify_webhook_signature(
                provider="mercadopago", raw_body=raw_body, headers=headers, secret=WEBHOOK_SECRET
            )
        )

    def test_stale_timestamp_is_rejected(self):
        data_id = "123456789"
        request_id = "req-abc-123"
        ts = str(int(time.time()) - 301)  # 5 minutos e 1 segundo atras
        v1 = self._sign(data_id, request_id, ts)
        raw_body = json.dumps({"data": {"id": data_id}}).encode()
        headers = {"x-signature": f"ts={ts},v1={v1}", "x-request-id": request_id}

        self.assertFalse(
            verify_webhook_signature(
                provider="mercadopago", raw_body=raw_body, headers=headers, secret=WEBHOOK_SECRET
            )
        )

    def test_timestamp_just_inside_the_window_is_accepted(self):
        data_id = "123456789"
        request_id = "req-abc-123"
        ts = str(int(time.time()) - 299)
        v1 = self._sign(data_id, request_id, ts)
        raw_body = json.dumps({"data": {"id": data_id}}).encode()
        headers = {"x-signature": f"ts={ts},v1={v1}", "x-request-id": request_id}

        self.assertTrue(
            verify_webhook_signature(
                provider="mercadopago", raw_body=raw_body, headers=headers, secret=WEBHOOK_SECRET
            )
        )

    def test_missing_signature_header_returns_false(self):
        raw_body = json.dumps({"data": {"id": "1"}}).encode()

        self.assertFalse(
            verify_webhook_signature(
                provider="mercadopago",
                raw_body=raw_body,
                headers={"x-request-id": "req-1"},
                secret=WEBHOOK_SECRET,
            )
        )

    def test_missing_request_id_header_returns_false(self):
        ts = str(int(time.time()))
        raw_body = json.dumps({"data": {"id": "1"}}).encode()

        self.assertFalse(
            verify_webhook_signature(
                provider="mercadopago",
                raw_body=raw_body,
                headers={"x-signature": f"ts={ts},v1=qualquercoisa"},
                secret=WEBHOOK_SECRET,
            )
        )

    def test_malformed_body_returns_false_not_an_exception(self):
        ts = str(int(time.time()))
        headers = {"x-signature": f"ts={ts},v1=qualquercoisa", "x-request-id": "req-1"}

        self.assertFalse(
            verify_webhook_signature(
                provider="mercadopago",
                raw_body=b"nao sou json",
                headers=headers,
                secret=WEBHOOK_SECRET,
            )
        )

    def test_without_secret_raises_not_configured(self):
        ts = str(int(time.time()))
        raw_body = json.dumps({"data": {"id": "1"}}).encode()
        headers = {"x-signature": f"ts={ts},v1=qualquercoisa", "x-request-id": "req-1"}

        with self.assertRaises(PaymentProviderNotConfiguredError):
            verify_webhook_signature(
                provider="mercadopago", raw_body=raw_body, headers=headers, secret=None
            )


if __name__ == "__main__":
    unittest.main()
