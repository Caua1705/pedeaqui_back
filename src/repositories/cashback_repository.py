import uuid
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.models.cashback_transaction_model import CashbackTransaction


class CashbackRepository:
    def __init__(self, db: Session):
        self.db = db

    def get_available_balance(self, customer_id: uuid.UUID) -> Decimal:
        stmt = select(func.coalesce(func.sum(CashbackTransaction.amount), 0)).where(
            CashbackTransaction.customer_id == customer_id,
            CashbackTransaction.status == "available",
        )
        return self.db.scalar(stmt) or Decimal("0.00")
