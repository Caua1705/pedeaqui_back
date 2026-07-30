from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from src.api.dependencies.admin_auth import get_current_admin
from src.api.dependencies.database import get_db
from src.models.admin_user_model import AdminUser
from src.schemas.admin_report_schema import CommissionReportResponse
from src.services.admin_report_service import AdminReportService


# Mesma regra das outras rotas /admin: o restaurante sai do token, nunca da
# URL ou do corpo. Aqui isso e ainda mais sensivel — o relatorio expoe o
# faturamento do periodo.
router = APIRouter(prefix="/admin/reports", tags=["admin reports"])


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
