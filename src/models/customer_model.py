import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Boolean, Date, ForeignKey, Integer, Numeric, Text, TIMESTAMP
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from src.db.base import Base


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    name: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    phone: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    # Nao e mais coletado nem lido: a revisao 0019 anulou os existentes e
    # NADA escreve aqui. A coluna sobrevive so para dar uma revisao de
    # margem antes do DROP, caso apareca um uso que o levantamento da LGPD
    # nao viu. Ver `docs/lgpd-proposta.md`, secao 2.3.
    cpf: Mapped[str | None] = mapped_column(Text, unique=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    # Marco de revogacao: JWT emitido antes deste instante nao vale mais.
    # Ver AuthService.get_customer_from_token.
    password_changed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    birth_date: Mapped[date] = mapped_column(Date, nullable=False)
    email_verified_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    phone_verified_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    marketing_opt_in: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    privacy_accepted_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Quando a pessoa exerceu o direito de exclusao (LGPD, Art. 18, VI). Nao
    # e o mesmo que `is_active=false`, que e suspensao reversivel com os
    # dados intactos: aqui os dados ja nao existem. Ver
    # `CustomerAnonymizationService` e docs/lgpd-fase2-exclusao-de-conta.md.
    anonymized_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now())

    addresses = relationship("CustomerAddress", back_populates="customer")


class CustomerAddress(Base):
    __tablename__ = "customer_addresses"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    customer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("customers.id"), nullable=False)
    client_reference: Mapped[str | None] = mapped_column(Text)
    label: Mapped[str | None] = mapped_column(Text)
    street: Mapped[str] = mapped_column(Text, nullable=False)
    number: Mapped[str] = mapped_column(Text, nullable=False)
    neighborhood: Mapped[str] = mapped_column(Text, nullable=False)
    complement: Mapped[str | None] = mapped_column(Text)
    reference: Mapped[str | None] = mapped_column(Text)
    city: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str | None] = mapped_column(Text)
    zipcode: Mapped[str | None] = mapped_column(Text)
    latitude: Mapped[Decimal | None] = mapped_column(Numeric(10, 7))
    longitude: Mapped[Decimal | None] = mapped_column(Numeric(10, 7))
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now())

    customer = relationship("Customer", back_populates="addresses")


class EmailVerificationCode(Base):
    __tablename__ = "email_verification_codes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    customer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("customers.id"), nullable=False)
    email: Mapped[str] = mapped_column(Text, nullable=False)
    code_hash: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    attempts_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    resend_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    used_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())


class PasswordResetCode(Base):
    __tablename__ = "password_reset_codes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    customer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("customers.id"), nullable=False)
    email: Mapped[str] = mapped_column(Text, nullable=False)
    code_hash: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    attempts_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    resend_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reset_token_hash: Mapped[str | None] = mapped_column(Text)
    reset_token_expires_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())


class AccountDeletionCode(Base):
    """O codigo que confirma a EXCLUSAO da conta. Revisao 0050.

    Tabela propria, e nao uma coluna `purpose` em `EmailVerificationCode`: sao
    tres fluxos consumindo codigo de seis digitos (verificar e-mail, ligar a
    conta do Google, apagar a conta), e um codigo que sirva a mais de um deles
    e um codigo que faz a coisa errada — apagar a conta de quem so queria
    entrar, sem desfazer.

    Separadas, a confusao deixa de ser um `if` que alguem pode remover:
    `latest_unused_email_code` consulta a outra tabela e nao enxerga esta.

    Mesma forma da tabela de verificacao, menos as duas colunas do token de
    reset — nao ha token aqui: o codigo certo apaga na mesma requisicao.
    """

    __tablename__ = "account_deletion_codes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    customer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("customers.id"), nullable=False)
    email: Mapped[str] = mapped_column(Text, nullable=False)
    code_hash: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    attempts_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    resend_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    used_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    # NOT NULL nos DOIS lados, ao contrario das duas tabelas de codigo acima.
    # Elas sao herdadas (o schema nasceu a mao, e `divergencias_orm_schema.py`
    # as conta ate hoje); esta nasce na revisao 0050, e tabela nova nao tem
    # motivo para nascer divergindo. Ver a armadilha 50.
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
