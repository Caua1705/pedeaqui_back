import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, ForeignKey, Index, Numeric, Text, TIMESTAMP, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from src.db.base import Base


class CashbackTransaction(Base):
    __tablename__ = "cashback_transactions"
    __table_args__ = (
        CheckConstraint(
            "type IN ('earned', 'redeemed', 'expired', 'cancelled', 'adjustment')",
            name="ck_cashback_transactions_type",
        ),
        CheckConstraint(
            "status IN ('pending', 'available', 'used', 'cancelled', 'expired')",
            name="ck_cashback_transactions_status",
        ),
        Index("ix_cashback_transactions_customer_id", "customer_id"),
        Index("ix_cashback_transactions_customer_id_status", "customer_id", "status"),
        Index("ix_cashback_transactions_created_at", "created_at"),
        Index(
            "ux_cashback_transactions_idempotency_key",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customers.id", ondelete="CASCADE"), nullable=False
    )
    restaurant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("restaurants.id", ondelete="SET NULL")
    )
    order_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id", ondelete="SET NULL")
    )
    type: Mapped[str] = mapped_column(Text, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    metadata_: Mapped[dict] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    idempotency_key: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
