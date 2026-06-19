import uuid
from decimal import Decimal

from sqlalchemy import ForeignKey, Numeric, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from src.db.base import Base


class OrderItemOption(Base):
    __tablename__ = "order_item_options"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    order_item_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("order_items.id"), nullable=False)
    option_group_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("product_option_groups.id"),
        nullable=False,
    )
    option_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("product_options.id"), nullable=False)
    option_group_name_snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    option_name_snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    additional_price_snapshot: Mapped[Decimal] = mapped_column(Numeric, nullable=False)

    order_item = relationship("OrderItem", back_populates="options")
