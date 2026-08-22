"""Como o cashback funciona numa loja: quanto gera, a partir de quanto se
resgata, e por quanto tempo o saldo vive.

Regime da revisao 20260822_0032, que e o de TERMO COMERCIAL da 20260818_0025
com uma diferenca que precisa ser sabida: **a heranca aqui e por LINHA, nao
por coluna.** Em `branches`, `NULL` numa coluna significa "herda o valor do
restaurante". Aqui a filial tem a regra INTEIRA (`branch_id` preenchido) ou
nao tem nenhuma, e nesse caso vale a linha de `branch_id IS NULL`.

O que impede a heranca por coluna e o percentual por dia da semana: ele mora
numa tabela filha, e "coluna nula" nao existe numa tabela filha. Uma regra
meio herdada — percentual base do restaurante, terca-feira da filial — nao e
explicavel para o lojista.

Quem resolve as duas em uma e `resolve_cashback_rule`. Nao leia a linha da
filial direto: sem a queda para o padrao, a filial que nunca foi configurada
responde "sem cashback" em vez de "o cashback da rede".
"""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    TIMESTAMP,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from src.db.base import Base


class CashbackRule(Base):
    __tablename__ = "cashback_rules"
    __table_args__ = (
        CheckConstraint(
            "default_percent >= 0 AND default_percent <= 100",
            name="ck_cashback_rules_default_percent",
        ),
        CheckConstraint("min_redeem_balance >= 0", name="ck_cashback_rules_min_redeem_balance"),
        CheckConstraint("expiry_days > 0", name="ck_cashback_rules_expiry_days"),
        ForeignKeyConstraint(
            ["restaurant_id", "branch_id"],
            ["branches.restaurant_id", "branches.id"],
            name="fk_cashback_rules_branch_do_restaurante",
        ),
        # Uma sobrescrita por filial, e UMA padrao por restaurante. Sao dois
        # indices porque UNIQUE comum aceita varios NULL.
        Index(
            "ux_cashback_rules_branch",
            "restaurant_id",
            "branch_id",
            unique=True,
            postgresql_where=text("branch_id IS NOT NULL"),
        ),
        Index(
            "ux_cashback_rules_padrao_do_restaurante",
            "restaurant_id",
            unique=True,
            postgresql_where=text("branch_id IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    restaurant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("restaurants.id", ondelete="CASCADE"), nullable=False
    )
    # NULL = a regra padrao da rede.
    branch_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("branches.id", ondelete="CASCADE")
    )
    # A chave geral. Falsa em todo lugar ate alguem ligar: enquanto for
    # falsa nao credita, nao resgata e nao expira.
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # O percentual dos dias que nao tem linha propria em
    # `cashback_rule_weekdays` — que sao quase todos, quase sempre.
    default_percent: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), nullable=False, default=Decimal("0")
    )
    min_redeem_balance: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, default=Decimal("0")
    )
    # Dias sem pedido ate o saldo vencer. A contagem e a partir do ULTIMO
    # PEDIDO, nao da data do credito: e o que faz cada pedido novo empurrar a
    # validade do saldo inteiro para a frente.
    expiry_days: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    weekdays = relationship(
        "CashbackRuleWeekday",
        back_populates="rule",
        cascade="all, delete-orphan",
    )


class CashbackRuleWeekday(Base):
    """O percentual de UM dia da semana. Alavanca para mover o dia fraco.

    **Dia ausente daqui herda `default_percent` da propria regra**, e nunca
    zero. E o oposto do `PUT` de horarios (armadilha 3), onde dia ausente
    significa dia fechado — e e de proposito: ausente valendo zero faria o
    lojista que configurou so a terca de 10% desligar o cashback dos outros
    seis dias, sem erro e sem log.

    `weekday` 0 = SEGUNDA, como `datetime.weekday()` e como
    `branch_business_hours` (armadilha 1). O `getDay()` do JavaScript e
    0 = domingo, e o painel que mandar o numero do JS grava a terca no dia
    errado.
    """

    __tablename__ = "cashback_rule_weekdays"
    __table_args__ = (
        CheckConstraint("weekday BETWEEN 0 AND 6", name="ck_cashback_rule_weekdays_weekday"),
        CheckConstraint(
            "percent >= 0 AND percent <= 100",
            name="ck_cashback_rule_weekdays_percent",
        ),
    )

    # Chave primaria composta, sem `id` sintetico: a chave natural ja e
    # unica, e um surrogate so criaria a chance de existirem duas linhas para
    # a mesma terca-feira.
    rule_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("cashback_rules.id", ondelete="CASCADE"),
        primary_key=True,
    )
    weekday: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    percent: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)

    rule = relationship("CashbackRule", back_populates="weekdays")
