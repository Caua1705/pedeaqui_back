from fastapi import APIRouter, Depends, Header, Request
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


# SUBSTITUI a antiga GET /{slug}/orders/{order_number}?phone=..., removida
# na Fase 2. `order_number` vem de uma sequence global: com o telefone de
# alguem, dava para varrer os numeros vizinhos e ler endereco residencial,
# itens e historico de outras pessoas. O token e sorteado na criacao do
# pedido e entregue so a quem o criou.
@router.get("/{restaurant_slug}/orders/track/{tracking_token}", response_model=OrderDetailResponse)
@limiter.limit(PUBLIC_ORDER_LOOKUP_RATE_LIMIT)
def track_order(
    request: Request,
    restaurant_slug: str,
    tracking_token: str,
    db: Session = Depends(get_db),
) -> OrderDetailResponse:
    return OrderService(db).get_order_by_tracking_token(restaurant_slug, tracking_token)
