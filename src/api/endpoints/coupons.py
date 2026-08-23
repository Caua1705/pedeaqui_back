from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from src.api.dependencies.admin_scope import (
    GERENCIA,
    SOMENTE_DONO,
    AdminScope,
    exigir_papel,
    get_admin_scope,
)
from src.api.dependencies.customer_auth import get_current_customer, get_optional_current_customer
from src.api.dependencies.database import get_db
from src.models.customer_model import Customer
from src.schemas.coupon_schema import (
    AvailableCouponsResponse,
    CouponAdminResponse,
    CouponCreate,
    CouponPreviewRequest,
    CouponPreviewResponse,
    CouponTemplateResponse,
    CouponUpdate,
)
from src.services.coupon_service import CouponService


router = APIRouter(prefix="/restaurants", tags=["coupons"])
# O `restaurant_id` saiu do path. Ele nao autorizava nada — era confrontado
# com o token por ensure_restaurant_scope —, mas manter na URL um dado que a
# rota nao pode obedecer so cria a chance de a proxima rota obedecer. Cupom
# nao tem filial: a campanha vale para o restaurante inteiro.
#
# **Papel: criar e editar cupom e SOMENTE_DONO.** Cupom nao aparece na matriz
# de papeis da proposta, e a omissao seria um buraco do tamanho da regra que
# ela protege: se `PATCH /admin/products/{id}` com `price` e do dono porque
# "a conta de gerente nao pode valer desconto ilimitado", entao um cupom de
# 99% pela porta ao lado vale exatamente a mesma coisa — sem nem precisar
# tocar no cardapio.
#
# Ler continua sendo do GERENCIA: quem toca a loja precisa saber qual
# campanha esta no ar para responder ao cliente que ligou.
admin_router = APIRouter(prefix="/admin/coupons", tags=["admin coupons"])

# Router proprio porque o caminho e irmao, nao filho, de `/admin/coupons`:
# pendurar a lista em `/admin/coupons/templates` faria o `{coupon_id}` do
# PATCH disputar o segmento com a palavra `templates`.
#
# GERENCIA, e nao SOMENTE_DONO, apesar de so o dono conseguir CRIAR cupom: a
# lista e o catalogo de arte da plataforma, sem valor, sem prazo e sem
# restaurante. Fecha-la no dono nao protegeria nada e tiraria da gerencia a
# tela de leitura de `GET /admin/coupons`, que mostra o template de cada
# campanha no ar.
template_router = APIRouter(prefix="/admin/coupon-templates", tags=["admin coupons"])


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


@admin_router.get(
    "",
    response_model=list[CouponAdminResponse],
    dependencies=[Depends(exigir_papel(GERENCIA))],
)
def list_admin_coupons(
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> list[CouponAdminResponse]:
    return CouponService(db).list_admin(scope.restaurant_id)


@admin_router.post(
    "",
    response_model=CouponAdminResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(exigir_papel(SOMENTE_DONO))],
)
def create_admin_coupon(
    payload: CouponCreate,
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> CouponAdminResponse:
    return CouponService(db).create_admin(scope.restaurant_id, payload)


@admin_router.patch(
    "/{coupon_id}",
    response_model=CouponAdminResponse,
    dependencies=[Depends(exigir_papel(SOMENTE_DONO))],
)
def update_admin_coupon(
    coupon_id: UUID,
    payload: CouponUpdate,
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> CouponAdminResponse:
    return CouponService(db).update_admin(scope.restaurant_id, coupon_id, payload)


@template_router.get(
    "",
    response_model=list[CouponTemplateResponse],
    dependencies=[Depends(exigir_papel(GERENCIA))],
)
def list_coupon_templates(
    db: Session = Depends(get_db),
) -> list[CouponTemplateResponse]:
    return CouponService(db).list_templates()
