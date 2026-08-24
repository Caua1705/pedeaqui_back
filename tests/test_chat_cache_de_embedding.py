"""A pergunta repetida nao paga o embedding de novo.

POR QUE ISTO NUNCA TINHA SIDO EXERCITADO. As oito perguntas da bateria de
producao eram todas distintas, entao `embedding_cache_hit` veio `false` nas
oito e o cache nunca foi observado funcionando. Um cache que ninguem viu
acertar e indistinguivel de um cache quebrado.

O QUE ESTA EM JOGO. O embedding e o segundo maior custo do turno depois do
modelo — oscilou entre 312 e 1390 ms na bateria —, e "quanto custa a
picanha?" e exatamente o tipo de pergunta que se repete num cardapio. Cada
acerto aqui e uma ida a OpenAI que nao acontece.

AS DUAS CHAVES SAO DIFERENTES DE PROPOSITO, e este arquivo trava a diferenca:

- **o vetor da pergunta** ignora a filial e o teto de preco. "tem picanha?"
  gera o MESMO vetor nas duas lojas; botar a filial ali compraria um
  embedding por loja para guardar copias do mesmo numero.
- **o resultado da busca** leva filial, teto e geracao do cardapio. Sem a
  filial, a segunda loja seria servida do cache da primeira — o Rapi
  oferecendo, com preco, um produto que aquela loja nao vende (armadilha 36).

O TTL do vetor e 60 min e o da busca 20 min, e a assimetria e a mesma ideia:
a pergunta nao envelhece, o cardapio sim.
"""

import uuid
from types import SimpleNamespace

import pytest

from src.ai.services.chat_cache import chat_cache
from src.ai.services.retrieval_service import RetrievalService


PERGUNTA = "quanto custa a picanha?"


@pytest.fixture(autouse=True)
def cache_limpo():
    chat_cache._embeddings.clear()
    chat_cache._retrievals.clear()
    yield
    chat_cache._embeddings.clear()
    chat_cache._retrievals.clear()


class EmbeddingQueConta:
    def __init__(self):
        self.chamadas = 0

    def generate_embedding(self, _texto):
        self.chamadas += 1
        return [0.1, 0.2, 0.3]


class BuscaQueConta:
    def __init__(self, produtos):
        self.chamadas = 0
        self.produtos = produtos

    def similarity_search(self, **kwargs):
        self.chamadas += 1
        return self.produtos


def servico(produtos=None, precos=None):
    """Um `RetrievalService` sem banco e sem OpenAI, contando as chamadas."""
    produto_id = uuid.uuid4()
    produtos = produtos if produtos is not None else [
        {"id": produto_id, "name": "Picanha", "description": "na chapa", "metadata": {}}
    ]
    precos = precos if precos is not None else {produto_id: 8990}

    service = RetrievalService.__new__(RetrievalService)
    service.agent = "/chat"
    service.embedding_service = EmbeddingQueConta()
    service.ai_repository = BuscaQueConta(produtos)
    service.product_repository = SimpleNamespace(
        sellable_prices_by_id=lambda _b, ids: {i: precos.get(i) for i in ids if i in precos}
    )
    return service


class TestPerguntaRepetida:
    def test_a_segunda_vez_nao_chama_a_openai(self, caplog):
        """O item C: repetir a pergunta na mesma sessao acerta o cache."""
        service = servico()
        restaurante, filial = uuid.uuid4(), uuid.uuid4()

        with caplog.at_level("INFO", logger="uvicorn.error"):
            service.retrieve_products(restaurante, filial, PERGUNTA)
            assert "embedding_cache_hit=false" in caplog.text
            caplog.clear()

            service.retrieve_products(restaurante, filial, PERGUNTA)

        assert "embedding_cache_hit=true" in caplog.text
        assert "retrieval_cache_hit=true" in caplog.text
        assert service.embedding_service.chamadas == 1
        assert service.ai_repository.chamadas == 1

    def test_caixa_e_acento_diferentes_acertam_o_mesmo_cache(self):
        """`normalize_message` achata os dois lados antes de montar a chave.

        Sem isso, "Quanto custa a PICANHA?" seria uma pergunta nova e pagaria
        outro embedding — e e assim que gente de verdade digita.
        """
        service = servico()
        restaurante, filial = uuid.uuid4(), uuid.uuid4()

        service.retrieve_products(restaurante, filial, PERGUNTA)
        service.retrieve_products(restaurante, filial, "  Quanto custa a PICANHA?  ")

        assert service.embedding_service.chamadas == 1


class TestAsDuasChavesSaoDiferentes:
    def test_o_vetor_e_reusado_entre_filiais_e_a_busca_nao(self):
        """A assimetria que economiza sem misturar cardapio.

        O vetor da frase e o mesmo nas duas lojas (1 embedding). O conjunto de
        produtos nao e (2 buscas) — sem isso, a segunda loja receberia a lista
        da primeira, com os ids da primeira.
        """
        service = servico()
        restaurante = uuid.uuid4()
        filial_centro, filial_aldeota = uuid.uuid4(), uuid.uuid4()

        service.retrieve_products(restaurante, filial_centro, PERGUNTA)
        service.retrieve_products(restaurante, filial_aldeota, PERGUNTA)

        assert service.embedding_service.chamadas == 1
        assert service.ai_repository.chamadas == 2

    def test_o_teto_de_preco_muda_a_busca_e_nao_o_vetor(self):
        """"sobremesa ate R$ 20" nao pode ser servida do cache de "sobremesa"."""
        from decimal import Decimal

        service = servico()
        restaurante, filial = uuid.uuid4(), uuid.uuid4()

        service.retrieve_products(restaurante, filial, "sobremesa")
        service.retrieve_products(restaurante, filial, "sobremesa", max_price=Decimal("20"))

        assert service.embedding_service.chamadas == 1
        assert service.ai_repository.chamadas == 2

    def test_restaurantes_diferentes_nao_compartilham_nada(self):
        service = servico()
        filial = uuid.uuid4()

        service.retrieve_products(uuid.uuid4(), filial, PERGUNTA)
        service.retrieve_products(uuid.uuid4(), filial, PERGUNTA)

        assert service.embedding_service.chamadas == 2
        assert service.ai_repository.chamadas == 2


class TestOPrecoNuncaVemDoCache:
    def test_o_preco_e_relido_a_cada_requisicao(self):
        """O acerto de cache nao pode congelar preco.

        `_format_retrieved_product` nao guarda `price`, e `_with_current_prices`
        rele a linha viva a cada turno. Servir preco do cache faria TODA
        alteracao divergir por ate 20 minutos — o texto do Rapi com o valor
        velho e o cartao com o novo, na mesma resposta.
        """
        service = servico()
        restaurante, filial = uuid.uuid4(), uuid.uuid4()

        primeira = service.retrieve_products(restaurante, filial, PERGUNTA)
        produto_id = primeira[0]["id"]
        service.product_repository.sellable_prices_by_id = lambda _b, _ids: {produto_id: 9990}
        segunda = service.retrieve_products(restaurante, filial, PERGUNTA)

        assert primeira[0]["price"] != segunda[0]["price"]
        assert service.ai_repository.chamadas == 1
