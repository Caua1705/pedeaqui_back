from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query
from sqlalchemy.orm import Session

from src.api.dependencies.admin_auth import get_current_admin
from src.api.dependencies.database import get_db
from src.models.admin_user_model import AdminUser
from src.schemas.admin_order_schema import AdminOrderListItem, UpdateOrderStatusRequest
from src.schemas.order_schema import OrderDetailResponse
from src.services.admin_order_service import AdminOrderService
from src.services.idempotency_service import normalize_idempotency_key


# O escopo destas rotas vem SEMPRE de admin_user.restaurant_id, nunca do que
# o cliente mandou na URL. Onde a URL tambem traz restaurante, os dois sao
# confrontados no service.
router = APIRouter(prefix="/admin", tags=["admin orders"])


@router.get("/restaurants/{restaurant_slug}/orders", response_model=list[AdminOrderListItem])
def list_orders(
    restaurant_slug: str,
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    admin_user: AdminUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
) -> list[AdminOrderListItem]:
    return AdminOrderService(db).list_orders(
        restaurant_slug=restaurant_slug,
        restaurant_id=admin_user.restaurant_id,
        order_status=status,
        limit=limit,
        offset=offset,
    )


@router.get("/orders/{order_id}", response_model=OrderDetailResponse)
def get_order_detail(
    order_id: UUID,
    admin_user: AdminUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
) -> OrderDetailResponse:
    return AdminOrderService(db).get_order_detail(order_id, admin_user.restaurant_id)


@router.patch("/orders/{order_id}/status", response_model=OrderDetailResponse)
def update_order_status(
    order_id: UUID,
    payload: UpdateOrderStatusRequest,
    admin_user: AdminUser = Depends(get_current_admin),
    idempotency_key: str | None = Header(
        default=None,
        alias="Idempotency-Key",
        description=(
            "Reenviar a mesma chave com o mesmo corpo devolve a resposta "
            "original em vez de gravar outra linha no historico de status."
        ),
    ),
    db: Session = Depends(get_db),
) -> OrderDetailResponse:
    return AdminOrderService(db).update_order_status(
        order_id,
        admin_user.restaurant_id,
        payload,
        admin_user_id=admin_user.id,
        idempotency_key=normalize_idempotency_key(idempotency_key),
    )
