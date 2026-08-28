from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, status
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
from src.api.rate_limit import COUPON_CLAIM_RATE_LIMIT, limiter
from src.models.customer_model import Customer
from src.schemas.coupon_schema import (
    CouponAdminResponse,
    CouponClaimRequest,
    CouponClaimResponse,
    CouponCreate,
    CouponPreviewRequest,
    CouponPreviewResponse,
    CouponTemplateResponse,
    CouponUpdate,
    CustomerCouponsResponse,
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


@router.get("/{restaurant_slug}/coupons", response_model=CustomerCouponsResponse)
def list_customer_coupons(
    restaurant_slug: str,
    subtotal: Decimal | None = Query(default=None, ge=0),
    delivery_fee: Decimal | None = Query(default=None, ge=0),
    order_type: str | None = Query(default=None),
    current_customer: Customer | None = Depends(get_optional_current_customer),
    db: Session = Depends(get_db),
) -> CustomerCouponsResponse:
    """Os cupons desta loja para QUEM ESTA OLHANDO, com o estado pronto.

    Substituiu `GET .../coupons/available`. O front nao calcula nada: cada
    card ja vem com a etiqueta, o estado do botao e o desconto que aquele
    cupom daria NESTA sacola. O porque de a conta nao poder ser do outro lado
    esta em `CustomerCouponResponse`.

    **A sacola e opcional.** Sem `subtotal` a rota responde a tela do Clube —
    os cupons que a pessoa tem, com o minimo inteiro faltando em quem tem
    minimo. Com `subtotal` ela responde a tela do checkout.

    **Sem token, so sai o que e publico.** Cupom de segmento e cupom privado
    nao aparecem para convidado nem como "entre para usar": a existencia
    deles, com titulo e codigo, e justamente o que eles nao publicam.

    Cupom sem conserto nesta sacola nao vem na lista — nem vencido, nem de
    outro segmento, nem primeira-compra para quem ja comprou. Cupom em que
    falta valor VEM, com `state = "missing_amount"` e o quanto falta.
    """
    return CouponService(db).list_for_customer(
        restaurant_slug,
        subtotal=subtotal,
        delivery_fee=delivery_fee,
        order_type=order_type,
        customer=current_customer,
    )


@router.post(
    "/{restaurant_slug}/coupons/claim",
    response_model=CouponClaimResponse,
    status_code=status.HTTP_201_CREATED,
)
@limiter.limit(COUPON_CLAIM_RATE_LIMIT)
def claim_coupon(
    request: Request,
    restaurant_slug: str,
    payload: CouponClaimRequest,
    current_customer: Customer = Depends(get_current_customer),
    db: Session = Depends(get_db),
) -> CouponClaimResponse:
    """Digitar um codigo SEM SACOLA — o cupom passa a ser do cliente.

    A porta do Clube, e o par de `GET .../coupons`: resgatado aqui, o cupom
    aparece na lista e aplica depois, no checkout.

    RESGATE nao e USO. A linha vai para `coupon_claims`, que nao tem pedido
    nem valor e concede visibilidade; `coupon_redemptions` continua sendo o
    registro de uso, e e ela que conta no teto da campanha. Gravar resgate la
    faria o cupom de 100 usos se esgotar com gente que so digitou o codigo.

    **Idempotente**: resgatar de novo devolve o mesmo cupom, 201, sem erro.

    O `request` na assinatura nao e decoracao — o `@limiter.limit` do slowapi
    o exige por posicao, e sem ele a rota levanta no boot.
    """
    return CouponService(db).claim(restaurant_slug, payload, current_customer)


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
