from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel


class CommissionReportItem(BaseModel):
    """Uma linha do extrato.

    Traz base e percentual junto do valor de proposito: o lojista precisa
    conseguir refazer a conta de cada pedido sem pedir explicacao para
    ninguem.
    """

    order_id: UUID
    order_number: int
    created_at: datetime | None = None
    status: str
    payment_status: str
    payment_method: str | None = None
    subtotal: Decimal
    coupon_discount_amount: Decimal
    cashback_redeemed_amount: Decimal
    commission_base_amount: Decimal
    commission_percent: Decimal
    commission_amount: Decimal
    order_total: Decimal


class CommissionReportResponse(BaseModel):
    restaurant_id: UUID
    start_date: date
    end_date: date
    orders_count: int
    # Pedidos do periodo que nao entraram: cancelados, recusados e
    # estornados. Explicita a diferenca entre o painel e o extrato.
    excluded_orders_count: int
    commission_base_total: Decimal
    commission_total: Decimal
    orders: list[CommissionReportItem]
