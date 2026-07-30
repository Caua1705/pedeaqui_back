"""Relatorios do painel do lojista.

Hoje so o de comissao. Ele nao calcula nada: le o que ja esta gravado em
cada pedido (`commission_*`) e soma. Recalcular aqui traria de volta o
problema que a Fase 2 resolveu — o numero mudaria conforme o percentual do
restaurante fosse alterado.
"""

from datetime import date, datetime, time, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from src.core.constants import PLATFORM_TIMEZONE
from src.repositories.order_repository import OrderRepository
from src.schemas.admin_report_schema import CommissionReportItem, CommissionReportResponse
from src.utils.money import ZERO, quantize_money, to_decimal


REPORT_TIMEZONE = ZoneInfo(PLATFORM_TIMEZONE)

# Teto do recorte. Sem limite, um `start_date=2020-01-01` varre a tabela
# inteira e devolve um JSON gigante para o navegador do lojista.
MAX_REPORT_DAYS = 92


class AdminReportService:
    def __init__(self, db: Session):
        self.order_repository = OrderRepository(db)

    def commission_report(
        self,
        restaurant_id: UUID,
        start_date: date,
        end_date: date,
    ) -> CommissionReportResponse:
        self._validate_period(start_date, end_date)
        start_at, end_at = self._period_bounds(start_date, end_date)

        orders = self.order_repository.list_orders_for_commission(
            restaurant_id, start_at, end_at
        )
        excluded_count = self.order_repository.count_excluded_from_commission(
            restaurant_id, start_at, end_at
        )

        items = [self._to_item(order) for order in orders]
        return CommissionReportResponse(
            restaurant_id=restaurant_id,
            start_date=start_date,
            end_date=end_date,
            orders_count=len(items),
            excluded_orders_count=excluded_count,
            commission_base_total=quantize_money(
                sum((item.commission_base_amount for item in items), ZERO)
            ),
            commission_total=quantize_money(
                sum((item.commission_amount for item in items), ZERO)
            ),
            orders=items,
        )

    @staticmethod
    def _validate_period(start_date: date, end_date: date) -> None:
        if end_date < start_date:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="end_date nao pode ser anterior a start_date",
            )
        if (end_date - start_date).days + 1 > MAX_REPORT_DAYS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Periodo maximo do relatorio: {MAX_REPORT_DAYS} dias",
            )

    @staticmethod
    def _period_bounds(start_date: date, end_date: date) -> tuple[datetime, datetime]:
        """Converte o recorte de datas para instantes UTC.

        As datas chegam no fuso da operacao (America/Fortaleza), nao em UTC:
        "os pedidos de ontem" para o lojista sao os do dia dele. Sem essa
        conversao, tres horas de pedidos cairiam no dia errado do relatorio.

        O fim e o comeco do dia SEGUINTE (exclusivo) para nao perder pedido
        gravado as 23:59:59.7.
        """
        start_at = datetime.combine(start_date, time.min, tzinfo=REPORT_TIMEZONE)
        end_at = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=REPORT_TIMEZONE)
        return start_at, end_at

    @staticmethod
    def _to_item(order) -> CommissionReportItem:
        return CommissionReportItem(
            order_id=order.id,
            order_number=order.order_number,
            created_at=order.created_at,
            status=order.status,
            payment_status=order.payment_status,
            payment_method=order.payment_method,
            subtotal=quantize_money(to_decimal(order.subtotal)),
            coupon_discount_amount=quantize_money(to_decimal(order.coupon_discount_amount)),
            cashback_redeemed_amount=quantize_money(to_decimal(order.cashback_redeemed_amount)),
            commission_base_amount=quantize_money(to_decimal(order.commission_base_amount)),
            commission_percent=to_decimal(order.commission_percent),
            commission_amount=quantize_money(to_decimal(order.commission_amount)),
            order_total=quantize_money(to_decimal(order.total)),
        )
