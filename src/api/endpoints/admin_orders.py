from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query
from sqlalchemy.orm import Session
from starlette.responses import StreamingResponse

from src.api.dependencies.admin_auth import get_current_admin
from src.api.dependencies.admin_scope import AdminScope, build_admin_scope, get_admin_scope
from src.api.dependencies.database import get_db
from src.models.admin_user_model import AdminUser
from src.schemas.admin_order_schema import (
    AdminOrderListResponse,
    AdminOrderStatusCountsResponse,
    AdminOrderStreamEvent,
    AdminStreamTicketResponse,
    UpdateOrderStatusRequest,
)
from src.schemas.order_schema import OrderDetailResponse
from src.services.admin_auth_service import AdminAuthService
from src.services.admin_order_service import AdminOrderService
from src.services.admin_order_stream_service import AdminOrderStreamService
from src.services.idempotency_service import normalize_idempotency_key


# O escopo destas rotas vem SEMPRE do token do lojista: restaurante e, quando
# o papel for manager/attendant com filial fixada, tambem a filial. Nenhuma
# rota aqui aceita restaurante como parametro — a listagem aceitava um slug
# na URL e foi corrigida, porque uma rota que RECEBE restaurante e uma rota
# que pode esquecer de conferi-lo.
router = APIRouter(prefix="/admin", tags=["admin orders"])


class EventStreamResponse(StreamingResponse):
    """StreamingResponse com media_type fixo.

    Unica razao de existir: o FastAPI monta o OpenAPI usando o `media_type`
    da classe de resposta. Sem ela o schema do evento sairia publicado como
    `application/json`, e o cliente gerado para o frontend descreveria
    errado o unico endpoint que nao devolve JSON.
    """

    media_type = "text/event-stream"


@router.get("/orders", response_model=AdminOrderListResponse)
def list_orders(
    branch_id: UUID | None = Query(
        default=None,
        description="Filtra por filial. Quem so tem acesso a uma filial ja vem filtrado.",
    ),
    status: str | None = Query(default=None, description="Um status de ORDER_STATUSES"),
    start_date: date | None = Query(
        default=None, description="Primeiro dia do periodo (inclusive), no fuso da operacao"
    ),
    end_date: date | None = Query(
        default=None, description="Ultimo dia do periodo (inclusive), no fuso da operacao"
    ),
    search: str | None = Query(
        default=None, description="Numero do pedido (so digitos) ou parte do nome do cliente"
    ),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> AdminOrderListResponse:
    return AdminOrderService(db).list_orders(
        scope,
        branch_id=branch_id,
        order_status=status,
        start_date=start_date,
        end_date=end_date,
        search=search,
        limit=limit,
        offset=offset,
    )


@router.get("/orders/status-counts", response_model=AdminOrderStatusCountsResponse)
def count_orders_by_status(
    branch_id: UUID | None = Query(default=None),
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    search: str | None = Query(default=None),
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> AdminOrderStatusCountsResponse:
    """Contadores dos badges da tela de pedidos.

    Aceita os mesmos filtros da listagem (menos `status`, que zeraria os
    outros contadores) para que badge e lista mostrem o mesmo recorte.
    """
    return AdminOrderService(db).count_orders_by_status(
        scope,
        branch_id=branch_id,
        start_date=start_date,
        end_date=end_date,
        search=search,
    )


@router.post("/orders/stream-ticket", response_model=AdminStreamTicketResponse)
def create_stream_ticket(
    admin_user: AdminUser = Depends(get_current_admin),
) -> AdminStreamTicketResponse:
    """Credencial de 30s para abrir `GET /admin/orders/stream`.

    Passo separado porque o `EventSource` do navegador nao envia cabecalho:
    o stream so pode ser autenticado pela URL, e o token de 12h nao pode ir
    para la (log de proxy, Referer, historico).
    """
    return AdminAuthService.create_stream_ticket(admin_user)


@router.get(
    "/orders/stream",
    response_class=EventStreamResponse,
    response_model=AdminOrderStreamEvent,
    responses={200: {"description": "Fluxo SSE; cada `data:` e um AdminOrderStreamEvent."}},
)
async def stream_orders(
    ticket: str = Query(..., description="Ticket obtido em POST /admin/orders/stream-ticket"),
    last_event_id: str | None = Header(
        default=None,
        alias="Last-Event-ID",
        description=(
            "Cursor da ultima mensagem recebida. O navegador reenvia sozinho "
            "na reconexao; o servidor repete tudo o que aconteceu depois dele."
        ),
    ),
    db: Session = Depends(get_db),
) -> EventStreamResponse:
    """Pedido novo e mudanca de status em tempo real, sem polling do painel.

    E `async def` para nao prender uma thread do pool durante os minutos em
    que a conexao fica ociosa — e a unica rota do projeto em que isso e uma
    decisao de arquitetura, e nao so a exigencia de um `await` (a de upload
    de imagem e async porque `UploadFile.read()` e assincrono). O
    trabalho de banco de cada poll vai para o threadpool. Ver
    `AdminOrderStreamService` para a escolha de SSE sobre WebSocket e para
    como a reconexao nao perde pedido.
    """
    admin_user = AdminAuthService(db).get_admin_from_stream_ticket(ticket)
    stream = AdminOrderStreamService(build_admin_scope(admin_user))
    return EventStreamResponse(
        stream.iter_events(last_event_id),
        headers={
            # Sem estes dois um proxy com buffer segura os eventos ate
            # encher o buffer, e o "tempo real" chega em lotes de minutos.
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/orders/{order_id}", response_model=OrderDetailResponse)
def get_order_detail(
    order_id: UUID,
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> OrderDetailResponse:
    return AdminOrderService(db).get_order_detail(order_id, scope)


@router.patch("/orders/{order_id}/status", response_model=OrderDetailResponse)
def update_order_status(
    order_id: UUID,
    payload: UpdateOrderStatusRequest,
    scope: AdminScope = Depends(get_admin_scope),
    idempotency_key: str | None = Header(
        default=None,
        alias="Idempotency-Key",
        description=(
            "Reenviar a mesma chave com o mesmo corpo devolve a resposta "
            "original em vez de gravar outra linha no historico de status."
        ),
    ),
    db: Session = Depends(get_db),
) -> OrderDetailResponse:
    return AdminOrderService(db).update_order_status(
        order_id,
        scope,
        payload,
        admin_user=scope.admin_user,
        idempotency_key=normalize_idempotency_key(idempotency_key),
    )
