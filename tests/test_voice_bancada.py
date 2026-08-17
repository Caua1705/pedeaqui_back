"""A bancada de teste da voz (`GET /voice/test`) continua servindo o HTML.

Por que existe um teste para uma rota de bancada: ela le um arquivo do disco
por CAMINHO, e caminho em string nao quebra no import — quebra na requisicao,
com 500, no dia em que alguem precisa da bancada. Foi o que aconteceu: a rota
apontava para `pagina.html` no proprio diretorio, e o arquivo tinha virado
`src/ai/voice/page.html` na reorganizacao do pacote de voz.

Nao leva marcador `db`: `/voice/test` nao toca no banco, nao autentica e nao
fala com a OpenAI. E so um `read_text`.
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api import voice as rota_de_voz


def _bancada() -> TestClient:
    app = FastAPI()
    app.include_router(rota_de_voz.router)
    return TestClient(app)


def test_o_arquivo_da_bancada_existe_no_caminho_que_a_rota_usa():
    """Separado do teste da rota de proposito: quando o caminho e o errado,
    esta linha diz QUAL arquivo faltou, e a outra so diria 500."""
    assert rota_de_voz.PAGINA.is_file(), f"nao existe: {rota_de_voz.PAGINA}"


def test_a_rota_devolve_o_html_da_bancada():
    resposta = _bancada().get("/voice/test")

    assert resposta.status_code == 200
    assert resposta.headers["content-type"].startswith("text/html")
    # Um HTML vazio passaria no status; o que se quer provar e que veio a
    # pagina, e nao um arquivo qualquer que por acaso existe naquele caminho.
    assert "<html" in resposta.text.lower()
