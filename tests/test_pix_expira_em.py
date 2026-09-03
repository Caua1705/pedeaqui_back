"""O prazo do pix sai do gateway, e a resposta o publica.

O app contava pelo relogio do cliente porque `StartPaymentResponse` nao
dizia quando o QR morre. Quatro coisas que estes testes protegem:

1. **O corpo manda o prazo, e so no pix.** Cartao nao tem QR para expirar.
2. **O prazo NAO entra na chave de idempotencia.** Ele muda a cada segundo;
   se entrasse no hash, o segundo clique em "pagar" abriria um segundo pix
   em vez de devolver o mesmo — a propriedade da armadilha 6.
3. **A resposta do gateway e a fonte.** O que volta e o `date_of_expiration`
   da cobranca, nao o relogio daqui — e o segundo clique devolve a MESMA
   cobranca com o MESMO prazo, entao o contador do app nao reinicia.
4. **Sandbox tambem responde**, para o fluxo inteiro valer sem o Mercado Pago.
"""

import unittest
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

from src.core.config import settings
from src.integrations.payment_gateway import PaymentIntent, create_payment
from src.schemas.payment_schema import StartPaymentResponse
from tests.test_mercadopago_gateway import (
    ACCESS_TOKEN,
    HTTPX_CLIENT_PATH,
    FakeHttpxClient,
    FakeResponse,
)
from tests.test_payments import build_service, make_order


PRAZO = datetime(2026, 9, 3, 22, 30, tzinfo=timezone.utc)
RESPOSTA_PIX = {
    "id": 42,
    "status": "pending",
    "date_of_expiration": "2026-09-03T19:30:00.000-03:00",
    "point_of_interaction": {"transaction_data": {"qr_code": "0002", "ticket_url": "https://x"}},
}


def _criar(fake_client, **extras):
    campos = dict(
        provider="mercadopago",
        order_id=uuid.uuid4(),
        amount=Decimal("93.00"),
        payment_method="pix",
        description="Pedido #1",
        access_token=ACCESS_TOKEN,
        payer_email="cliente@exemplo.com",
        pix_expires_at=PRAZO,
    )
    campos.update(extras)
    with patch(HTTPX_CLIENT_PATH, return_value=fake_client):
        return create_payment(**campos)


class TestOCorpo(unittest.TestCase):
    def test_o_pix_manda_o_prazo_no_formato_deles_e_no_fuso_da_operacao(self):
        fake_client = FakeHttpxClient(response=FakeResponse(201, RESPOSTA_PIX))

        _criar(fake_client)

        enviado = fake_client.requests[0]["json"]
        self.assertEqual(enviado["date_of_expiration"], "2026-09-03T19:30:00.000-03:00")

    def test_sem_prazo_o_corpo_nao_manda_o_campo(self):
        """Mandar `null` nao e "sem prazo" para eles: e corpo invalido."""
        fake_client = FakeHttpxClient(response=FakeResponse(201, RESPOSTA_PIX))

        _criar(fake_client, pix_expires_at=None)

        self.assertNotIn("date_of_expiration", fake_client.requests[0]["json"])


class TestAChave(unittest.TestCase):
    def _chave(self, order_id, prazo):
        fake_client = FakeHttpxClient(response=FakeResponse(201, RESPOSTA_PIX))
        _criar(fake_client, order_id=order_id, pix_expires_at=prazo)
        return fake_client.requests[0]["headers"]["X-Idempotency-Key"]

    def test_prazos_diferentes_repetem_a_chave(self):
        """O segundo clique chega alguns segundos depois, com outro prazo. A
        chave tem que ser a mesma, senao nasce um segundo pix."""
        order_id = uuid.uuid4()

        primeira = self._chave(order_id, PRAZO)
        segunda = self._chave(order_id, PRAZO + timedelta(seconds=7))

        self.assertEqual(primeira, segunda)

    def test_e_a_chave_continua_mudando_com_o_corpo(self):
        """A metade que impede o teste acima de passar por vacuidade: o hash
        ainda le o corpo — so o prazo saiu dele."""
        order_id = uuid.uuid4()
        fake_client = FakeHttpxClient(response=FakeResponse(201, RESPOSTA_PIX))
        _criar(fake_client, order_id=order_id, amount=Decimal("93.00"))
        _criar(fake_client, order_id=order_id, amount=Decimal("94.00"))

        chaves = [r["headers"]["X-Idempotency-Key"] for r in fake_client.requests]
        self.assertNotEqual(chaves[0], chaves[1])


class TestAResposta(unittest.TestCase):
    def test_le_o_prazo_da_resposta_do_gateway(self):
        intent = _criar(FakeHttpxClient(response=FakeResponse(201, RESPOSTA_PIX)))

        self.assertEqual(intent.expires_at, PRAZO)
        self.assertIsNotNone(intent.expires_at.tzinfo)

    def test_resposta_sem_prazo_nao_quebra(self):
        intent = _criar(FakeHttpxClient(response=FakeResponse(201, {"id": 1, "status": "pending"})))

        self.assertIsNone(intent.expires_at)

    def test_o_sandbox_devolve_o_prazo_que_recebeu(self):
        intent = create_payment(
            provider="sandbox",
            order_id=uuid.uuid4(),
            amount=Decimal("10"),
            payment_method="pix",
            description="x",
            pix_expires_at=PRAZO,
        )

        self.assertEqual(intent.expires_at, PRAZO)


class TestARota(unittest.TestCase):
    def test_o_pix_sai_com_expires_at_a_partir_da_configuracao(self):
        order = make_order()
        service = build_service(order)
        antes = datetime.now(timezone.utc)

        with patch.object(settings, "PAYMENT_PROVIDER", "sandbox"), patch.object(
            settings, "PIX_EXPIRATION_MINUTES", 20
        ):
            response = service.start_online_payment("junior", "token-do-pedido")

        self.assertIsNotNone(response.expires_at)
        esperado = antes + timedelta(minutes=20)
        self.assertLess(abs((response.expires_at - esperado).total_seconds()), 60)

    def test_o_cartao_nao_manda_prazo_ao_gateway(self):
        """Cartao nao tem QR para expirar: o gateway recebe `None` e a
        resposta sai sem `expires_at`."""
        service = build_service(make_order(payment_method="credit_card"))
        intent = PaymentIntent(
            provider="mercadopago", provider_payment_id="1", payment_status="paid", expires_at=None
        )

        with patch("src.services.payment_service.create_payment", return_value=intent) as mocked:
            devolvido = service._create_payment_at_gateway(
                order_id=uuid.uuid4(),
                amount=Decimal("50"),
                payment_method="credit_card",
                order_number=1,
                description="x",
                access_token="t",
            )

        self.assertIsNone(devolvido.expires_at)
        self.assertIsNone(mocked.call_args.kwargs["pix_expires_at"])

    def test_o_campo_existe_no_contrato_com_default_nulo(self):
        response = StartPaymentResponse(provider="sandbox", provider_payment_id="1", payment_status="pending")

        self.assertIsNone(response.expires_at)
