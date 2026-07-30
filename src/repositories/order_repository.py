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

    def get_order_by_tracking_token(
        self,
        restaurant_id: uuid.UUID,
        tracking_token: str,
    ) -> Order | None:
        # Substituiu a busca por (order_number, telefone), que era
        # enumeravel: order_number vem de uma sequence global.
        stmt = (
            select(Order)
            .options(selectinload(Order.items), selectinload(Order.status_history))
            .where(
                Order.restaurant_id == restaurant_id,
                Order.tracking_token == tracking_token,
            )
        )
        return self.db.scalar(stmt)

    def get_order_detail_for_customer(
        self,
        order_id: uuid.UUID,
        customer_id: uuid.UUID,
    ) -> Order | None:
        # customer_id e obrigatorio pelo mesmo motivo que restaurant_id e em
        # get_order_detail: sem ele a rota entrega o pedido de qualquer um
        # para quem tiver o UUID.
        stmt = (
            select(Order)
            .options(selectinload(Order.items), selectinload(Order.status_history))
            .where(Order.id == order_id, Order.customer_id == customer_id)
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

    def get_order_detail(self, order_id: uuid.UUID, restaurant_id: uuid.UUID) -> Order | None:
        # restaurant_id e obrigatorio de proposito. Enquanto o filtro era so
        # por Order.id, qualquer lojista com um UUID de pedido em maos lia o
        # pedido de outro restaurante — nome, telefone e endereco do cliente.
        # Deixa-lo opcional convidaria a repetir o erro na proxima rota.
        stmt = (
            select(Order)
            .options(selectinload(Order.items), selectinload(Order.status_history))
            .where(Order.id == order_id, Order.restaurant_id == restaurant_id)
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

    def get_order_by_provider_payment(
        self,
        payment_provider: str,
        provider_payment_id: str,
    ) -> Order | None:
        # Sem filtro por restaurante de proposito: o webhook chega do
        # gateway, nao de um tenant, e o par (provider, provider_payment_id)
        # e unico na tabela.
        stmt = select(Order).where(
            Order.payment_provider == payment_provider,
            Order.provider_payment_id == provider_payment_id,
        )
        return self.db.scalar(stmt)

    def attach_payment_intent(
        self,
        order: Order,
        *,
        provider: str,
        provider_payment_id: str,
    ) -> Order:
        order.payment_provider = provider
        order.provider_payment_id = provider_payment_id
        # Uma nova tentativa depois de uma recusa volta o pagamento para
        # "pending"; se ja estava pending, isto e um no-op.
        order.payment_status = "pending"
        self.db.add(order)
        self.db.flush()
        return order

    def update_payment_status(
        self,
        order: Order,
        payment_status: str,
        paid_at=None,
    ) -> Order:
        order.payment_status = payment_status
        if paid_at is not None:
            order.paid_at = paid_at
        self.db.add(order)
        self.db.flush()
        return order
