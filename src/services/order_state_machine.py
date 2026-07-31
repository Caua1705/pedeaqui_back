"""Transicoes validas de um pedido.

Um pedido carrega DUAS maquinas de estado ao mesmo tempo:

- `orders.status` — o ciclo operacional (lojista, cozinha, entregador).
- `orders.payment_status` — o dinheiro (ver PAYMENT_STATUSES em
  src/core/constants.py).

Elas nao sao independentes. A regra que amarra as duas esta em
`ensure_payment_allows_order_status`: um pedido com pagamento ONLINE so
entra no fluxo operacional (e portanto so aparece para a cozinha) depois
que o gateway confirmou o recebimento. Pedido pago na entrega entra direto,
porque nesse caso nao ha nada a confirmar antes.

Por que um grafo explicito e nao um `if` espalhado pelos services: antes
disso `update_order_status` so conferia se o status EXISTIA. Dava para ir de
`cancelled` para `completed`, o que faturava um pedido cancelado cujo cupom
ja tinha sido estornado — o cliente levava o desconto duas vezes. Estado
terminal e terminal aqui, em um lugar so.
"""

from fastapi import HTTPException, status as http_status


# Para onde cada status pode ir. Chave sem destinos = estado TERMINAL.
#
# Nao ha atalho: `accepted` nao pula direto para `ready`, e `preparing` nao
# volta para `accepted`. Se o lojista precisar de um atalho, ele se abre
# acrescentando um destino aqui e mais nada.
ORDER_STATUS_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "pending": ("accepted", "rejected", "cancelled"),
    "accepted": ("preparing", "cancelled"),
    "preparing": ("ready", "cancelled"),
    "ready": ("out_for_delivery", "completed", "cancelled"),
    "out_for_delivery": ("completed", "cancelled"),
    "completed": (),
    "cancelled": (),
    "rejected": (),
}

# Estados dos quais nao se sai. `rejected` entra na lista junto com os dois
# exigidos: recusar um pedido e uma decisao final, e "desrecusar" traz o
# mesmo problema de cupom estornado que o `cancelled -> completed` trazia.
TERMINAL_ORDER_STATUSES = tuple(
    status for status, destinations in ORDER_STATUS_TRANSITIONS.items() if not destinations
)

# A partir daqui o pedido esta na mao do restaurante: aparece na cozinha,
# consome insumo, sai para entrega. E a fronteira que o pagamento online
# precisa ter cruzado.
KITCHEN_ORDER_STATUSES = ("accepted", "preparing", "ready", "out_for_delivery", "completed")

# Estados de pagamento que autorizam o pedido a entrar na cozinha.
# `on_delivery` entra porque o dinheiro so existe no fim mesmo.
PAYMENT_STATUSES_THAT_RELEASE_ORDER = ("paid", "on_delivery")

PAYMENT_STATUS_TRANSITIONS: dict[str, tuple[str, ...]] = {
    # Pago na entrega nunca muda: o pedido nasce e morre assim.
    "on_delivery": (),
    "pending": ("paid", "failed"),
    "paid": ("refunded",),
    # Cartao negado ou pix expirado nao mata o pedido: o cliente pode gerar
    # outro pagamento para o MESMO pedido, e ai voltamos para `pending`.
    "failed": ("pending",),
    "refunded": (),
}

# Prefixo usado ao registrar mudanca de PAGAMENTO em order_status_history.
# A tabela guarda as duas maquinas, e sem o prefixo "paid" (pagamento) se
# confundiria com um status operacional.
PAYMENT_HISTORY_PREFIX = "payment:"


def ensure_order_transition_allowed(
    current_status: str,
    new_status: str,
    order_type: str,
) -> None:
    """Recusa mudanca de status que o grafo nao permite."""
    if current_status == new_status:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=f"O pedido ja esta em '{new_status}'.",
        )
    if current_status in TERMINAL_ORDER_STATUSES:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=f"Pedido em '{current_status}' e um estado final e nao muda mais.",
        )
    allowed = ORDER_STATUS_TRANSITIONS.get(current_status)
    if allowed is None:
        # Status gravado no banco que nao existe no grafo. Nao inventamos
        # uma transicao: e dado corrompido e precisa aparecer.
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=f"Status atual desconhecido: '{current_status}'.",
        )
    if new_status not in allowed:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=f"Nao e possivel ir de '{current_status}' para '{new_status}'.",
        )
    if new_status == "out_for_delivery" and order_type != "delivery":
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="Pedido de retirada nao sai para entrega.",
        )


def ensure_payment_allows_order_status(new_status: str, payment_status: str) -> None:
    """A regra central da Fase 2.

    Pedido com pagamento online so passa de `pending` depois de pago. Sem
    isto, bastava o lojista clicar em "aceitar" para o pedido ir para a
    cozinha com o pix ainda em aberto — e o cliente podia simplesmente nao
    pagar.

    Cancelar e recusar continuam liberados em qualquer estado de pagamento:
    sao justamente a saida para um pagamento que nunca chegou.
    """
    if new_status not in KITCHEN_ORDER_STATUSES:
        return
    if payment_status in PAYMENT_STATUSES_THAT_RELEASE_ORDER:
        return
    raise HTTPException(
        status_code=http_status.HTTP_409_CONFLICT,
        detail=(
            "Pagamento online ainda nao confirmado "
            f"(payment_status='{payment_status}'). O pedido nao pode ser aceito."
        ),
    )


def ensure_payment_transition_allowed(current_status: str, new_status: str) -> None:
    """Recusa mudanca de estado de pagamento que o grafo nao permite."""
    if current_status == new_status:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=f"O pagamento ja esta em '{new_status}'.",
        )
    allowed = PAYMENT_STATUS_TRANSITIONS.get(current_status)
    if allowed is None:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=f"Estado de pagamento desconhecido: '{current_status}'.",
        )
    if new_status not in allowed:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=f"Nao e possivel ir de '{current_status}' para '{new_status}'.",
        )


def payment_history_status(payment_status: str) -> str:
    """Como um evento de pagamento aparece em order_status_history."""
    return f"{PAYMENT_HISTORY_PREFIX}{payment_status}"
