"""Entregadores, pelo painel: a taxa da filial, o cadastro e a atribuicao.

O entregador em si NAO entra aqui — ele nao tem Bearer de lojista. As rotas
dele estao em `courier.py`, com outra dependencia de autenticacao.

Nenhuma destas rotas aceita `print_agent`: a conta de maquina do agente de
impressao continua alcancando as quatro rotas de sempre e mais nenhuma.
"""

from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from src.api.dependencies.admin_scope import (
    GERENCIA,
    SOMENTE_DONO,
    AdminScope,
    exigir_papel,
    get_admin_scope,
)
from src.api.dependencies.database import get_db
from src.schemas.courier_schema import (
    AdminBranchCourierFeeResponse,
    AdminBranchCourierFeeUpdate,
)
from src.services.admin_courier_service import AdminCourierService


router = APIRouter(prefix="/admin", tags=["admin couriers"])


@router.get(
    "/branches/{branch_id}/courier-fee",
    response_model=AdminBranchCourierFeeResponse,
    # GERENCIA e nao PESSOAS: e termo comercial da loja (o que ela paga por
    # corrida), como a leitura de cupom — nao alavanca de balcao.
    dependencies=[Depends(exigir_papel(GERENCIA))],
)
def get_courier_fee(
    branch_id: UUID,
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> AdminBranchCourierFeeResponse:
    """Quanto esta filial paga ao entregador por corrida.

    `null` nos dois campos e "sem taxa configurada": a atribuicao congela
    snapshot nulo e o historico do entregador mostra a corrida sem valor.
    Nao e zero, de proposito.
    """
    return AdminCourierService(db).get_courier_fee(scope, branch_id)


@router.patch(
    "/branches/{branch_id}/courier-fee",
    response_model=AdminBranchCourierFeeResponse,
    # SOMENTE_DONO: e dinheiro que a loja paga. A regra do repositorio e que
    # quem escreve dinheiro e o dono, e aqui ela vale sem excecao por corpo.
    dependencies=[Depends(exigir_papel(SOMENTE_DONO))],
)
def update_courier_fee(
    branch_id: UUID,
    payload: AdminBranchCourierFeeUpdate,
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> AdminBranchCourierFeeResponse:
    """`base + km x por_km`, congelada em cada atribuicao a partir daqui.

    Motoboy pago por corrida: `courier_fee_base` preenchida e
    `courier_fee_per_km` em `0` (ou ausente). Campo ausente do corpo nao e
    tocado; `null` explicito apaga. **Nao mexe em nenhuma corrida ja
    atribuida** — a taxa e congelada no momento da atribuicao.

    Nenhum numero que o CLIENTE paga muda com isto.
    """
    return AdminCourierService(db).update_courier_fee(scope, branch_id, payload)
