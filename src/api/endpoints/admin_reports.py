from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from src.api.dependencies.admin_scope import (
    GERENCIA,
    SOMENTE_DONO,
    AdminScope,
    ensure_pode_ler_dinheiro,
    exigir_papel,
    get_admin_scope,
)
from src.api.dependencies.database import get_db
from src.schemas.admin_report_schema import (
    CancellationsResponse,
    CommissionReportResponse,
    CustomersReportResponse,
    NeighborhoodSalesResponse,
    OperationsReportResponse,
    PaymentMethodsResponse,
    ProductSalesResponse,
    SalesByDayResponse,
    SalesByHourResponse,
    SalesSummaryResponse,
)
from src.services.admin_report_service import (
    DEFAULT_PRODUCT_LIMIT,
    MAX_PRODUCT_LIMIT,
    AdminReportService,
)


# Mesma regra das outras rotas /admin: o restaurante sai do token, nunca da
# URL ou do corpo. Aqui isso e ainda mais sensivel — o relatorio expoe o
# faturamento do periodo.
#
# **Todas as rotas daqui passaram a usar `get_admin_scope`** (era
# `get_current_admin`) na revisao 20260820_0026, e ganharam `?branch_id=`. O
# comentario que estava aqui dizia: "quando a tela pedir recorte por filial, e
# aqui e no repositorio que entra o `branch_id`". E o que aconteceu.
#
# **A regra de papel muda junto, e nao por acaso.** Ela estava assim:
#
# - dinheiro (faturamento, comissao, ticket medio, formas de pagamento):
#   SOMENTE_DONO;
# - operacao (o que mais vende, o que e cancelado): GERENCIA.
#
# O motivo do primeiro grupo nunca foi "dinheiro e do dono". Era que, SEM
# recorte, ler o faturamento significava ler o do restaurante inteiro — dar
# isso ao gerente do Centro era entregar-lhe o resultado da Aldeota. Agora que
# existe recorte, a divisao passa a ser:
#
# - `/commission`: continua **SOMENTE_DONO**, com ou sem recorte. Comissao e o
#   percentual negociado com a plataforma (armadilha 17), nao desempenho de
#   loja; nao ha filtro que a transforme em assunto de quem toca o balcao.
# - `/summary`, `/sales-by-day`, `/payment-methods`: **GERENCIA com recorte
#   obrigatorio** para quem nao e dono — ver `ensure_pode_ler_dinheiro`.
#   Gerente sem `branch_id` recebe 403, porque ai a consulta soma as lojas
#   todas e e a mesma leitura que se recusava antes.
# - `/products`, `/cancellations`: **GERENCIA**, como ja eram, agora com o
#   filtro. Nenhum dos dois diz quanto entrou.
#
# As quatro rotas de 05/09/2026 entram na mesma divisao, e tres delas do lado
# do dinheiro:
#
# - `/sales-by-hour`, `/neighborhoods`, `/customers`: **GERENCIA com recorte
#   obrigatorio** (`ensure_pode_ler_dinheiro`), porque as tres publicam
#   faturamento. A de bairro parece operacional e nao e: ela diz quanto cada
#   regiao rendeu.
# - `/operations`: **GERENCIA** sem recorte obrigatorio. E a unica das quatro
#   que nao tem uma linha de dinheiro — sao minutos entre carimbos, e quem
#   toca o balcao precisa ler o proprio tempo de preparo.
#
# `branch_id` na querystring so RESTRINGE: quem esta preso a uma filial e
# pedir outra recebe 404, pela mesma `AdminScope.resolve_branch_filter` do
# resto do painel.
router = APIRouter(prefix="/admin/reports", tags=["admin reports"])

# Repetido nas cinco rotas de Desempenho. Ficam como constante para que a
# descricao do OpenAPI nao se descole de rota para rota.
_START_DATE = Query(..., description="Primeiro dia do periodo (inclusive)")
_END_DATE = Query(..., description="Ultimo dia do periodo (inclusive)")
_BRANCH_ID = Query(
    default=None,
    description="Recorte por filial. Omitido, soma o restaurante inteiro. So restringe.",
)


@router.get(
    "/commission",
    response_model=CommissionReportResponse,
    dependencies=[Depends(exigir_papel(SOMENTE_DONO))],
)
def commission_report(
    start_date: date = Query(..., description="Primeiro dia do periodo (inclusive)"),
    end_date: date = Query(..., description="Ultimo dia do periodo (inclusive)"),
    branch_id: UUID | None = _BRANCH_ID,
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> CommissionReportResponse:
    """Comissao da plataforma no periodo, com extrato pedido a pedido.

    As datas sao interpretadas no fuso da operacao (America/Fortaleza).
    Cancelados, recusados e estornados nao entram; quantos foram fica em
    `excluded_orders_count`.

    SOMENTE_DONO com ou sem `branch_id`: comissao e contrato com a
    plataforma, nao desempenho de loja.
    """
    return AdminReportService(db).commission_report(
        restaurant_id=scope.restaurant_id,
        start_date=start_date,
        end_date=end_date,
        branch_id=scope.resolve_branch_filter(branch_id),
    )


@router.get(
    "/summary",
    response_model=SalesSummaryResponse,
    dependencies=[Depends(exigir_papel(GERENCIA))],
)
def sales_summary(
    start_date: date = _START_DATE,
    end_date: date = _END_DATE,
    branch_id: UUID | None = _BRANCH_ID,
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> SalesSummaryResponse:
    """Faturamento, pedidos, ticket medio e divisao entrega/retirada.

    Traz junto os mesmos numeros do periodo anterior de igual tamanho — sete
    dias comparam com os sete anteriores. `change_percent` vem nulo quando o
    periodo anterior foi zero; nao existe variacao percentual a partir de
    zero.

    Cancelados, recusados e estornados nao entram no faturamento. Quantos
    foram fica em `excluded_orders_count`, e o detalhe em
    `/reports/cancellations`.

    Quem nao e dono precisa mandar `branch_id` — ver
    `ensure_pode_ler_dinheiro`.
    """
    branch_filter = scope.resolve_branch_filter(branch_id)
    ensure_pode_ler_dinheiro(scope, branch_filter)
    return AdminReportService(db).sales_summary(
        restaurant_id=scope.restaurant_id,
        start_date=start_date,
        end_date=end_date,
        branch_id=branch_filter,
    )


@router.get(
    "/sales-by-day",
    response_model=SalesByDayResponse,
    dependencies=[Depends(exigir_papel(GERENCIA))],
)
def sales_by_day(
    start_date: date = _START_DATE,
    end_date: date = _END_DATE,
    branch_id: UUID | None = _BRANCH_ID,
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> SalesByDayResponse:
    """Faturamento e pedidos dia a dia, para o grafico.

    Devolve TODOS os dias do periodo, inclusive os sem venda, com zero. O
    dia e o dia local (America/Fortaleza): um pedido das 22h de sexta conta
    na sexta, nao no sabado UTC.

    Quem nao e dono precisa mandar `branch_id`.
    """
    branch_filter = scope.resolve_branch_filter(branch_id)
    ensure_pode_ler_dinheiro(scope, branch_filter)
    return AdminReportService(db).sales_by_day(
        restaurant_id=scope.restaurant_id,
        start_date=start_date,
        end_date=end_date,
        branch_id=branch_filter,
    )


@router.get(
    "/payment-methods",
    response_model=PaymentMethodsResponse,
    dependencies=[Depends(exigir_papel(GERENCIA))],
)
def payment_methods_report(
    start_date: date = _START_DATE,
    end_date: date = _END_DATE,
    branch_id: UUID | None = _BRANCH_ID,
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> PaymentMethodsResponse:
    """Quanto entrou por forma de pagamento no periodo.

    `payment_method` nulo e pedido sem forma registrada, e continua nulo na
    resposta — nao vira "other", que e uma forma de pagamento de verdade.

    Quem nao e dono precisa mandar `branch_id`. E o relatorio em que o
    recorte mais muda a leitura: as formas aceitas sao de cada filial
    (`branch_payment_methods`), entao a soma da rede mistura lojas que nem
    oferecem os mesmos meios.
    """
    branch_filter = scope.resolve_branch_filter(branch_id)
    ensure_pode_ler_dinheiro(scope, branch_filter)
    return AdminReportService(db).payment_methods_report(
        restaurant_id=scope.restaurant_id,
        start_date=start_date,
        end_date=end_date,
        branch_id=branch_filter,
    )


@router.get(
    "/products",
    response_model=ProductSalesResponse,
    dependencies=[Depends(exigir_papel(GERENCIA))],
)
def product_sales_report(
    start_date: date = _START_DATE,
    end_date: date = _END_DATE,
    branch_id: UUID | None = _BRANCH_ID,
    limit: int = Query(
        default=DEFAULT_PRODUCT_LIMIT,
        ge=1,
        le=MAX_PRODUCT_LIMIT,
        description="Quantos produtos o ranking devolve",
    ),
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> ProductSalesResponse:
    """Produtos mais vendidos no periodo, por unidades.

    Agrupa pelo nome gravado no item do pedido, nao pelo nome atual do
    produto: renomear um produto no meio do periodo o separa em duas linhas,
    que e o correto — foram dois itens diferentes no cardapio de quem
    comprou.

    **Sem `branch_id`, os produtos que compartilham `catalog_key` somam as
    lojas numa linha so.** E a pergunta que a chave existe para responder
    ("quanto vendi de picanha nas duas lojas"). Produto sem chave continua
    contado por linha de `products`.

    `listed_revenue_total` NAO fecha com o faturamento de `/reports/summary`:
    e receita bruta de item, sem cupom, cashback nem taxas. A resposta
    carrega essa ressalva em `revenue_note`.
    """
    return AdminReportService(db).product_sales_report(
        restaurant_id=scope.restaurant_id,
        start_date=start_date,
        end_date=end_date,
        limit=limit,
        branch_id=scope.resolve_branch_filter(branch_id),
    )


@router.get(
    "/cancellations",
    response_model=CancellationsResponse,
    dependencies=[Depends(exigir_papel(GERENCIA))],
)
def cancellations_report(
    start_date: date = _START_DATE,
    end_date: date = _END_DATE,
    branch_id: UUID | None = _BRANCH_ID,
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> CancellationsResponse:
    """O outro lado do faturamento: o que nao virou venda.

    Exatamente o complemento do que os outros relatorios excluem —
    cancelados, recusados e estornados. A taxa e sobre TODOS os pedidos do
    periodo (faturados + excluidos), nao so sobre os faturados, e o recorte
    de filial vale para os dois lados da fracao.
    """
    return AdminReportService(db).cancellations_report(
        restaurant_id=scope.restaurant_id,
        start_date=start_date,
        end_date=end_date,
        branch_id=scope.resolve_branch_filter(branch_id),
    )


@router.get(
    "/sales-by-hour",
    response_model=SalesByHourResponse,
    dependencies=[Depends(exigir_papel(GERENCIA))],
)
def sales_by_hour(
    start_date: date = _START_DATE,
    end_date: date = _END_DATE,
    branch_id: UUID | None = _BRANCH_ID,
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> SalesByHourResponse:
    """A que horas a loja vende, somando todos os dias do periodo.

    Devolve as 24 HORAS SEMPRE, inclusive as sem venda, com zero — a mesma
    regra dos dias de `/reports/sales-by-day`. A hora e a hora LOCAL da
    operacao (America/Fortaleza): um pedido das 22h de sexta e hora 22, e
    nao 1h do sabado em UTC.

    `weekday_hours` e o mapa dia x hora, com `weekday` 0 = SEGUNDA e
    6 = domingo (o `datetime.weekday()` do Python, NAO o `getDay()` do
    JavaScript). Ele NAO vem preenchido com zeros, e a assimetria com
    `hours` e deliberada: as 24 horas existem em todo dia, mas um dia da
    semana pode simplesmente nao estar no periodo pedido — emitir
    "segunda: 0" num recorte sem nenhuma segunda seria afirmar que a loja
    nao vendeu num dia sobre o qual ninguem perguntou.

    Mesmo criterio de "faturado" do resumo: cancelados, recusados e
    estornados ficam de fora.

    Quem nao e dono precisa mandar `branch_id`.
    """
    branch_filter = scope.resolve_branch_filter(branch_id)
    ensure_pode_ler_dinheiro(scope, branch_filter)
    return AdminReportService(db).sales_by_hour(
        restaurant_id=scope.restaurant_id,
        start_date=start_date,
        end_date=end_date,
        branch_id=branch_filter,
    )


@router.get(
    "/neighborhoods",
    response_model=NeighborhoodSalesResponse,
    dependencies=[Depends(exigir_papel(GERENCIA))],
)
def neighborhoods_report(
    start_date: date = _START_DATE,
    end_date: date = _END_DATE,
    branch_id: UUID | None = _BRANCH_ID,
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> NeighborhoodSalesResponse:
    """Faturamento por bairro, para decidir onde estender ou encolher a area.

    **SO PEDIDO DE ENTREGA.** Retirada nao tem bairro, e joga-la num balde
    "sem bairro" faria a maior regiao da tela ser o balcao. Por isso
    `orders_count` daqui NAO bate com o de `/reports/summary`, e a diferenca
    vem publicada em `non_delivery_orders_count` — `non_delivery` e nao
    `pickup` porque hoje o que sobra e so retirada, e um tipo de pedido novo
    cairia ali dentro.

    O bairro sai do SNAPSHOT do pedido (`address_neighborhood`), como veio
    no endereco naquele dia, sem normalizar — o endereco cadastrado do
    cliente muda depois, e o relatorio precisa dizer para onde a comida foi.
    **Cidade entra no agrupamento junto com o bairro**: "Centro" de
    Fortaleza e "Centro" de Maracanau sao dois lugares.

    Pedido de entrega SEM bairro registrado vem com `neighborhood: null`, e
    nao num balde "outro" — o pedido existe, o dinheiro entrou, e ninguem
    anotou onde. Mesma regra do `payment_method` nulo de
    `/reports/payment-methods`.

    Ordenado por faturamento, do maior para o menor. Sem `limit`: a lista de
    bairros de um restaurante cabe numa resposta, e paginar um ranking de
    cinco linhas seria contrato a mais sem pergunta a menos.

    Quem nao e dono precisa mandar `branch_id`.
    """
    branch_filter = scope.resolve_branch_filter(branch_id)
    ensure_pode_ler_dinheiro(scope, branch_filter)
    return AdminReportService(db).neighborhoods_report(
        restaurant_id=scope.restaurant_id,
        start_date=start_date,
        end_date=end_date,
        branch_id=branch_filter,
    )


@router.get(
    "/customers",
    response_model=CustomersReportResponse,
    dependencies=[Depends(exigir_papel(GERENCIA))],
)
def customers_report(
    start_date: date = _START_DATE,
    end_date: date = _END_DATE,
    branch_id: UUID | None = _BRANCH_ID,
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> CustomersReportResponse:
    """Quem comprou no periodo — novos, recorrentes — e o cashback dos dois lados.

    **Identidade do cliente: o telefone do pedido**
    (`customer_phone_snapshot`), a mesma de `/admin/customers`. Agrupar por
    `customer_id` descartaria o pedido de visitante, que nao tem conta, e
    "12 clientes" aqui contaria menos gente que a tela de Clientes.

    **"Novo" e pelo PERIODO DESTE RELATORIO**, e nao o `segment` de
    `/admin/customers`: o primeiro pedido faturado da vida do cliente cai
    dentro de `[start_date, end_date]`. O segmento usa a janela RFV em dias
    corridos, e num recorte de 7 dias ele chamaria de "novo" quem estreou ha
    tres semanas.

    **Com `branch_id`, "primeiro pedido" continua sendo NO RESTAURANTE**, e
    nao na filial. Duas razoes: quem pediu na Aldeota depois de dois anos no
    Centro nao e um cliente novo do negocio, e com a leitura por filial a
    soma das lojas teria mais clientes novos que o restaurante inteiro. O
    recorte de filial restringe QUAIS PEDIDOS entram no periodo, nunca de
    onde vem a estreia.

    **O cashback tem dois numeros que nao fecham entre si, de proposito:**

    - `earned_total` e o credito GERADO no periodo (linhas `earned` do
      razao), qualquer que seja o destino delas depois. Sem filtro de
      `status`: filtrar por `available` faria o numero de um mes encolher
      sozinho conforme os clientes gastassem o saldo;
    - `redeemed_total` e o saldo que entrou nos pedidos faturados do
      periodo. O credito nasce na conclusao de um pedido e o resgate
      acontece na criacao de outro, meses depois — nao ha razao para os dois
      baterem.

    **`cashback_transactions` nao tem filial**, entao com `branch_id` o
    credito e atribuido pelo PEDIDO que o gerou. Consequencia: credito sem
    pedido — hoje so ajuste manual por SQL — nao entra em recorte de filial
    nenhum, porque nao ha como dizer de qual loja ele e. Sem `branch_id`
    ele entra.

    `configured` diz se ha campanha VALENDO no recorte, e existe porque
    "R$ 0,00 resgatados" sozinho nao distingue "ninguem usa" de "ninguem
    ligou" — e a segunda e o estado de fabrica: `cashback_rules.enabled`
    nasce falso em todo restaurante. Sem `branch_id`, a pergunta e do
    restaurante e so a regra dele responde.

    Quem nao e dono precisa mandar `branch_id`.
    """
    branch_filter = scope.resolve_branch_filter(branch_id)
    ensure_pode_ler_dinheiro(scope, branch_filter)
    return AdminReportService(db).customers_report(
        restaurant_id=scope.restaurant_id,
        start_date=start_date,
        end_date=end_date,
        branch_id=branch_filter,
    )


@router.get(
    "/operations",
    response_model=OperationsReportResponse,
    dependencies=[Depends(exigir_papel(GERENCIA))],
)
def operations_report(
    start_date: date = _START_DATE,
    end_date: date = _END_DATE,
    branch_id: UUID | None = _BRANCH_ID,
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> OperationsReportResponse:
    """Os tempos entre os carimbos do pedido: aceite, preparo e entrega.

    Sai de `order_status_history`, e o marco de cada estagio e o PRIMEIRO
    carimbo daquele status. Um pedido que volta a `preparing` depois de
    `ready` nao reabre o relogio: a promessa foi cumprida, ou nao, na
    primeira vez que a loja disse "pronto".

    - `accept_minutes` — da criacao do pedido ate o aceite;
    - `prep_minutes` — do aceite ate `ready`;
    - `delivery_minutes` — de `out_for_delivery` ate `completed`, **so em
      pedido de entrega**.

    Cada bloco traz o proprio `orders_count` porque nem todo pedido passa
    por todos os estagios: retirada nao tem entrega, e pedido aceito e
    cancelado nao tem preparo. Sem ele, "mediana de 12 min" nao diz se saiu
    de 3 pedidos ou de 300.

    **Mediana e p90 vem antes da media**, e nao por estilo: um pedido
    esquecido tres horas puxa a media para "preparo de 40 min" e nao move a
    mediana. Nulo quando nenhum pedido teve aquele estagio — e nao zero, que
    afirmaria um aceite instantaneo que nao aconteceu.

    `late_orders_count` compara com o `delivery_prep_time_max` do PROPRIO
    pedido, congelado na criacao, e nao com a configuracao atual da filial:
    o cliente leu aquele prazo na vitrine, e julgar a loja pela promessa que
    ela nao fez seria mudar a nota dela ao mexer numa tela de configuracao.

    O denominador de `late_orders_percent` vem publicado em
    `late_orders_base_count`, e ele NAO e `prep_minutes.orders_count`:
    pedido com preparo medido mas SEM prazo prometido nao pode ser julgado
    atrasado, e conta-lo embaixo faria a tela subestimar o atraso. Nulo
    quando nao ha denominador.

    GERENCIA sem recorte obrigatorio: e a unica rota de Desempenho que nao
    publica uma linha de dinheiro, e quem toca o balcao precisa ler o
    proprio tempo de preparo.
    """
    return AdminReportService(db).operations_report(
        restaurant_id=scope.restaurant_id,
        start_date=start_date,
        end_date=end_date,
        branch_id=scope.resolve_branch_filter(branch_id),
    )
