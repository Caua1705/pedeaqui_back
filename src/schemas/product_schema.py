from uuid import UUID

from pydantic import Field

from src.schemas.common_schema import BaseResponse


class ProductOptionResponse(BaseResponse):
    id: UUID
    name: str
    description: str | None = None
    additional_price: float
    sort_order: int | None = 0


class ProductOptionGroupResponse(BaseResponse):
    id: UUID
    name: str
    description: str | None = None
    min_select: int
    max_select: int
    is_required: bool
    sort_order: int | None = 0
    options: list[ProductOptionResponse]


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
    option_groups: list[ProductOptionGroupResponse] = Field(default_factory=list)
