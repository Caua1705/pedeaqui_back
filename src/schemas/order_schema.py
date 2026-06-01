from datetime import datetime
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


class OrderItemInput(BaseModel):
    product_id: UUID
    quantity: int = Field(default=1, ge=1)
    observation: str | None = None


class CreateOrderRequest(BaseModel):
    branch_id: UUID
    customer: CustomerInput
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
    notes: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    items: list[OrderItemResponse]
    status_history: list[StatusHistoryResponse]
