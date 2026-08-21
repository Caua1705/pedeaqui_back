"""Relatorios do painel do lojista.

Duas familias, com naturezas diferentes:

**Comissao.** Nao calcula nada: le o que ja esta gravado em cada pedido
(`commission_*`) e soma. Recalcular aqui traria de volta o problema que a
Fase 2 resolveu — o numero mudaria conforme o percentual do restaurante
fosse alterado.

**Desempenho.** Agregacoes vivas: faturamento, serie diaria, formas de
pagamento, produtos e cancelamentos. Sao somas feitas no banco, na hora, e
mudam se um pedido do periodo for cancelado depois. Isso e proposital — a
tela responde "como esta indo", nao "o que foi fechado".

O que os dois compartilham, e que nao pode divergir:

1. **O fuso.** As datas da querystring sao lidas em America/Fortaleza, nao
   em UTC. Ver `_period_bounds`.
2. **Quem entra na conta.** Cancelado, recusado e estornado ficam de fora,
   pelo mesmo predicado SQL do extrato — `billable_order_conditions`, em
   src/repositories/order_repository.py. Nenhum relatorio daqui reescreve
   essa regra.
3. **O recorte de filial.** `branch_id` nulo e "o restaurante inteiro", e ele
   entra pelo mesmo predicado do item 2 — nenhuma consulta daqui filtra
   filial por conta propria. Quem decide se o nulo e permitido nao e este
   arquivo: e `ensure_pode_ler_dinheiro`, na rota.
"""

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from src.core.constants import PLATFORM_TIMEZONE
from src.utils.date_window import period_bounds
from src.repositories.admin_report_repository import AdminReportRepository
from src.repositories.order_repository import OrderRepository
from src.schemas.admin_report_schema import (
    CancellationBreakdownItem,
    CancellationsResponse,
    CommissionReportItem,
    CommissionReportResponse,
    MetricComparison,
    OrderTypeSplitItem,
    PaymentMethodItem,
    PaymentMethodsResponse,
    ProductSalesItem,
    ProductSalesResponse,
    ReportPeriod,
    SalesBreakdown,
    SalesByDayItem,
    SalesByDayResponse,
    SalesSummaryResponse,
)
from src.utils.money import ZERO, quantize_money, to_decimal


REPORT_TIMEZONE = ZoneInfo(PLATFORM_TIMEZONE)

# Teto do recorte. Sem limite, um `start_date=2020-01-01` varre a tabela
# inteira e devolve um JSON gigante para o navegador do lojista.
MAX_REPORT_DAYS = 92

# Quantos produtos o ranking devolve por padrao e no maximo. O teto existe
# porque um restaurante grande tem milhares de produtos e a tela mostra uma
# lista, nao o catalogo inteiro.
DEFAULT_PRODUCT_LIMIT = 20
MAX_PRODUCT_LIMIT = 100

# Aviso fixo na resposta de /reports/products. Fica como constante porque e
# uma ressalva de contrato, nao um texto de tela: o numero daquele relatorio
# genuinamente nao fecha com o do resumo, e quem consome precisa saber
# disso sem ler o codigo.
PRODUCT_REVENUE_NOTE = (
    "Receita bruta dos itens, sem cupom, cashback, taxa de entrega ou taxa "
    "de servico. Nao fecha com revenue_total de /reports/summary."
)

ONE_HUNDRED = Decimal("100")


class AdminReportService:
    def __init__(self, db: Session):
        self.order_repository = OrderRepository(db)
        self.report_repository = AdminReportRepository(db)

    def commission_report(
        self,
        restaurant_id: UUID,
        start_date: date,
        end_date: date,
        branch_id: UUID | None = None,
    ) -> CommissionReportResponse:
        self._validate_period(start_date, end_date)
        start_at, end_at = self._period_bounds(start_date, end_date)

        orders = self.order_repository.list_orders_for_commission(
            restaurant_id, start_at, end_at, branch_id
        )
        excluded_count = self.order_repository.count_excluded_from_commission(
            restaurant_id, start_at, end_at, branch_id
        )

        items = [self._to_item(order) for order in orders]
        return CommissionReportResponse(
            restaurant_id=restaurant_id,
            branch_id=branch_id,
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

    def sales_summary(
        self,
        restaurant_id: UUID,
        start_date: date,
        end_date: date,
        branch_id: UUID | None = None,
    ) -> SalesSummaryResponse:
        """Os numeros do topo da aba Desempenho, com o periodo anterior ao lado.

        O periodo anterior e o bloco de mesmo tamanho imediatamente antes:
        pediu 7 dias, compara com os 7 dias anteriores. Nao e "o mes
        passado" nem "a mesma semana do ano passado" — comparar blocos de
        tamanhos diferentes produz uma variacao que so mede a diferenca de
        tamanho.
        """
        self._validate_period(start_date, end_date)
        period = self._period(start_date, end_date)
        previous = self._previous_period(period)

        current_totals = self._totals_for(restaurant_id, period, branch_id)
        previous_totals = self._totals_for(restaurant_id, previous, branch_id)

        start_at, end_at = self._period_bounds(start_date, end_date)
        type_rows = self.report_repository.totals_by_order_type(
            restaurant_id, start_at, end_at, branch_id
        )
        excluded = self.report_repository.cancellation_totals(
            restaurant_id, start_at, end_at, branch_id
        )

        revenue = current_totals["revenue_total"]
        return SalesSummaryResponse(
            restaurant_id=restaurant_id,
            branch_id=branch_id,
            period=period,
            previous_period=previous,
            orders_count=current_totals["orders_count"],
            revenue_total=revenue,
            average_ticket=current_totals["average_ticket"],
            breakdown=SalesBreakdown(
                subtotal_total=current_totals["subtotal_total"],
                delivery_fee_total=current_totals["delivery_fee_total"],
                service_fee_total=current_totals["service_fee_total"],
                discount_total=current_totals["discount_total"],
                commission_total=current_totals["commission_total"],
            ),
            order_types=[
                OrderTypeSplitItem(
                    order_type=order_type,
                    orders_count=orders_count,
                    revenue_total=quantize_money(to_decimal(type_revenue)),
                    revenue_share_percent=self._share(to_decimal(type_revenue), revenue),
                )
                for order_type, orders_count, type_revenue in type_rows
            ],
            excluded_orders_count=excluded["orders_count"],
            orders_count_comparison=self._compare(
                Decimal(current_totals["orders_count"]),
                Decimal(previous_totals["orders_count"]),
            ),
            revenue_comparison=self._compare(revenue, previous_totals["revenue_total"]),
            average_ticket_comparison=self._compare(
                current_totals["average_ticket"], previous_totals["average_ticket"]
            ),
        )

    def sales_by_day(
        self,
        restaurant_id: UUID,
        start_date: date,
        end_date: date,
        branch_id: UUID | None = None,
    ) -> SalesByDayResponse:
        """Serie diaria do periodo, com os dias sem venda preenchidos com zero.

        O banco so devolve dia que teve pedido. O preenchimento e feito aqui
        porque e aqui que se sabe o periodo pedido — e sem ele o grafico do
        painel ligaria terca direto em quinta, escondendo a quarta parada.
        """
        self._validate_period(start_date, end_date)
        start_at, end_at = self._period_bounds(start_date, end_date)

        rows = self.report_repository.sales_by_day(restaurant_id, start_at, end_at, branch_id)
        by_day = {
            row[0]: (row[1], quantize_money(to_decimal(row[2]))) for row in rows
        }

        days: list[SalesByDayItem] = []
        current = start_date
        while current <= end_date:
            orders_count, revenue = by_day.get(current, (0, ZERO))
            days.append(
                SalesByDayItem(
                    day=current, orders_count=orders_count, revenue_total=revenue
                )
            )
            current += timedelta(days=1)

        return SalesByDayResponse(
            restaurant_id=restaurant_id,
            branch_id=branch_id,
            period=self._period(start_date, end_date),
            orders_count=sum(item.orders_count for item in days),
            revenue_total=quantize_money(
                sum((item.revenue_total for item in days), ZERO)
            ),
            days=days,
        )

    def payment_methods_report(
        self,
        restaurant_id: UUID,
        start_date: date,
        end_date: date,
        branch_id: UUID | None = None,
    ) -> PaymentMethodsResponse:
        self._validate_period(start_date, end_date)
        start_at, end_at = self._period_bounds(start_date, end_date)

        rows = self.report_repository.totals_by_payment_method(
            restaurant_id, start_at, end_at, branch_id
        )
        revenue_total = quantize_money(
            sum((to_decimal(row[2]) for row in rows), ZERO)
        )
        return PaymentMethodsResponse(
            restaurant_id=restaurant_id,
            branch_id=branch_id,
            period=self._period(start_date, end_date),
            orders_count=sum(row[1] for row in rows),
            revenue_total=revenue_total,
            payment_methods=[
                PaymentMethodItem(
                    payment_method=method,
                    orders_count=orders_count,
                    revenue_total=quantize_money(to_decimal(method_revenue)),
                    revenue_share_percent=self._share(
                        to_decimal(method_revenue), revenue_total
                    ),
                )
                for method, orders_count, method_revenue in rows
            ],
        )

    def product_sales_report(
        self,
        restaurant_id: UUID,
        start_date: date,
        end_date: date,
        limit: int = DEFAULT_PRODUCT_LIMIT,
        branch_id: UUID | None = None,
    ) -> ProductSalesResponse:
        """Ranking de produtos por unidades vendidas.

        Ordena por quantidade, nao por receita: a pergunta da tela e "o que
        mais sai", e ordenar por dinheiro poe o prato mais caro no topo com
        tres unidades vendidas. A receita vem na linha para quem quiser a
        outra leitura.

        **Sem `branch_id`, produtos que compartilham `catalog_key` somam as
        lojas numa linha so** — e a pergunta que a chave existe para
        responder. Produto sem chave continua contado por linha de
        `products`, como antes. Ver `_identidade_do_item` no repositorio.
        """
        self._validate_period(start_date, end_date)
        start_at, end_at = self._period_bounds(start_date, end_date)

        rows = self.report_repository.top_products(
            restaurant_id, start_at, end_at, limit, branch_id
        )
        products = [
            ProductSalesItem(
                product_id=product_id,
                product_name=product_name,
                catalog_key=catalog_key,
                orders_count=orders_count,
                quantity_total=int(quantity_total or 0),
                revenue_total=quantize_money(to_decimal(item_revenue)),
            )
            for (
                product_id,
                product_name,
                catalog_key,
                orders_count,
                quantity_total,
                item_revenue,
            ) in rows
        ]
        return ProductSalesResponse(
            restaurant_id=restaurant_id,
            branch_id=branch_id,
            period=self._period(start_date, end_date),
            products=products,
            listed_revenue_total=quantize_money(
                sum((item.revenue_total for item in products), ZERO)
            ),
            revenue_note=PRODUCT_REVENUE_NOTE,
        )

    def cancellations_report(
        self,
        restaurant_id: UUID,
        start_date: date,
        end_date: date,
        branch_id: UUID | None = None,
    ) -> CancellationsResponse:
        """Pedidos que nao viraram venda, e a taxa que isso representa.

        O denominador da taxa e o total do periodo (o que virou venda mais o
        que nao virou), nao so o que virou. Sobre a base faturada, "12
        cancelamentos em 100 vendas" daria 12%, mas foram 12 em 112 pedidos
        feitos — 10,7%. O numero que interessa e a fatia de tudo que entrou.
        """
        self._validate_period(start_date, end_date)
        start_at, end_at = self._period_bounds(start_date, end_date)

        totals = self.report_repository.cancellation_totals(
            restaurant_id, start_at, end_at, branch_id
        )
        billable = self.report_repository.sales_totals(restaurant_id, start_at, end_at, branch_id)
        rows = self.report_repository.cancellations_by_status(
            restaurant_id, start_at, end_at, branch_id
        )

        cancelled_count = totals["orders_count"]
        billable_count = billable["orders_count"]
        all_orders = cancelled_count + billable_count
        return CancellationsResponse(
            restaurant_id=restaurant_id,
            branch_id=branch_id,
            period=self._period(start_date, end_date),
            orders_count=cancelled_count,
            amount_total=quantize_money(to_decimal(totals["amount_total"])),
            billable_orders_count=billable_count,
            cancellation_rate_percent=self._share(
                Decimal(cancelled_count), Decimal(all_orders)
            ),
            breakdown=[
                CancellationBreakdownItem(
                    status=order_status,
                    payment_status=payment_status,
                    orders_count=orders_count,
                    amount_total=quantize_money(to_decimal(amount)),
                )
                for order_status, payment_status, orders_count, amount in rows
            ],
        )

    def _totals_for(
        self,
        restaurant_id: UUID,
        period: ReportPeriod,
        branch_id: UUID | None = None,
    ) -> dict:
        """Somas de um periodo, ja com o ticket medio resolvido.

        Existe porque o resumo precisa exatamente disto duas vezes — para o
        periodo pedido e para o anterior — e um ticket medio calculado de
        dois jeitos diferentes nas duas metades da mesma tela seria um bug
        dificil de enxergar.
        """
        start_at, end_at = self._period_bounds(period.start_date, period.end_date)
        totals = self.report_repository.sales_totals(restaurant_id, start_at, end_at, branch_id)

        orders_count = totals["orders_count"]
        revenue = quantize_money(to_decimal(totals["revenue_total"]))
        return {
            "orders_count": orders_count,
            "revenue_total": revenue,
            "average_ticket": (
                quantize_money(revenue / orders_count) if orders_count else ZERO
            ),
            "subtotal_total": quantize_money(to_decimal(totals["subtotal_total"])),
            "delivery_fee_total": quantize_money(to_decimal(totals["delivery_fee_total"])),
            "service_fee_total": quantize_money(to_decimal(totals["service_fee_total"])),
            "discount_total": quantize_money(to_decimal(totals["discount_total"])),
            "commission_total": quantize_money(to_decimal(totals["commission_total"])),
        }

    @staticmethod
    def _period(start_date: date, end_date: date) -> ReportPeriod:
        return ReportPeriod(
            start_date=start_date,
            end_date=end_date,
            days=(end_date - start_date).days + 1,
        )

    @staticmethod
    def _previous_period(period: ReportPeriod) -> ReportPeriod:
        """O bloco de mesmo tamanho que termina na vespera do periodo pedido."""
        previous_end = period.start_date - timedelta(days=1)
        previous_start = previous_end - timedelta(days=period.days - 1)
        return ReportPeriod(
            start_date=previous_start, end_date=previous_end, days=period.days
        )

    @staticmethod
    def _share(part: Decimal, whole: Decimal) -> Decimal | None:
        """Fatia percentual, ou None quando o todo e zero.

        None e nao zero: com denominador zero nao existe fatia, e devolver
        "0%" faria a tela afirmar que aquela forma de pagamento nao vendeu
        nada quando na verdade nao vendeu nada NENHUMA delas.
        """
        if whole == 0:
            return None
        return quantize_money(part * ONE_HUNDRED / whole)

    @staticmethod
    def _compare(current: Decimal, previous: Decimal) -> MetricComparison:
        change = current - previous
        return MetricComparison(
            current=current,
            previous=previous,
            change=change,
            # Sem percentual quando a base e zero. Ver MetricComparison.
            change_percent=(
                quantize_money(change * ONE_HUNDRED / previous) if previous else None
            ),
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
        return period_bounds(start_date, end_date)

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
