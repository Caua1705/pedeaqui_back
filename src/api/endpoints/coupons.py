from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from src.api.dependencies.api_key import validate_internal_api_key
from src.api.dependencies.customer_auth import get_current_customer, get_optional_current_customer
from src.api.dependencies.database import get_db
from src.models.customer_model import Customer
from src.schemas.coupon_schema import (
    AvailableCouponsResponse,
    CouponAdminResponse,
    CouponCreate,
    CouponPreviewRequest,
    CouponPreviewResponse,
    CouponUpdate,
)
from src.services.coupon_service import CouponService


router = APIRouter(prefix="/restaurants", tags=["coupons"])
admin_router = APIRouter(
    prefix="/admin/restaurants",
    tags=["admin coupons"],
    dependencies=[Depends(validate_internal_api_key)],
)


@router.get("/{restaurant_slug}/coupons/available", response_model=AvailableCouponsResponse)
def list_available_coupons(
    restaurant_slug: str,
    subtotal: Decimal | None = Query(default=None, ge=0),
    delivery_fee: Decimal | None = Query(default=None, ge=0),
    order_type: str | None = Query(default=None),
    current_customer: Customer | None = Depends(get_optional_current_customer),
    db: Session = Depends(get_db),
) -> AvailableCouponsResponse:
    return CouponService(db).get_available(
        restaurant_slug,
        subtotal=subtotal,
        delivery_fee=delivery_fee,
        order_type=order_type,
        customer=current_customer,
    )


@router.post("/{restaurant_slug}/coupons/preview", response_model=CouponPreviewResponse)
def preview_coupon(
    restaurant_slug: str,
    payload: CouponPreviewRequest,
    current_customer: Customer = Depends(get_current_customer),
    db: Session = Depends(get_db),
) -> CouponPreviewResponse:
    return CouponService(db).preview(restaurant_slug, payload, current_customer)


@admin_router.get("/{restaurant_id}/coupons", response_model=list[CouponAdminResponse])
def list_admin_coupons(
    restaurant_id: UUID,
    db: Session = Depends(get_db),
) -> list[CouponAdminResponse]:
    return CouponService(db).list_admin(restaurant_id)


@admin_router.post("/{restaurant_id}/coupons", response_model=CouponAdminResponse, status_code=status.HTTP_201_CREATED)
def create_admin_coupon(
    restaurant_id: UUID,
    payload: CouponCreate,
    db: Session = Depends(get_db),
) -> CouponAdminResponse:
    return CouponService(db).create_admin(restaurant_id, payload)


@admin_router.patch("/{restaurant_id}/coupons/{coupon_id}", response_model=CouponAdminResponse)
def update_admin_coupon(
    restaurant_id: UUID,
    coupon_id: UUID,
    payload: CouponUpdate,
    db: Session = Depends(get_db),
) -> CouponAdminResponse:
    return CouponService(db).update_admin(restaurant_id, coupon_id, payload)
