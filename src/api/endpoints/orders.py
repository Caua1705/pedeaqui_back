from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from src.api.dependencies.database import get_db
from src.schemas.order_schema import CreateOrderRequest, CreateOrderResponse, OrderDetailResponse
from src.services.order_service import OrderService


router = APIRouter(prefix="/restaurants", tags=["orders"])


@router.post("/{restaurant_slug}/orders", response_model=CreateOrderResponse)
def create_order(
    restaurant_slug: str,
    payload: CreateOrderRequest,
    db: Session = Depends(get_db),
) -> CreateOrderResponse:
    return OrderService(db).create_order(restaurant_slug, payload)


@router.get("/{restaurant_slug}/orders/{order_number}", response_model=OrderDetailResponse)
def get_customer_order(
    restaurant_slug: str,
    order_number: int,
    phone: str = Query(..., min_length=8),
    db: Session = Depends(get_db),
) -> OrderDetailResponse:
    return OrderService(db).get_customer_order(restaurant_slug, order_number, phone)
