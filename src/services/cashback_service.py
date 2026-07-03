from sqlalchemy.orm import Session

from src.models.customer_model import Customer
from src.repositories.cashback_repository import CashbackRepository
from src.schemas.cashback_schema import CashbackBalanceResponse
from src.utils.money import money_to_float


class CashbackService:
    def __init__(self, db: Session):
        self.cashback_repository = CashbackRepository(db)

    def get_balance(self, customer: Customer) -> CashbackBalanceResponse:
        balance = self.cashback_repository.get_available_balance(customer.id)
        return CashbackBalanceResponse(balance=money_to_float(balance))
