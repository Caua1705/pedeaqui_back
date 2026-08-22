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


class RestaurantCashbackBalance(BaseModel):
    """O saldo de UM restaurante — o unico numero que da para gastar.

    O `balance` da resposta de cima e a soma destes, e a soma nao e gastavel
    em lugar nenhum: cashback de um restaurante gasto em outro seria quem
    concedeu pagando o marketing do concorrente. **A tela mostra esta lista;
    o total, se aparecer, e "acumulado", nunca "disponivel para usar".**

    `restaurant_slug` vai junto porque e por ele que o app chega no cardapio:
    sem ele a tela mostra um saldo sem botao para gasta-lo.
    """

    restaurant_id: UUID
    restaurant_name: str
    restaurant_slug: str
    balance: float
    expires_at: datetime | None


class CashbackBalanceResponse(BaseModel):
    """O saldo do cliente, com o total e a quebra por restaurante."""

    balance: float
    currency: Literal["BRL"] = "BRL"
    # Sem default: campo obrigatorio na resposta e o que faz o cliente gerado
    # a partir do `/openapi.json` tipa-lo como sempre presente. Resposta nao
    # e corpo de requisicao — nao ha quem deixe de manda-lo.
    by_restaurant: list[RestaurantCashbackBalance]


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


class CashbackTransactionsResponse(BaseModel):
    """O extrato. **Nao herda de `CashbackBalanceResponse` de proposito.**

    Herdando, a quebra por restaurante entraria aqui junto — e o extrato
    passaria a pagar as tres consultas do saldo por restaurante para exibir
    uma lista que esta tela nao mostra. Os campos que ele publica sao os
    mesmos de antes; quem consome nao ve diferenca.
    """

    balance: float
    currency: Literal["BRL"] = "BRL"
    transactions: list[CashbackTransactionResponse]
