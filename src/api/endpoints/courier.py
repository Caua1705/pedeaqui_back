"""As rotas do ENTREGADOR. Link + codigo, nunca Bearer.

Quatro telas e mais nenhuma: quem sou, o que esta comigo, saiu/entregou, e
quanto fiz. Nada aqui le pedido que nao esteja atribuido a ele, e nada aqui
alcanca o painel.

Todas com rate limit por IP: o codigo tem seis digitos, e o limite e a
barreira contra forca bruta de quem conseguiu o link.
"""

from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from src.api.dependencies.courier_auth import get_current_courier
from src.api.dependencies.database import get_db
from src.api.rate_limit import COURIER_RATE_LIMIT, limiter
from src.models.courier_model import Courier
from src.schemas.courier_schema import (
    CourierHistoryResponse,
    CourierMeResponse,
    CourierOrderResponse,
    CourierOrdersStatusRequest,
    CourierStatusBatchResponse,
)
from src.services.courier_delivery_service import CourierDeliveryService


router = APIRouter(prefix="/courier", tags=["courier"])

_RESPOSTAS_DO_PAR = {
    401: {"description": "Codigo de acesso ausente ou errado (cabecalho `X-Courier-Code`)"},
    404: {"description": "Link invalido: desconhecido, regenerado, inativo ou excluido"},
}


@router.get("/{link_token}/me", response_model=CourierMeResponse, responses=_RESPOSTAS_DO_PAR)
@limiter.limit(COURIER_RATE_LIMIT)
def get_me(
    request: Request,
    link_token: str,
    courier: Courier = Depends(get_current_courier),
    db: Session = Depends(get_db),
) -> CourierMeResponse:
    """Quem e o entregador deste link. E a rota que o app chama para conferir
    o codigo digitado antes de guarda-lo: 200 e o par certo."""
    return CourierDeliveryService(db).me(courier)


@router.get("/{link_token}/orders", response_model=list[CourierOrderResponse], responses=_RESPOSTAS_DO_PAR)
@limiter.limit(COURIER_RATE_LIMIT)
def list_orders(
    request: Request,
    link_token: str,
    courier: Courier = Depends(get_current_courier),
    db: Session = Depends(get_db),
) -> list[CourierOrderResponse]:
    """Os pedidos atribuidos a ele que ainda nao terminaram, do mais antigo
    ao mais novo.

    Cada um traz status, endereco, forma de pagamento, `is_paid`, e
    `amount_to_collect` — o que ha para receber na porta, que e o total so
    quando o pedido e pago na entrega. `can_leave` e `can_deliver` dizem o
    que o botao pode fazer: em preparo ainda nao sai.
    """
    return CourierDeliveryService(db).list_orders(courier)


@router.post(
    "/{link_token}/orders/out-for-delivery",
    response_model=CourierStatusBatchResponse,
    responses=_RESPOSTAS_DO_PAR,
)
@limiter.limit(COURIER_RATE_LIMIT)
def mark_out_for_delivery(
    request: Request,
    link_token: str,
    payload: CourierOrdersStatusRequest,
    courier: Courier = Depends(get_current_courier),
    db: Session = Depends(get_db),
) -> CourierStatusBatchResponse:
    """"Sai para entrega", um ou varios de uma vez.

    **A resposta e por item, na ordem do corpo.** `not_found` e o pedido que
    nao esta com ele; `wrong_status` e o pedido que ainda nao esta pronto
    (ou ja saiu), com a frase em `message`. Os que deram `ok` JA SAIRAM,
    mesmo que outro do lote tenha falhado — nao ha como desfazer uma moto
    que ja esta na rua.

    O cliente ve a mudanca no acompanhamento do pedido; o painel recebe o
    evento no stream. Nenhuma notificacao e disparada alem disso.
    """
    return CourierDeliveryService(db).mark_out_for_delivery(courier, payload)


@router.post(
    "/{link_token}/orders/{order_id}/delivered",
    response_model=CourierOrderResponse,
    responses={
        **_RESPOSTAS_DO_PAR,
        409: {"description": "O pedido ainda nao saiu para entrega"},
    },
)
@limiter.limit(COURIER_RATE_LIMIT)
def mark_delivered(
    request: Request,
    link_token: str,
    order_id: UUID,
    courier: Courier = Depends(get_current_courier),
    db: Session = Depends(get_db),
) -> CourierOrderResponse:
    """"Entregue". So a partir de `out_for_delivery` (409 antes disso).

    Pedido que nao esta com ele — inclusive o que ja foi entregue — e 404:
    terminal saiu da lista dele. E a mesma escrita do painel, entao o
    cashback do cliente e creditado aqui, de graca.
    """
    return CourierDeliveryService(db).mark_delivered(courier, order_id)


@router.get("/{link_token}/history", response_model=CourierHistoryResponse, responses=_RESPOSTAS_DO_PAR)
@limiter.limit(COURIER_RATE_LIMIT)
def get_history(
    request: Request,
    link_token: str,
    start_date: date | None = Query(default=None, description="Primeiro dia (inclusive), no fuso da operacao. Sem datas: hoje."),
    end_date: date | None = Query(default=None, description="Ultimo dia (inclusive). Ausente: o mesmo de start_date."),
    courier: Courier = Depends(get_current_courier),
    db: Session = Depends(get_db),
) -> CourierHistoryResponse:
    """As entregas concluidas no periodo e a soma das taxas — "quanto fiz".

    Ate 92 dias. Corrida sem taxa (a filial nao tinha taxa configurada na
    atribuicao) conta como entrega e nao como zero: sai em
    `deliveries_without_fee`, que e o numero que o motoboy leva ao dono.
    """
    return CourierDeliveryService(db).history(courier, start_date, end_date)
