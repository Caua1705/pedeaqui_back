import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Text, TIMESTAMP
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func
from sqlalchemy.types import UserDefinedType

from src.db.base import Base


class Vector(UserDefinedType):
    cache_ok = True

    def get_col_spec(self, **kw: Any) -> str:
        return "vector"


class AIProductEmbedding(Base):
    __tablename__ = "ai_product_embeddings"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    restaurant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("restaurants.id"), nullable=False)
    product_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("products.id"), nullable=False)
    content: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str | None] = mapped_column(Text)
    # Maps the database column named "metadata"; "metadata" is reserved by SQLAlchemy declarative models.
    embedding_metadata: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSONB)
    # Stored as a PostgreSQL pgvector/vector value.
    embedding: Mapped[Any] = mapped_column(Vector(), nullable=False)
    # NULAVEL no banco, ao contrario das outras cinco desta rodada. Sem
    # `onupdate`, e sem tocar no `updated_at` ao lado: aquele NAO e "a hora em
    # que indexou", e sim A VERSAO DO PRODUTO QUE INDEXOU (ver
    # `docs/arquitetura.md`) — uma edicao salva durante a geracao do vetor se
    # perderia em silencio se ele virasse carimbo de relogio.
    created_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())

    restaurant = relationship("Restaurant")
    product = relationship("Product")
