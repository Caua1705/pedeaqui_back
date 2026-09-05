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

from pydantic import BaseModel, Field

from src.core.constants import DESCRICAO_DE_WEEKDAY


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
    # De que recorte este relatorio esta falando. Nulo significa "o
    # restaurante inteiro", nunca "filial nenhuma" — e o que o dono recebe
    # quando nao pede loja nenhuma. Sem este campo o painel nao tem como
    # saber se o numero na tela e da loja escolhida ou da rede.
    branch_id: UUID | None = None
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
    # De que recorte este relatorio esta falando. Nulo significa "o
    # restaurante inteiro", nunca "filial nenhuma" — e o que o dono recebe
    # quando nao pede loja nenhuma. Sem este campo o painel nao tem como
    # saber se o numero na tela e da loja escolhida ou da rede.
    branch_id: UUID | None = None
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
    # De que recorte este relatorio esta falando. Nulo significa "o
    # restaurante inteiro", nunca "filial nenhuma" — e o que o dono recebe
    # quando nao pede loja nenhuma. Sem este campo o painel nao tem como
    # saber se o numero na tela e da loja escolhida ou da rede.
    branch_id: UUID | None = None
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
    # De que recorte este relatorio esta falando. Nulo significa "o
    # restaurante inteiro", nunca "filial nenhuma" — e o que o dono recebe
    # quando nao pede loja nenhuma. Sem este campo o painel nao tem como
    # saber se o numero na tela e da loja escolhida ou da rede.
    branch_id: UUID | None = None
    period: ReportPeriod
    orders_count: int
    revenue_total: Decimal
    payment_methods: list[PaymentMethodItem]


class ProductSalesItem(BaseModel):
    # Nulo se o produto foi removido do cardapio depois da venda. O nome
    # continua, porque vem do snapshot gravado no item do pedido.
    product_id: UUID | None = None
    product_name: str
    # A chave que junta o mesmo item das varias lojas nesta linha.
    #
    # Nula para produto sem chave — e ai a linha conta uma linha de
    # `products` so, como antes do cardapio por filial. Preenchida, a linha
    # pode somar duas lojas, e `product_id` aponta para uma delas: para
    # separar, chame a rota com `branch_id`.
    catalog_key: str | None = None
    orders_count: int
    quantity_total: int
    revenue_total: Decimal


class ProductSalesResponse(BaseModel):
    restaurant_id: UUID
    # De que recorte este relatorio esta falando. Nulo significa "o
    # restaurante inteiro", nunca "filial nenhuma" — e o que o dono recebe
    # quando nao pede loja nenhuma. Sem este campo o painel nao tem como
    # saber se o numero na tela e da loja escolhida ou da rede.
    branch_id: UUID | None = None
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
    # De que recorte este relatorio esta falando. Nulo significa "o
    # restaurante inteiro", nunca "filial nenhuma" — e o que o dono recebe
    # quando nao pede loja nenhuma. Sem este campo o painel nao tem como
    # saber se o numero na tela e da loja escolhida ou da rede.
    branch_id: UUID | None = None
    period: ReportPeriod
    orders_count: int
    amount_total: Decimal
    # Pedidos que VIRARAM venda no mesmo periodo, para dar denominador ao
    # numero de cima. Sem ele, "12 cancelamentos" nao diz nada.
    billable_orders_count: int
    cancellation_rate_percent: Decimal | None = None
    breakdown: list[CancellationBreakdownItem]

class SalesByHourItem(BaseModel):
    # 0 a 23, na hora LOCAL da operacao (America/Fortaleza). Um pedido das
    # 22h de sexta e hora 22, e nao 1 do sabado UTC.
    hour: int
    orders_count: int
    revenue_total: Decimal


class SalesByWeekdayHourItem(BaseModel):
    """Uma celula do mapa dia x hora."""

    # A frase sai de `DESCRICAO_DE_WEEKDAY` e nao esta escrita aqui, pelo
    # mesmo motivo dos outros sete campos de dia da semana: o painel consome
    # o `/openapi.json`, e a convencao tem que chegar identica em todos.
    weekday: int = Field(description=DESCRICAO_DE_WEEKDAY)
    hour: int
    orders_count: int
    revenue_total: Decimal


class SalesByHourResponse(BaseModel):
    restaurant_id: UUID
    # De que recorte este relatorio esta falando. Nulo significa "o
    # restaurante inteiro", nunca "filial nenhuma".
    branch_id: UUID | None = None
    period: ReportPeriod
    orders_count: int
    revenue_total: Decimal
    # As 24 horas SEMPRE, inclusive as sem venda, com zero — mesma regra de
    # `SalesByDayResponse.days`. O eixo do grafico e o dia inteiro, e uma
    # hora omitida faria a tela ligar 11h direto em 13h.
    hours: list[SalesByHourItem]
    # O mapa dia x hora, e aqui NAO ha preenchimento com zero: as celulas
    # ausentes sao as que nao existem no periodo. Num recorte de 7 dias,
    # emitir "segunda: 0" quando o periodo nao contem nenhuma segunda seria
    # a resposta afirmando que a loja nao vendeu num dia em que ela nem foi
    # perguntada.
    weekday_hours: list[SalesByWeekdayHourItem]


class NeighborhoodSalesItem(BaseModel):
    # Como veio no endereco do pedido, sem normalizar. Nulo e pedido de
    # entrega sem bairro registrado — o pedido existe e o dinheiro entrou,
    # e ninguem anotou onde. Mesma regra do `payment_method` nulo: nao vira
    # "outro", que seria um bairro de verdade.
    neighborhood: str | None = None
    city: str | None = None
    orders_count: int
    revenue_total: Decimal
    average_ticket: Decimal
    revenue_share_percent: Decimal | None = None


class NeighborhoodSalesResponse(BaseModel):
    restaurant_id: UUID
    branch_id: UUID | None = None
    period: ReportPeriod
    # SO PEDIDO DE ENTREGA. Retirada nao tem bairro, e joga-la num balde
    # "sem bairro" faria a maior regiao da tela ser o balcao. Por isso este
    # `orders_count` NAO bate com o de `/reports/summary`, e o campo abaixo
    # diz quantos ficaram de fora.
    orders_count: int
    revenue_total: Decimal
    # Pedidos faturados do periodo que NAO sao entrega. Existe para a
    # diferenca entre esta tela e o resumo ser explicavel sem abrir o codigo.
    #
    # `non_delivery` e nao `pickup`: hoje o que sobra e so retirada, e um
    # tipo de pedido novo cairia aqui dentro. O nome que descreve a REGRA
    # continua verdadeiro nesse dia; o que nomeasse o unico membro de hoje
    # passaria a mentir em silencio.
    non_delivery_orders_count: int
    neighborhoods: list[NeighborhoodSalesItem]


class CashbackReportBlock(BaseModel):
    """O cashback do periodo, dos dois lados.

    `earned_total` e credito GERADO no periodo (linhas `earned` de
    `cashback_transactions`), qualquer que seja o destino delas depois —
    saldo usado, vencido ou cancelado continua tendo sido gerado.

    `redeemed_total` e o que saiu do saldo DENTRO de pedidos faturados do
    periodo (`orders.cashback_redeemed_amount`). Os dois nao se cancelam e
    nao tem por que fechar entre si: o crédito nasce na conclusao de um
    pedido e o resgate acontece na criacao de outro, meses depois.
    """

    earned_total: MetricComparison
    redeemed_total: MetricComparison
    orders_with_redeem_count: int
    # Ha campanha VALENDO no recorte. Sem isto, "R$ 0,00 resgatados" nao
    # distingue "ninguem usa" de "ninguem ligou" — e a segunda e o estado
    # de fabrica: `cashback_rules.enabled` nasce falso em todo restaurante.
    configured: bool


class CustomersReportResponse(BaseModel):
    restaurant_id: UUID
    branch_id: UUID | None = None
    period: ReportPeriod
    previous_period: ReportPeriod
    # Clientes DISTINTOS com pedido faturado no periodo. A identidade e o
    # telefone do pedido (`customer_phone_snapshot`), a mesma de
    # /admin/customers — agrupar por `customer_id` descartaria o pedido de
    # visitante, que nao tem conta.
    customers_count: MetricComparison
    # "Novo" e pelo PERIODO DO RELATORIO: o primeiro pedido faturado da vida
    # do cliente NESTE RESTAURANTE cai dentro do recorte. Nao e o `segment`
    # de /admin/customers, que usa a janela RFV de dias corridos.
    new_customers_count: MetricComparison
    returning_customers_count: MetricComparison
    new_revenue_total: Decimal
    returning_revenue_total: Decimal
    cashback: CashbackReportBlock


class DurationStats(BaseModel):
    """Um tempo em minutos, com uma casa decimal.

    Mediana e p90 ANTES da media, e nao por estilo: um pedido esquecido tres
    horas puxa a media para "preparo de 40 min" e nao move a mediana. Quem
    le a tela precisa ver primeiro o numero que descreve o pedido tipico.

    `orders_count` e por bloco porque nem todo pedido passa por todos os
    estagios: retirada nao tem entrega, e pedido aceito e cancelado nao tem
    preparo. Sem ele, "mediana de 12 min" nao diz se saiu de 3 pedidos ou
    de 300.
    """

    median: Decimal | None = None
    p90: Decimal | None = None
    average: Decimal | None = None
    orders_count: int


class OperationsReportResponse(BaseModel):
    restaurant_id: UUID
    branch_id: UUID | None = None
    period: ReportPeriod
    # Pedidos faturados do periodo. E o universo de onde os tres blocos
    # saem, e nenhum deles chega a este numero.
    orders_count: int
    # Do pedido criado ate o lojista aceitar.
    accept_minutes: DurationStats
    # Do aceite ate `ready`.
    prep_minutes: DurationStats
    # De `out_for_delivery` ate `completed`. So entrega.
    delivery_minutes: DurationStats
    # Pedidos que ficaram prontos DEPOIS do prazo que o proprio pedido
    # prometeu (`orders.delivery_prep_time_max`, congelado na criacao), e
    # nao da configuracao atual da filial — o cliente leu o prazo antigo.
    late_orders_count: int
    # O denominador de `late_orders_percent`, publicado porque ele NAO e
    # `prep_minutes.orders_count`: pedido com preparo medido mas sem prazo
    # prometido nao pode ser julgado atrasado, e conta-lo embaixo faria a
    # tela subestimar o atraso.
    late_orders_base_count: int
    late_orders_percent: Decimal | None = None
