import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, ForeignKey, Integer, Numeric, Text, TIMESTAMP
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from src.db.base import Base


class Branch(Base):
    __tablename__ = "branches"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    restaurant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("restaurants.id"), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str | None] = mapped_column(Text)
    email: Mapped[str | None] = mapped_column(Text)
    address: Mapped[str] = mapped_column(Text, nullable=False)
    neighborhood: Mapped[str] = mapped_column(Text, nullable=False)
    city: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    zipcode: Mapped[str | None] = mapped_column(Text)
    address_street: Mapped[str | None] = mapped_column(Text)
    address_number: Mapped[str | None] = mapped_column(Text)
    address_neighborhood: Mapped[str | None] = mapped_column(Text)
    address_city: Mapped[str | None] = mapped_column(Text)
    address_state: Mapped[str | None] = mapped_column(Text)
    address_zipcode: Mapped[str | None] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(Text)
    whatsapp: Mapped[str | None] = mapped_column(Text)
    latitude: Mapped[Decimal | None] = mapped_column(Numeric)
    longitude: Mapped[Decimal | None] = mapped_column(Numeric)
    delivery_base_fee: Mapped[Decimal | None] = mapped_column(Numeric)
    delivery_fee_per_km: Mapped[Decimal | None] = mapped_column(Numeric)
    delivery_min_fee: Mapped[Decimal | None] = mapped_column(Numeric)
    delivery_max_fee: Mapped[Decimal | None] = mapped_column(Numeric)
    delivery_max_distance_km: Mapped[Decimal | None] = mapped_column(Numeric)
    # --- Estado do dia. NOT NULL, e NAO herda nada do restaurante. ---
    #
    # Sao o que alguem no balcao aperta durante o expediente. Estavam em
    # `restaurant_settings` e valiam para a rede inteira: fechar a filial do
    # Centro fechava a da Aldeota junto. Um padrao do restaurante para eles
    # nao responderia pergunta nenhuma — "o restaurante esta fechado mas esta
    # filial esta aberta" nao e um estado que a operacao consiga ler.
    is_open: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    accepts_delivery: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    accepts_pickup: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # --- Termo comercial. NULL significa "herda do restaurante". ---
    #
    # O valor efetivo NAO se le daqui: sai de
    # `src/services/branch_operation.resolve_branch_operation`, que e o unico
    # lugar que combina filial e padrao. Ler a coluna crua e ler "o que esta
    # sobrescrito", que quase nunca e a pergunta.
    min_order_value: Mapped[Decimal | None] = mapped_column(Numeric)
    service_fee_enabled: Mapped[bool | None] = mapped_column(Boolean)
    service_fee_amount: Mapped[Decimal | None] = mapped_column(Numeric)
    estimated_delivery_time_min: Mapped[int | None] = mapped_column(Integer)
    estimated_delivery_time_max: Mapped[int | None] = mapped_column(Integer)
    default_delivery_fee: Mapped[Decimal | None] = mapped_column(Numeric)
    is_main: Mapped[bool | None] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool | None] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())

    restaurant = relationship("Restaurant", back_populates="branches")
    business_hours = relationship("BranchBusinessHour", back_populates="branch")
    payment_methods = relationship("BranchPaymentMethod", back_populates="branch")
    printing_sectors = relationship("PrintingSector", back_populates="branch")
