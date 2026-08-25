from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.schemas.common_schema import BaseResponse, StatusHistoryResponse
from src.utils.normalization import normalize_digits


MAX_ITEMS_PER_ORDER = 100
MAX_OPTIONS_PER_ITEM = 30
MAX_QUANTITY_PER_ITEM = 99


class CustomerInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    phone: str = Field(min_length=8, max_length=20)

    @field_validator("phone")
    @classmethod
    def validate_and_normalize_phone(cls, value: str) -> str:
        # Guarda so digitos. O telefone do pedido guest e a UNICA chave que o
        # cliente tem para consultar o proprio pedido depois, e a busca compara
        # por igualdade exata: se o formato digitado entrasse cru, quem digitou
        # "(85) 99999-9999" nunca acharia o pedido procurando por
        # "85999999999". Mesma normalizacao do cadastro (customer_schema).
        phone = normalize_digits(value)
        if len(phone) < 8:
            raise ValueError("invalid phone")
        return phone


class AddressInput(BaseModel):
    street: str | None = Field(default=None, max_length=200)
    number: str | None = Field(default=None, max_length=20)
    neighborhood: str | None = Field(default=None, max_length=120)
    complement: str | None = Field(default=None, max_length=120)
    reference: str | None = Field(default=None, max_length=200)
    city: str | None = Field(default=None, max_length=120)
    state: str | None = Field(default=None, max_length=40)
    zipcode: str | None = Field(default=None, max_length=20)
    latitude: Decimal | None = Field(default=None, ge=-90, le=90)
    longitude: Decimal | None = Field(default=None, ge=-180, le=180)


class OrderItemSelectedOptionInput(BaseModel):
    option_group_id: UUID
    option_id: UUID


class OrderItemInput(BaseModel):
    product_id: UUID
    quantity: int = Field(default=1, ge=1, le=MAX_QUANTITY_PER_ITEM)
    observation: str | None = Field(default=None, max_length=300)
    selected_options: list[OrderItemSelectedOptionInput] = Field(
        default_factory=list,
        max_length=MAX_OPTIONS_PER_ITEM,
    )


class CreateOrderRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    branch_id: UUID
    customer: CustomerInput | None = None
    customer_address_id: UUID | None = None
    order_type: str = Field(max_length=30)
    payment_method: str | None = Field(default=None, max_length=50)
    address: AddressInput | None = None
    # Token devolvido por POST /delivery/estimate. Com ele, o pedido
    # reaproveita a estimativa ja calculada em vez de refazer geocode e
    # rota no Google. NAO traz valor nenhum dentro: taxa, distancia e prazo
    # continuam saindo do banco. Token ausente, vencido ou de outro
    # endereco so faz o servidor recalcular.
    delivery_estimate_token: str | None = Field(default=None, max_length=100)
    notes: str | None = Field(default=None, max_length=500)
    items: list[OrderItemInput] = Field(min_length=1, max_length=MAX_ITEMS_PER_ORDER)
    coupon_id: UUID | None = None
    coupon_code: str | None = Field(default=None, min_length=1, max_length=100)
    # "Usar meu saldo neste pedido". Booleano, e nao valor: nenhum preco vem
    # do cliente, e um `Decimal` enviado aqui teria que ser conferido
    # refazendo a conta inteira do resgate — que e a propria conta. Quanto
    # entra sai de `CashbackService.amount_to_redeem`, limitado ao saldo e ao
    # subtotal ja descontado o cupom.
    #
    # ENTRA no fingerprint da idempotencia, e tem que entrar: ele muda o
    # total do pedido, entao a mesma chave com este campo diferente e
    # conflito de verdade, e recusar e a resposta certa. O preco disso e 24h
    # de 422 para chaves em voo no deploy — ver `_idempotency_fingerprint` e
    # a armadilha 37.
    use_cashback: bool = False

    @model_validator(mode="after")
    def validate_single_coupon(self):
        if self.coupon_id is not None and self.coupon_code is not None:
            raise ValueError("Informe somente coupon_id ou coupon_code")
        if self.coupon_code:
            self.coupon_code = self.coupon_code.strip().upper()
        return self


class CreateOrderResponse(BaseModel):
    id: UUID
    order_number: int
    # Guarde: e o que permite acompanhar o pedido sem login. Devolvido
    # somente aqui, para quem acabou de criar o pedido.
    tracking_token: str
    status: str
    # O front usa estes dois para decidir o proximo passo: `payment_flow`
    # "online" com `payment_status` "pending" significa "leve o cliente para
    # o checkout do gateway".
    payment_flow: str
    payment_status: str
    subtotal: float
    delivery_fee: float
    service_fee: float
    coupon_code: str | None = None
    coupon_discount_amount: Decimal = Decimal("0.00")
    cashback_redeemed_amount: Decimal = Decimal("0.00")
    discount_total: Decimal = Decimal("0.00")
    total: float
    message: str


class OrderItemOptionResponse(BaseResponse):
    """Um adicional escolhido, congelado como estava no cardapio.

    Tudo aqui e snapshot pelo mesmo motivo do produto: o lojista renomeia
    "Espaguete" ou muda o preco depois, e a comanda de um pedido de ontem
    precisa continuar dizendo o que foi vendido naquele dia.
    """

    id: UUID
    option_id: UUID
    option_name_snapshot: str
    additional_price_snapshot: float


class OrderItemOptionGroupResponse(BaseResponse):
    """Os adicionais de um item, reunidos pelo grupo a que pertencem.

    Agrupado e nao uma lista solta de nomes porque e o grupo que da sentido
    a escolha: "Acompanhamento: espaguete" e uma TROCA (o arroz nao vai), e
    "Adicional: espaguete" e uma porcao a mais. Sem o grupo as duas chegam
    na cozinha como a mesma linha.
    """

    option_group_id: UUID
    option_group_name_snapshot: str
    options: list[OrderItemOptionResponse]


class OrderItemResponse(BaseResponse):
    """Um item da comanda.

    `unit_price_snapshot` JA inclui os adicionais de `option_groups` (ver
    `OrderService._build_order_item`): quem monta a tela nao deve somar
    `additional_price_snapshot` de novo, ou o item aparece mais caro do que
    o pedido cobrou. Os valores dos adicionais vem para a conferencia — o
    cliente que reclama do preco quer ver de onde saiu.
    """

    id: UUID
    product_id: UUID | None = None
    product_code_snapshot: str | None = None
    product_name_snapshot: str
    product_description_snapshot: str | None = None
    unit_price_snapshot: float
    quantity: int
    observation: str | None = None
    total: float
    created_at: datetime | None = None
    # Vazio para item sem complemento, que e a maioria. Lista com default
    # em vez de nulo para a tela nao precisar de dois caminhos.
    option_groups: list[OrderItemOptionGroupResponse] = Field(default_factory=list)


class OrderDetailResponse(BaseResponse):
    id: UUID
    order_number: int
    restaurant_id: UUID
    branch_id: UUID
    customer_id: UUID | None = None
    customer_address_id: UUID | None = None
    customer_name_snapshot: str
    customer_phone_snapshot: str
    order_type: str
    status: str
    payment_method: str | None = None
    payment_flow: str | None = None
    payment_status: str
    paid_at: datetime | None = None
    subtotal: float
    delivery_fee: float
    service_fee: float
    coupon_code: str | None = None
    coupon_discount_amount: Decimal = Decimal("0.00")
    cashback_redeemed_amount: Decimal = Decimal("0.00")
    discount_total: Decimal = Decimal("0.00")
    total: float
    address_street: str | None = None
    address_number: str | None = None
    address_neighborhood: str | None = None
    address_complement: str | None = None
    address_reference: str | None = None
    address_city: str | None = None
    address_state: str | None = None
    address_zipcode: str | None = None
    delivery_latitude: float | None = None
    delivery_longitude: float | None = None
    delivery_distance_km: float | None = None
    delivery_travel_time_min: int | None = None
    delivery_prep_time_min: int | None = None
    delivery_prep_time_max: int | None = None
    delivery_eta_min: int | None = None
    delivery_eta_max: int | None = None
    delivery_estimate_provider: str | None = None
    delivery_estimated_at: datetime | None = None
    notes: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    items: list[OrderItemResponse]
    status_history: list[StatusHistoryResponse]


# Teto do motivo escrito pelo cliente. Metade do teto do painel: aqui e um
# comentario espontaneo ("mudei de ideia"), la e o registro que o suporte le
# quando o cliente liga perguntando por que o pedido dele sumiu.
MAX_CUSTOMER_CANCELLATION_REASON_LENGTH = 150


class CustomerCancelOrderRequest(BaseModel):
    """Corpo do cancelamento pelo cliente. Tudo opcional, inclusive ele.

    **O motivo e OPCIONAL aqui e obrigatorio no painel**, e a assimetria e
    proposital: exigir justificativa de quem desiste de um pedido que nem
    comecou vira um campo que todo mundo preenche com "a", e o historico
    ganha ruido em vez de informacao. Quem cancelou ja fica em
    `order_status_history.changed_by`, que e a pergunta que o suporte faz.

    Nao ha campo de status, e nao havera: esta rota so cancela, e so ate
    `accepted`. Ver CustomerOrderCancelService.
    """

    reason: str | None = Field(
        default=None,
        max_length=MAX_CUSTOMER_CANCELLATION_REASON_LENGTH,
        description="Opcional. Entra no historico do pedido, precedido de 'Cancelado pelo cliente'.",
    )

    @field_validator("reason")
    @classmethod
    def strip_reason(cls, value: str | None) -> str | None:
        # Espaco em branco vira None: gravar "   " no historico e pior que
        # nao gravar nada, porque parece um motivo que se perdeu.
        if value is None:
            return None
        reason = value.strip()
        return reason or None
