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
    PaymentMethodsResponse,
    ProductSalesResponse,
    SalesByDayResponse,
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
