import logging
import uuid
from time import perf_counter
from typing import Any

from sqlalchemy.orm import Session

from src.ai.services.chat_cache import chat_cache
from src.ai.services.embedding_service import EmbeddingService
from src.repositories.ai_repository import AIRepository

logger = logging.getLogger("uvicorn.error")


class RetrievalService:
    """Retrieve restaurant products that are relevant to a user question."""

    def __init__(self, db: Session):
        self.embedding_service = EmbeddingService()
        self.ai_repository = AIRepository(db)

    def retrieve_products(
        self,
        restaurant_id: uuid.UUID,
        question: str,
        top_k: int = 5,
    ) -> list[dict[str, Any]]:
        """Return a compact context for the top matching products."""
        # Duas chaves, e nao uma: o vetor da pergunta sobrevive ao reindex, o
        # resultado da busca nao. Ver `ChatCache.embedding_key`/`retrieval_key`.
        embedding_cache_key = chat_cache.embedding_key(restaurant_id, question)
        retrieval_cache_key = chat_cache.retrieval_key(restaurant_id, question)

        embedding_started_at = perf_counter()
        embedding = chat_cache.get_embedding(embedding_cache_key)
        embedding_cache_hit = embedding is not None
        if embedding is None:
            embedding = self.embedding_service.generate_embedding(question)
            chat_cache.set_embedding(embedding_cache_key, embedding)
        logger.info(
            "[AI /chat perf] embedding_ms=%.2f",
            (perf_counter() - embedding_started_at) * 1000,
        )
        logger.info(
            "[AI /chat cache] embedding_cache_hit=%s",
            str(embedding_cache_hit).lower(),
        )

        retrieval_started_at = perf_counter()
        retrieved_products = chat_cache.get_retrieval(retrieval_cache_key)
        retrieval_cache_hit = retrieved_products is not None
        if retrieved_products is None:
            products = self.ai_repository.similarity_search(
                restaurant_id=restaurant_id,
                embedding=embedding,
                top_k=top_k,
            )
            retrieved_products = [
                self._format_retrieved_product(product) for product in products
            ]
            chat_cache.set_retrieval(retrieval_cache_key, retrieved_products)
        logger.info(
            "[AI /chat perf] retrieval_ms=%.2f",
            (perf_counter() - retrieval_started_at) * 1000,
        )
        logger.info(
            "[AI /chat cache] retrieval_cache_hit=%s",
            str(retrieval_cache_hit).lower(),
        )
        logger.info("[AI /chat perf] context_products=%d", len(retrieved_products))
        return retrieved_products

    @staticmethod
    def _format_retrieved_product(product: dict[str, Any]) -> dict[str, Any]:
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
