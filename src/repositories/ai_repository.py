import uuid
from typing import Any

from sqlalchemy import bindparam, delete, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from src.models.ai_product_embedding_model import AIProductEmbedding


class AIRepository:
    """Database access for AI product embeddings."""

    def __init__(self, db: Session):
        self.db = db

    def similarity_search(
        self,
        restaurant_id: uuid.UUID,
        embedding: list[float],
        top_k: int = 5,
    ) -> list[dict[str, Any]]:
        """Return the most similar active products for one restaurant."""
        stmt = text(
            """
            SELECT
                p.id,
                p.restaurant_id,
                p.name,
                p.slug,
                p.description,
                p.price,
                p.image_path,
                ape.metadata AS metadata,
                1 - (ape.embedding <=> CAST(:embedding AS vector)) AS similarity
            FROM ai_product_embeddings ape
            JOIN products p ON p.id = ape.product_id
            WHERE
                ape.restaurant_id = :restaurant_id
                AND p.restaurant_id = :restaurant_id
                AND p.is_active IS TRUE
                AND p.is_available IS TRUE
            ORDER BY ape.embedding <=> CAST(:embedding AS vector)
            LIMIT :top_k
            """
        )
        rows = self.db.execute(
            stmt,
            {
                "restaurant_id": restaurant_id,
                "embedding": self._format_vector(embedding),
                "top_k": top_k,
            },
        ).mappings()
        return [dict(row) for row in rows]

    def save_embedding(
        self,
        restaurant_id: uuid.UUID,
        product_id: uuid.UUID,
        content: str,
        content_hash: str,
        metadata: dict[str, Any],
        embedding: list[float],
    ) -> None:
        """Insert or update one product embedding."""
        stmt = (
            text(
                """
                INSERT INTO ai_product_embeddings (
                    restaurant_id,
                    product_id,
                    content,
                    content_hash,
                    metadata,
                    embedding,
                    updated_at
                )
                VALUES (
                    :restaurant_id,
                    :product_id,
                    :content,
                    :content_hash,
                    :metadata,
                    CAST(:embedding AS vector),
                    NOW()
                )
                ON CONFLICT (restaurant_id, product_id)
                DO UPDATE SET
                    embedding = EXCLUDED.embedding,
                    content = EXCLUDED.content,
                    content_hash = EXCLUDED.content_hash,
                    metadata = EXCLUDED.metadata,
                    updated_at = NOW()
                """
            )
            .bindparams(bindparam("metadata", type_=JSONB))
        )
        self.db.execute(
            stmt,
            {
                "restaurant_id": restaurant_id,
                "product_id": product_id,
                "content": content,
                "content_hash": content_hash,
                "metadata": metadata,
                "embedding": self._format_vector(embedding),
            },
        )
        self.db.commit()

    def get_embedding_by_product(
        self,
        restaurant_id: uuid.UUID,
        product_id: uuid.UUID,
    ) -> dict[str, Any] | None:
        """Return sync metadata for one product embedding."""
        stmt = select(
            AIProductEmbedding.content_hash,
            AIProductEmbedding.updated_at,
        ).where(
            AIProductEmbedding.restaurant_id == restaurant_id,
            AIProductEmbedding.product_id == product_id,
        )
        row = self.db.execute(stmt).mappings().one_or_none()
        return dict(row) if row else None

    def delete_embeddings(self, restaurant_id: uuid.UUID, product_id: uuid.UUID | None = None) -> None:
        """Delete embeddings for a restaurant or for a specific product."""
        stmt = delete(AIProductEmbedding).where(AIProductEmbedding.restaurant_id == restaurant_id)
        if product_id:
            stmt = stmt.where(AIProductEmbedding.product_id == product_id)

        self.db.execute(stmt)
        self.db.commit()

    def get_restaurant_embeddings(self, restaurant_id: uuid.UUID) -> list[dict[str, Any]]:
        """Return embeddings registered for one restaurant."""
        stmt = select(
            AIProductEmbedding.id,
            AIProductEmbedding.restaurant_id,
            AIProductEmbedding.product_id,
            AIProductEmbedding.embedding,
        ).where(AIProductEmbedding.restaurant_id == restaurant_id)
        rows = self.db.execute(stmt).mappings()
        return [dict(row) for row in rows]

    @staticmethod
    def _format_vector(embedding: list[float]) -> str:
        return "[" + ",".join(str(value) for value in embedding) + "]"
