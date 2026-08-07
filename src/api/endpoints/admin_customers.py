from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from src.api.dependencies.admin_scope import AdminScope, get_admin_scope
from src.api.dependencies.database import get_db
from src.schemas.admin_customer_schema import AdminCustomerListResponse
from src.services.admin_customer_service import AdminCustomerService


# A lista sai de `orders` e nunca de `customers`: a conta do cliente e
# global da plataforma, e uma consulta ao cadastro entregaria ao lojista
# tambem quem nunca pediu na loja dele. Ver AdminCustomerService.
router = APIRouter(prefix="/admin", tags=["admin customers"])


@router.get("/customers", response_model=AdminCustomerListResponse)
def list_customers(
    branch_id: UUID | None = Query(
        default=None,
        description="Filtra por filial. Quem so tem acesso a uma filial ja vem filtrado.",
    ),
    search: str | None = Query(
        default=None, description="Telefone (so digitos) ou parte do nome"
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
    """
    return AdminCustomerService(db).list_customers(
        scope,
        branch_id=branch_id,
        search=search,
        limit=limit,
        offset=offset,
    )
