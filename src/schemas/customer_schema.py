from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.schemas.common_schema import BaseResponse
from src.schemas.order_schema import OrderItemResponse
from src.utils.normalization import normalize_digits


class CustomerAddressBase(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    label: str | None = None
    street: str | None = None
    number: str | None = None
    neighborhood: str | None = None
    complement: str | None = None
    reference: str | None = None
    city: str | None = None
    state: str | None = None
    zipcode: str | None = None
    latitude: Decimal | None = None
    longitude: Decimal | None = None

    @field_validator("zipcode")
    @classmethod
    def normalize_zipcode(cls, value: str | None) -> str | None:
        return normalize_digits(value) if value else value


class CreateCustomerAddressRequest(CustomerAddressBase):
    street: str = Field(min_length=1)
    number: str = Field(min_length=1)
    neighborhood: str = Field(min_length=1)
    is_default: bool = False


class UpdateCustomerAddressRequest(CustomerAddressBase):
    is_default: bool | None = None


class CustomerAddressResponse(BaseResponse):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: UUID
    customer_id: UUID
    label: str | None = None
    street: str
    number: str
    neighborhood: str
    complement: str | None = None
    reference: str | None = None
    city: str | None = None
    state: str | None = None
    zipcode: str | None = None
    latitude: Decimal | None = None
    longitude: Decimal | None = None
    is_default: bool
    created_at: datetime | None = None
    updated_at: datetime | None = None


class CurrentCustomerResponse(BaseResponse):
    id: UUID
    name: str
    email: str
    phone: str
    cpf: str
    birth_date: date
    email_verified: bool
    marketing_opt_in: bool


class CustomerOrderHistoryItem(BaseModel):
    id: UUID
    order_number: int
    restaurant_name: str
    branch_name: str
    status: str
    order_type: str
    subtotal: float
    delivery_fee: float
    service_fee: float
    total: float
    created_at: datetime | None = None
    items: list[OrderItemResponse]
