from typing import Literal
from uuid import UUID

from pydantic import BaseModel

from src.schemas.common_schema import BaseResponse


class RestaurantPublicResponse(BaseResponse):
    id: UUID
    name: str
    slug: str
    description: str | None = None
    logo_path: str | None = None
    logo_url: str | None = None
    cover_path: str | None = None
    cover_url: str | None = None
    primary_color: str | None = None
    secondary_color: str | None = None
    is_active: bool | None = True


class RestaurantSettingsResponse(BaseResponse):
    min_order_value: float
    estimated_delivery_time_min: int | None = None
    estimated_delivery_time_max: int | None = None
    default_delivery_fee: float
    service_fee_enabled: bool | None = True
    service_fee_amount: float
    accepts_delivery: bool | None = True
    accepts_pickup: bool | None = True
    payment_methods: list[str] | None = None
    is_open: bool | None = True


class BranchResponse(BaseResponse):
    id: UUID
    name: str
    slug: str
    address: str
    neighborhood: str
    city: str
    state: str
    zipcode: str | None = None
    phone: str | None = None
    whatsapp: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    is_main: bool | None = False
    is_active: bool | None = True


class CategoryResponse(BaseResponse):
    id: UUID
    name: str
    slug: str
    sort_order: int | None = 0
    is_active: bool | None = True


class RestaurantInfoRestaurantResponse(BaseModel):
    id: UUID
    name: str
    logo_url: str | None = None


class BranchAddressResponse(BaseModel):
    street: str | None = None
    number: str | None = None
    neighborhood: str | None = None
    city: str | None = None
    state: str | None = None
    zipcode: str | None = None
    full_address: str


class RestaurantInfoBranchResponse(BaseModel):
    id: UUID
    name: str
    display_name: str | None = None
    email: str | None = None
    phone: str | None = None
    whatsapp: str | None = None
    address: BranchAddressResponse


class BusinessHourPeriodResponse(BaseModel):
    opens_at: str
    closes_at: str


class BusinessHourDayResponse(BaseModel):
    weekday: int
    day_label: str
    periods: list[BusinessHourPeriodResponse]
    is_closed: bool


PaymentFlow = Literal["online", "delivery"]
PaymentMethodType = Literal[
    "pix", "credit_card", "debit_card", "cash", "voucher", "meal_voucher", "other"
]


class BranchPaymentMethodResponse(BaseModel):
    id: UUID
    payment_flow: PaymentFlow
    method_type: PaymentMethodType
    brand: str | None = None
    label: str
    icon_key: str | None = None
    enabled: bool
    requires_gateway: bool


class PaymentMethodsResponse(BaseModel):
    online: list[BranchPaymentMethodResponse]
    delivery: list[BranchPaymentMethodResponse]


class RestaurantInfoResponse(BaseModel):
    restaurant: RestaurantInfoRestaurantResponse
    branch: RestaurantInfoBranchResponse
    business_hours: list[BusinessHourDayResponse]
    payment_methods: PaymentMethodsResponse
    timezone: Literal["America/Fortaleza"] = "America/Fortaleza"
    current_weekday: int
    current_day_label: str
