from uuid import UUID

from src.schemas.common_schema import BaseResponse


class ProductResponse(BaseResponse):
    id: UUID
    restaurant_id: UUID
    category_id: UUID
    code: str | None = None
    name: str
    slug: str | None = None
    description: str | None = None
    price: float
    image_path: str | None = None
    image_url: str | None = None
    is_active: bool | None = True
    is_available: bool | None = True
    sort_order: int | None = 0
    is_featured: bool | None = False
    badge: str | None = None
    highlight_order: int | None = 0
