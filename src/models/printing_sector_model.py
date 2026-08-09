import uuid
from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Integer, Text, TIMESTAMP
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from src.db.base import Base


class PrintingSector(Base):
    """Uma impressora de producao dentro de uma filial.

    Pende de `branch_id` e nao de restaurante porque a impressora e um
    objeto fisico: a "Cozinha" da unidade do Centro e outra maquina que a
    "Cozinha" da Aldeota. Ver a migracao 20260809_0011.

    `is_active` e `sort_order` sao NOT NULL (a coluna tem default no banco),
    ao contrario do resto do cardapio, que os deixou nullable: aqui a
    listagem ordena e filtra por eles sem tratar nulo.
    """

    __tablename__ = "printing_sectors"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    branch_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("branches.id"), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())

    branch = relationship("Branch", back_populates="printing_sectors")
