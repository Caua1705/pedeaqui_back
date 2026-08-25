"""Cartao salvo do cliente, por restaurante.

## O que NUNCA esta aqui

Nao existe coluna de numero de cartao, de CVV nem de validade completa em
texto que sirva para cobrar. O que se guarda e o `provider_card_id` — um
identificador OPACO que so vale dentro da conta do Mercado Pago que o
emitiu — mais bandeira e ultimos quatro digitos, que existem para a pessoa
reconhecer o cartao na tela e nada mais. **Se um dia aparecer PAN ou CVV
neste arquivo, o perimetro de PCI mudou e a integracao saiu do padrao de
tokenizacao.**

## Por que o cartao pende do RESTAURANTE, e nao so do cliente

A credencial do Mercado Pago e do lojista (ver
`restaurant_payment_credentials`): a cobranca nasce na conta dele e o
dinheiro cai na conta dele. O "customer" do Mercado Pago, e os cartoes
pendurados nele, existem **dentro daquela conta** — o `card_id` salvo no
Junior da Picanha nao e cobravel pela conta de outro restaurante, e nem
sequer e legivel por ela.

Consequencia que vale escrever: **no dia em que o split entrar** e as
cobrancas passarem a nascer numa conta de marketplace, estes ids param de
valer e os clientes recadastram o cartao. Isso e sabido e aceito; nao e um
efeito colateral esquecido.

## Por que `environment` esta na chave

Mesmo motivo de `restaurant_payment_credentials`: a conta de teste e a de
producao sao contas DIFERENTES. Um customer criado com a credencial de
teste nao existe para a de producao, e cobrar o `card_id` de uma na outra
da 404 do gateway. Sem esta coluna, virar `MERCADOPAGO_ENVIRONMENT` faria
o backend tentar cobrar cartoes que a conta ativa nunca viu.
"""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Integer, Text, TIMESTAMP, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from src.db.base import Base


class CustomerPaymentProfile(Base):
    """O "customer" do Mercado Pago de UMA pessoa em UM restaurante.

    E a linha que amarra o nosso `customers.id` ao id que o Mercado Pago
    gerou na conta daquele lojista. Os cartoes pendem dela, e nao direto de
    `customers`, porque e ela que carrega o unico contexto em que um
    `card_id` significa alguma coisa: (conta do restaurante, ambiente).
    """

    __tablename__ = "customer_payment_profiles"
    __table_args__ = (
        CheckConstraint(
            "environment IN ('test', 'production')",
            name="ck_customer_payment_profiles_environment",
        ),
        UniqueConstraint(
            "customer_id", "restaurant_id", "environment",
            name="uq_customer_payment_profiles_customer_restaurant_environment",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customers.id", ondelete="CASCADE"), nullable=False
    )
    restaurant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("restaurants.id", ondelete="CASCADE"), nullable=False
    )
    environment: Mapped[str] = mapped_column(Text, nullable=False)
    # Id opaco do Mercado Pago. Nao e segredo (sozinho ele nao cobra nada:
    # a cobranca exige o access_token do restaurante), mas tambem nao ha
    # motivo para ele sair numa resposta de API.
    provider_customer_id: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    cards = relationship(
        "CustomerSavedCard", back_populates="profile", cascade="all, delete-orphan"
    )


class CustomerSavedCard(Base):
    """Um cartao salvo. Bandeira, quatro digitos e um id opaco — mais nada.

    `last_four_digits` e `brand` sao dado de TELA: servem para a pessoa
    dizer "esse aqui, o Visa final 4321". Nao se cobra com eles, nao se
    reconstroi o numero a partir deles, e o CHECK garante que a coluna nao
    vire depositario acidental do numero inteiro.
    """

    __tablename__ = "customer_saved_cards"
    __table_args__ = (
        CheckConstraint(
            "last_four_digits ~ '^[0-9]{4}$'",
            name="ck_customer_saved_cards_last_four_digits",
        ),
        UniqueConstraint(
            "payment_profile_id", "provider_card_id",
            name="uq_customer_saved_cards_profile_card",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    payment_profile_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("customer_payment_profiles.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Id opaco do cartao dentro da conta do restaurante no Mercado Pago.
    provider_card_id: Mapped[str] = mapped_column(Text, nullable=False)
    # `payment_method_id` deles ("visa", "master", "elo") — NAO o
    # "credit_card" do nosso vocabulario. Guardado porque a cobranca com
    # cartao salvo precisa manda-lo de volta, e o valor gravado e mais
    # confiavel que um que viesse do cliente.
    brand: Mapped[str] = mapped_column(Text, nullable=False)
    last_four_digits: Mapped[str] = mapped_column(Text, nullable=False)
    # Mes/ano de validade. Sozinhos nao cobram nada e nao identificam o
    # cartao; ficam para a tela poder marcar o vencido em cinza.
    expiration_month: Mapped[int | None] = mapped_column(Integer, nullable=True)
    expiration_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    profile = relationship("CustomerPaymentProfile", back_populates="cards")
