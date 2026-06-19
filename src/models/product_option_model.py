import uuid
from decimal import Decimal

from sqlalchemy import Boolean, ForeignKey, Integer, Numeric, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from src.db.base import Base


class ProductOptionGroup(Base):
    __tablename__ = "product_option_groups"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    product_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("products.id"), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    min_select: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_select: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    is_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    sort_order: Mapped[int | None] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    product = relationship("Product", back_populates="option_groups")
    options = relationship("ProductOption", back_populates="option_group")


class ProductOption(Base):
    __tablename__ = "product_options"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    option_group_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("product_option_groups.id"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    additional_price: Mapped[Decimal] = mapped_column(Numeric, nullable=False, default=0)
    sort_order: Mapped[int | None] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    option_group = relationship("ProductOptionGroup", back_populates="options")
