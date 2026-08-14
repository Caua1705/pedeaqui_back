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
    restaurante inteiro mesmo com filial preenchida; os demais papeis ficam
    presos a filial quando ela esta preenchida.

    `role` responde a OUTRA pergunta, e desde a revisao 20260814_0020 ela
    tambem e aplicada: filial e ONDE, papel e O QUE. Os quatro valores estao
    em `ADMIN_USER_ROLES` e no CHECK da tabela.

    **`print_agent` e papel de MAQUINA, nao de pessoa.** E o usuario do agente
    de impressao, cuja senha fica em texto puro no `config.ini` do computador
    do balcao; ele alcanca as quatro rotas de que o agente precisa e mais
    nenhuma. Nao deve aparecer no seletor de usuarios do painel, e exige
    `branch_id` preenchido — nao existe a maquina de todas as lojas.
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
    # Instante da ultima troca de senha. E o que revoga token: o `iat` de todo
    # token emitido antes deste momento deixa de valer. Nulo = nunca trocou a
    # senha depois da revisao 0013, e ai nada e revogado.
    password_changed_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True)
    )
    created_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now()
    )
