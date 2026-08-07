from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from src.api.dependencies.admin_scope import AdminScope, get_admin_scope
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
# O `restaurant_id` saiu do path. Ele nao autorizava nada — era confrontado
# com o token por ensure_restaurant_scope —, mas manter na URL um dado que a
# rota nao pode obedecer so cria a chance de a proxima rota obedecer. Cupom
# nao tem filial: a campanha vale para o restaurante inteiro.
admin_router = APIRouter(prefix="/admin/coupons", tags=["admin coupons"])


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


@admin_router.get("", response_model=list[CouponAdminResponse])
def list_admin_coupons(
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> list[CouponAdminResponse]:
    return CouponService(db).list_admin(scope.restaurant_id)


@admin_router.post("", response_model=CouponAdminResponse, status_code=status.HTTP_201_CREATED)
def create_admin_coupon(
    payload: CouponCreate,
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> CouponAdminResponse:
    return CouponService(db).create_admin(scope.restaurant_id, payload)


@admin_router.patch("/{coupon_id}", response_model=CouponAdminResponse)
def update_admin_coupon(
    coupon_id: UUID,
    payload: CouponUpdate,
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> CouponAdminResponse:
    return CouponService(db).update_admin(scope.restaurant_id, coupon_id, payload)
