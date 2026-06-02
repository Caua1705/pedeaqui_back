import uuid
from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Integer, Text, TIMESTAMP
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from src.db.base import Base


class RestaurantBanner(Base):
    __tablename__ = "restaurant_banners"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    restaurant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("restaurants.id"), nullable=False)
    banner_type: Mapped[str] = mapped_column(Text, nullable=False, default="hero")
    image_path: Mapped[str] = mapped_column(Text, nullable=False)
    sort_order: Mapped[int | None] = mapped_column(Integer, default=0)
    is_active: Mapped[bool | None] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())
