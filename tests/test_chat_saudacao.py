"""Saudacao nao chama a busca vetorial — e nada alem de saudacao escapa dela.

O CASO CONCRETO. "oi" foi respondido com "temos H2O R$ 7,05". A busca rodava
em todo turno, a agua passou do piso de similaridade contra uma saudacao, e o
prompt manda oferecer o que chega em `retrieved_products`. Alem do produto
aleatorio, o turno pagava um embedding na OpenAI — mediana medida de ~400 ms
em 24/08/2026.

O QUE ESTE ARQUIVO PROTEGE. Metade dele testa que a saudacao pula a busca; a
outra metade, MAIOR de proposito, testa que pergunta de cardapio NAO pula.
Os dois erros nao custam o mesmo:

- buscar sem precisar custa ~400 ms;
- nao buscar quando precisava faz o Rapi dizer "nao temos" sobre item que
  esta no cardapio, sem erro e sem log.

Por isso `test_o_que_nao_e_saudacao_exata_busca` carrega os casos que uma
heuristica esperta quebraria — e nenhum deles pode virar False sem alguem
decidir isso na cara.
"""

import uuid
from types import SimpleNamespace

import pytest

from src.ai.services.greeting import GREETINGS, is_greeting
from src.services import chat_service as chat_module
from src.services.chat_service import _SESSION_HISTORY, ChatService


@pytest.fixture(autouse=True)
def sessao_limpa():
    _SESSION_HISTORY.clear()
    yield
    _SESSION_HISTORY.clear()


class TestReconheceSaudacao:
    @pytest.mark.parametrize("mensagem", sorted(GREETINGS))
    def test_toda_entrada_da_lista_e_saudacao(self, mensagem):
        assert is_greeting(mensagem) is True

    @pytest.mark.parametrize(
        "mensagem",
        ["Oi", "OI", "Olá", "OLÁ!", "oi!!!", "bom dia?", "  oi  ", "Oi, tudo bem?"],
        ids=[
            "caixa_alta_no_comeco", "tudo_maiusculo", "com_acento",
            "acento_e_pontuacao", "pontuacao_repetida", "interrogacao",
            "espaco_nas_pontas", "virgula_no_meio",
        ],
    )
    def test_caixa_acento_e_pontuacao_nao_atrapalham(self, mensagem):
        """`fold_for_match` achata caixa e acento; o resto some a pontuacao.

        Nada aqui alarga o criterio: a comparacao continua sendo com a
        mensagem INTEIRA contra a lista fechada.
        """
        assert is_greeting(mensagem) is True


class TestNaDuvidaBusca:
    @pytest.mark.parametrize(
        "mensagem",
        [
            "oi, tem picanha?",
            "ola, qual o preco do pudim?",
            "bom dia, voces entregam no Papicu?",
            "tem?",
            "sushi?",
            "pudim",
            "oii",
            "salve salve",
            "bom diaa",
            "oi tudo bem com o pedido que eu fiz ontem",
            "",
            "   ",
        ],
        ids=[
            "saudacao_mais_pergunta", "saudacao_mais_preco", "saudacao_mais_entrega",
            "pergunta_de_quatro_letras", "produto_de_seis_letras", "so_o_produto",
            "variacao_fora_da_lista", "repeticao_fora_da_lista", "erro_de_digitacao",
            "comeca_igual_e_continua", "vazio", "so_espaco",
        ],
    )
    def test_o_que_nao_e_saudacao_exata_busca(self, mensagem):
        """O lado caro do erro. Cada id aqui e uma heuristica que quebraria.

        `tem?` e `sushi?` sao o motivo de NUNCA decidir por tamanho: sao mais
        curtas que "boa noite" e precisam do cardapio. `oi, tem picanha?`
        comeca com uma saudacao e precisa do cardapio — e por isso a
        comparacao nunca pode ser por substring nem por prefixo.
        """
        assert is_greeting(mensagem) is False


class TestPipeline:
    """A decisao chega ao `/chat`: saudacao nao toca no `RetrievalService`."""

    def _servico(self, monkeypatch, mensagem: str, buscas: list):
        restaurante = SimpleNamespace(
            id=uuid.uuid4(), name="Junior da Picanha", assistant_notes=None
        )
        filial = SimpleNamespace(id=uuid.uuid4())

        service = ChatService(db=SimpleNamespace())
        service.restaurant_repository = SimpleNamespace(
            get_active_by_id=lambda _id: restaurante
        )
        service.branch_repository = SimpleNamespace(
            get_active_by_id_and_restaurant=lambda _b, _r: filial
        )
        service.product_repository = SimpleNamespace(
            list_active_by_ids=lambda _b, _ids: []
        )

        def registra_busca(**kwargs):
            buscas.append(kwargs)
            return []

        service.retrieval_service = SimpleNamespace(retrieve_products=registra_busca)
        monkeypatch.setattr(
            chat_module,
            "ChatLLMService",
            lambda: SimpleNamespace(
                invoke=lambda **kwargs: SimpleNamespace(
                    message="Ola! Como posso ajudar?",
                    response_type="text",
                    selected_product_ids=[],
                )
            ),
        )
        return service, restaurante, filial

    def test_saudacao_nao_chama_a_busca(self, monkeypatch):
        buscas: list = []
        service, restaurante, filial = self._servico(monkeypatch, "oi", buscas)

        resposta = service.chat(
            restaurant_id=restaurante.id,
            branch_id=filial.id,
            session_id="sessao-1",
            message="oi",
        )

        assert buscas == []
        assert resposta.products == []
        assert resposta.response_type == "text"

    def test_pergunta_de_cardapio_continua_chamando_a_busca(self, monkeypatch):
        buscas: list = []
        service, restaurante, filial = self._servico(monkeypatch, "x", buscas)

        service.chat(
            restaurant_id=restaurante.id,
            branch_id=filial.id,
            session_id="sessao-1",
            message="quanto custa a picanha?",
        )

        assert len(buscas) == 1
        assert buscas[0]["question"] == "quanto custa a picanha?"
        assert buscas[0]["branch_id"] == filial.id

    def test_a_saudacao_e_registrada_no_log(self, monkeypatch, caplog):
        """Sem esta linha, um turno rapido e um turno com busca sao iguais no log."""
        buscas: list = []
        service, restaurante, filial = self._servico(monkeypatch, "oi", buscas)

        with caplog.at_level("INFO", logger="uvicorn.error"):
            service.chat(
                restaurant_id=restaurante.id,
                branch_id=filial.id,
                session_id="sessao-1",
                message="oi",
            )

        assert "busca no cardapio ignorada | motivo=saudacao" in caplog.text
