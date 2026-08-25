from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query
from sqlalchemy.orm import Session
from starlette.responses import StreamingResponse

from src.api.dependencies.admin_auth import get_current_admin
from src.api.dependencies.admin_scope import (
    GERENCIA,
    PESSOAS,
    PESSOAS_E_AGENTE,
    AdminScope,
    build_admin_scope,
    ensure_role,
    exigir_papel,
    get_admin_scope,
)
from src.api.dependencies.database import get_db
from src.models.admin_user_model import AdminUser
from src.schemas.admin_order_schema import (
    CancelOrderErrorResponse,
    AdminOrderListResponse,
    AdminOrderStatusCountsResponse,
    AdminOrderStreamEvent,
    AdminStreamTicketResponse,
    CancelOrderRequest,
    UpdateOrderStatusRequest,
)
from src.schemas.admin_printing_schema import OrderPrintJobsResponse
from src.schemas.order_schema import OrderDetailResponse
from src.services.admin_auth_service import AdminAuthService
from src.services.admin_order_service import AdminOrderService
from src.services.admin_order_stream_service import AdminOrderStreamService
from src.services.admin_printing_service import AdminPrintingService
from src.services.idempotency_service import normalize_idempotency_key


# O escopo destas rotas vem SEMPRE do token do lojista: restaurante e, quando
# o papel for manager/attendant com filial fixada, tambem a filial. Nenhuma
# rota aqui aceita restaurante como parametro — a listagem aceitava um slug
# na URL e foi corrigida, porque uma rota que RECEBE restaurante e uma rota
# que pode esquecer de conferi-lo.
#
# Este arquivo tem as unicas tres rotas do painel que o AGENTE de impressao
# alcanca: o ticket, o stream e as vias de um pedido. Sao o minimo para uma
# comanda sair do servidor e chegar na bobina.
#
# **A listagem de pedidos NAO esta entre elas, e e o ponto da frente
# inteira.** `GET /admin/orders` devolve nome, telefone e endereco de todo
# cliente que pediu na loja; a senha do agente esta em texto puro no
# `config.ini` da maquina do balcao. O agente nunca precisou dessa lista — ele
# so reage ao evento do stream e busca as vias daquele pedido.
router = APIRouter(prefix="/admin", tags=["admin orders"])


class EventStreamResponse(StreamingResponse):
    """StreamingResponse com media_type fixo.

    Unica razao de existir: o FastAPI monta o OpenAPI usando o `media_type`
    da classe de resposta. Sem ela o schema do evento sairia publicado como
    `application/json`, e o cliente gerado para o frontend descreveria
    errado o unico endpoint que nao devolve JSON.

    **NAO acrescente `; charset=utf-8` aqui.** A tentacao e real: a regra
    default do HTTP para `text/*` sem charset e ISO-8859-1, e cliente que a
    aplica (o `requests`, ate hoje, em `get_encoding_from_headers`) leria
    todo acento do stream como mojibake.

    So que o Starlette JA acrescenta o charset sozinho: `init_headers` poe
    `; charset=utf-8` em todo media_type que comece com `text/` e nao traga
    um. O cabecalho que sai na rede e identico com ou sem esta declaracao —
    conferido.

    O que muda e o OpenAPI, que usa este valor como CHAVE do `content`: a
    rota passaria a ser publicada sob `text/event-stream; charset=utf-8` em
    vez de `text/event-stream`. O painel consome o `/openapi.json`, entao
    isso e mudanca de contrato — custo real, beneficio zero.
    """

    media_type = "text/event-stream"


@router.get(
    "/orders",
    response_model=AdminOrderListResponse,
    dependencies=[Depends(exigir_papel(PESSOAS))],
)
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


@router.get(
    "/orders/status-counts",
    response_model=AdminOrderStatusCountsResponse,
    dependencies=[Depends(exigir_papel(PESSOAS))],
)
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


@router.post(
    "/orders/stream-ticket",
    response_model=AdminStreamTicketResponse,
    dependencies=[Depends(exigir_papel(PESSOAS_E_AGENTE))],
)
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
    # `ensure_role` no corpo, e nao `dependencies=[...]` como nas outras: esta
    # rota nao autentica por `Depends(get_current_admin)` — ela le um ticket
    # da querystring, porque o EventSource do navegador nao manda cabecalho.
    # A dependencia de papel exigiria o Bearer que aqui nao existe e recusaria
    # o painel inteiro com 401.
    ensure_role(admin_user, PESSOAS_E_AGENTE)
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


@router.get(
    "/orders/{order_id}",
    response_model=OrderDetailResponse,
    dependencies=[Depends(exigir_papel(PESSOAS))],
)
def get_order_detail(
    order_id: UUID,
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> OrderDetailResponse:
    return AdminOrderService(db).get_order_detail(order_id, scope)


@router.get(
    "/orders/{order_id}/print-jobs",
    response_model=OrderPrintJobsResponse,
    dependencies=[Depends(exigir_papel(PESSOAS_E_AGENTE))],
)
def get_order_print_jobs(
    order_id: UUID,
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> OrderPrintJobsResponse:
    """As vias deste pedido, ja formatadas em texto de largura fixa.

    Mesmo token e mesmo escopo de filial do resto de /admin/orders — quem
    nao pode ler o pedido nao pode imprimi-lo, e a comanda carrega nome,
    telefone e endereco do cliente.

    O agente de impressao da loja e burro de proposito: ele le `content`,
    seleciona `font_size` e manda para a impressora. Nao alinha, nao quebra
    linha e nao decide o que entra em cada via — isso tudo vive em
    `src/services/print_layout.py`, num lugar so, testavel, e uma correcao
    de layout vira um deploy em vez de uma visita a cada loja.

    Pedido com pagamento online ainda nao confirmado devolve SO a via do
    cliente: comanda de producao e ordem de preparo, e a regra do
    "aguardando pagamento, nao preparar" nao pode valer apenas para quem
    esta olhando a tela.

    A rota nao marca nada como impresso. Reimprimir e a operacao mais comum
    do balcao (papel picotou, comanda molhou), e ela precisa ser um simples
    GET repetido.
    """
    return AdminPrintingService(db).build_print_jobs(order_id, scope)


# 428 e nao 409, e a escolha e o contrato. Os 409 destas rotas sao conflitos
# de estado de verdade ("pedido ja entregue nao muda mais") e saem com
# `detail` de texto; o 428 nao e erro nenhum — e o backend pedindo uma
# precondicao que o painel satisfaz na hora, com um corpo TIPADO que diz qual
# dialogo abrir. Sobrepor os dois no mesmo codigo obrigaria o painel a
# distinguir pelo texto da mensagem, e publicar um `model` de 409 que so vale
# para metade dos 409 da rota seria pior ainda (armadilha 16).
#
# Declarado nas DUAS rotas porque o PATCH de status aceita
# `status="cancelled"`: so na rota de cancelamento, ficaria de pe exatamente a
# porta que a confirmacao existe para fechar.
_CONFIRMATION_REQUIRED_RESPONSE = {
    428: {
        "model": CancelOrderErrorResponse,
        "description": (
            "Cancelamento de pedido ja em producao sem `confirm_prepared_order`. "
            "Nao e erro: o painel abre o dialogo de confirmacao e reenvia."
        ),
    }
}

@router.patch(
    "/orders/{order_id}/status",
    response_model=OrderDetailResponse,
    responses=_CONFIRMATION_REQUIRED_RESPONSE,
    # PESSOAS: aceitar e recusar pedido E o trabalho do balcao. O agente de
    # impressao fica de fora — ele reage a mudanca de status, nao a provoca, e
    # uma credencial de maquina que aceita pedido aceitaria tambem o pedido
    # que o lojista ainda estava olhando.
    dependencies=[Depends(exigir_papel(PESSOAS))],
)
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


@router.patch(
    "/orders/{order_id}/cancel",
    response_model=OrderDetailResponse,
    responses=_CONFIRMATION_REQUIRED_RESPONSE,
    # GERENCIA, e o atendente perde esta: cancelar pedido PAGO nao estorna
    # nada (armadilha 25) e o log e o unico rastro de dinheiro de cliente
    # parado. Quem esta no balcao e precisa nao atender um pedido tem
    # `rejected` na maquina de estados, que e a saida certa para o pedido que
    # nao vai ser feito.
    dependencies=[Depends(exigir_papel(GERENCIA))],
)
def cancel_order(
    order_id: UUID,
    payload: CancelOrderRequest,
    scope: AdminScope = Depends(get_admin_scope),
    idempotency_key: str | None = Header(
        default=None,
        alias="Idempotency-Key",
        description=(
            "Reenviar a mesma chave com o mesmo motivo devolve a resposta "
            "original em vez de gravar outra linha no historico."
        ),
    ),
    db: Session = Depends(get_db),
) -> OrderDetailResponse:
    """Cancela o pedido registrando o motivo (obrigatorio).

    O motivo vai para `order_status_history.note`, junto do lojista que
    cancelou — e o unico lugar onde o suporte consegue reconstruir depois
    por que o pedido do cliente sumiu.

    Cancelar continua sujeito a mesma maquina de estados do PATCH de
    status: pedido ja entregue ou ja cancelado responde 409.
    """
    return AdminOrderService(db).cancel_order(
        order_id,
        scope,
        payload,
        admin_user=scope.admin_user,
        idempotency_key=normalize_idempotency_key(idempotency_key),
    )
