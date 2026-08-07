"""Contrato da listagem de clientes do painel (BLOCO D da Fase 3).

O que NAO esta aqui e a parte importante: e-mail, CPF, data de nascimento e
`customer_id`. Esses campos sao do cadastro global da plataforma
(`customers`), e o lojista nao e dono deles — ele e dono do que o cliente
informou ao pedir NA LOJA DELE. Por isso o contrato so tem nome, telefone e
o resumo dos pedidos daquele restaurante.
"""

from datetime import datetime

from pydantic import BaseModel


class AdminCustomerListItem(BaseModel):
    customer_name: str
    customer_phone: str
    orders_count: int
    # Nao soma cancelado nem recusado: pedido cancelado nao e dinheiro que
    # entrou, e somar faria o pior cliente parecer o melhor.
    total_spent: float
    first_order_at: datetime | None = None
    last_order_at: datetime | None = None


class AdminCustomerListResponse(BaseModel):
    items: list[AdminCustomerListItem]
    total: int
    limit: int
    offset: int
