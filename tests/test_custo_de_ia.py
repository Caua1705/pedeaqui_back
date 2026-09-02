"""O custo de IA por restaurante: a conta, a rota e o que ela NAO publica.

A suite rapida cobre a aritmetica e a porta da rota. A metade que fala com o
Postgres esta em `tests/test_custo_de_ia_db.py` — o UNIQUE que torna a
gravacao da voz idempotente e a agregacao por periodo SAO consultas, e dublar
qualquer uma seria testar o dublê.
"""

import unittest
from decimal import Decimal
from types import SimpleNamespace

from fastapi.testclient import TestClient

from main import app
from src.ai.custo import custo_de_texto, custo_de_voz
from src.ai.services.chat_llm_service import ChatLLMService
from src.core.config import settings
from src.services.ai_usage_service import AIUsageService


class CustoDeTextoTests(unittest.TestCase):
    def test_a_conta_de_um_turno_sem_cache(self):
        # gpt-5-mini: US$ 0,25/1M na entrada e US$ 2,00/1M na saida.
        # 1000 entrada = 0,00025 ; 500 saida = 0,001 ; total 0,00125
        custo = custo_de_texto(
            modelo="gpt-5-mini", entrada=1000, entrada_em_cache=0, saida=500
        )
        self.assertEqual(custo, Decimal("0.001250"))

    def test_o_cache_e_subtraido_da_entrada_e_nao_somado_a_ela(self):
        """A conta que erra em dobro se `cached` for lido como parcela extra.

        800 de 1000 vieram do cache. O certo e 200 a US$ 0,25 e 800 a
        US$ 0,025 — nunca 1000 a US$ 0,25 MAIS 800 a US$ 0,025.
        """
        custo = custo_de_texto(
            modelo="gpt-5-mini", entrada=1000, entrada_em_cache=800, saida=0
        )
        esperado = (Decimal(200) * Decimal("0.25") + Decimal(800) * Decimal("0.025")) / Decimal(
            1000000
        )
        self.assertEqual(custo, esperado.quantize(Decimal("0.000001")))
        self.assertLess(custo, custo_de_texto("gpt-5-mini", 1000, 0, 0))

    def test_modelo_desconhecido_custa_None_e_nao_zero(self):
        """Zero e um numero que SOMA.

        Um modelo novo no `.env` sem linha na tabela de precos faria o
        restaurante inteiro aparecer de graca no relatorio que existe para
        dizer quanto ele custa. `None` propaga ate a coluna e a rota o conta
        em `calls_without_price`.
        """
        self.assertIsNone(custo_de_texto("gpt-6-turbo-imaginario", 1000, 0, 500))


class CustoDeVozTests(unittest.TestCase):
    def test_audio_e_texto_sao_cobrados_em_faixas_diferentes(self):
        """1000 tokens de audio de saida nao custam o mesmo que 1000 de texto.

        No `gpt-realtime-mini` sao US$ 20,00 contra US$ 2,40 por milhao — se
        esta conta somasse tudo numa faixa so, a voz sairia oito vezes mais
        barata do que e.
        """
        so_audio = custo_de_voz(
            modelo="gpt-realtime-mini",
            entrada_audio=0,
            entrada_texto=0,
            entrada_em_cache=0,
            saida_audio=1000,
            saida_texto=0,
        )
        so_texto = custo_de_voz(
            modelo="gpt-realtime-mini",
            entrada_audio=0,
            entrada_texto=0,
            entrada_em_cache=0,
            saida_audio=0,
            saida_texto=1000,
        )
        self.assertEqual(so_audio, Decimal("0.020000"))
        self.assertEqual(so_texto, Decimal("0.002400"))

    def test_o_cache_sai_do_audio_primeiro(self):
        """A escolha documentada em `custo_de_voz`, travada aqui.

        A Realtime manda um `cached_tokens` so, sem dizer se era audio ou
        texto. Numa conversa falada o audio e a quase totalidade da entrada,
        entao o desconto sai dele primeiro.
        """
        custo = custo_de_voz(
            modelo="gpt-realtime-mini",
            entrada_audio=1000,
            entrada_texto=1000,
            entrada_em_cache=1000,
            saida_audio=0,
            saida_texto=0,
        )
        # 1000 de audio em cache (US$ 0,30/1M) + 1000 de texto cheio
        # (US$ 0,60/1M) = 0,0003 + 0,0006
        self.assertEqual(custo, Decimal("0.000900"))

    def test_cache_maior_que_o_audio_transborda_para_o_texto(self):
        custo = custo_de_voz(
            modelo="gpt-realtime-mini",
            entrada_audio=100,
            entrada_texto=100,
            entrada_em_cache=150,
            saida_audio=0,
            saida_texto=0,
        )
        # 100 de audio em cache + 50 de texto em cache + 50 de texto cheio
        esperado = (
            Decimal(100) * Decimal("0.30")
            + Decimal(50) * Decimal("0.06")
            + Decimal(50) * Decimal("0.60")
        ) / Decimal(1000000)
        self.assertEqual(custo, esperado.quantize(Decimal("0.000001")))

    def test_modelo_desconhecido_custa_None_e_nao_zero(self):
        self.assertIsNone(custo_de_voz("gpt-realtime-imaginario", 1, 1, 0, 1, 1))


class UsoDaRespostaTests(unittest.TestCase):
    """O `usage_metadata` virando os tres numeros que a conta multiplica."""

    def test_le_entrada_cache_e_saida(self):
        raw = SimpleNamespace(
            usage_metadata={
                "input_tokens": 1200,
                "output_tokens": 300,
                "input_token_details": {"cache_read": 1024},
            }
        )
        uso = ChatLLMService()._uso_da_resposta(raw)

        self.assertEqual(uso.entrada, 1200)
        self.assertEqual(uso.entrada_em_cache, 1024)
        self.assertEqual(uso.saida, 300)
        self.assertEqual(uso.modelo, settings.MODEL_NAME)

    def test_sem_usage_devolve_None_em_vez_de_zeros(self):
        """Zeros seriam uma chamada de graca gravada no livro-razao."""
        self.assertIsNone(ChatLLMService()._uso_da_resposta(SimpleNamespace()))

    def test_formato_inesperado_nao_levanta(self):
        """Medir nao pode derrubar o que esta sendo medido."""
        self.assertIsNone(ChatLLMService()._uso_da_resposta(SimpleNamespace(usage_metadata=7)))


class RegistrarTextoSemUsoTests(unittest.TestCase):
    def test_uso_nulo_nao_toca_no_banco(self):
        """Turno sem `usage` nao vira linha — e nao vira excecao tambem.

        O `db` aqui e um dublê de COLABORADOR: qualquer acesso a ele levanta,
        que e como este teste prova que nada foi escrito.
        """

        class BancoQueRecusa:
            def __getattr__(self, nome):
                raise AssertionError(f"o service tocou no banco: {nome}")

        servico = AIUsageService(BancoQueRecusa())
        servico.registrar_texto(restaurant_id=None, branch_id=None, uso=None)


class RotaInternaTests(unittest.TestCase):
    """A porta de `GET /internal/ai-usage`, e o que ela nao publica."""

    ROTA = "/internal/ai-usage"

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def test_a_rota_nao_aparece_no_openapi(self):
        """O painel consome o documento (armadilha 16).

        Quanto o assistente custa a PLATAFORMA nao e assunto do lojista — e o
        mesmo raciocinio que mantem `platform_commission_percent` fora de todo
        schema do painel (armadilha 17). Publicar aqui a poria na mesa de
        negociacao da comissao.
        """
        caminhos = app.openapi()["paths"]
        self.assertNotIn(self.ROTA, caminhos)

    def test_sem_chave_configurada_responde_503(self):
        """503 e nao 401: nao ha chave certa a procurar, o conserto e no .env."""
        original = settings.PLATFORM_METRICS_KEY
        settings.PLATFORM_METRICS_KEY = None
        try:
            resposta = self.client.get(self.ROTA, headers={"X-Internal-Key": "seja-o-que-for"})
            self.assertEqual(resposta.status_code, 503)
        finally:
            settings.PLATFORM_METRICS_KEY = original

    def test_chave_errada_responde_401(self):
        original = settings.PLATFORM_METRICS_KEY
        settings.PLATFORM_METRICS_KEY = "a-chave-certa"
        try:
            self.assertEqual(
                self.client.get(self.ROTA, headers={"X-Internal-Key": "a-errada"}).status_code,
                401,
            )
            # Sem cabecalho nenhum tambem e 401, e nao 422: a chave e da
            # porta, nao do corpo.
            self.assertEqual(self.client.get(self.ROTA).status_code, 401)
        finally:
            settings.PLATFORM_METRICS_KEY = original
