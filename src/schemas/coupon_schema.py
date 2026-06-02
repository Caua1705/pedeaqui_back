from uuid import UUID

from src.schemas.common_schema import BaseResponse


class PublicCouponResponse(BaseResponse):
    id: UUID
    code: str
    name: str
    image_path: str | None = None
    image_url: str | None = None
    discount_type: str
    discount_value: float
    min_order_value: float
    sort_order: int
    is_active: bool
