from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from src.api.dependencies.admin_auth import get_current_admin
from src.api.dependencies.database import get_db
from src.models.admin_user_model import AdminUser
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
# Todas as rotas daqui usam `get_current_admin` e nao `get_admin_scope`: o
# relatorio e do RESTAURANTE, nao da filial. Um manager preso a uma filial
# ainda ve o faturamento do restaurante inteiro — a mesma situacao que ja
# vale para cardapio e cupom, que tambem nao tem filial. Quando a tela pedir
# recorte por filial, e aqui e no repositorio que entra o `branch_id`.
router = APIRouter(prefix="/admin/reports", tags=["admin reports"])

# Repetido nas cinco rotas de Desempenho. Ficam como constante para que a
# descricao do OpenAPI nao se descole de rota para rota.
_START_DATE = Query(..., description="Primeiro dia do periodo (inclusive)")
_END_DATE = Query(..., description="Ultimo dia do periodo (inclusive)")


@router.get("/commission", response_model=CommissionReportResponse)
def commission_report(
    start_date: date = Query(..., description="Primeiro dia do periodo (inclusive)"),
    end_date: date = Query(..., description="Ultimo dia do periodo (inclusive)"),
    admin_user: AdminUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
) -> CommissionReportResponse:
    """Comissao da plataforma no periodo, com extrato pedido a pedido.

    As datas sao interpretadas no fuso da operacao (America/Fortaleza).
    Cancelados, recusados e estornados nao entram; quantos foram fica em
    `excluded_orders_count`.
    """
    return AdminReportService(db).commission_report(
        restaurant_id=admin_user.restaurant_id,
        start_date=start_date,
        end_date=end_date,
    )


@router.get("/summary", response_model=SalesSummaryResponse)
def sales_summary(
    start_date: date = _START_DATE,
    end_date: date = _END_DATE,
    admin_user: AdminUser = Depends(get_current_admin),
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
    """
    return AdminReportService(db).sales_summary(
        restaurant_id=admin_user.restaurant_id,
        start_date=start_date,
        end_date=end_date,
    )


@router.get("/sales-by-day", response_model=SalesByDayResponse)
def sales_by_day(
    start_date: date = _START_DATE,
    end_date: date = _END_DATE,
    admin_user: AdminUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
) -> SalesByDayResponse:
    """Faturamento e pedidos dia a dia, para o grafico.

    Devolve TODOS os dias do periodo, inclusive os sem venda, com zero. O
    dia e o dia local (America/Fortaleza): um pedido das 22h de sexta conta
    na sexta, nao no sabado UTC.
    """
    return AdminReportService(db).sales_by_day(
        restaurant_id=admin_user.restaurant_id,
        start_date=start_date,
        end_date=end_date,
    )


@router.get("/payment-methods", response_model=PaymentMethodsResponse)
def payment_methods_report(
    start_date: date = _START_DATE,
    end_date: date = _END_DATE,
    admin_user: AdminUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
) -> PaymentMethodsResponse:
    """Quanto entrou por forma de pagamento no periodo.

    `payment_method` nulo e pedido sem forma registrada, e continua nulo na
    resposta — nao vira "other", que e uma forma de pagamento de verdade.
    """
    return AdminReportService(db).payment_methods_report(
        restaurant_id=admin_user.restaurant_id,
        start_date=start_date,
        end_date=end_date,
    )


@router.get("/products", response_model=ProductSalesResponse)
def product_sales_report(
    start_date: date = _START_DATE,
    end_date: date = _END_DATE,
    limit: int = Query(
        default=DEFAULT_PRODUCT_LIMIT,
        ge=1,
        le=MAX_PRODUCT_LIMIT,
        description="Quantos produtos o ranking devolve",
    ),
    admin_user: AdminUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
) -> ProductSalesResponse:
    """Produtos mais vendidos no periodo, por unidades.

    Agrupa pelo nome gravado no item do pedido, nao pelo nome atual do
    produto: renomear um produto no meio do periodo o separa em duas linhas,
    que e o correto — foram dois itens diferentes no cardapio de quem
    comprou.

    `listed_revenue_total` NAO fecha com o faturamento de `/reports/summary`:
    e receita bruta de item, sem cupom, cashback nem taxas. A resposta
    carrega essa ressalva em `revenue_note`.
    """
    return AdminReportService(db).product_sales_report(
        restaurant_id=admin_user.restaurant_id,
        start_date=start_date,
        end_date=end_date,
        limit=limit,
    )


@router.get("/cancellations", response_model=CancellationsResponse)
def cancellations_report(
    start_date: date = _START_DATE,
    end_date: date = _END_DATE,
    admin_user: AdminUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
) -> CancellationsResponse:
    """O outro lado do faturamento: o que nao virou venda.

    Exatamente o complemento do que os outros relatorios excluem —
    cancelados, recusados e estornados. A taxa e sobre TODOS os pedidos do
    periodo (faturados + excluidos), nao so sobre os faturados.
    """
    return AdminReportService(db).cancellations_report(
        restaurant_id=admin_user.restaurant_id,
        start_date=start_date,
        end_date=end_date,
    )
