"""`POST /restaurants/{slug}/delivery/estimate` tem limite por IP.

Até 20/08/2026 não tinha nenhum. A rota é pública (login é OPCIONAL), e cada
chamada pode virar geocode + rota no Google — que são pagos — além de gravar
uma linha em `delivery_estimates`. Era a nota que o próprio
`rate_limit.py` deixava escrita ao lado do limite da tela de filiais: *"aquela
rota, que chama o mesmo Google para UMA filial, continua sem limite nenhum"*.

Contra o `main.app` de verdade, e não contra um `FastAPI()` montado aqui: o
wrapper do `@limiter.limit` lê `request.state.view_rate_limit`, que quem grava
é o `RateLimitStateMiddleware` do app real. Um app de teste montado à mão
passaria por cima justamente do acoplamento que se quer conferir — é a mesma
razão registrada em `test_branch_availability.RotaTests`.
"""

import unittest
import uuid
from types import SimpleNamespace

from fastapi.testclient import TestClient

from src.api.rate_limit import DELIVERY_ESTIMATE_RATE_LIMIT, limiter


CAMINHO = "/restaurants/junior-da-picanha/delivery/estimate"

CORPO = {
    "branch_id": None,
    "address": {
        "street": "Rua das Flores",
        "number": "200",
        "neighborhood": "Centro",
        "city": "Fortaleza",
        "state": "CE",
    },
}


def _teto_por_minuto(limite: str) -> int:
    """O primeiro número de `"20/minute;200/hour"`."""
    return int(limite.split("/")[0])


class RateLimitDaEstimativaTests(unittest.TestCase):
    def setUp(self) -> None:
        from main import app
        from src.api.dependencies.database import get_db

        self.app = app
        self.app.dependency_overrides[get_db] = lambda: None
        self.client = TestClient(app)

        # Balde limpo. O storage do limiter é de MÓDULO e sobrevive entre
        # testes: sem isto, a contagem de um teste anterior entraria neste e o
        # 429 chegaria antes da hora — ou não chegaria.
        limiter.reset()
        self.addCleanup(limiter.reset)

        self._responder_sempre_igual()

    def tearDown(self) -> None:
        self.app.dependency_overrides.clear()

    def _responder_sempre_igual(self) -> None:
        """Substitui o service inteiro: aqui se testa o limite, não a conta.

        Também é o que garante que nenhum teste desta suíte chame o Google —
        e a rota sem dublê chamaria, que é exatamente o custo em questão.
        """
        from src.api.endpoints import delivery as rota
        from src.schemas.delivery_schema import DeliveryEstimateResponse

        resposta = DeliveryEstimateResponse(
            serviceable=True,
            delivery_fee=7.5,
            distance_km=4.2,
            eta_min=30,
            eta_max=45,
            provider="google_routes",
        )
        original = rota.DeliveryEstimateService
        rota.DeliveryEstimateService = lambda db: SimpleNamespace(
            estimate_and_store=lambda *args: (
                SimpleNamespace(to_response=lambda: resposta.model_copy()),
                None,
            )
        )
        self.addCleanup(setattr, rota, "DeliveryEstimateService", original)

    def _chamar(self, ip: str = "203.0.113.10"):
        return self.client.post(CAMINHO, json=CORPO, headers={"x-real-ip": ip})

    def test_a_rota_esta_registrada(self):
        from tests.rotas_do_app import caminhos

        self.assertIn("/restaurants/{restaurant_slug}/delivery/estimate", caminhos())

    def test_dentro_do_teto_responde_200(self):
        resposta = self._chamar()

        self.assertEqual(resposta.status_code, 200)

    def test_estourar_o_teto_por_minuto_responde_429(self):
        teto = _teto_por_minuto(DELIVERY_ESTIMATE_RATE_LIMIT)

        for _ in range(teto):
            self.assertEqual(self._chamar().status_code, 200)

        self.assertEqual(self._chamar().status_code, 429)

    def test_o_429_traz_a_mensagem_do_projeto(self):
        """Mesmo corpo dos outros 429, e não o texto padrão do slowapi.

        O app do cliente mostra esse `detail` na tela.
        """
        for _ in range(_teto_por_minuto(DELIVERY_ESTIMATE_RATE_LIMIT) + 1):
            resposta = self._chamar()

        self.assertEqual(resposta.status_code, 429)
        self.assertIn("Muitas requisições", resposta.json()["detail"])

    def test_o_balde_e_por_ip(self):
        """Um IP estourado não derruba o vizinho.

        É a propriedade que separa "limite" de "chave geral": sem ela, quem
        varre endereços fecharia a estimativa para a cidade inteira.
        """
        teto = _teto_por_minuto(DELIVERY_ESTIMATE_RATE_LIMIT)
        for _ in range(teto + 1):
            self._chamar(ip="203.0.113.10")

        self.assertEqual(self._chamar(ip="203.0.113.99").status_code, 200)

    def test_nao_reaproveita_o_balde_da_tela_de_filiais(self):
        """Dois limites, dois baldes.

        Os dois números são iguais de propósito (ver o comentário em
        `rate_limit.py`), e isso torna fácil concluir errado que compartilham
        contagem. Não compartilham: estourar um deixa o outro respondendo, e
        é isso que faz a soma das duas cotas ser o gasto real no Google.
        """
        teto = _teto_por_minuto(DELIVERY_ESTIMATE_RATE_LIMIT)
        for _ in range(teto + 1):
            self._chamar()

        outra = self.client.post(
            "/restaurants/junior-da-picanha/branches/availability",
            json={"branch_id": str(uuid.uuid4())},
            headers={"x-real-ip": "203.0.113.10"},
        )

        self.assertNotEqual(outra.status_code, 429)


if __name__ == "__main__":
    unittest.main()
