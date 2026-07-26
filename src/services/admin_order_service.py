from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from src.core.constants import ORDER_STATUSES
from src.models.order_status_history_model import OrderStatusHistory
from src.repositories.order_repository import OrderRepository
from src.schemas.admin_order_schema import AdminOrderListItem, UpdateOrderStatusRequest
from src.schemas.order_schema import OrderDetailResponse
from src.services.order_service import OrderService
from src.services.coupon_service import CouponService
from src.services.restaurant_service import RestaurantService
from src.utils.money import money_to_float


class AdminOrderService:
    def __init__(self, db: Session):
        self.db = db
        self.restaurant_service = RestaurantService(db)
        self.order_repository = OrderRepository(db)
        self.coupon_service = CouponService(db)

    def list_orders(
        self,
        restaurant_slug: str,
        restaurant_id: UUID,
        order_status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[AdminOrderListItem]:
        if order_status and order_status not in ORDER_STATUSES:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Status inválido")

        restaurant = self.restaurant_service.get_active_restaurant(restaurant_slug)
        # O slug vem da URL, o restaurant_id vem do token. Divergiu, o lojista
        # esta pedindo a lista de outro restaurante.
        if restaurant.id != restaurant_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Restaurante nao encontrado")

        orders = self.order_repository.list_orders_by_restaurant(
            restaurant_id=restaurant.id,
            status=order_status,
            limit=min(limit, 100),
            offset=offset,
        )
        return [
            AdminOrderListItem(
                id=order.id,
                order_number=order.order_number,
                customer_name_snapshot=order.customer_name_snapshot,
                customer_phone_snapshot=order.customer_phone_snapshot,
                order_type=order.order_type,
                status=order.status,
                total=money_to_float(order.total),
                created_at=order.created_at,
            )
            for order in orders
        ]

    def get_order_detail(self, order_id: UUID, restaurant_id: UUID) -> OrderDetailResponse:
        order = self.order_repository.get_order_detail(order_id, restaurant_id)
        if not order:
            # Mesmo 404 para "nao existe" e para "existe mas e de outro
            # restaurante": distinguir os dois transformaria a rota em um
            # oraculo de quais UUIDs de pedido existem na plataforma.
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pedido não encontrado")
        return OrderService.to_order_detail_response(order)

    def update_order_status(
        self,
        order_id: UUID,
        restaurant_id: UUID,
        payload: UpdateOrderStatusRequest,
    ) -> OrderDetailResponse:
        if payload.status not in ORDER_STATUSES:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Status inválido")

        order = self.order_repository.get_order_detail(order_id, restaurant_id)
        if not order:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pedido não encontrado")

        try:
            self.order_repository.update_status(order, payload.status)
            if payload.status in {"cancelled", "rejected"}:
                self.coupon_service.reverse_for_order(order.id)
            self.order_repository.create_status_history(
                OrderStatusHistory(
                    order_id=order.id,
                    status=payload.status,
                    changed_by=payload.changed_by,
                    note=payload.note,
                )
            )
            self.db.commit()
            order = self.order_repository.get_order_detail(order_id, restaurant_id)
        except Exception:
            self.db.rollback()
            raise

        return OrderService.to_order_detail_response(order)
