import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, ForeignKey, Integer, Numeric, TIMESTAMP
from sqlalchemy.dialects.postgresql import UUID
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
    # `is_open`, `accepts_delivery` e `accepts_pickup` NAO estao mais aqui:
    # foram para `branches` na revisao 20260818_0025. Sao o estado do dia, e
    # o estado do dia e de UMA loja — compartilha-los fechava a rede inteira
    # de uma vez.
    #
    # Os campos que sobraram nesta tabela e que a filial tambem tem sao
    # PADRAO: a coluna homonima em `branches` vem nula por default e nula
    # significa "herda daqui".
    # `payment_methods` (jsonb) tambem NAO esta mais aqui: saiu na revisao
    # 20260820_0027. Era dado morto desde que `branch_payment_methods`
    # passou a mandar em forma de pagamento — o jsonb so era ecoado em
    # `/menu` e podia discordar do que a filial de fato aceita.
    # Percentual da plataforma sobre este restaurante. Por restaurante e nao
    # constante global: e valor negociado, e muda de contrato para contrato.
    platform_commission_percent: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), nullable=False, default=Decimal("10.00")
    )
    # Atendimento por voz ligado NESTE restaurante. Falso em todo lugar ate
    # alguem dizer o contrario por SQL — cada sessao de voz custa dinheiro, e
    # ligar a base inteira de uma vez nao pode ser o padrao.
    #
    # Como `platform_commission_percent`, NAO aparece em nenhum schema do
    # painel (armadilha 17): quem paga a conta da OpenAI e a plataforma, entao
    # a decisao e nossa. Campo de tela aqui seria o lojista escolhendo quanto
    # gastamos. A chave mestra continua sendo `VOICE_ENABLED` no ambiente.
    voice_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())

    restaurant = relationship("Restaurant", back_populates="settings")
