import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Text, TIMESTAMP, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from src.db.base import Base


class RestaurantPaymentCredential(Base):
    """Credencial do gateway de pagamento de UM restaurante.

    Uma linha por (restaurante, ambiente): o mesmo restaurante tem uma
    credencial de teste e, quando o Mercado Pago aprovar a conta, uma de
    producao — as duas guardadas ao mesmo tempo, sem apagar uma para
    cadastrar a outra. Qual das duas vale em cada request e decidido pela
    settings.MERCADOPAGO_ENVIRONMENT (ver src/services/payment_credential_service.py),
    entao trocar de teste para producao e mudar uma variavel de ambiente, nao
    o banco nem o codigo.

    `access_token` e a credencial da CONTA DO RESTAURANTE no Mercado Pago
    (nao e segredo nosso, e dele): fica cifrado em repouso e nunca deve ir
    para log. `public_key` nao e sigilosa — o proprio Mercado Pago manda
    expor no frontend — e por isso fica em texto puro.
    """

    __tablename__ = "restaurant_payment_credentials"
    __table_args__ = (
        CheckConstraint(
            "environment IN ('test', 'production')",
            name="ck_restaurant_payment_credentials_environment",
        ),
        UniqueConstraint(
            "restaurant_id", "environment",
            name="uq_restaurant_payment_credentials_restaurant_environment",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    restaurant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("restaurants.id", ondelete="CASCADE"), nullable=False
    )
    environment: Mapped[str] = mapped_column(Text, nullable=False)
    public_key: Mapped[str] = mapped_column(Text, nullable=False)
    # Cifrado com Fernet (src/utils/crypto.py), chave em
    # settings.PAYMENT_CREDENTIALS_ENCRYPTION_KEY. Nunca decodificar isto em
    # um log ou em uma resposta de API.
    access_token_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    restaurant = relationship("Restaurant")
