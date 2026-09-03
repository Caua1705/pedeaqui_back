import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, ForeignKey, Integer, Numeric, Text, TIMESTAMP, and_
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, foreign, mapped_column, relationship
from sqlalchemy.sql import func, text

from src.db.base import Base
from src.models.courier_model import CourierAssignment


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    order_number: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False, server_default=text("nextval('orders_order_number_seq'::regclass)"))
    # Segredo de acompanhamento, em HASH. Sorteado na criacao, devolvido UMA
    # vez em claro a quem fez o pedido e exigido na consulta publica. O
    # order_number nao serve para isso: e sequence global e previsivel.
    #
    # A coluna `orders.tracking_token`, em texto puro, existiu ate a revisao
    # 0017 e NAO e mapeada aqui de proposito — nao ha caminho de leitura do
    # token em claro a partir do banco, e e isso que um dump vazado deixa de
    # entregar. Ver src/utils/security.py:hash_tracking_token.
    tracking_token_hash: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    restaurant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("restaurants.id"), nullable=False)
    branch_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("branches.id"), nullable=False)
    customer_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("customers.id"))
    customer_address_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("customer_addresses.id", ondelete="SET NULL"))
    customer_name_snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    customer_phone_snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    order_type: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    payment_method: Mapped[str | None] = mapped_column(Text)
    # Como o dinheiro chega neste pedido: "online" (gateway) ou "delivery"
    # (na entrega/retirada). Gravado na criacao a partir da configuracao da
    # filial, nao do que o cliente mandou — ver OrderService._resolve_payment.
    payment_flow: Mapped[str | None] = mapped_column(Text)
    # Estados possiveis em src/core/constants.py:PAYMENT_STATUSES.
    payment_status: Mapped[str] = mapped_column(Text, nullable=False, default="on_delivery")
    paid_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    # Quanto ja voltou para o cliente. Zero na esmagadora maioria dos pedidos.
    #
    # Existe porque estorno PARCIAL nao aparece em `payment_status`: no
    # Mercado Pago ele mantem o pagamento em `approved`, e o unico sinal e o
    # valor estornado na consulta do pagamento. `refunded` continua
    # significando "voltou por inteiro".
    #
    # NAO entra no calculo da comissao — ela e cobrada sobre a venda que
    # aconteceu, e essa e decisao tomada, nao pendencia. A coluna existe para
    # a decisao contraria continuar POSSIVEL: o valor so existe do lado do
    # gateway, e sem grava-lo agora nao ha como reconstitui-lo depois.
    refunded_amount: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=0, server_default="0"
    )
    # Quem processou o pagamento ("mercadopago", "sandbox"). Fica nulo em
    # pedido pago na entrega.
    payment_provider: Mapped[str | None] = mapped_column(Text)
    # Id do pagamento no gateway. E por ele que o webhook encontra o pedido,
    # dai o indice unico em (payment_provider, provider_payment_id).
    provider_payment_id: Mapped[str | None] = mapped_column(Text)
    subtotal: Mapped[Decimal] = mapped_column(Numeric, nullable=False, default=0)
    delivery_fee: Mapped[Decimal] = mapped_column(Numeric, nullable=False, default=0)
    service_fee: Mapped[Decimal] = mapped_column(Numeric, nullable=False, default=0)
    coupon_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("restaurant_coupons.id"))
    coupon_code_snapshot: Mapped[str | None] = mapped_column(Text)
    coupon_discount_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    cashback_redeemed_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    discount_total: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    total: Mapped[Decimal] = mapped_column(Numeric, nullable=False, default=0)
    # Comissao da plataforma, congelada na criacao do pedido. Os tres campos
    # andam juntos: sem base e percentual, o valor nao e conferivel depois.
    # Base = subtotal - desconto de cupom sobre PRODUTO - cashback usado. NAO
    # entram taxa de entrega, taxa de servico nem taxa do gateway — e por isso
    # cupom de frete gratis tambem nao entra: ele desconta a taxa, que ja esta
    # fora da base.
    commission_percent: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False, default=0)
    commission_base_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    commission_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    address_street: Mapped[str | None] = mapped_column(Text)
    address_number: Mapped[str | None] = mapped_column(Text)
    address_neighborhood: Mapped[str | None] = mapped_column(Text)
    address_complement: Mapped[str | None] = mapped_column(Text)
    address_reference: Mapped[str | None] = mapped_column(Text)
    address_city: Mapped[str | None] = mapped_column(Text)
    address_state: Mapped[str | None] = mapped_column(Text)
    address_zipcode: Mapped[str | None] = mapped_column(Text)
    delivery_latitude: Mapped[Decimal | None] = mapped_column(Numeric(10, 7))
    delivery_longitude: Mapped[Decimal | None] = mapped_column(Numeric(10, 7))
    delivery_distance_km: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    delivery_travel_time_min: Mapped[int | None] = mapped_column(Integer)
    delivery_prep_time_min: Mapped[int | None] = mapped_column(Integer)
    delivery_prep_time_max: Mapped[int | None] = mapped_column(Integer)
    delivery_eta_min: Mapped[int | None] = mapped_column(Integer)
    delivery_eta_max: Mapped[int | None] = mapped_column(Integer)
    # Quanto a regra de frete gratis deixou de cobrar NESTE pedido.
    #
    # Nenhum relatorio le esta coluna hoje. Ela existe porque dado nao
    # capturado na escrita nao se recupera depois: com so `delivery_fee = 0`
    # gravado, "quanto essa campanha me custou em agosto" nao tem resposta —
    # nao da para saber quanto a rota teria cobrado num pedido que ja passou.
    delivery_fee_waived: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), nullable=False, default=Decimal("0")
    )
    delivery_estimate_provider: Mapped[str | None] = mapped_column(Text)
    delivery_estimated_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now())

    items = relationship("OrderItem", back_populates="order")
    status_history = relationship("OrderStatusHistory", back_populates="order")
    coupon = relationship("RestaurantCoupon")
    coupon_redemption = relationship("CouponRedemption", back_populates="order", uselist=False)
    # A atribuicao ABERTA (`unassigned_at IS NULL`): quem esta com o pedido
    # agora. `viewonly` porque quem escreve atribuicao e `CourierRepository`,
    # e uma relacao gravavel aqui seria a segunda porta de escrita. Uma so
    # por pedido, garantida pelo indice parcial da revisao 0045.
    #
    # Existe para a listagem do painel (e o evento do stream, que monta o
    # mesmo item) carregarem o nome do motoboy com `selectinload` — sem um
    # SELECT por linha. Em objeto transiente vale `None`, que e o que a
    # suite rapida e o pedido sem motoboy produzem.
    courier_assignment = relationship(
        CourierAssignment,
        primaryjoin=lambda: and_(
            Order.id == foreign(CourierAssignment.order_id),
            CourierAssignment.unassigned_at.is_(None),
        ),
        uselist=False,
        viewonly=True,
    )
