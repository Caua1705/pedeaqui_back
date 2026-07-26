from fastapi import APIRouter, Depends, Header, Query, Request
from sqlalchemy.orm import Session

from src.api.dependencies.customer_auth import get_optional_current_customer
from src.api.dependencies.database import get_db
from src.api.rate_limit import (
    CREATE_ORDER_RATE_LIMIT,
    PUBLIC_ORDER_LOOKUP_RATE_LIMIT,
    limiter,
)
from src.models.customer_model import Customer
from src.schemas.order_schema import CreateOrderRequest, CreateOrderResponse, OrderDetailResponse
from src.services.idempotency_service import normalize_idempotency_key
from src.services.order_service import OrderService


router = APIRouter(prefix="/restaurants", tags=["orders"])

IDEMPOTENCY_KEY_DESCRIPTION = (
    "Identificador unico da tentativa de criar ESTE pedido. Reenviar a mesma "
    "chave com o mesmo corpo devolve a resposta original em vez de criar um "
    "segundo pedido. Gere um UUID por pedido e reutilize-o em todas as "
    "retentativas. Vale por 24h."
)


@router.post("/{restaurant_slug}/orders", response_model=CreateOrderResponse)
@limiter.limit(CREATE_ORDER_RATE_LIMIT)
def create_order(
    request: Request,
    restaurant_slug: str,
    payload: CreateOrderRequest,
    current_customer: Customer | None = Depends(get_optional_current_customer),
    idempotency_key: str | None = Header(
        default=None,
        alias="Idempotency-Key",
        description=IDEMPOTENCY_KEY_DESCRIPTION,
    ),
    db: Session = Depends(get_db),
) -> CreateOrderResponse:
    return OrderService(db).create_order(
        restaurant_slug,
        payload,
        current_customer,
        idempotency_key=normalize_idempotency_key(idempotency_key),
    )


@router.get("/{restaurant_slug}/orders/{order_number}", response_model=OrderDetailResponse)
@limiter.limit(PUBLIC_ORDER_LOOKUP_RATE_LIMIT)
def get_customer_order(
    request: Request,
    restaurant_slug: str,
    order_number: int,
    phone: str = Query(..., min_length=8),
    db: Session = Depends(get_db),
) -> OrderDetailResponse:
    return OrderService(db).get_customer_order(restaurant_slug, order_number, phone)
