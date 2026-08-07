import uuid
from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Text, TIMESTAMP
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from src.db.base import Base


class AdminUser(Base):
    """Lojista. Sempre pertence a um restaurante — e esse vinculo que passa
    a delimitar o que ele enxerga nas rotas /admin.

    `branch_id` opcional: nulo significa acesso a todas as filiais do
    restaurante. Desde a Fase 3 o campo E aplicado nas rotas /admin, em um
    lugar so (`src/api/dependencies/admin_scope.py`): "owner" enxerga o
    restaurante inteiro mesmo com filial preenchida; "manager" e "attendant"
    ficam presos a filial quando ela esta preenchida.
    """

    __tablename__ = "admin_users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    restaurant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("restaurants.id"), nullable=False
    )
    branch_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("branches.id")
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now()
    )
