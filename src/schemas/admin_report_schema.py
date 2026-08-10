"""Contrato dos relatorios do painel.

Duas familias aqui. O extrato de comissao (`CommissionReport*`) le valores
ja congelados em cada pedido. Os relatorios de Desempenho (`Sales*`,
`PaymentMethod*`, `Product*`, `Cancellation*`) sao agregacoes vivas do
periodo — eles mudam se um pedido do periodo for cancelado depois.

Uma convencao vale para todos: `faturamento` e sempre `SUM(orders.total)`,
o valor cheio que o cliente pagou, ja com taxa de entrega e de servico e ja
com desconto aplicado. E o unico numero que bate com o caixa. As partes
ficam abertas em `SalesBreakdown` para quem precisa de outra leitura, em
vez de cada tela escolher a propria definicao de faturamento.
"""

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


class ReportPeriod(BaseModel):
    """O recorte que foi efetivamente lido.

    Devolvido em toda resposta de Desempenho porque o periodo anterior e
    calculado pelo servidor: sem ele na resposta, o painel nao teria como
    rotular a coluna de comparacao ("vs. 01/06 a 30/06") sem refazer a conta
    e arriscar discordar do servidor.
    """

    start_date: date
    end_date: date
    days: int


class SalesBreakdown(BaseModel):
    """As partes que compoem o faturamento do periodo.

    Existe para que `revenue_total` tenha uma definicao unica e conferivel.
    A identidade que vale:

        revenue_total = subtotal + delivery_fee + service_fee - discount

    `commission_total` NAO entra nessa conta: e o que a plataforma cobra do
    restaurante depois, nao algo que o cliente pagou. Vem junto porque a tela
    de desempenho mostra "quanto sobrou" e sem ele o lojista teria que abrir
    outro relatorio.
    """

    subtotal_total: Decimal
    delivery_fee_total: Decimal
    service_fee_total: Decimal
    discount_total: Decimal
    commission_total: Decimal


class OrderTypeSplitItem(BaseModel):
    order_type: str
    orders_count: int
    revenue_total: Decimal
    # Fatia do faturamento do periodo, 0 a 100. Nula quando o periodo nao teve
    # faturamento nenhum — dividir por zero para exibir "0%" mentiria sobre
    # uma divisao que nao existe.
    revenue_share_percent: Decimal | None = None


class MetricComparison(BaseModel):
    """Um numero do periodo atual ao lado do mesmo numero do anterior.

    `change_percent` e NULO quando `previous` e zero, e nao 100 ou infinito:
    sair de zero pedidos para dez nao e "crescimento de 1000%", e um comeco.
    O painel mostra um travessao nesse caso.
    """

    current: Decimal
    previous: Decimal
    change: Decimal
    change_percent: Decimal | None = None


class SalesSummaryResponse(BaseModel):
    restaurant_id: UUID
    period: ReportPeriod
    previous_period: ReportPeriod
    orders_count: int
    revenue_total: Decimal
    # Faturamento dividido pelo numero de pedidos. Zero pedidos devolve 0.00
    # em vez de nulo: aqui o zero nao e ambiguo, nao houve venda.
    average_ticket: Decimal
    breakdown: SalesBreakdown
    order_types: list[OrderTypeSplitItem]
    # Pedidos do periodo que nao entraram no faturamento (cancelados,
    # recusados, estornados). Mesmo criterio de `excluded_orders_count` do
    # extrato de comissao — o detalhe esta em /reports/cancellations.
    excluded_orders_count: int
    orders_count_comparison: MetricComparison
    revenue_comparison: MetricComparison
    average_ticket_comparison: MetricComparison


class SalesByDayItem(BaseModel):
    day: date
    orders_count: int
    revenue_total: Decimal


class SalesByDayResponse(BaseModel):
    restaurant_id: UUID
    period: ReportPeriod
    orders_count: int
    revenue_total: Decimal
    # Um item por dia do periodo, INCLUSIVE os sem venda. O grafico precisa
    # do dia zerado para desenhar o vale; se o servidor omitisse, cada tela
    # teria que reconstruir o calendario e acertar o fuso de novo.
    days: list[SalesByDayItem]


class PaymentMethodItem(BaseModel):
    # Nulo quando o pedido nao tem forma de pagamento registrada. Continua
    # nulo na resposta em vez de virar "other": sao coisas diferentes.
    payment_method: str | None = None
    orders_count: int
    revenue_total: Decimal
    revenue_share_percent: Decimal | None = None


class PaymentMethodsResponse(BaseModel):
    restaurant_id: UUID
    period: ReportPeriod
    orders_count: int
    revenue_total: Decimal
    payment_methods: list[PaymentMethodItem]


class ProductSalesItem(BaseModel):
    # Nulo se o produto foi removido do cardapio depois da venda. O nome
    # continua, porque vem do snapshot gravado no item do pedido.
    product_id: UUID | None = None
    product_name: str
    orders_count: int
    quantity_total: int
    revenue_total: Decimal


class ProductSalesResponse(BaseModel):
    restaurant_id: UUID
    period: ReportPeriod
    products: list[ProductSalesItem]
    # Soma de `revenue_total` dos itens listados. NAO e o faturamento do
    # periodo: e a receita bruta dos itens no top N, sem cupom, sem cashback
    # e sem taxa. Ver `revenue_note`.
    listed_revenue_total: Decimal
    revenue_note: str


class CancellationBreakdownItem(BaseModel):
    status: str
    payment_status: str
    orders_count: int
    amount_total: Decimal


class CancellationsResponse(BaseModel):
    restaurant_id: UUID
    period: ReportPeriod
    orders_count: int
    amount_total: Decimal
    # Pedidos que VIRARAM venda no mesmo periodo, para dar denominador ao
    # numero de cima. Sem ele, "12 cancelamentos" nao diz nada.
    billable_orders_count: int
    cancellation_rate_percent: Decimal | None = None
    breakdown: list[CancellationBreakdownItem]
