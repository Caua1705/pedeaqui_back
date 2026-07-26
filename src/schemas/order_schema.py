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
    notes: str | None = Field(default=None, max_length=500)
    items: list[OrderItemInput] = Field(min_length=1, max_length=MAX_ITEMS_PER_ORDER)
    coupon_id: UUID | None = None
    coupon_code: str | None = Field(default=None, min_length=1, max_length=100)

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
    status: str
    subtotal: float
    delivery_fee: float
    service_fee: float
    coupon_code: str | None = None
    coupon_discount_amount: Decimal = Decimal("0.00")
    cashback_redeemed_amount: Decimal = Decimal("0.00")
    discount_total: Decimal = Decimal("0.00")
    total: float
    message: str


class OrderItemResponse(BaseResponse):
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
