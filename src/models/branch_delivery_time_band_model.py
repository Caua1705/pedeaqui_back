import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, ForeignKey, Integer, Numeric, TIMESTAMP, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from src.db.base import Base


class BranchDeliveryTimeBand(Base):
    """Quanto tempo a entrega leva, por faixa de distancia (revisao 0030).

    O prazo de uma filial era um so, e bairro perto e bairro longe nao levam
    o mesmo tempo. O tempo do Google e tempo de DIRIGIR: nao inclui ensacar,
    a segunda entrega da mesma corrida, estacionar e subir escada. O lojista
    sabe esse numero; a API nao.

    **A faixa e um TETO, e nao um intervalo.** Guardar tambem o piso
    permitiria configurar `0-5` e `6-10` e deixar o endereco de 5.4 km sem
    faixa nenhuma — um buraco que aparece no endereco de um cliente
    especifico e some quando alguem vai conferir. Com teto, a cobertura sai
    de graca: vale a primeira faixa, em ordem crescente, cujo teto alcanca a
    distancia.

    Endereco alem do ultimo teto nao cai em faixa nenhuma, e isso e valido:
    vale o prazo do Google, como antes desta tabela existir. Nao confundir
    com `branches.delivery_max_distance_km`, que e ate onde a filial ATENDE —
    sao perguntas diferentes e nao se misturam.
    """

    __tablename__ = "branch_delivery_time_bands"
    __table_args__ = (
        UniqueConstraint(
            "branch_id",
            "max_distance_km",
            name="uq_branch_delivery_time_bands_branch_distance",
        ),
        CheckConstraint("max_distance_km > 0", name="ck_branch_delivery_time_bands_distance"),
        CheckConstraint(
            "delivery_time_max >= delivery_time_min",
            name="ck_branch_delivery_time_bands_range",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    branch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("branches.id", ondelete="CASCADE"), nullable=False
    )
    max_distance_km: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False)
    delivery_time_min: Mapped[int] = mapped_column(Integer, nullable=False)
    delivery_time_max: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now()
    )

    branch = relationship("Branch", back_populates="delivery_time_bands")
