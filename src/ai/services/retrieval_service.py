import logging
import uuid
from decimal import Decimal
from time import perf_counter
from typing import Any

from sqlalchemy.orm import Session

from src.ai.services.chat_cache import chat_cache
from src.ai.services.embedding_service import EmbeddingService
from src.core.config import settings
from src.repositories.ai_repository import AIRepository
from src.repositories.product_repository import ProductRepository
from src.utils.money import format_money_br

logger = logging.getLogger("uvicorn.error")


class RetrievalService:
    """Retrieve restaurant products that are relevant to a user question.

    `agent` so existe para o LOG. Esta busca serve dois agentes — o chat de
    texto e o de voz — e as linhas de medicao saiam todas com o
    prefixo `[AI /chat perf]`, vindas dos dois. Quem grepava esse prefixo para
    medir o chat estava medindo a soma, sem nenhuma forma de separar.

    Vale como parametro e nao como duplicacao do codigo de medicao: os
    cronometros continuam sendo um so, e o unico que muda e o rotulo.
    """

    def __init__(self, db: Session, agent: str = "/chat"):
        self.agent = agent
        self.embedding_service = EmbeddingService()
        self.ai_repository = AIRepository(db)
        self.product_repository = ProductRepository(db)

    def retrieve_products(
        self,
        restaurant_id: uuid.UUID,
        branch_id: uuid.UUID,
        question: str,
        top_k: int = 5,
        max_price: Decimal | None = None,
    ) -> list[dict[str, Any]]:
        """Os produtos DAQUELA LOJA que respondem a pergunta.

        `branch_id` e obrigatorio desde a revisao 20260820_0026 e atravessa as
        tres camadas deste caminho, porque cada uma delas errava sozinha: a
        BUSCA (filtro por filial no SQL), o CACHE (chave por filial) e o PRECO
        VIGENTE (leitura da linha viva daquela loja). Faltando em qualquer
        uma, o Rapi volta a oferecer com preco um produto que a loja nao
        vende — sem erro e sem log.

        `max_price` continua opcional: sem ele a busca e exatamente a de
        antes.
        """
        # Duas chaves, e nao uma: o vetor da pergunta sobrevive ao reindex, o
        # resultado da busca nao. Ver `ChatCache.embedding_key`/`retrieval_key`.
        #
        # `max_price` e `branch_id` entram so na chave da BUSCA. O vetor de
        # "quero uma sobremesa" e o mesmo com ou sem teto de preco, e o mesmo
        # nas duas lojas; o conjunto de produtos, nao. Sem isso, uma pergunta
        # com teto seria servida do cache da mesma pergunta sem teto — e a
        # segunda loja, do cache da primeira.
        embedding_cache_key = chat_cache.embedding_key(restaurant_id, question)
        retrieval_cache_key = chat_cache.retrieval_key(
            restaurant_id, branch_id, question, max_price
        )

        embedding_started_at = perf_counter()
        embedding = chat_cache.get_embedding(embedding_cache_key)
        embedding_cache_hit = embedding is not None
        if embedding is None:
            embedding = self.embedding_service.generate_embedding(question)
            chat_cache.set_embedding(embedding_cache_key, embedding)
        logger.info(
            "[AI %s perf] embedding_ms=%.2f",
            self.agent,
            (perf_counter() - embedding_started_at) * 1000,
        )
        logger.info(
            "[AI %s cache] embedding_cache_hit=%s",
            self.agent,
            str(embedding_cache_hit).lower(),
        )

        retrieval_started_at = perf_counter()
        retrieved_products = chat_cache.get_retrieval(retrieval_cache_key)
        retrieval_cache_hit = retrieved_products is not None
        if retrieved_products is None:
            products = self.ai_repository.similarity_search(
                restaurant_id=restaurant_id,
                branch_id=branch_id,
                embedding=embedding,
                top_k=top_k,
                max_price=max_price,
                min_similarity=settings.AI_SEARCH_MIN_SIMILARITY,
            )
            retrieved_products = [
                self._format_retrieved_product(product) for product in products
            ]
            chat_cache.set_retrieval(retrieval_cache_key, retrieved_products)
        logger.info(
            "[AI %s perf] retrieval_ms=%.2f",
            self.agent,
            (perf_counter() - retrieval_started_at) * 1000,
        )
        logger.info(
            "[AI %s cache] retrieval_cache_hit=%s",
            self.agent,
            str(retrieval_cache_hit).lower(),
        )
        # A terceira etapa desta funcao, e a unica que nao tinha cronometro.
        # Ela e uma consulta ao banco que roda SEMPRE — cache de busca nenhum
        # a evita, de proposito (ver o docstring dela) —, entao ela e piso, e
        # nao pico. Sem medi-la, o custo dela aparecia diluido dentro do
        # `retrieval_ms` de quem estivesse lendo o log de longe.
        prices_started_at = perf_counter()
        retrieved_products = self._with_current_prices(branch_id, retrieved_products)
        logger.info(
            "[AI %s perf] current_prices_ms=%.2f",
            self.agent,
            (perf_counter() - prices_started_at) * 1000,
        )
        logger.info("[AI %s perf] context_products=%d", self.agent, len(retrieved_products))
        return retrieved_products

    def _with_current_prices(
        self,
        branch_id: uuid.UUID,
        retrieved_products: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Carimba o preco vigente em cada produto, a cada requisicao.

        O preco NAO entra no que vai para o cache (`_format_retrieved_product`
        nao o inclui), e essa e a garantia principal contra o texto do Rapi
        divergir do cartao. O cache de busca dura 20 minutos: servir preco de
        la faria TODA alteracao de preco divergir por ate 20 minutos — o texto
        com o valor velho e o cartao com o novo, na mesma resposta.

        Aqui a leitura e da linha viva de `products`, no mesmo request em que
        o cartao vai ser hidratado. O que sobra e a janela da propria chamada
        ao modelo (~1s), que `ChatService._log_price_divergence` confere
        depois.

        Produto que nao volta da consulta some do contexto: ele foi desativado
        ou ficou indisponivel depois de entrar no cache, e o modelo nao pode
        recomendar o que a hidratacao nao vai conseguir transformar em cartao.

        A consulta e por FILIAL, e e a ultima rede deste caminho: um id que
        tenha escapado do cache de outra loja nao encontra preco aqui e sai do
        contexto em vez de chegar ao modelo.
        """
        product_ids = [product["id"] for product in retrieved_products]
        prices = self.product_repository.sellable_prices_by_id(branch_id, product_ids)

        priced_products = []
        for product in retrieved_products:
            price = prices.get(product["id"])
            if price is None:
                continue
            priced_products.append({**product, "price": format_money_br(price)})
        return priced_products

    @staticmethod
    def _format_retrieved_product(product: dict[str, Any]) -> dict[str, Any]:
        """O que vai para o CACHE. Sem preco de proposito — ver `_with_current_prices`."""
        metadata = product.get("metadata") or {}
        compact_product = {
            "id": product["id"],
            "name": product["name"],
            "short_description": (
                metadata.get("short_description") or product.get("description") or ""
            )[:240],
        }
        category_name = metadata.get("category_name")
        if category_name:
            compact_product["category_name"] = category_name
        return compact_product
