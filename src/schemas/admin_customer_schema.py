"""Contrato da listagem de clientes do painel (BLOCO D da Fase 3).

O que NAO esta aqui e a parte importante: e-mail, CPF, data de nascimento e
`customer_id`. Esses campos sao do cadastro global da plataforma
(`customers`), e o lojista nao e dono deles — ele e dono do que o cliente
informou ao pedir NA LOJA DELE. Por isso o contrato so tem nome, telefone e
o resumo dos pedidos daquele restaurante.
"""

from datetime import datetime
from enum import Enum

from pydantic import BaseModel


class CustomerSegment(str, Enum):
    """A classificacao RFV que o painel pinta na linha do cliente.

    `str, Enum` e nao string livre pelo mesmo motivo de `PaymentErrorCode`: so
    assim a LISTA de valores sai no `/openapi.json`, e o painel gera o tipo
    dele a partir do documento em vez de decorar as cinco strings.

    Os valores sao codigos estaveis, em minusculas e sem acento. **Nao existe
    `segment_label`**, e e decisao: rotulo em portugues vindo daqui
    transformaria mudanca de texto de tela em deploy de backend. Quem escreve
    "Em risco" e o painel.

    A regra que produz cada um esta em `src/services/customer_segment.py`, e
    a leitura de cada rotulo esta no contrato do painel
    (`docs/contrato-clientes-frontend.md`) — em especial a de `NOVO`, que
    significa "relacionamento novo" e nao "poucos pedidos".
    """

    NOVO = "novo"
    OCASIONAL = "ocasional"
    FIEL = "fiel"
    EM_RISCO = "em_risco"
    PERDIDO = "perdido"


class AdminCustomerListItem(BaseModel):
    customer_name: str
    customer_phone: str
    orders_count: int
    # Os pedidos que viraram dinheiro — `orders_count` menos cancelado e
    # recusado. Vai no contrato, e nao fica so interno, porque sem ele a tela
    # mostra tres numeros que nao fecham: 5 pedidos, R$ 160 gastos e ticket
    # medio de R$ 40. O que fecha a conta e este.
    billable_orders_count: int
    # Nao soma cancelado nem recusado: pedido cancelado nao e dinheiro que
    # entrou, e somar faria o pior cliente parecer o melhor.
    total_spent: float
    # `total_spent / billable_orders_count`, e NUNCA por `orders_count`:
    # dividir um numerador filtrado por um denominador que nao e sub-reporta
    # o ticket de todo cliente que ja cancelou alguma coisa. Sem pedido
    # faturavel, zero.
    average_ticket: float
    first_order_at: datetime | None = None
    last_order_at: datetime | None = None
    # O que torna a classificacao auditavel na propria tela: o lojista ve o
    # rotulo e o numero que o produziu, lado a lado.
    days_since_last_order: int | None = None
    segment: CustomerSegment


class AdminCustomerListResponse(BaseModel):
    items: list[AdminCustomerListItem]
    total: int
    limit: int
    offset: int
