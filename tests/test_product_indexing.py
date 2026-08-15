"""O texto que vira vetor, e as duas chaves do cache do Rapi.

Suíte rápida: nada aqui toca banco nem OpenAI.
"""

import uuid
from decimal import Decimal

from src.ai.services.chat_cache import ChatCache, menu_generation
from src.ai.services.product_indexing import build_content_hash, build_product_content
from src.models.product_model import Product


def _produto(**campos) -> Product:
    """Um `Product` fora da sessão, só com os campos que o conteúdo indexado lê."""
    padrao = {
        "id": uuid.uuid4(),
        "restaurant_id": uuid.uuid4(),
        "category_id": uuid.uuid4(),
        "code": None,
        "name": "Picanha na Chapa",
        "slug": "picanha-na-chapa",
        "description": "Serve duas pessoas",
        "price": Decimal("89.90"),
        "is_available": True,
    }
    return Product(**{**padrao, **campos})


class TestOQueEntraNoConteudoIndexado:
    def test_o_conteudo_tem_nome_codigo_categoria_e_descricao(self):
        conteudo = build_product_content(_produto(code="X12"), category_name="Carnes")

        assert conteudo == (
            "Nome: Picanha na Chapa\n"
            "Codigo: X12\n"
            "Categoria: Carnes\n"
            "Descricao: Serve duas pessoas"
        )

    def test_preco_e_disponibilidade_ficam_de_fora(self):
        """A razão de o toggle "acabou o X" não custar um embedding.

        Enquanto `Preco` e `Disponivel` estavam no conteúdo, marcar um produto
        como indisponível mudava o `content_hash` e comprava um vetor novo — e
        é o toque mais frequente do dia no painel de um restaurante.
        """
        conteudo = build_product_content(_produto(), category_name="Carnes")

        assert "89.90" not in conteudo
        assert "Preco" not in conteudo
        assert "Disponivel" not in conteudo

    def test_slug_fica_de_fora_porque_e_o_nome_repetido(self):
        conteudo = build_product_content(_produto(), category_name="Carnes")

        assert "picanha-na-chapa" not in conteudo

    def test_campo_vazio_nao_deixa_linha_orfa(self):
        conteudo = build_product_content(
            _produto(code=None, description=None),
            category_name=None,
        )

        assert conteudo == "Nome: Picanha na Chapa"


class TestOHashAcompanhaOConteudo:
    def test_mudar_o_preco_nao_muda_o_hash(self):
        """O que faz a varredura carimbar em vez de reindexar."""
        barato = build_product_content(_produto(price=Decimal("10.00")), "Carnes")
        caro = build_product_content(_produto(price=Decimal("99.00")), "Carnes")

        assert build_content_hash(barato) == build_content_hash(caro)

    def test_mudar_a_descricao_muda_o_hash(self):
        antes = build_product_content(_produto(description="Serve duas pessoas"), "Carnes")
        depois = build_product_content(_produto(description="Sem gluten"), "Carnes")

        assert build_content_hash(antes) != build_content_hash(depois)

    def test_mudar_a_categoria_muda_o_hash(self):
        antes = build_product_content(_produto(), "Carnes")
        depois = build_product_content(_produto(), "Pratos Quentes")

        assert build_content_hash(antes) != build_content_hash(depois)


class TestAsDuasChavesDoCache:
    """O embedding da pergunta sobrevive ao reindex; o resultado da busca não."""

    def test_a_chave_do_embedding_ignora_a_geracao(self, monkeypatch):
        """Reindexar o cardápio não pode jogar fora os vetores das perguntas.

        "tem pizza vegana?" vira o mesmo vetor antes e depois de o lojista
        mexer no menu, e é essa a chamada que custa dinheiro.
        """
        cache = ChatCache()
        restaurant_id = uuid.uuid4()

        monkeypatch.setattr(menu_generation, "current", lambda _: 0)
        antes = cache.embedding_key(restaurant_id, "tem pizza vegana?")
        monkeypatch.setattr(menu_generation, "current", lambda _: 7)
        depois = cache.embedding_key(restaurant_id, "tem pizza vegana?")

        assert antes == depois

    def test_a_chave_da_busca_muda_quando_a_geracao_muda(self, monkeypatch):
        cache = ChatCache()
        restaurant_id = uuid.uuid4()

        monkeypatch.setattr(menu_generation, "current", lambda _: 0)
        antes = cache.retrieval_key(restaurant_id, "tem pizza vegana?")
        monkeypatch.setattr(menu_generation, "current", lambda _: 1)
        depois = cache.retrieval_key(restaurant_id, "tem pizza vegana?")

        assert antes != depois

    def test_o_reindex_de_um_restaurante_nao_invalida_o_do_outro(self, monkeypatch):
        cache = ChatCache()
        reindexado = uuid.uuid4()
        intocado = uuid.uuid4()
        geracoes = {reindexado: 0, intocado: 5}

        monkeypatch.setattr(menu_generation, "current", lambda rid: geracoes[rid])
        antes = cache.retrieval_key(intocado, "tem pizza vegana?")
        geracoes[reindexado] = 1
        depois = cache.retrieval_key(intocado, "tem pizza vegana?")

        assert antes == depois

    def test_a_normalizacao_da_pergunta_continua_valendo(self):
        cache = ChatCache()
        restaurant_id = uuid.uuid4()

        assert cache.embedding_key(restaurant_id, "  TEM  Pizza Vegana? ") == cache.embedding_key(
            restaurant_id, "tem pizza vegana?"
        )
