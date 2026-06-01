from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class BaseResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class HealthResponse(BaseModel):
    status: str
    app: str


class StatusHistoryResponse(BaseResponse):
    id: UUID
    status: str
    changed_by: str | None = None
    note: str | None = None
    created_at: datetime | None = None
