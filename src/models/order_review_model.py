import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    SmallInteger,
    Text,
    TIMESTAMP,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from src.db.base import Base


class OrderReview(Base):
    """A nota que o cliente deu para um pedido entregue.

    Uma linha por pedido, no maximo. Nao tem `branch_id`, `restaurant_id`
    nem `customer_id` de proposito: os tres vivem em `orders`, e a consulta
    do painel entra la de qualquer jeito para trazer o `order_number`.
    Repeti-los aqui abriria a chance de a avaliacao apontar para uma filial e
    o pedido para outra — ver o docstring da revisao 20260820_0028.
    """

    __tablename__ = "order_reviews"
    __table_args__ = (
        CheckConstraint("rating BETWEEN 1 AND 5", name="ck_order_reviews_rating"),
        CheckConstraint(
            "problem_tag IS NULL OR problem_tag IN "
            "('atrasou', 'veio_errado', 'veio_frio', 'faltou_item', 'qualidade', 'outro')",
            name="ck_order_reviews_problem_tag",
        ),
        UniqueConstraint("order_id", name="uq_order_reviews_order_id"),
        Index("ix_order_reviews_created_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id"), nullable=False
    )
    rating: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    # Preenchido so quando a nota e baixa: nao se pergunta o que deu errado a
    # quem disse que deu certo.
    problem_tag: Mapped[str | None] = mapped_column(Text)
    # O unico campo livre, e por isso o unico que a LGPD alcanca aqui. Sai na
    # anonimizacao e vence pela retencao; a nota fica nas duas.
    comment: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
