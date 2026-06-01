from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from src.core.constants import ORDER_STATUSES
from src.models.order_status_history_model import OrderStatusHistory
from src.repositories.order_repository import OrderRepository
from src.schemas.admin_order_schema import AdminOrderListItem, UpdateOrderStatusRequest
from src.schemas.order_schema import OrderDetailResponse
from src.services.order_service import OrderService
from src.services.restaurant_service import RestaurantService
from src.utils.money import money_to_float


class AdminOrderService:
    def __init__(self, db: Session):
        self.db = db
        self.restaurant_service = RestaurantService(db)
        self.order_repository = OrderRepository(db)

    def list_orders(
        self,
        restaurant_slug: str,
        order_status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[AdminOrderListItem]:
        if order_status and order_status not in ORDER_STATUSES:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Status inválido")

        restaurant = self.restaurant_service.get_active_restaurant(restaurant_slug)
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

    def get_order_detail(self, order_id: UUID) -> OrderDetailResponse:
        order = self.order_repository.get_order_detail(order_id)
        if not order:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pedido não encontrado")
        return OrderService.to_order_detail_response(order)

    def update_order_status(self, order_id: UUID, payload: UpdateOrderStatusRequest) -> OrderDetailResponse:
        if payload.status not in ORDER_STATUSES:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Status inválido")

        order = self.order_repository.get_order_detail(order_id)
        if not order:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pedido não encontrado")

        try:
            self.order_repository.update_status(order, payload.status)
            self.order_repository.create_status_history(
                OrderStatusHistory(
                    order_id=order.id,
                    status=payload.status,
                    changed_by=payload.changed_by,
                    note=payload.note,
                )
            )
            self.db.commit()
            order = self.order_repository.get_order_detail(order_id)
        except Exception:
            self.db.rollback()
            raise

        return OrderService.to_order_detail_response(order)
