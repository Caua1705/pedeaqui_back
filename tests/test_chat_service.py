"""Caracterizacao de `services/chat_service.py` — o Rapi.

TESTE DE CARACTERIZACAO: descreve o que o codigo faz HOJE. Comportamento
esquisito fica registrado e verde, com comentario apontando o problema.

Duas coisas aqui merecem rede antes de qualquer refatoracao:

1. **A validacao dos ids que o LLM devolve.** O modelo pode inventar produto —
   e a unica coisa entre a alucinacao dele e a tela do cliente e
   `_validate_selected_product_ids`.
2. **O historico de sessao e um dicionario de MODULO** (`_SESSION_HISTORY`),
   com TTL e teto de mensagens. Estado global entre requisicoes e o tipo de
   coisa que uma refatoracao move de lugar sem querer — e a armadilha 20 ja
   registra que ele nao sobrevive a mais de um worker.

Todo teste daqui limpa `_SESSION_HISTORY` (fixture `sessao_limpa`): sem isso
um teste enche o dicionario e o proximo mede o lixo do anterior.
"""

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from src.services import chat_service as chat_module
from src.services.chat_service import (
    _MAX_SESSION_MESSAGES,
    _SESSION_HISTORY,
    _SESSION_TTL,
    ChatService,
    _cleanup_inactive_sessions,
    _get_session_conversation,
    _message_digest,
    _store_session_turn,
)
from src.services.menu_service import MenuService


AGORA = datetime(2026, 8, 11, 20, 41, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def sessao_limpa():
    _SESSION_HISTORY.clear()
    yield
    _SESSION_HISTORY.clear()


class FakeDb:
    """O ChatService so repassa a sessao aos repositorios, trocados por fakes."""


class FakeRestaurantRepository:
    def __init__(self, restaurant=None):
        self.restaurant = restaurant

    def get_active_by_id(self, restaurant_id):
        return self.restaurant


class FakeProductRepository:
    def __init__(self, products=()):
        self.products = list(products)
        self.ids_asked = None

    def list_active_by_ids(self, restaurant_id, product_ids):
        self.ids_asked = list(product_ids)
        return [product for product in self.products if product.id in set(product_ids)]


def make_product(name="X-Burger", product_id=None):
    return SimpleNamespace(
        id=product_id or uuid.uuid4(),
        restaurant_id=uuid.uuid4(),
        category_id=uuid.uuid4(),
        code="X1",
        name=name,
        slug="x-burger",
        description=None,
        price=Decimal("24.90"),
        image_path=None,
        is_active=True,
        is_available=True,
        sort_order=0,
        option_groups=[],
    )


def make_service(restaurant=None, products=()):
    service = ChatService(FakeDb())
    service.restaurant_repository = FakeRestaurantRepository(restaurant)
    service.product_repository = FakeProductRepository(products)
    return service


# ---------------------------------------------------------------------------
# A defesa contra alucinacao do modelo
# ---------------------------------------------------------------------------


class TestValidateSelectedProductIds:
    def test_an_id_the_model_invented_is_dropped(self):
        """O teste que justifica a funcao existir: id que nao veio da busca
        nao chega ao cliente."""
        real = uuid.uuid4()
        retrieved = [{"id": str(real)}]

        validos = ChatService._validate_selected_product_ids(retrieved, [real, uuid.uuid4()])

        assert validos == [real]

    def test_repeated_ids_are_collapsed_keeping_the_first_position(self):
        a, b = uuid.uuid4(), uuid.uuid4()
        retrieved = [{"id": str(a)}, {"id": str(b)}]

        assert ChatService._validate_selected_product_ids(retrieved, [a, b, a]) == [a, b]

    def test_ids_that_arrive_as_strings_are_accepted(self):
        """O LLM devolve JSON, entao id chega como texto. A funcao converte os
        dois lados antes de comparar."""
        real = uuid.uuid4()

        assert ChatService._validate_selected_product_ids([{"id": str(real)}], [str(real)]) == [real]

    def test_nothing_selected_is_an_empty_list(self):
        assert ChatService._validate_selected_product_ids([{"id": str(uuid.uuid4())}], []) == []

    def test_a_malformed_id_is_discarded_like_an_invented_one(self):
        """Era 500, e o teste registrava isso.

        A funcao existe para NAO confiar no que o modelo devolve, e confiava
        num caso: que o texto dele e um uuid bem formado. Id inventado mas bem
        formado era descartado em silencio; id malformado ("produto-1", "o
        primeiro") estourava ValueError, subia pelo `except Exception: raise`
        do `chat()` e virava 500 na cara do cliente.

        Os dois sao a mesma coisa — o modelo devolveu algo que nao aponta para
        produto nenhum — e agora tem o mesmo destino.
        """
        real = uuid.uuid4()

        validos = ChatService._validate_selected_product_ids(
            [{"id": str(real)}], ["nao-e-uuid", str(real)]
        )

        assert validos == [real]

    def test_a_whole_answer_of_garbage_is_an_empty_list_not_a_crash(self):
        """O pior caso: o modelo ignora o formato inteiro. A resposta vira
        texto sem carrossel, que e degradacao — nao erro."""
        assert ChatService._validate_selected_product_ids(
            [{"id": str(uuid.uuid4())}], ["produto-1", None, 42, ""]
        ) == []

    def test_a_malformed_id_from_OUR_search_still_raises(self):
        """A conversao dos ids da BUSCA continua estrita de proposito: eles
        saem do nosso banco, e um id malformado vindo dali seria defeito
        nosso. Engoli-lo esconderia o defeito em vez de tratar entrada
        hostil."""
        with pytest.raises(ValueError):
            ChatService._validate_selected_product_ids([{"id": "nao-e-uuid"}], [])


# ---------------------------------------------------------------------------
# O tipo da resposta
# ---------------------------------------------------------------------------


def make_llm_response(response_type="text", message="Oi!", selected_product_ids=()):
    return SimpleNamespace(
        response_type=response_type,
        message=message,
        selected_product_ids=list(selected_product_ids),
    )


class TestBuildResponse:
    def test_having_products_forces_the_products_type(self):
        """Mesmo que o modelo tenha dito "text": se ha produto para mostrar, a
        resposta e de produtos. Quem manda e o que sobrou da validacao."""
        produtos = [MenuService.product_response(make_product())]

        response = ChatService._build_response(make_llm_response("text"), [], produtos)

        assert response.response_type == "products"

    def test_products_type_without_any_product_falls_back_to_text(self):
        """O caso que a validacao produz: o modelo pediu "products", todos os
        ids eram inventados, e sobrou zero. Responder "products" com lista
        vazia deixaria a tela com um carrossel sem nada dentro."""
        response = ChatService._build_response(
            make_llm_response("products", selected_product_ids=[uuid.uuid4()]), [], []
        )

        assert response.response_type == "text"
        assert response.products == []

    def test_a_text_answer_stays_text(self):
        response = ChatService._build_response(make_llm_response("text", message="Bom dia"), [], [])

        assert response.response_type == "text"
        assert response.message == "Bom dia"


# ---------------------------------------------------------------------------
# Hidratacao: a ordem e do modelo, o dado e do banco
# ---------------------------------------------------------------------------


class TestHydrateProducts:
    def test_the_order_follows_the_selection_not_the_database(self):
        """O banco devolve na ordem dele; a resposta sai na ordem em que o
        modelo escolheu, que e a ordem em que ele explicou os produtos no
        texto. Trocar isso desalinha texto e carrossel."""
        primeiro, segundo = make_product("A"), make_product("B")
        service = make_service(products=[primeiro, segundo])

        produtos = service._hydrate_products(uuid.uuid4(), [segundo.id, primeiro.id])

        assert [produto.name for produto in produtos] == ["B", "A"]

    def test_an_id_that_is_no_longer_active_is_dropped_silently(self):
        """Produto esgotado entre a busca e a resposta simplesmente nao
        aparece — a conversa continua, sem erro."""
        vivo = make_product("Vivo")
        service = make_service(products=[vivo])

        produtos = service._hydrate_products(uuid.uuid4(), [vivo.id, uuid.uuid4()])

        assert [produto.name for produto in produtos] == ["Vivo"]

    def test_nothing_selected_asks_the_repository_for_an_empty_list(self):
        service = make_service(products=[make_product()])

        assert service._hydrate_products(uuid.uuid4(), []) == []
        assert service.product_repository.ids_asked == []


# ---------------------------------------------------------------------------
# Restaurante inativo
# ---------------------------------------------------------------------------


class TestEnsureActiveRestaurant:
    def test_an_unknown_restaurant_is_404(self):
        """Barrado ANTES de gastar chamada de embedding e de LLM — e fora do
        `try`, para o 404 nao virar stack trace no log."""
        with pytest.raises(HTTPException) as exc:
            make_service(restaurant=None)._ensure_active_restaurant(uuid.uuid4())

        assert exc.value.status_code == 404

    def test_an_active_restaurant_passes(self):
        service = make_service(restaurant=SimpleNamespace(id=uuid.uuid4()))

        assert service._ensure_active_restaurant(uuid.uuid4()) is None


# ---------------------------------------------------------------------------
# O historico de sessao — dicionario de modulo
# ---------------------------------------------------------------------------


class TestSessionConversation:
    def test_an_unknown_session_starts_empty(self):
        assert _get_session_conversation("nunca-vista") == []

    def test_a_turn_stores_the_user_and_the_assistant(self):
        _store_session_turn("s1", "quero pizza", "temos calabresa", AGORA)

        assert _get_session_conversation("s1") == [
            {"role": "user", "content": "quero pizza"},
            {"role": "assistant", "content": "temos calabresa"},
        ]

    def test_the_history_is_capped_at_twenty_messages(self):
        """Teto de `_MAX_SESSION_MESSAGES`. Sem ele, uma conversa longa cresce
        sem limite dentro do processo — e o prompt do LLM junto."""
        for turno in range(30):
            _store_session_turn("s1", f"pergunta {turno}", f"resposta {turno}", AGORA)

        conversa = _get_session_conversation("s1")

        assert len(conversa) == _MAX_SESSION_MESSAGES
        # O teto corta pelo COMECO: o que sobra e o fim da conversa.
        assert conversa[-1] == {"role": "assistant", "content": "resposta 29"}

    def test_the_last_interaction_moves_forward_on_every_turn(self):
        _store_session_turn("s1", "a", "b", AGORA)
        depois = AGORA + timedelta(minutes=30)
        _store_session_turn("s1", "c", "d", depois)

        assert _SESSION_HISTORY["s1"]["last_interaction"] == depois


class TestSessionCleanup:
    def test_a_session_older_than_the_ttl_is_removed(self):
        _store_session_turn("velha", "a", "b", AGORA)

        _cleanup_inactive_sessions(AGORA + _SESSION_TTL + timedelta(seconds=1))

        assert "velha" not in _SESSION_HISTORY

    def test_a_session_exactly_at_the_ttl_survives(self):
        """A comparacao e `>`, nao `>=`. Registrado porque e a fronteira que
        uma reescrita troca sem perceber."""
        _store_session_turn("no-limite", "a", "b", AGORA)

        _cleanup_inactive_sessions(AGORA + _SESSION_TTL)

        assert "no-limite" in _SESSION_HISTORY

    def test_only_the_expired_ones_go(self):
        _store_session_turn("velha", "a", "b", AGORA)
        _store_session_turn("nova", "a", "b", AGORA + timedelta(minutes=59))

        _cleanup_inactive_sessions(AGORA + _SESSION_TTL + timedelta(seconds=1))

        assert list(_SESSION_HISTORY) == ["nova"]


class TestMessageDigest:
    def test_it_is_short_and_stable(self):
        assert _message_digest("quero pizza") == _message_digest("quero pizza")
        assert len(_message_digest("quero pizza")) == 12

    def test_different_messages_give_different_digests(self):
        assert _message_digest("quero pizza") != _message_digest("quero coxinha")

    def test_the_message_itself_is_not_recoverable_from_the_log(self):
        """A razao de existir: a mensagem do cliente e dado pessoal e nao vai
        para o log (ver "Logs nao levam dado pessoal" na skill)."""
        mensagem = "meu endereco e rua das flores 123"

        assert mensagem not in _message_digest(mensagem)


class FakeFeedbackDb:
    def __init__(self, falha=None):
        self.events = []
        self.falha = falha

    def commit(self):
        if self.falha is not None:
            self.events.append("commit-falhou")
            raise self.falha
        self.events.append("commit")

    def rollback(self):
        self.events.append("rollback")


def fake_feedback_repository(monkeypatch, gravados, falha=None):
    class FakeFeedbackRepository:
        def __init__(self, db):
            self.db = db

        def create(self, request):
            if falha is not None:
                raise falha
            gravados.append(request)

    monkeypatch.setattr(chat_module, "AIFeedbackRepository", FakeFeedbackRepository)


class TestCreateFeedback:
    def test_the_success_is_said_only_after_the_commit(self, monkeypatch):
        """Antes, `success=True` era devolvido sem depender de nada: quem
        commitava era o REPOSITORIO, contra a regra de camadas, e o service
        afirmava sucesso sobre uma transacao que ele nao controlava."""
        gravados = []
        db = FakeFeedbackDb()
        fake_feedback_repository(monkeypatch, gravados)

        service = make_service()
        service.db = db
        pedido = SimpleNamespace(session_id="s1", rating=5)

        response = service.create_feedback(pedido)

        assert gravados == [pedido]
        assert db.events == ["commit"]
        assert response.success is True

    def test_a_failure_rolls_back_and_propagates(self, monkeypatch):
        """Nao vira `success=False`: o cliente nao tem o que fazer com um voto
        recusado, e engolir a excecao esconderia do log a unica pista de que o
        feedback parou de ser gravado."""
        db = FakeFeedbackDb()
        fake_feedback_repository(monkeypatch, [], falha=RuntimeError("banco fora"))

        service = make_service()
        service.db = db

        with pytest.raises(RuntimeError):
            service.create_feedback(SimpleNamespace(session_id="s1", rating=5))

        assert db.events == ["rollback"]

    def test_a_failure_on_the_commit_also_rolls_back(self, monkeypatch):
        db = FakeFeedbackDb(falha=RuntimeError("commit falhou"))
        fake_feedback_repository(monkeypatch, [])

        service = make_service()
        service.db = db

        with pytest.raises(RuntimeError):
            service.create_feedback(SimpleNamespace(session_id="s1", rating=5))

        assert db.events == ["commit-falhou", "rollback"]


# ---------------------------------------------------------------------------
# O pipeline inteiro, com o LLM trocado
# ---------------------------------------------------------------------------


class TestChatPipeline:
    def test_a_full_turn_answers_and_records_the_history(self, monkeypatch):
        produto = make_product("Pizza Calabresa")
        service = make_service(restaurant=SimpleNamespace(id=uuid.uuid4()), products=[produto])
        service.retrieval_service = SimpleNamespace(
            retrieve_products=lambda restaurant_id, question: [{"id": str(produto.id)}]
        )
        monkeypatch.setattr(
            chat_module,
            "ChatLLMService",
            lambda: SimpleNamespace(
                invoke=lambda **kwargs: make_llm_response("products", "temos esta", [produto.id])
            ),
        )

        response = service.chat(uuid.uuid4(), "sessao-1", "quero pizza")

        assert response.response_type == "products"
        assert [p.name for p in response.products] == ["Pizza Calabresa"]
        assert _get_session_conversation("sessao-1") == [
            {"role": "user", "content": "quero pizza"},
            {"role": "assistant", "content": "temos esta"},
        ]

    def test_an_unknown_restaurant_never_reaches_the_llm(self, monkeypatch):
        """O 404 acontece antes da chamada paga. Se alguem mover o
        `_ensure_active_restaurant` para dentro do `try`, este teste cai."""
        def nao_deveria_ser_chamado():
            raise AssertionError("o LLM foi chamado para restaurante inexistente")

        monkeypatch.setattr(chat_module, "ChatLLMService", nao_deveria_ser_chamado)
        service = make_service(restaurant=None)

        with pytest.raises(HTTPException) as exc:
            service.chat(uuid.uuid4(), "sessao-1", "quero pizza")

        assert exc.value.status_code == 404

    def test_a_failing_turn_leaves_no_history_behind(self, monkeypatch):
        """`_store_session_turn` so roda depois da resposta pronta. Uma falha
        no meio nao deixa a pergunta gravada sem resposta — que envenenaria o
        proximo prompt daquela sessao."""
        service = make_service(restaurant=SimpleNamespace(id=uuid.uuid4()))
        service.retrieval_service = SimpleNamespace(
            retrieve_products=lambda restaurant_id, question: (_ for _ in ()).throw(RuntimeError("busca caiu"))
        )

        with pytest.raises(RuntimeError):
            service.chat(uuid.uuid4(), "sessao-1", "quero pizza")

        assert _get_session_conversation("sessao-1") == []
