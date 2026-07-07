import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, ForeignKey, Integer, Numeric, Text, TIMESTAMP
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func, text

from src.db.base import Base


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    order_number: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False, server_default=text("nextval('orders_order_number_seq'::regclass)"))
    restaurant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("restaurants.id"), nullable=False)
    branch_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("branches.id"), nullable=False)
    customer_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("customers.id"))
    customer_address_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("customer_addresses.id", ondelete="SET NULL"))
    customer_name_snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    customer_phone_snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    order_type: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    payment_method: Mapped[str | None] = mapped_column(Text)
    subtotal: Mapped[Decimal] = mapped_column(Numeric, nullable=False, default=0)
    delivery_fee: Mapped[Decimal] = mapped_column(Numeric, nullable=False, default=0)
    service_fee: Mapped[Decimal] = mapped_column(Numeric, nullable=False, default=0)
    total: Mapped[Decimal] = mapped_column(Numeric, nullable=False, default=0)
    address_street: Mapped[str | None] = mapped_column(Text)
    address_number: Mapped[str | None] = mapped_column(Text)
    address_neighborhood: Mapped[str | None] = mapped_column(Text)
    address_complement: Mapped[str | None] = mapped_column(Text)
    address_reference: Mapped[str | None] = mapped_column(Text)
    address_city: Mapped[str | None] = mapped_column(Text)
    address_state: Mapped[str | None] = mapped_column(Text)
    address_zipcode: Mapped[str | None] = mapped_column(Text)
    delivery_latitude: Mapped[Decimal | None] = mapped_column(Numeric(10, 7))
    delivery_longitude: Mapped[Decimal | None] = mapped_column(Numeric(10, 7))
    delivery_distance_km: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    delivery_travel_time_min: Mapped[int | None] = mapped_column(Integer)
    delivery_prep_time_min: Mapped[int | None] = mapped_column(Integer)
    delivery_prep_time_max: Mapped[int | None] = mapped_column(Integer)
    delivery_eta_min: Mapped[int | None] = mapped_column(Integer)
    delivery_eta_max: Mapped[int | None] = mapped_column(Integer)
    delivery_estimate_provider: Mapped[str | None] = mapped_column(Text)
    delivery_estimated_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now())

    items = relationship("OrderItem", back_populates="order")
    status_history = relationship("OrderStatusHistory", back_populates="order")
