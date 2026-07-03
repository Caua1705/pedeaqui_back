from sqlalchemy.orm import Session

from src.models.customer_model import Customer
from src.repositories.cashback_repository import CashbackRepository
from src.schemas.cashback_schema import (
    CashbackBalanceResponse,
    CashbackTransactionResponse,
    CashbackTransactionsResponse,
)
from src.utils.money import money_to_float


class CashbackService:
    DESCRIPTIONS = {
        "earned": "Cashback recebido",
        "redeemed": "Cashback utilizado",
        "expired": "Cashback expirado",
        "cancelled": "Cashback cancelado",
        "adjustment": "Ajuste de cashback",
    }

    def __init__(self, db: Session):
        self.cashback_repository = CashbackRepository(db)

    def get_balance(self, customer: Customer) -> CashbackBalanceResponse:
        balance = self.cashback_repository.get_available_balance(customer.id)
        return CashbackBalanceResponse(balance=money_to_float(balance))

    def list_transactions(
        self,
        customer: Customer,
        limit: int,
        offset: int,
    ) -> CashbackTransactionsResponse:
        balance = self.cashback_repository.get_available_balance(customer.id)
        rows = self.cashback_repository.list_transactions(customer.id, limit, offset)
        transactions = [
            CashbackTransactionResponse(
                id=transaction.id,
                type=transaction.type,
                amount=money_to_float(transaction.amount),
                status=transaction.status,
                description=self.DESCRIPTIONS[transaction.type],
                restaurant_name=restaurant_name,
                order_id=transaction.order_id,
                expires_at=transaction.expires_at,
                created_at=transaction.created_at,
            )
            for transaction, restaurant_name in rows
        ]
        return CashbackTransactionsResponse(
            balance=money_to_float(balance),
            transactions=transactions,
        )
