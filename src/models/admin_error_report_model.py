import uuid
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, Text, TIMESTAMP
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from src.db.base import Base


class AdminErrorReport(Base):
    """O "deu erro" do lojista, com o que faltava para reproduzi-lo.

    Tres campos vem do CORPO (o que a pessoa sabe) e tres do TOKEN (onde ela
    esta). Nenhum dos do token aceita vir do corpo — escopo de lojista sai do
    token, sempre. Ver a revisao 20260823_0038.

    **Nao tem `customer_id` e nunca vai ter**, e por isso a retencao e o
    mecanismo de exclusao desta tabela (armadilha 38): o texto e escrito por
    um lojista, sobre um cliente que nao tem como saber que este registro
    existe. Prazo em `admin_error_report_service.error_report_retention_cutoff`.
    """

    __tablename__ = "admin_error_reports"
    __table_args__ = (
        CheckConstraint(
            "btrim(description) <> ''",
            name="ck_admin_error_reports_description_not_blank",
        ),
        Index("ix_admin_error_reports_created_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    restaurant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("restaurants.id", ondelete="CASCADE"), nullable=False
    )
    # Nulo = o relato nao aponta uma loja, e nao "loja desconhecida". Dono
    # enxerga todas as filiais e nao esta em nenhuma.
    branch_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("branches.id", ondelete="SET NULL")
    )
    admin_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("admin_users.id", ondelete="SET NULL")
    )
    # Os dois campos livres, e por isso os dois que a redacao de credencial
    # atravessa antes do INSERT.
    description: Mapped[str] = mapped_column(Text, nullable=False)
    error_log: Mapped[str | None] = mapped_column(Text)
    screen: Mapped[str | None] = mapped_column(Text)
    # Numero solto: sem FK e sem conferencia, porque e o que uma pessoa
    # digitou olhando para a tela. E a alternativa estruturada a escrever
    # "o pedido do Joao, telefone ..." no texto livre.
    order_number: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
