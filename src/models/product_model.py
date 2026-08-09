import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, ForeignKey, Integer, Numeric, Text, TIMESTAMP
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from src.db.base import Base


class Product(Base):
    __tablename__ = "products"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    restaurant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("restaurants.id"), nullable=False)
    category_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("categories.id"), nullable=False)
    # Setor de impressao (a praca que prepara este produto). NULO tem
    # significado proprio: o produto nao gera via de producao — e a lata de
    # refrigerante, que sai da geladeira do balcao. Ver a migracao
    # 20260809_0011 e src/services/admin_printing_service.py.
    printing_sector_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("printing_sectors.id"))
    code: Mapped[str | None] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    price: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    image_path: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool | None] = mapped_column(Boolean, default=True)
    is_available: Mapped[bool | None] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int | None] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())

    restaurant = relationship("Restaurant", back_populates="products")
    category = relationship("Category", back_populates="products")
    option_groups = relationship("ProductOptionGroup", back_populates="product")
