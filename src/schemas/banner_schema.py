from datetime import datetime
from uuid import UUID

from src.schemas.common_schema import BaseResponse


class BannerResponse(BaseResponse):
    id: UUID
    title: str | None = None
    subtitle: str | None = None
    image_path: str
    image_url: str | None = None
    action_type: str | None = "none"
    action_value: str | None = None
    sort_order: int | None = 0
    is_active: bool | None = True
    starts_at: datetime | None = None
    ends_at: datetime | None = None
