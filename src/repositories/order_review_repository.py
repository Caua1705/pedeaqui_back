import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.models.order_review_model import OrderReview


class OrderReviewRepository:
    def __init__(self, db: Session):
        self.db = db

    def get_by_order_id(self, order_id: uuid.UUID) -> OrderReview | None:
        stmt = select(OrderReview).where(OrderReview.order_id == order_id)
        return self.db.scalar(stmt)

    def create(self, order_id: uuid.UUID, **campos) -> OrderReview:
        """NAO commita: quem commita e o service (regra de camadas)."""
        avaliacao = OrderReview(order_id=order_id, **campos)
        self.db.add(avaliacao)
        self.db.flush()
        return avaliacao

    def update(self, avaliacao: OrderReview, **campos) -> OrderReview:
        for nome, valor in campos.items():
            setattr(avaliacao, nome, valor)
        self.db.flush()
        return avaliacao
