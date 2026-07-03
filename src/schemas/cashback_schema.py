from typing import Literal

from pydantic import BaseModel


class CashbackBalanceResponse(BaseModel):
    balance: float
    currency: Literal["BRL"] = "BRL"
