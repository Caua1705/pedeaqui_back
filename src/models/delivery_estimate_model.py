import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import ForeignKey, Integer, Numeric, Text, TIMESTAMP
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from src.db.base import Base


class DeliveryEstimate(Base):
    """Estimativa de entrega ja calculada, guardada para ser reaproveitada.

    Existe por causa de dinheiro: geocodificar o endereco e calcular a rota
    sao chamadas PAGAS ao Google. O cliente ja pagava por elas em
    /delivery/estimate no checkout, e o pedido as refazia minutos depois —
    duas vezes o custo por pedido, e a conexao de banco presa durante o I/O
    externo.

    Guardar no banco e nao em cache de memoria porque o valor daqui vira a
    taxa cobrada do cliente: precisa sobreviver a deploy e valer igual em
    qualquer worker.

    O token e a unica coisa que o cliente devolve. Todo o resto (taxa,
    distancia, prazo) e lido daqui, nunca do corpo da requisicao — senao o
    cliente escolheria a propria taxa de entrega.
    """

    __tablename__ = "delivery_estimates"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    token: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    restaurant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("restaurants.id"), nullable=False
    )
    branch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("branches.id"), nullable=False
    )
    # Nulo em estimativa de visitante. Serve para o token de um cliente
    # logado nao valer para outro.
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customers.id")
    )
    # Identidade do endereco que foi estimado. E o que impede pedir
    # estimativa para o endereco perto e fechar o pedido para o distante.
    address_fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    distance_km: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    travel_time_min: Mapped[int | None] = mapped_column(Integer)
    prep_time_min: Mapped[int | None] = mapped_column(Integer)
    prep_time_max: Mapped[int | None] = mapped_column(Integer)
    eta_min: Mapped[int | None] = mapped_column(Integer)
    eta_max: Mapped[int | None] = mapped_column(Integer)
    delivery_fee: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    latitude: Mapped[Decimal | None] = mapped_column(Numeric(10, 7))
    longitude: Mapped[Decimal | None] = mapped_column(Numeric(10, 7))
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
