import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, TIMESTAMP, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from src.db.base import Base


class CouponClaim(Base):
    """O cliente RESGATOU este cupom. Nao usou — resgatou.

    A distincao e a razao de a tabela existir, e ela nao e vocabulario:
    `coupon_redemptions` tem `order_id NOT NULL` e conta USO, que e o numero
    que barra o proximo cliente no teto da campanha. Um resgate gravado la
    passaria a contar como uso gente que so digitou um codigo no Clube e
    nunca fechou pedido — e o cupom de 100 usos se esgotaria sem ter dado
    100 descontos.

    O que o resgate concede e uma coisa so: **visibilidade**. Um cupom
    `private` resgatado passa a aparecer na lista daquele cliente e a poder
    ser escolhido no checkout. Toda regra de valor continua sendo conferida
    na hora do pedido, sobre a sacola daquele momento — janela, minimo, teto
    total, teto por cliente, cooldown e primeira-compra. Resgatar nao
    congela nem antecipa nenhuma delas.

    Sem coluna de status e sem coluna de valor, de proposito: nao ha estado
    a percorrer aqui. Ou a linha existe, e o cliente enxerga o cupom, ou nao
    existe. O UNIQUE `(coupon_id, customer_id)` fecha o resto — resgatar
    duas vezes e a mesma coisa que resgatar uma, e a rota e idempotente
    justamente por causa dele.
    """

    __tablename__ = "coupon_claims"
    __table_args__ = (
        UniqueConstraint("coupon_id", "customer_id", name="uq_coupon_claims_coupon_customer"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    coupon_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("restaurant_coupons.id"), nullable=False)
    customer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("customers.id"), nullable=False)
    claimed_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    coupon = relationship("RestaurantCoupon")
