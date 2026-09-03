"""A camada HTTP do webhook: a verificação da Meta e o POST.

Contra o `main.app` de verdade, e não contra um `FastAPI()` montado aqui: o
que se confere é que a rota está registrada e que o transporte faz o que a
Meta espera — o `hub.challenge` de volta como TEXTO PURO, e não como JSON.

**O `GET` é o passo que trava o piloto se estiver errado**, e ele acontece uma
vez só: a Meta faz a chamada no instante em que se clica em "Verificar e
salvar" no painel. Se a API responder outra coisa, o painel só diz que não
conseguiu validar — sem dizer o quê. Por isso os três casos estão aqui:
token certo, token errado e variável ausente.
"""

import hashlib
import hmac
import json
import unittest
from types import SimpleNamespace

from src.core.config import settings


APP_SECRET = "segredo-do-app-da-meta"
VERIFY_TOKEN = "token-que-eu-inventei"


class WebhookDoWhatsAppTests(unittest.TestCase):
    def setUp(self) -> None:
        from fastapi.testclient import TestClient

        from main import app
        from src.api.dependencies.database import get_db

        self.app = app
        self.app.dependency_overrides[get_db] = lambda: None
        self.client = TestClient(app)

        self._original_secret = settings.WHATSAPP_APP_SECRET
        self._original_verify = settings.WHATSAPP_WEBHOOK_VERIFY_TOKEN
        settings.WHATSAPP_APP_SECRET = APP_SECRET
        settings.WHATSAPP_WEBHOOK_VERIFY_TOKEN = VERIFY_TOKEN

    def tearDown(self) -> None:
        self.app.dependency_overrides.clear()
        settings.WHATSAPP_APP_SECRET = self._original_secret
        settings.WHATSAPP_WEBHOOK_VERIFY_TOKEN = self._original_verify

    def _responder_com(self, resultado: dict) -> None:
        """Substitui o service inteiro: esta classe testa transporte, não regra."""
        from src.api.endpoints import whatsapp as rota

        original = rota.WhatsAppWebhookService
        rota.WhatsAppWebhookService = lambda db: SimpleNamespace(
            handle=lambda **kwargs: resultado
        )
        self.addCleanup(setattr, rota, "WhatsAppWebhookService", original)

    def test_a_rota_esta_registrada(self) -> None:
        from tests.rotas_do_app import caminhos

        self.assertIn("/webhooks/whatsapp", caminhos())

    # --- GET: a verificação que a Meta faz uma vez, no clique do painel ---

    def test_o_challenge_volta_como_texto_puro(self) -> None:
        resposta = self.client.get(
            "/webhooks/whatsapp",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": VERIFY_TOKEN,
                "hub.challenge": "1158201444",
            },
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(resposta.text, "1158201444")
        self.assertTrue(resposta.headers["content-type"].startswith("text/plain"))

    def test_token_errado_responde_403(self) -> None:
        resposta = self.client.get(
            "/webhooks/whatsapp",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": "token-de-outra-pessoa",
                "hub.challenge": "1158201444",
            },
        )

        self.assertEqual(resposta.status_code, 403)

    def test_sem_a_variavel_configurada_responde_503(self) -> None:
        """503 e não 403: o problema é nosso, e a diferença é o que eu leio no
        log quando o painel da Meta disser só "não foi possível validar"."""
        settings.WHATSAPP_WEBHOOK_VERIFY_TOKEN = None

        resposta = self.client.get(
            "/webhooks/whatsapp",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": VERIFY_TOKEN,
                "hub.challenge": "1158201444",
            },
        )

        self.assertEqual(resposta.status_code, 503)

    def test_modo_diferente_de_subscribe_responde_403(self) -> None:
        resposta = self.client.get(
            "/webhooks/whatsapp",
            params={
                "hub.mode": "unsubscribe",
                "hub.verify_token": VERIFY_TOKEN,
                "hub.challenge": "1158201444",
            },
        )

        self.assertEqual(resposta.status_code, 403)

    # --- POST: o corpo assinado ---

    def test_post_assinado_chega_ao_service(self) -> None:
        self._responder_com({"status": "ok"})
        corpo = json.dumps({"object": "whatsapp_business_account", "entry": []}).encode()
        digest = hmac.new(APP_SECRET.encode(), corpo, hashlib.sha256).hexdigest()

        resposta = self.client.post(
            "/webhooks/whatsapp",
            content=corpo,
            headers={
                "content-type": "application/json",
                "x-hub-signature-256": f"sha256={digest}",
            },
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(resposta.json(), {"status": "ok", "reason": None})

    def test_post_sem_assinatura_e_recusado_pelo_service(self) -> None:
        """O service é quem recusa, e não a rota: a assinatura é sobre os
        BYTES CRUS, e quem os tem é ele."""
        corpo = json.dumps({"object": "whatsapp_business_account", "entry": []}).encode()

        resposta = self.client.post(
            "/webhooks/whatsapp",
            content=corpo,
            headers={"content-type": "application/json"},
        )

        self.assertEqual(resposta.status_code, 401)

    def test_o_corpo_cru_chega_ao_service_byte_a_byte(self) -> None:
        """Reserializar o JSON muda espaços e ordem de chaves e derruba a
        assinatura. O que a rota entrega tem que ser o que a Meta enviou."""
        from src.api.endpoints import whatsapp as rota

        recebido: dict = {}
        original = rota.WhatsAppWebhookService
        rota.WhatsAppWebhookService = lambda db: SimpleNamespace(
            handle=lambda **kwargs: recebido.update(kwargs) or {"status": "ok"}
        )
        self.addCleanup(setattr, rota, "WhatsAppWebhookService", original)

        corpo = b'{"object": "whatsapp_business_account",   "entry": []}'
        digest = hmac.new(APP_SECRET.encode(), corpo, hashlib.sha256).hexdigest()

        self.client.post(
            "/webhooks/whatsapp",
            content=corpo,
            headers={
                "content-type": "application/json",
                "x-hub-signature-256": f"sha256={digest}",
            },
        )

        self.assertEqual(recebido["raw_body"], corpo)
