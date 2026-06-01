from datetime import datetime
from uuid import UUID

from src.schemas.common_schema import BaseResponse


class CouponResponse(BaseResponse):
    id: UUID
    code: str
    title: str
    description: str | None = None
    discount_type: str
    discount_value: float
    min_order_value: float
    image_path: str | None = None
    image_url: str | None = None
    usage_limit: int | None = None
    is_active: bool | None = True
    starts_at: datetime | None = None
    ends_at: datetime | None = None
