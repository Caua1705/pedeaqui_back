"""A contagem de avisos de WhatsApp por restaurante: a porta e o que ela nao publica.

A metade que fala com o Postgres esta em `tests/test_uso_de_whatsapp_db.py` — a
agregacao E uma consulta (`GROUP BY` com tres `FILTER` e janela meio-aberta), e
dublar isso seria testar o dublê.

O que se prova aqui e o que nao depende de banco: a rota fica FORA do
`/openapi.json` e a chave da plataforma e a mesma de `/ai-usage`.
"""

import unittest

from fastapi.testclient import TestClient

from main import app
from src.api.endpoints import internal_metrics
from src.core.config import settings


class RotaInternaDoWhatsAppTests(unittest.TestCase):
    ROTA = "/internal/whatsapp-usage"

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def test_a_rota_nao_aparece_no_openapi(self):
        """O painel consome o documento (armadilha 16).

        Quanto cada loja consome do cartao da PLATAFORMA e numero de quem paga
        a fatura, e nao do lojista — o mesmo raciocinio de `/ai-usage` e de
        `platform_commission_percent` (armadilha 17).
        """
        self.assertNotIn(self.ROTA, app.openapi()["paths"])

    def test_sem_chave_configurada_responde_503(self):
        """503 e nao 401: nao ha chave certa a procurar, o conserto e no .env."""
        original = settings.PLATFORM_METRICS_KEY
        settings.PLATFORM_METRICS_KEY = None
        try:
            resposta = self.client.get(
                self.ROTA, headers={"X-Internal-Key": "seja-o-que-for"}
            )
            self.assertEqual(resposta.status_code, 503)
        finally:
            settings.PLATFORM_METRICS_KEY = original

    def test_chave_errada_responde_401(self):
        original = settings.PLATFORM_METRICS_KEY
        settings.PLATFORM_METRICS_KEY = "a-chave-certa"
        try:
            self.assertEqual(
                self.client.get(
                    self.ROTA, headers={"X-Internal-Key": "a-errada"}
                ).status_code,
                401,
            )
            self.assertEqual(self.client.get(self.ROTA).status_code, 401)
        finally:
            settings.PLATFORM_METRICS_KEY = original

    def test_a_porta_e_a_MESMA_funcao_de_ai_usage(self):
        """Publico igual, segredo igual — e isso e a armadilha 32 lida direito.

        Ela diz "segredo novo por publico NOVO". O cartao da Meta e o da OpenAI
        sao os dois da plataforma, cobrados por conta e lidos pela mesma pessoa;
        uma segunda chave aqui seria uma credencial a mais para vazar sem
        separar nada.

        A afirmacao e sobre a FUNCAO da porta, e nao sobre duas chamadas darem o
        mesmo codigo: duas dependencias diferentes que hoje leem a mesma
        variavel passariam nisso e divergiriam no dia em que uma delas mudasse.
        """
        portas = {
            rota.path: {declarada.dependency for declarada in rota.dependencies}
            for rota in internal_metrics.router.routes
        }

        self.assertEqual(
            set(portas),
            {"/internal/whatsapp-usage", "/internal/ai-usage"},
            portas,
        )
        self.assertEqual(
            portas["/internal/whatsapp-usage"],
            portas["/internal/ai-usage"],
            "as duas rotas internas deixaram de compartilhar a porta",
        )
        self.assertEqual(
            portas["/internal/ai-usage"],
            {internal_metrics.exigir_chave_da_plataforma},
        )

    def test_start_date_depois_de_end_date_e_400(self):
        original = settings.PLATFORM_METRICS_KEY
        settings.PLATFORM_METRICS_KEY = "a-chave-da-plataforma"
        try:
            resposta = self.client.get(
                self.ROTA,
                headers={"X-Internal-Key": "a-chave-da-plataforma"},
                params={"start_date": "2026-09-30", "end_date": "2026-09-01"},
            )
            self.assertEqual(resposta.status_code, 400)
        finally:
            settings.PLATFORM_METRICS_KEY = original
