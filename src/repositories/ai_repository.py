import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session


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
        embedding: list[float],
    ) -> None:
        """Insert or update one product embedding."""
        stmt = text(
            """
            INSERT INTO ai_product_embeddings (restaurant_id, product_id, embedding)
            VALUES (:restaurant_id, :product_id, CAST(:embedding AS vector))
            ON CONFLICT (restaurant_id, product_id)
            DO UPDATE SET embedding = EXCLUDED.embedding
            """
        )
        self.db.execute(
            stmt,
            {
                "restaurant_id": restaurant_id,
                "product_id": product_id,
                "embedding": self._format_vector(embedding),
            },
        )
        self.db.commit()

    def delete_embeddings(self, restaurant_id: uuid.UUID, product_id: uuid.UUID | None = None) -> None:
        """Delete embeddings for a restaurant or for a specific product."""
        if product_id:
            stmt = text(
                """
                DELETE FROM ai_product_embeddings
                WHERE restaurant_id = :restaurant_id AND product_id = :product_id
                """
            )
            params = {"restaurant_id": restaurant_id, "product_id": product_id}
        else:
            stmt = text("DELETE FROM ai_product_embeddings WHERE restaurant_id = :restaurant_id")
            params = {"restaurant_id": restaurant_id}

        self.db.execute(stmt, params)
        self.db.commit()

    def get_restaurant_embeddings(self, restaurant_id: uuid.UUID) -> list[dict[str, Any]]:
        """Return embeddings registered for one restaurant."""
        stmt = text(
            """
            SELECT id, restaurant_id, product_id, embedding
            FROM ai_product_embeddings
            WHERE restaurant_id = :restaurant_id
            """
        )
        rows = self.db.execute(stmt, {"restaurant_id": restaurant_id}).mappings()
        return [dict(row) for row in rows]

    @staticmethod
    def _format_vector(embedding: list[float]) -> str:
        return "[" + ",".join(str(value) for value in embedding) + "]"
