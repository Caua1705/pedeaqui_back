"""Saudacao nao chama o modelo — e a resposta enlatada nao vira armadilha.

O NUMERO QUE CRIOU ESTE ARQUIVO. O portao de saudacao pulava a busca mas
ainda ia ao modelo: medido em producao em 24/08/2026, `llm_call_ms=3616` para
gerar 85 tokens de "oi, tudo bem?", num turno de 3761 ms total. Pagar uma
geracao depois de ja ter decidido que a mensagem e uma saudacao e pagar o
modelo para confirmar o que o portao concluiu de graca.

O que este arquivo trava, em ordem de quanto custaria perder:

1. **O modelo nao e chamado.** E o ganho inteiro. Uma refatoracao que mova o
   `is_greeting` de volta para dentro do pipeline devolve os 3,6 s sem
   quebrar nada mais.
2. **O turno vai para o historico.** Sem ele, um "e o preco desse?" logo
   depois do "oi" chega ao modelo sem o unico contexto que tinha.
3. **Nenhuma frase cita nome de cliente.** `POST /chat` nao tem autenticacao
   nenhuma: nao existe cliente na requisicao de onde tirar um nome, e uma
   frase com `{cliente}` nao teria o que interpolar.
4. **O nome da casa nunca vem depois de artigo.** "do {restaurant}" quebra em
   "Pizzaria Bella"; "da {restaurant}" quebra em "Junior da Picanha".
"""

import re
from types import SimpleNamespace

import pytest

from src.ai.services.greeting import GREETING_REPLIES, greeting_reply
from src.services import chat_service as chat_module
from src.services.chat_service import _SESSION_HISTORY, ChatService
from tests import fabricas


@pytest.fixture(autouse=True)
def sessao_limpa():
    _SESSION_HISTORY.clear()
    yield
    _SESSION_HISTORY.clear()


class NaoDeveriaSerChamado:
    def __call__(self, *args, **kwargs):
        raise AssertionError("a saudacao nao pode chegar aqui")


def servico_de_saudacao(monkeypatch):
    """Um `ChatService` em que busca e modelo EXPLODEM se forem tocados."""
    restaurante = fabricas.restaurante(name="Junior da Picanha")
    filial = fabricas.filial()

    service = ChatService(db=SimpleNamespace())
    service.restaurant_repository = SimpleNamespace(
        get_active_by_id=lambda _id: restaurante
    )
    service.branch_repository = SimpleNamespace(
        get_active_by_id_and_restaurant=lambda _b, _r: filial
    )
    service.retrieval_service = SimpleNamespace(retrieve_products=NaoDeveriaSerChamado())
    monkeypatch.setattr(chat_module, "ChatLLMService", NaoDeveriaSerChamado())
    return service, restaurante, filial


class TestNaoChamaOModelo:
    @pytest.mark.parametrize("saudacao", ["oi", "Olá!", "bom dia", "e ai"])
    def test_saudacao_responde_sem_busca_e_sem_modelo(self, monkeypatch, saudacao):
        """Os dois fakes levantam AssertionError se forem chamados."""
        service, restaurante, filial = servico_de_saudacao(monkeypatch)

        resposta = service.chat(
            restaurant_id=restaurante.id,
            branch_id=filial.id,
            session_id="sessao-1",
            message=saudacao,
        )

        assert resposta.response_type == "text"
        assert resposta.products == []
        assert "Junior da Picanha" in resposta.message

    def test_pergunta_de_cardapio_continua_indo_ao_modelo(self, monkeypatch):
        """A contraprova. Sem ela, um `is_greeting` que devolvesse sempre True
        passaria em todo o resto deste arquivo."""
        service, restaurante, filial = servico_de_saudacao(monkeypatch)

        with pytest.raises(AssertionError, match="nao pode chegar aqui"):
            service.chat(
                restaurant_id=restaurante.id,
                branch_id=filial.id,
                session_id="sessao-1",
                message="quanto custa a picanha?",
            )


class TestHistorico:
    def test_o_turno_de_saudacao_e_gravado(self, monkeypatch):
        service, restaurante, filial = servico_de_saudacao(monkeypatch)

        resposta = service.chat(
            restaurant_id=restaurante.id,
            branch_id=filial.id,
            session_id="sessao-1",
            message="oi",
        )

        mensagens = _SESSION_HISTORY["sessao-1"]["messages"]
        assert mensagens == [
            {"role": "user", "content": "oi"},
            {"role": "assistant", "content": resposta.message},
        ]


class TestAsFrases:
    def test_toda_frase_leva_o_nome_da_casa(self):
        for frase in GREETING_REPLIES:
            assert "{restaurant}" in frase

    def test_nenhuma_frase_pede_nome_de_cliente(self):
        """`POST /chat` nao tem autenticacao: nao ha nome para interpolar.

        Qualquer `{...}` que nao seja `{restaurant}` viraria `KeyError` no
        `.format` — ou, pior, um buraco visivel na tela do cliente.
        """
        for frase in GREETING_REPLIES:
            campos = set(re.findall(r"\{(\w+)\}", frase))
            assert campos == {"restaurant"}

    @pytest.mark.parametrize(
        "nome",
        ["Junior da Picanha", "Pizzaria Bella", "Sushi Yamato", "O Rei do Churrasco"],
        ids=["nome_com_da", "nome_feminino", "nome_neutro", "nome_com_artigo"],
    )
    def test_o_nome_nunca_vem_depois_de_artigo(self, nome):
        """A regra que impede "do Pizzaria Bella" e "da Junior da Picanha".

        Nao existe genero conhecido para nome de restaurante, entao a frase
        tem que colocar o nome em posicao que dispense a concordancia. Este
        teste falha na hora em que alguem acrescentar uma frase com artigo.
        """
        for frase in GREETING_REPLIES:
            texto = frase.format(restaurant=nome)
            assert not re.search(rf"\b(do|da|no|na)\s+{re.escape(nome)}", texto)

    def test_as_tres_frases_saem_ao_longo_de_varios_sorteios(self):
        """O sorteio existe para a saudacao nao soar gravada."""
        saidas = {greeting_reply("Junior da Picanha") for _ in range(200)}

        assert len(saidas) == len(GREETING_REPLIES)

    def test_o_nome_da_casa_e_interpolado(self):
        assert "Junior da Picanha" in greeting_reply("Junior da Picanha")
