from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from src.api.dependencies.api_key import validate_internal_api_key
from src.api.dependencies.database import get_db
from src.schemas.admin_order_schema import AdminOrderListItem, UpdateOrderStatusRequest
from src.schemas.order_schema import OrderDetailResponse
from src.services.admin_order_service import AdminOrderService


router = APIRouter(
    prefix="/admin",
    tags=["admin orders"],
    dependencies=[Depends(validate_internal_api_key)],
)


@router.get("/restaurants/{restaurant_slug}/orders", response_model=list[AdminOrderListItem])
def list_orders(
    restaurant_slug: str,
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> list[AdminOrderListItem]:
    return AdminOrderService(db).list_orders(
        restaurant_slug=restaurant_slug,
        order_status=status,
        limit=limit,
        offset=offset,
    )


@router.get("/orders/{order_id}", response_model=OrderDetailResponse)
def get_order_detail(
    order_id: UUID,
    db: Session = Depends(get_db),
) -> OrderDetailResponse:
    return AdminOrderService(db).get_order_detail(order_id)


@router.patch("/orders/{order_id}/status", response_model=OrderDetailResponse)
def update_order_status(
    order_id: UUID,
    payload: UpdateOrderStatusRequest,
    db: Session = Depends(get_db),
) -> OrderDetailResponse:
    return AdminOrderService(db).update_order_status(order_id, payload)
