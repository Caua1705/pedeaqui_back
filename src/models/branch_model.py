import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, ForeignKey, Integer, Numeric, SmallInteger, Text, TIMESTAMP
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from src.db.base import Base


class Branch(Base):
    __tablename__ = "branches"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    restaurant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("restaurants.id"), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str | None] = mapped_column(Text)
    email: Mapped[str | None] = mapped_column(Text)
    # --- Endereco: o conjunto VIVO. E o que `AdminBranchUpdate` grava e o
    # unico que `RestaurantService._build_address` le. ---
    address: Mapped[str] = mapped_column(Text, nullable=False)
    neighborhood: Mapped[str] = mapped_column(Text, nullable=False)
    city: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    zipcode: Mapped[str | None] = mapped_column(Text)
    # --- Endereco: o conjunto MORTO. Nao escreva nestas. ---
    #
    # Resto do schema pre-Alembic (estao no `schema_baseline.sql`, e nenhuma
    # revisao as toca). NADA no codigo escreve nelas, e ate a correcao do
    # `_build_address` elas eram LIDAS PRIMEIRO — venciam o conjunto vivo. O
    # efeito numa filial com elas preenchidas: o lojista corrigia o endereco
    # no painel, o painel exibia o valor novo (ele le `address`) e o app do
    # cliente continuava mostrando o antigo, sem erro e sem log.
    #
    # Hoje sao orfas e podem sair numa revisao futura — MENOS
    # `address_number`, que continua sendo lida por nao ter par vivo: nao
    # existe `branches.number` nem campo de numero no painel.
    address_street: Mapped[str | None] = mapped_column(Text)
    address_number: Mapped[str | None] = mapped_column(Text)
    address_neighborhood: Mapped[str | None] = mapped_column(Text)
    address_city: Mapped[str | None] = mapped_column(Text)
    address_state: Mapped[str | None] = mapped_column(Text)
    address_zipcode: Mapped[str | None] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(Text)
    whatsapp: Mapped[str | None] = mapped_column(Text)
    latitude: Mapped[Decimal | None] = mapped_column(Numeric)
    longitude: Mapped[Decimal | None] = mapped_column(Numeric)
    delivery_base_fee: Mapped[Decimal | None] = mapped_column(Numeric)
    delivery_fee_per_km: Mapped[Decimal | None] = mapped_column(Numeric)
    delivery_min_fee: Mapped[Decimal | None] = mapped_column(Numeric)
    delivery_max_fee: Mapped[Decimal | None] = mapped_column(Numeric)
    delivery_max_distance_km: Mapped[Decimal | None] = mapped_column(Numeric)
    # --- A taxa do ENTREGADOR. Mesmo regime das cinco de cima: so da filial,
    # sem heranca, NULL = nao configurado. ---
    #
    # E o que a loja PAGA ao motoboy por corrida, e nao o que o cliente paga:
    # nao entra em estimativa, em `orders.total` nem na comissao. E lida uma
    # vez, na atribuicao do pedido, e congelada em
    # `courier_assignments.courier_fee_snapshot` (ver `services/courier_fee.py`).
    # Motoboy pago por corrida = `base` preenchida e `per_km = 0`.
    courier_fee_base: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    courier_fee_per_km: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    # --- Estado do dia. NOT NULL, e NAO herda nada do restaurante. ---
    #
    # Sao o que alguem no balcao aperta durante o expediente. Estavam em
    # `restaurant_settings` e valiam para a rede inteira: fechar a filial do
    # Centro fechava a da Aldeota junto. Um padrao do restaurante para eles
    # nao responderia pergunta nenhuma — "o restaurante esta fechado mas esta
    # filial esta aberta" nao e um estado que a operacao consiga ler.
    is_open: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    accepts_delivery: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    accepts_pickup: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # A pausa TEMPORARIA da entrega: chuva, entregador que sumiu, avenida em
    # obra. Nulo (ou no passado) = nao pausada.
    #
    # E um PRAZO, e nao uma chave, e essa e a diferenca inteira para
    # `accepts_delivery`: a pausa se desfaz sozinha. O dia em que ela e usada
    # e o dia em que ninguem lembra de desfaze-la, e a loja amanheceria
    # aberta sem aceitar entrega — com a ausencia de pedido como unico
    # sintoma. Quem quer desligar a entrega sem prazo continua em
    # `accepts_delivery`, que e a chave estrutural.
    delivery_paused_until: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    delivery_pause_reason: Mapped[str | None] = mapped_column(Text)
    # --- Termo comercial. NULL significa "herda do restaurante". ---
    #
    # O valor efetivo NAO se le daqui: sai de
    # `src/services/branch_operation.resolve_branch_operation`, que e o unico
    # lugar que combina filial e padrao. Ler a coluna crua e ler "o que esta
    # sobrescrito", que quase nunca e a pergunta.
    min_order_value: Mapped[Decimal | None] = mapped_column(Numeric)
    service_fee_enabled: Mapped[bool | None] = mapped_column(Boolean)
    service_fee_amount: Mapped[Decimal | None] = mapped_column(Numeric)
    estimated_delivery_time_min: Mapped[int | None] = mapped_column(Integer)
    estimated_delivery_time_max: Mapped[int | None] = mapped_column(Integer)
    default_delivery_fee: Mapped[Decimal | None] = mapped_column(Numeric)
    # Frete gratis acima de um valor. O par ligado/valor existe pela mesma
    # razao do par da taxa de servico: `NULL` significa "herda", e nao ha
    # numero que signifique "desligado" — `0` seria "gratis sempre", o
    # oposto. Sem o booleano, a filial de 12 km nao teria como recusar a
    # campanha da marca.
    #
    # Resolvido, o ligado default e FALSO (ao contrario da taxa de servico):
    # frete gratis ligado por omissao da entrega de graca em nome de quem nao
    # pediu. Ver `resolve_branch_operation`.
    free_delivery_enabled: Mapped[bool | None] = mapped_column(Boolean)
    free_delivery_min_order_value: Mapped[Decimal | None] = mapped_column(Numeric)
    # Mensagem livre do lojista no rodape da via do cliente. Mesmo regime das
    # colunas acima — NULL significa "herda de `restaurant_settings`" —, com
    # uma diferenca que precisa ser lida antes de mexer: `''` NAO e nulo. A
    # string vazia e "esta loja nao imprime rodape", e e o unico jeito de uma
    # filial recusar a mensagem da marca. Quem resolve os dois e
    # `resolve_branch_operation`.
    receipt_footer_message: Mapped[str | None] = mapped_column(Text)
    # --- Quantas vias saem. SO da filial, NOT NULL, sem heranca. ---
    #
    # Descrevem o balcao (quantas impressoras, se a comanda vai grampeada no
    # pacote), nao o termo comercial da marca — como o resto da configuracao
    # de impressao, que ja pende de filial sem herdar nada. Zero e valido e
    # significa "esta via nao sai neste tipo de pedido": e a retirada que nao
    # precisa da via da sacola.
    #
    # Estas quatro NAO passam por `resolve_branch_operation`: nao ha o que
    # combinar, e ler a coluna crua e a resposta certa.
    print_customer_copies_delivery: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=1
    )
    print_production_copies_delivery: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=1
    )
    print_customer_copies_pickup: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=1
    )
    print_production_copies_pickup: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=1
    )
    is_main: Mapped[bool | None] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool | None] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())

    restaurant = relationship("Restaurant", back_populates="branches")
    business_hours = relationship("BranchBusinessHour", back_populates="branch")
    payment_methods = relationship("BranchPaymentMethod", back_populates="branch")
    printing_sectors = relationship("PrintingSector", back_populates="branch")
    delivery_time_bands = relationship("BranchDeliveryTimeBand", back_populates="branch")
