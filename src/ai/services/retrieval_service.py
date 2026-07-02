import logging
import uuid
from time import perf_counter
from typing import Any

from sqlalchemy.orm import Session

from src.ai.services.embedding_service import EmbeddingService
from src.repositories.ai_repository import AIRepository
from src.utils.money import money_to_float
from src.utils.storage import build_storage_url

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
        """Generate the question embedding and return the top matching products."""
        logger.info("[AI Retrieval] Inicio da geracao do embedding")
        embedding_started_at = perf_counter()
        embedding = self.embedding_service.generate_embedding(question)
        logger.info(
            "[AI /chat perf] embedding_ms=%.2f",
            (perf_counter() - embedding_started_at) * 1000,
        )
        logger.info("[AI Retrieval] Fim da geracao do embedding")

        logger.info("[AI Retrieval] Inicio da busca vetorial (similarity_search)")
        retrieval_started_at = perf_counter()
        products = self.ai_repository.similarity_search(
            restaurant_id=restaurant_id,
            embedding=embedding,
            top_k=top_k,
        )
        logger.info(
            "[AI /chat perf] retrieval_ms=%.2f",
            (perf_counter() - retrieval_started_at) * 1000,
        )
        logger.info("[AI Retrieval] Fim da busca vetorial (similarity_search)")
        logger.info("[AI /chat perf] context_products=%d", len(products))
        logger.info(
            "[AI Retrieval] Produtos encontrados | quantidade=%d | nomes=%s",
            len(products),
            [product["name"] for product in products],
        )
        context_started_at = perf_counter()
        retrieved_products = [self._format_product(product) for product in products]
        logger.info(
            "[AI /chat perf] context_build_ms=%.2f",
            (perf_counter() - context_started_at) * 1000,
        )
        return retrieved_products

    @staticmethod
    def _format_product(product: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": product["id"],
            "restaurant_id": product["restaurant_id"],
            "name": product["name"],
            "slug": product["slug"],
            "description": product["description"],
            "price": money_to_float(product["price"]),
            "image_url": build_storage_url(product["image_path"]),
            "metadata": product.get("metadata"),
            "similarity": product["similarity"],
        }
