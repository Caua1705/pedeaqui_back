from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class AdminOrderListItem(BaseModel):
    id: UUID
    order_number: int
    customer_name_snapshot: str
    customer_phone_snapshot: str
    order_type: str
    status: str
    total: float
    created_at: datetime | None = None


class UpdateOrderStatusRequest(BaseModel):
    status: str
    changed_by: str | None = "admin"
    note: str | None = None
