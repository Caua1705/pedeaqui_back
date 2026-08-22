import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Integer, Text, TIMESTAMP
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from src.db.base import Base


class BranchPaymentMethod(Base):
    __tablename__ = "branch_payment_methods"
    __table_args__ = (
        CheckConstraint(
            "payment_flow IN ('online', 'delivery')",
            name="ck_branch_payment_methods_payment_flow",
        ),
        CheckConstraint(
            "method_type IN ('pix', 'credit_card', 'debit_card', 'cash', "
            "'voucher', 'meal_voucher', 'other')",
            name="ck_branch_payment_methods_method_type",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    branch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("branches.id", ondelete="CASCADE"), nullable=False
    )
    payment_flow: Mapped[str] = mapped_column(Text, nullable=False)
    method_type: Mapped[str] = mapped_column(Text, nullable=False)
    brand: Mapped[str | None] = mapped_column(Text)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    icon_key: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Esta forma de pagamento gera cashback? A pergunta mora AQUI, e nao numa
    # lista propria da configuracao de cashback, porque esta ja e a tabela
    # que manda em forma de pagamento por filial — uma terceira lista de
    # metodos seria a armadilha 15 pela terceira vez.
    #
    # Verdadeiro por default: quem decide se ha cashback e
    # `cashback_rules.enabled`. Com ela desligada este campo nao faz nada.
    earns_cashback: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    requires_gateway: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    branch = relationship("Branch", back_populates="payment_methods")
