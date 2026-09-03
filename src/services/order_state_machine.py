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

# Estados em que a comida JA CUSTOU DINHEIRO ao restaurante. Cancelar aqui
# nao e desfazer nada: o insumo saiu do estoque e alguem vai comer o
# prejuizo. Por isso o cancelamento nestes estados exige confirmacao
# explicita de quem clica — ver `cancellation_needs_confirmation`.
#
# `preparing` ENTRA, e a escolha e deliberada: "a comida ja foi feita" comeca
# quando ela comeca a ser feita, nao quando fica pronta. Em `accepted` o
# lojista so aceitou, e o pedido pode nao ter chegado a praca.
PREPARED_ORDER_STATUSES = ("preparing", "ready", "out_for_delivery")

# Ate onde o CLIENTE cancela sozinho. A regra e a mesma do outro lado da
# moeda: antes do preparo ninguem gastou nada, e desistir e barato para os
# dois. Depois disso o cancelamento passa a ser uma conversa — o cliente liga
# e o lojista decide, com a confirmacao que PREPARED_ORDER_STATUSES exige.
#
# `pending` cobre o pedido esperando o aceite E o pix ainda nao pago; nos dois
# o cliente desiste sem custo para ninguem.
CUSTOMER_CANCELLABLE_STATUSES = ("pending", "accepted")

# Ate onde o ENTREGADOR anda no grafo: de pronto para na rua, e de na rua
# para entregue. Sao arestas que JA existem em ORDER_STATUS_TRANSITIONS —
# esta tabela nao abre caminho nenhum, so diz quais dos existentes a porta
# do entregador pode usar. Nem cancelar, nem voltar, nem aceitar.
#
# Dicionario e nao lista de pares porque a tela do motoboy pergunta "deste
# estado, o que EU posso fazer?" (`can_leave`, `can_deliver`), e a resposta
# e um destino so por estado.
COURIER_TRANSITIONS: dict[str, str] = {
    "ready": "out_for_delivery",
    "out_for_delivery": "completed",
}

# Estados de pagamento que autorizam o pedido a entrar na cozinha.
# `on_delivery` entra porque o dinheiro so existe no fim mesmo.
#
# `in_review` fica de FORA junto com `pending`: analise do antifraude nao e
# pagamento, e um pedido preparado durante a analise vira prejuizo do lojista
# quando ela recusa. A diferenca entre os dois e o que o lojista LE na tela,
# nao o que ele pode fazer.
PAYMENT_STATUSES_THAT_RELEASE_ORDER = ("paid", "on_delivery")

PAYMENT_STATUS_TRANSITIONS: dict[str, tuple[str, ...]] = {
    # Pago na entrega nunca muda: o pedido nasce e morre assim.
    "on_delivery": (),
    "pending": ("in_review", "paid", "failed"),
    # Analise do antifraude (so cartao). Sai para aprovado ou recusado, nunca
    # de volta para `pending`: o cliente nao tem o que refazer enquanto o
    # gateway analisa, e uma aresta de volta deixaria a mesma cobranca ser
    # substituida no meio da analise.
    "in_review": ("paid", "failed"),
    "paid": ("refunded",),
    # Cartao negado ou pix expirado nao mata o pedido: o cliente pode gerar
    # outro pagamento para o MESMO pedido.
    #
    # Os tres destinos existem porque a nova tentativa tem desfechos
    # diferentes por metodo: no pix ela nasce `pending` e o webhook confirma
    # depois; no CARTAO o proprio POST ja responde, entao a tentativa
    # seguinte pode ir direto para `paid` ou `in_review` sem nunca passar por
    # `pending`. Sem essas duas arestas, um cartao aprovado na segunda
    # tentativa seria recusado pela propria maquina de estados.
    "failed": ("pending", "in_review", "paid"),
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
            detail="Pedido de retirada não sai para entrega.",
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


def cancellation_needs_confirmation(current_status: str, new_status: str) -> bool:
    """Este cancelamento precisa de um segundo clique de quem o pediu?

    Predicado e nao `ensure_*`, ao contrario dos vizinhos deste arquivo, e o
    motivo e o formato da resposta: os outros recusam com 409 e uma frase, e
    este recusa com um corpo TIPADO que o painel le para decidir abrir o
    dialogo de confirmacao (ver AdminOrderService._ensure_cancellation_confirmed).
    Deixar a forma HTTP fora daqui e o que mantem este arquivo sem importar
    schema nenhum.

    So `cancelled` passa por aqui. `rejected` nao: o grafo so o alcanca a
    partir de `pending`, onde nada foi preparado ainda.
    """
    if new_status != "cancelled":
        return False
    return current_status in PREPARED_ORDER_STATUSES


def ensure_customer_can_cancel(current_status: str) -> None:
    """O cliente so cancela sozinho antes de a comida comecar a ser feita.

    Depois disso o pedido ja custou dinheiro ao restaurante, e quem decide
    quem come o prejuizo e o lojista — que tem a rota do painel, com motivo
    obrigatorio e confirmacao explicita. Deixar o cliente cancelar um pedido
    em preparo daria a ele o poder de gerar prejuizo com um toque, sem
    ninguem do outro lado saber por que.

    A recusa cita o status para o app conseguir escrever a frase certa: "seu
    pedido ja esta sendo preparado, fale com o restaurante" e uma coisa,
    "este pedido ja foi entregue" e outra.
    """
    if current_status in CUSTOMER_CANCELLABLE_STATUSES:
        return
    raise HTTPException(
        status_code=http_status.HTTP_409_CONFLICT,
        detail=(
            f"Pedido em '{current_status}' nao pode mais ser cancelado por voce. "
            "Fale com o restaurante."
        ),
    )


def ensure_courier_can_set(current_status: str, new_status: str) -> None:
    """A regra da PORTA do entregador: so `ready -> out_for_delivery` e
    `out_for_delivery -> completed`.

    Mora aqui, e nao no service da porta, pelo mesmo motivo de
    `ensure_customer_can_cancel`: e uma regra sobre o grafo, e o grafo e um
    arquivo so. O writer continua sem saber quem chamou — ele so confere a
    transicao geral; esta funcao roda ANTES, na porta.

    A recusa cita o status atual para o app escrever a frase certa: "ainda
    esta em preparo" e uma coisa, "ja saiu" e outra.
    """
    if COURIER_TRANSITIONS.get(current_status) == new_status:
        return
    raise HTTPException(
        status_code=http_status.HTTP_409_CONFLICT,
        detail=(
            f"O entregador nao pode levar o pedido de '{current_status}' para "
            f"'{new_status}'."
        ),
    )


def payment_transition_is_allowed(current_status: str, new_status: str) -> bool:
    """A mesma regra de `ensure_payment_transition_allowed`, como pergunta.

    Existe porque o estorno automatico (`PaymentRefundService`) precisa
    DECIDIR sobre a transicao, e nao recusar uma requisicao: ele roda depois
    de o cancelamento ja estar gravado, onde levantar HTTPException nao
    chega a lugar nenhum util. As duas leem o mesmo grafo; o que muda e o
    que acontece quando a resposta e nao.
    """
    return new_status in PAYMENT_STATUS_TRANSITIONS.get(current_status, ())


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
