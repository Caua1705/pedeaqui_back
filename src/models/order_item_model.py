import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import ForeignKey, Integer, Numeric, Text, TIMESTAMP
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from src.db.base import Base


class OrderItem(Base):
    __tablename__ = "order_items"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    order_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("orders.id"), nullable=False)
    product_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("products.id"))
    product_code_snapshot: Mapped[str | None] = mapped_column(Text)
    product_name_snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    product_description_snapshot: Mapped[str | None] = mapped_column(Text)
    unit_price_snapshot: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    observation: Mapped[str | None] = mapped_column(Text)
    total: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    created_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())

    order = relationship("Order", back_populates="items")
    options = relationship("OrderItemOption", back_populates="order_item")
