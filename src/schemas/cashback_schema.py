from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel

CashbackTransactionType = Literal[
    "earned", "redeemed", "expired", "cancelled", "adjustment"
]
CashbackTransactionStatus = Literal[
    "pending", "available", "used", "cancelled", "expired"
]


class CashbackBalanceResponse(BaseModel):
    balance: float
    currency: Literal["BRL"] = "BRL"


class CashbackTransactionResponse(BaseModel):
    id: UUID
    type: CashbackTransactionType
    amount: float
    status: CashbackTransactionStatus
    description: str
    restaurant_name: str | None
    order_id: UUID | None
    expires_at: datetime | None
    created_at: datetime


class CashbackTransactionsResponse(CashbackBalanceResponse):
    transactions: list[CashbackTransactionResponse]
