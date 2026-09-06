import uuid
from datetime import datetime

from sqlalchemy import Boolean, Text, TIMESTAMP
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from src.db.base import Base


class Restaurant(Base):
    __tablename__ = "restaurants"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    name: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    # O que o ASSISTENTE precisa saber sobre a casa. Separado da `description`
    # na revisao 20260823_0034 porque os dois destinos pedem textos opostos:
    # ela e vitrine (`RestaurantPublicResponse`, o cliente decide pedir por
    # ela), esta e prompt (`ChatService._build_restaurant_context`).
    #
    # NAO sai em resposta publica nenhuma, e a leitura do prompt NAO cai para
    # `description` quando isto e nulo: o fallback preservaria o anuncio no
    # prompt para todo mundo que nunca preencher.
    assistant_notes: Mapped[str | None] = mapped_column(Text)
    logo_path: Mapped[str | None] = mapped_column(Text)
    cover_path: Mapped[str | None] = mapped_column(Text)
    primary_color: Mapped[str | None] = mapped_column(Text, default="#D95C04")
    secondary_color: Mapped[str | None] = mapped_column(Text, default="#111111")
    is_active: Mapped[bool | None] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())

    branches = relationship("Branch", back_populates="restaurant")
    settings = relationship("RestaurantSetting", back_populates="restaurant", uselist=False)
    categories = relationship("Category", back_populates="restaurant")
    products = relationship("Product", back_populates="restaurant")
