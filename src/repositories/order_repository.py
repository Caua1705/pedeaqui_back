import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from src.models.order_item_model import OrderItem
from src.models.order_item_option_model import OrderItemOption
from src.models.order_model import Order
from src.models.order_status_history_model import OrderStatusHistory
from src.models.branch_model import Branch
from src.models.restaurant_model import Restaurant


class OrderRepository:
    def __init__(self, db: Session):
        self.db = db

    def create_order(self, order: Order) -> Order:
        self.db.add(order)
        self.db.flush()
        return order

    def create_order_items(self, items: list[OrderItem]) -> list[OrderItem]:
        self.db.add_all(items)
        self.db.flush()
        return items

    def create_order_item_options(self, options: list[OrderItemOption]) -> list[OrderItemOption]:
        self.db.add_all(options)
        self.db.flush()
        return options

    def create_status_history(self, history: OrderStatusHistory) -> OrderStatusHistory:
        self.db.add(history)
        self.db.flush()
        return history

    def get_order_by_number_and_phone(
        self,
        restaurant_id: uuid.UUID,
        order_number: int,
        phone: str,
    ) -> Order | None:
        stmt = (
            select(Order)
            .options(selectinload(Order.items), selectinload(Order.status_history))
            .where(
                Order.restaurant_id == restaurant_id,
                Order.order_number == order_number,
                Order.customer_phone_snapshot == phone,
            )
        )
        return self.db.scalar(stmt)

    def list_orders_by_restaurant(
        self,
        restaurant_id: uuid.UUID,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Order]:
        stmt = select(Order).where(Order.restaurant_id == restaurant_id)
        if status:
            stmt = stmt.where(Order.status == status)

        stmt = stmt.order_by(Order.created_at.desc()).limit(limit).offset(offset)
        return list(self.db.scalars(stmt).all())

    def get_order_detail(self, order_id: uuid.UUID) -> Order | None:
        stmt = (
            select(Order)
            .options(selectinload(Order.items), selectinload(Order.status_history))
            .where(Order.id == order_id)
        )
        return self.db.scalar(stmt)

    def list_orders_by_customer(self, customer_id: uuid.UUID) -> list[tuple[Order, str, str]]:
        stmt = (
            select(Order, Restaurant.name, Branch.name)
            .join(Restaurant, Restaurant.id == Order.restaurant_id)
            .join(Branch, Branch.id == Order.branch_id)
            .options(selectinload(Order.items))
            .where(Order.customer_id == customer_id)
            .order_by(Order.created_at.desc())
        )
        return [(row[0], row[1], row[2]) for row in self.db.execute(stmt).all()]

    def update_status(self, order: Order, status: str) -> Order:
        order.status = status
        self.db.add(order)
        self.db.flush()
        return order
