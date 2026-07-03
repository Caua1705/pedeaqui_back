import uuid
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.models.cashback_transaction_model import CashbackTransaction
from src.models.restaurant_model import Restaurant


class CashbackRepository:
    def __init__(self, db: Session):
        self.db = db

    def get_available_balance(self, customer_id: uuid.UUID) -> Decimal:
        stmt = select(func.coalesce(func.sum(CashbackTransaction.amount), 0)).where(
            CashbackTransaction.customer_id == customer_id,
            CashbackTransaction.status == "available",
        )
        return self.db.scalar(stmt) or Decimal("0.00")

    def list_transactions(
        self,
        customer_id: uuid.UUID,
        limit: int,
        offset: int,
    ) -> list[tuple[CashbackTransaction, str | None]]:
        stmt = (
            select(CashbackTransaction, Restaurant.name)
            .outerjoin(Restaurant, Restaurant.id == CashbackTransaction.restaurant_id)
            .where(CashbackTransaction.customer_id == customer_id)
            .order_by(CashbackTransaction.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return [(row[0], row[1]) for row in self.db.execute(stmt).all()]
