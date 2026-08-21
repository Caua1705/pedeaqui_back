from datetime import date
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from src.api.dependencies.admin_scope import (
    GERENCIA,
    AdminScope,
    exigir_papel,
    get_admin_scope,
)
from src.api.dependencies.database import get_db
from src.schemas.admin_customer_schema import (
    AdminCustomerListResponse,
    CustomerSegment,
)
from src.services.admin_customer_service import AdminCustomerService


# A lista sai de `orders` e nunca de `customers`: a conta do cliente e
# global da plataforma, e uma consulta ao cadastro entregaria ao lojista
# tambem quem nunca pediu na loja dele. Ver AdminCustomerService.
router = APIRouter(prefix="/admin", tags=["admin customers"])


@router.get(
    "/customers",
    response_model=AdminCustomerListResponse,
    # Nome + telefone de toda a base de clientes da loja, exportavel em
    # pouca coisa mais que um `for` sobre o `offset`. E a rota que mais
    # pesa numa senha vazada, e o balcao nao precisa dela: quem atende ja ve
    # o telefone do pedido que esta na tela.
    dependencies=[Depends(exigir_papel(GERENCIA))],
)
def list_customers(
    branch_id: UUID | None = Query(
        default=None,
        description="Filtra por filial. Quem so tem acesso a uma filial ja vem filtrado.",
    ),
    search: str | None = Query(
        default=None, description="Telefone (so digitos) ou parte do nome"
    ),
    segment: CustomerSegment | None = Query(
        default=None, description="Classificacao RFV: novo, ocasional, fiel, em_risco, perdido"
    ),
    last_order_from: date | None = Query(
        default=None, description="Ultimo pedido a partir deste dia (inclusive)"
    ),
    last_order_to: date | None = Query(
        default=None, description="Ultimo pedido ate este dia (inclusive)"
    ),
    min_ticket: Decimal | None = Query(
        default=None, ge=0, description="Ticket medio minimo, em reais"
    ),
    max_ticket: Decimal | None = Query(
        default=None, ge=0, description="Ticket medio maximo, em reais"
    ),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> AdminCustomerListResponse:
    """Clientes que ja pediram neste restaurante (BLOCO D1).

    Agrupado por telefone, do pedido mais recente para o mais antigo. Nao
    devolve e-mail, CPF nem o id de cadastro: sao dados da conta global da
    plataforma, nao do relacionamento com esta loja.

    **Os cinco filtros valem antes do `LIMIT`**, e o `total` do envelope conta
    o que sobrou depois deles. Filtrar a pagina ja paginada devolveria tres
    linhas de cinquenta e um total que nao bate com o que a tela mostra.

    As duas datas sao lidas no fuso da operacao (America/Fortaleza), como nos
    relatorios, e `last_order_to` e INCLUSIVO — o dia inteiro entra.
    """
    return AdminCustomerService(db).list_customers(
        scope,
        branch_id=branch_id,
        search=search,
        segment=segment,
        last_order_from=last_order_from,
        last_order_to=last_order_to,
        min_ticket=min_ticket,
        max_ticket=max_ticket,
        limit=limit,
        offset=offset,
    )
