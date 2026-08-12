"""O rate limit com o storage fora do ar.

O caso real: `REDIS_URL` apontando para um Redis que recusa autenticacao. Todo
endpoint com `@limiter.limit` respondeu 500 — nao 200 sem limite, nao 429:
500. A falha de um componente auxiliar derrubou as rotas publicas inteiras.

A mecanica, que e o que estes testes travam:

    Limiter.__evaluate_limits()
        ...
        self.limiter.hit(...)              <- levanta aqui
        request.state.view_rate_limit = x  <- nunca chega a executar

    wrapper do @limiter.limit, depois da rota responder:
        self._inject_headers(response, request.state.view_rate_limit)
                                       ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
                                       AttributeError -> 500

`swallow_errors=True` sozinho NAO resolve: ele engole a excecao do `hit` e
deixa a requisicao seguir sem limite, mas o atributo continua faltando e o
500 acontece do mesmo jeito. Quem resolve e `in_memory_fallback_enabled`,
que reavalia o mesmo limite contra MemoryStorage — o `view_rate_limit` volta
a ser gravado e o limite continua valendo.
"""

import unittest

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from limits.storage import MemoryStorage
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded

from src.api.middleware.rate_limit_state import RateLimitStateMiddleware
from src.api.rate_limit import SHARED_BUCKET_KEY, client_ip, rate_limit_exceeded_handler


class StorageQuebrado(MemoryStorage):
    """Storage que levanta em toda escrita, como o Redis com NOAUTH.

    Herda de MemoryStorage para nao ter que implementar a interface inteira —
    o que importa e que `incr` (o caminho do `hit`) levante.
    """

    def incr(self, *args, **kwargs):
        raise ConnectionError("NOAUTH Authentication required.")

    def get(self, *args, **kwargs):
        raise ConnectionError("NOAUTH Authentication required.")

    def check(self) -> bool:
        return False


def _app_com(limiter: Limiter) -> FastAPI:
    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)
    app.add_middleware(RateLimitStateMiddleware)

    @app.get("/limitada")
    @limiter.limit("3/minute")
    def limitada(request: Request) -> dict:
        return {"ok": True}

    return app


def _limiter(storage_uri: str = "memory://", **kwargs) -> Limiter:
    return Limiter(key_func=lambda: "cliente-fixo", storage_uri=storage_uri, **kwargs)


class DegradacaoDoStorageTests(unittest.TestCase):
    def test_storage_que_levanta_nao_derruba_a_rota(self):
        """O 500 do dia 12/08 nao pode voltar."""
        limiter = _limiter(in_memory_fallback_enabled=True, swallow_errors=True)
        limiter._storage = StorageQuebrado()
        limiter._limiter.storage = StorageQuebrado()

        with TestClient(_app_com(limiter)) as client:
            resposta = client.get("/limitada")

        self.assertEqual(resposta.status_code, 200, resposta.text)
        self.assertEqual(resposta.json(), {"ok": True})

    def test_o_limite_continua_valendo_em_memoria(self):
        """Degradar e cair para memoria, nao desligar o limite.

        E a diferenca entre este teste e o anterior: 200 sozinho tambem
        aconteceria se o limite tivesse simplesmente sumido.
        """
        limiter = _limiter(in_memory_fallback_enabled=True, swallow_errors=True)
        limiter._storage = StorageQuebrado()
        limiter._limiter.storage = StorageQuebrado()

        with TestClient(_app_com(limiter)) as client:
            status = [client.get("/limitada").status_code for _ in range(6)]

        self.assertEqual(status[:3], [200, 200, 200], status)
        self.assertEqual(status[3:], [429, 429, 429], status)

    def test_o_429_da_degradacao_sai_com_corpo_json_e_nao_500(self):
        limiter = _limiter(in_memory_fallback_enabled=True, swallow_errors=True)
        limiter._storage = StorageQuebrado()
        limiter._limiter.storage = StorageQuebrado()

        with TestClient(_app_com(limiter)) as client:
            for _ in range(3):
                client.get("/limitada")
            resposta = client.get("/limitada")

        self.assertEqual(resposta.status_code, 429, resposta.text)
        self.assertIn("detail", resposta.json())

    def test_sem_o_fallback_a_rota_quebraria(self):
        """Caracteriza o defeito, para o `in_memory_fallback_enabled` nao ser
        removido por parecer supérfluo.

        So `swallow_errors`: a excecao do `hit` e engolida, a requisicao
        segue, e o wrapper estoura ao ler `view_rate_limit`. O middleware do
        projeto e deixado de fora justamente para o defeito aparecer.

        A excecao e conferida pelo TIPO e pela MENSAGEM: um 500 generico
        passaria por qualquer outro motivo, e este teste so vale se falhar
        exatamente pelo que derrubou producao.
        """
        limiter = _limiter(in_memory_fallback_enabled=False, swallow_errors=True)
        limiter._storage = StorageQuebrado()
        limiter._limiter.storage = StorageQuebrado()

        app = FastAPI()
        app.state.limiter = limiter

        @app.get("/limitada")
        @limiter.limit("3/minute")
        def limitada(request: Request) -> dict:
            return {"ok": True}

        with TestClient(app) as client:
            with self.assertRaises(AttributeError) as capturada:
                client.get("/limitada")

        self.assertIn("view_rate_limit", str(capturada.exception))

    def test_o_middleware_sozinho_evita_o_500_mesmo_sem_fallback(self):
        """Segunda linha de defesa: sem limite, mas sem derrubar.

        Cobre o caminho em que ate a memoria falhar. O limite deixa de valer
        (nao ha onde contar), e a escolha e responder em vez de dar 500.
        """
        limiter = _limiter(in_memory_fallback_enabled=False, swallow_errors=True)
        limiter._storage = StorageQuebrado()
        limiter._limiter.storage = StorageQuebrado()

        with TestClient(_app_com(limiter)) as client:
            resposta = client.get("/limitada")

        self.assertEqual(resposta.status_code, 200, resposta.text)


class ChaveDoBaldeTests(unittest.TestCase):
    """`client_ip` nunca pode aceitar o que o cliente escolheu."""

    @staticmethod
    def _request(headers: dict, client_host: str = "203.0.113.9") -> Request:
        return Request(
            {
                "type": "http",
                "http_version": "1.1",
                "method": "GET",
                "path": "/",
                "headers": [
                    (k.lower().encode(), v.encode()) for k, v in headers.items()
                ],
                "client": (client_host, 51234),
            }
        )

    def setUp(self):
        from src.core.config import settings

        self.settings = settings
        self.original = settings.RATE_LIMIT_CLIENT_IP_HEADER

    def tearDown(self):
        self.settings.RATE_LIMIT_CLIENT_IP_HEADER = self.original

    def test_usa_o_cabecalho_confiavel_quando_ele_vem(self):
        self.settings.RATE_LIMIT_CLIENT_IP_HEADER = "cf-connecting-ip"
        chave = client_ip(self._request({"cf-connecting-ip": "198.51.100.4"}))
        self.assertEqual(chave, "198.51.100.4")

    def test_cabecalho_ausente_cai_no_balde_compartilhado(self):
        """E NAO no socket peer.

        Cair no peer daria a todo mundo um balde plausivel e silencioso — o
        proxy mal configurado passaria despercebido. O balde unico faz o
        problema aparecer no primeiro minuto de trafego.
        """
        self.settings.RATE_LIMIT_CLIENT_IP_HEADER = "cf-connecting-ip"
        chave = client_ip(self._request({}))
        self.assertEqual(chave, SHARED_BUCKET_KEY)

    def test_valor_que_nao_e_ip_cai_no_balde_compartilhado(self):
        """Sem isto, lixo no cabecalho gera um balde novo por requisicao."""
        self.settings.RATE_LIMIT_CLIENT_IP_HEADER = "cf-connecting-ip"
        for lixo in ("balde-novo-42", "'; drop table", "", "   "):
            with self.subTest(valor=lixo):
                chave = client_ip(self._request({"cf-connecting-ip": lixo}))
                self.assertEqual(chave, SHARED_BUCKET_KEY)

    def test_lista_de_saltos_usa_o_primeiro(self):
        self.settings.RATE_LIMIT_CLIENT_IP_HEADER = "x-forwarded-for"
        chave = client_ip(
            self._request({"x-forwarded-for": "198.51.100.4, 10.0.0.1, 10.0.0.2"})
        )
        self.assertEqual(chave, "198.51.100.4")

    def test_ipv6_e_aceito(self):
        self.settings.RATE_LIMIT_CLIENT_IP_HEADER = "cf-connecting-ip"
        chave = client_ip(self._request({"cf-connecting-ip": "2001:db8::1"}))
        self.assertEqual(chave, "2001:db8::1")

    def test_sem_cabecalho_configurado_usa_o_socket(self):
        """O modo sem proxy na frente, documentado no .env.example."""
        self.settings.RATE_LIMIT_CLIENT_IP_HEADER = ""
        chave = client_ip(self._request({"cf-connecting-ip": "198.51.100.4"}))
        self.assertEqual(chave, "203.0.113.9")


if __name__ == "__main__":
    unittest.main()
