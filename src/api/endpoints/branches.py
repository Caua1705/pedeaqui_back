"""A rota que alimenta a tela de escolha de filial.

Fica em arquivo proprio, e nao dentro de `restaurants.py`, porque e a unica
rota publica que pode gastar dinheiro: cada filial no raio vira uma consulta
paga de rota ao Google. Misturada com as leituras baratas de restaurante, essa
propriedade some de vista de quem le o arquivo — e ela e o motivo de o
`@limiter.limit` estar aqui.
"""

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from src.api.dependencies.customer_auth import get_optional_current_customer
from src.api.dependencies.database import get_db
from src.api.rate_limit import BRANCH_AVAILABILITY_RATE_LIMIT, limiter
from src.models.customer_model import Customer
from src.schemas.branch_availability_schema import (
    BranchAvailabilityRequest,
    BranchAvailabilityResponse,
)
from src.services.branch_availability_service import BranchAvailabilityService


router = APIRouter(prefix="/restaurants", tags=["branches"])


@router.post(
    "/{restaurant_slug}/branches/availability",
    response_model=BranchAvailabilityResponse,
)
@limiter.limit(BRANCH_AVAILABILITY_RATE_LIMIT)
def list_branch_availability(
    request: Request,
    restaurant_slug: str,
    payload: BranchAvailabilityRequest,
    current_customer: Customer | None = Depends(get_optional_current_customer),
    db: Session = Depends(get_db),
) -> BranchAvailabilityResponse:
    """As filiais do restaurante, com aberta/fechada e — se vier endereco —
    distancia, taxa e se aquela filial entrega ali.

    **POST e nao GET** por dois motivos, e nenhum deles e criacao de recurso:
    o endereco do cliente e um objeto com seis campos, que na querystring
    viraria log de proxy com o endereco residencial de quem pediu; e a
    chamada tem custo (rota paga do Google por filial), que e coisa que nao se
    deve convidar um cache de CDN a repetir.

    O corpo pode vir **vazio** (`{}`): a resposta traz as filiais e o estado
    aberta/fechada, com `delivery` nulo em todas. E o primeiro carregamento da
    tela, antes de o cliente informar onde mora.

    Com endereco (`address_id` OU `address`, nunca os dois), cada filial ganha
    o bloco `delivery`. `delivery = null` significa "nao perguntei", nao
    "nao entrega" — a tela precisa distinguir os dois para nao desabilitar a
    filial cedo demais.

    Login e OPCIONAL, e so muda uma coisa: `address_id` so resolve endereco
    salvo de quem esta autenticado. Sem token, use `address`.
    """
    return BranchAvailabilityService(db).list_availability(
        restaurant_slug,
        payload,
        current_customer,
    )
