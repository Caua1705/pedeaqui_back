import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import TIMESTAMP, ForeignKey, Numeric, Text
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
    # NOT NULL no banco, com `DEFAULT now()`. Sem `updated_at`: a linha e um
    # SNAPSHOT do que foi pedido e nao muda depois de gravada — e a tabela nao
    # tem a coluna.
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    order_item = relationship("OrderItem", back_populates="options")
