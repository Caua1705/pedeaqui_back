import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, ForeignKey, Integer, Numeric, TIMESTAMP
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from src.db.base import Base


class RestaurantSetting(Base):
    __tablename__ = "restaurant_settings"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    restaurant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("restaurants.id"), unique=True, nullable=False)
    min_order_value: Mapped[Decimal | None] = mapped_column(Numeric, default=0)
    estimated_delivery_time_min: Mapped[int | None] = mapped_column(Integer, default=30)
    estimated_delivery_time_max: Mapped[int | None] = mapped_column(Integer, default=60)
    default_delivery_fee: Mapped[Decimal | None] = mapped_column(Numeric, default=0)
    service_fee_enabled: Mapped[bool | None] = mapped_column(Boolean, default=True)
    service_fee_amount: Mapped[Decimal | None] = mapped_column(Numeric, default=Decimal("0.99"))
    accepts_delivery: Mapped[bool | None] = mapped_column(Boolean, default=True)
    accepts_pickup: Mapped[bool | None] = mapped_column(Boolean, default=True)
    payment_methods: Mapped[list[str] | None] = mapped_column(
        JSONB,
        default=lambda: ["pix", "credit_card", "debit_card"],
    )
    is_open: Mapped[bool | None] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())

    restaurant = relationship("Restaurant", back_populates="settings")
