from uuid import UUID

from src.schemas.common_schema import BaseResponse


class BannerResponse(BaseResponse):
    id: UUID
    restaurant_id: UUID
    banner_type: str = "hero"
    image_path: str
    image_url: str | None = None
    sort_order: int | None = 0
    is_active: bool | None = True
