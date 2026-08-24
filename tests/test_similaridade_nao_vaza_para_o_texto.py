"""`similarity` chega a VOZ e nao chega ao prompt do TEXTO.

Por que isto merece teste proprio: o campo nasce na busca porque o log da voz
precisa dele (`VoiceSearchService.buscar`, linha `topo=`), e a mesma lista, no
chat de texto, e interpolada INTEIRA em `{retrieved_products}`
(`src/ai/prompts/chat_prompt.py`). Sao ~10 tokens por produto em todo turno do
`/chat`, mais um campo sobre o qual o `system_prompt` nao diz uma palavra.

O unico ponto que tira e `ChatService._retrieve_menu_products`. Ele e uma
linha, nao tem efeito visivel, e nenhum outro teste falharia se alguem o
apagasse numa refatoracao — o `/chat` continuaria respondendo, so que com o
prompt inflado e um numero solto dentro dele.

Nao leva marcador `db`: nada aqui toca no banco.
"""

import uuid
from types import SimpleNamespace

from src.ai.services.retrieval_service import RetrievalService
from src.services.chat_service import ChatService


def _retrieval_service_dublado(similaridade: float = 0.66) -> RetrievalService:
    """Um `RetrievalService` sem banco e sem OpenAI.

    A linha devolvida tem a forma da que o SQL devolve — `similarity` inclusa
    (`ai_repository.similarity_search`, `1 - (embedding <=> ...)`).
    """
    produto_id = uuid.uuid4()
    service = RetrievalService.__new__(RetrievalService)
    service.agent = "/chat"
    service.embedding_service = SimpleNamespace(generate_embedding=lambda _t: [0.1, 0.2])
    service.ai_repository = SimpleNamespace(
        similarity_search=lambda **_k: [
            {
                "id": produto_id,
                "name": "Baiao de dois",
                "description": "com queijo coalho",
                "metadata": {},
                "similarity": similaridade,
            }
        ]
    )
    service.product_repository = SimpleNamespace(
        sellable_prices_by_id=lambda _b, ids: {i: 3530 for i in ids}
    )
    return service


def _busca(service: RetrievalService) -> list[dict]:
    return service.retrieve_products(
        restaurant_id=uuid.uuid4(),
        branch_id=uuid.uuid4(),
        question="baiao",
    )


def test_a_busca_devolve_a_similaridade():
    """O caminho da VOZ. Sem isto o `topo=` do log de `buscar` nao existe."""
    produtos = _busca(_retrieval_service_dublado(similaridade=0.502))

    assert produtos[0]["similarity"] == 0.502


def test_o_caminho_do_texto_nao_leva_a_similaridade_para_o_prompt():
    chat_service = ChatService(db=SimpleNamespace())
    chat_service.retrieval_service = _retrieval_service_dublado()

    produtos = chat_service._retrieve_menu_products(
        restaurant=SimpleNamespace(id=uuid.uuid4()),
        branch=SimpleNamespace(id=uuid.uuid4()),
        message="baiao",
    )

    assert produtos, "o dublê devolveu lista vazia; o teste nao prova nada assim"
    assert "similarity" not in produtos[0]


def test_o_texto_continua_recebendo_o_resto_do_produto():
    """A tirada e de UM campo. Se ela levar o resto junto, o `/chat` fica sem
    cardapio no prompt e nenhum assert acima perceberia."""
    chat_service = ChatService(db=SimpleNamespace())
    chat_service.retrieval_service = _retrieval_service_dublado()

    produto = chat_service._retrieve_menu_products(
        restaurant=SimpleNamespace(id=uuid.uuid4()),
        branch=SimpleNamespace(id=uuid.uuid4()),
        message="baiao",
    )[0]

    assert produto["name"] == "Baiao de dois"
    assert "id" in produto and "price" in produto
