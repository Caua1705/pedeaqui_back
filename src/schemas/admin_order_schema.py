from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


# Teto do motivo do cancelamento. Cabe uma frase ("cliente desistiu",
# "acabou a costela"), que e o que o suporte precisa ler depois — nao um
# relatorio.
MAX_CANCELLATION_REASON_LENGTH = 300
# Piso para barrar o motivo simbolico. Um "x" digitado so para o botao
# liberar responde a exigencia sem responder a pergunta.
MIN_CANCELLATION_REASON_LENGTH = 3


class AdminOrderListItem(BaseModel):
    id: UUID
    order_number: int
    # A filial vem no item porque o painel de quem enxerga o restaurante
    # inteiro mistura pedidos de varias unidades na mesma lista, e "sair
    # entrega" so faz sentido sabendo de qual cozinha.
    branch_id: UUID
    customer_name_snapshot: str
    customer_phone_snapshot: str
    order_type: str
    status: str
    # A cozinha precisa ver o pagamento junto do status: um pedido em
    # `pending` com `payment_status='pending'` esta esperando o gateway, nao
    # esperando o lojista.
    payment_method: str | None = None
    payment_status: str
    total: float
    created_at: datetime | None = None


class AdminOrderListResponse(BaseModel):
    """Pagina de pedidos com o total do filtro.

    Passou a ser um envelope em vez de uma lista crua porque sem `total` o
    painel nao consegue desenhar a paginacao — com 50 itens em maos nao da
    para saber se existe pagina seguinte.
    """

    items: list[AdminOrderListItem]
    total: int
    limit: int
    offset: int


class AdminOrderStatusCount(BaseModel):
    status: str
    count: int


class AdminOrderStatusCountsResponse(BaseModel):
    """Badges da tela de pedidos.

    Traz TODOS os status de ORDER_STATUSES, inclusive os zerados: sem isso o
    badge de "pendentes" sumiria da tela quando chegasse a zero, que e
    justamente quando o lojista quer ver o zero.
    """

    counts: list[AdminOrderStatusCount]
    total: int


class UpdateOrderStatusRequest(BaseModel):
    """Corpo do PATCH de status.

    `changed_by` foi REMOVIDO do contrato: quem mudou passou a sair do token
    do lojista (AdminOrderService._admin_signature). Era texto livre vindo do
    cliente, entao o historico do pedido registrava qualquer autor que o
    painel quisesse escrever. Clientes antigos que ainda mandam o campo nao
    quebram — o Pydantic ignora chave desconhecida —, ele so nao tem mais
    efeito.
    """

    status: str
    note: str | None = None


class CancelOrderRequest(BaseModel):
    """Corpo do cancelamento pelo painel.

    O motivo e OBRIGATORIO aqui, e o `note` do PATCH de status continua
    opcional: mudar para `preparing` nao precisa de justificativa, cancelar
    precisa. Cancelamento e a unica transicao que o cliente questiona
    depois — ele ligou, esperou, e o pedido sumiu — e sem motivo gravado o
    historico so consegue dizer que alguem cancelou as 20h14.

    Nao ha campo de status: a rota so cancela. Fosse `status` do corpo, ela
    seria o PATCH de status com nome diferente e a obrigatoriedade do motivo
    viraria um `if` por status.
    """

    reason: str = Field(max_length=MAX_CANCELLATION_REASON_LENGTH)

    @field_validator("reason")
    @classmethod
    def strip_reason(cls, value: str) -> str:
        # O corte vem antes da medida de proposito: `min_length` do Field
        # roda sobre o texto cru e deixaria passar tres espacos.
        reason = value.strip()
        if len(reason) < MIN_CANCELLATION_REASON_LENGTH:
            raise ValueError(
                f"O motivo do cancelamento precisa de ao menos "
                f"{MIN_CANCELLATION_REASON_LENGTH} caracteres"
            )
        return reason


class AdminStreamTicketResponse(BaseModel):
    """Credencial de uso unico para abrir o SSE.

    O EventSource do navegador nao aceita cabecalho — nao da para mandar o
    `Authorization: Bearer` no stream. A saida seria passar o token de 12h
    na querystring, mas ai ele acaba no log de acesso do Traefik, no
    Referer e no historico do navegador. O ticket resolve isso: vale
    poucos segundos, so serve para abrir stream e e obtido por POST
    autenticado normalmente.
    """

    ticket: str
    expires_in_seconds: int


AdminOrderStreamEventType = Literal[
    "order.created",
    "order.status_changed",
    "sync_required",
]


class AdminOrderStreamEvent(BaseModel):
    """Payload de cada `data:` do stream.

    `event_key` e estavel para o mesmo fato: o stream entrega AO MENOS uma
    vez (a janela de sobreposicao do cursor pode repetir eventos na
    reconexao), entao o painel precisa descartar o que ja aplicou. Descartar
    por `occurred_at` nao serve — dois pedidos podem nascer no mesmo
    instante.

    `sync_required` nao traz pedido: e o aviso de que o cliente ficou
    offline tempo demais para o replay e precisa recarregar a lista.
    """

    type: AdminOrderStreamEventType
    event_key: str
    occurred_at: datetime
    order: AdminOrderListItem | None = None
    note: str | None = None
