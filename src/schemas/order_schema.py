from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, Field

from src.schemas.common_schema import BaseResponse, StatusHistoryResponse


class CustomerInput(BaseModel):
    name: str = Field(min_length=1)
    phone: str = Field(min_length=8)


class AddressInput(BaseModel):
    street: str | None = None
    number: str | None = None
    neighborhood: str | None = None
    complement: str | None = None
    reference: str | None = None
    city: str | None = None
    state: str | None = None
    zipcode: str | None = None
    latitude: Decimal | None = Field(default=None, ge=-90, le=90)
    longitude: Decimal | None = Field(default=None, ge=-180, le=180)


class OrderItemSelectedOptionInput(BaseModel):
    option_group_id: UUID
    option_id: UUID


class OrderItemInput(BaseModel):
    product_id: UUID
    quantity: int = Field(default=1, ge=1)
    observation: str | None = None
    selected_options: list[OrderItemSelectedOptionInput] = Field(default_factory=list)


class CreateOrderRequest(BaseModel):
    branch_id: UUID
    customer: CustomerInput | None = None
    customer_address_id: UUID | None = None
    order_type: str
    payment_method: str | None = None
    address: AddressInput | None = None
    notes: str | None = None
    items: list[OrderItemInput] = Field(min_length=1)


class CreateOrderResponse(BaseModel):
    id: UUID
    order_number: int
    status: str
    subtotal: float
    delivery_fee: float
    service_fee: float
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
