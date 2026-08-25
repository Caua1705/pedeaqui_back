from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy.orm import Session

from src.api.dependencies.customer_auth import get_optional_current_customer
from src.api.dependencies.database import get_db
from src.api.rate_limit import (
    CANCEL_ORDER_RATE_LIMIT,
    CREATE_ORDER_RATE_LIMIT,
    PUBLIC_ORDER_LOOKUP_RATE_LIMIT,
    REVIEW_ORDER_RATE_LIMIT,
    limiter,
)
from src.models.customer_model import Customer
from src.schemas.order_review_schema import (
    CreateOrderReviewRequest,
    OrderReviewResponse,
)
from src.schemas.order_schema import (
    CreateOrderRequest,
    CreateOrderResponse,
    CustomerCancelOrderRequest,
    OrderDetailResponse,
)
from src.services.customer_order_cancel_service import CustomerOrderCancelService
from src.services.idempotency_service import normalize_idempotency_key
from src.services.order_review_service import OrderReviewService
from src.services.order_service import OrderService


router = APIRouter(prefix="/restaurants", tags=["orders"])

IDEMPOTENCY_KEY_DESCRIPTION = (
    "Identificador unico da tentativa de criar ESTE pedido. Reenviar a mesma "
    "chave com o mesmo corpo devolve a resposta original em vez de criar um "
    "segundo pedido. Gere um UUID por pedido e reutilize-o em todas as "
    "retentativas. Vale por 24h."
)


@router.post("/{restaurant_slug}/orders", response_model=CreateOrderResponse)
@limiter.limit(CREATE_ORDER_RATE_LIMIT)
def create_order(
    request: Request,
    restaurant_slug: str,
    payload: CreateOrderRequest,
    current_customer: Customer | None = Depends(get_optional_current_customer),
    idempotency_key: str | None = Header(
        default=None,
        alias="Idempotency-Key",
        description=IDEMPOTENCY_KEY_DESCRIPTION,
    ),
    db: Session = Depends(get_db),
) -> CreateOrderResponse:
    return OrderService(db).create_order(
        restaurant_slug,
        payload,
        current_customer,
        idempotency_key=normalize_idempotency_key(idempotency_key),
    )


# SUBSTITUI a antiga GET /{slug}/orders/{order_number}?phone=..., removida
# na Fase 2. `order_number` vem de uma sequence global: com o telefone de
# alguem, dava para varrer os numeros vizinhos e ler endereco residencial,
# itens e historico de outras pessoas. O token e sorteado na criacao do
# pedido e entregue so a quem o criou.
@router.get("/{restaurant_slug}/orders/track/{tracking_token}", response_model=OrderDetailResponse)
@limiter.limit(PUBLIC_ORDER_LOOKUP_RATE_LIMIT)
def track_order(
    request: Request,
    restaurant_slug: str,
    tracking_token: str,
    db: Session = Depends(get_db),
) -> OrderDetailResponse:
    return OrderService(db).get_order_by_tracking_token(restaurant_slug, tracking_token)


@router.put(
    "/{restaurant_slug}/orders/track/{tracking_token}/review",
    response_model=OrderReviewResponse,
    responses={
        404: {"description": "Pedido nao encontrado, ou token invalido"},
        409: {"description": "Pedido ainda nao entregue, ou prazo de avaliacao encerrado"},
    },
)
@limiter.limit(REVIEW_ORDER_RATE_LIMIT)
def review_order(
    request: Request,
    restaurant_slug: str,
    tracking_token: str,
    payload: CreateOrderReviewRequest,
    db: Session = Depends(get_db),
) -> OrderReviewResponse:
    """A nota do cliente sobre um pedido entregue.

    **PUT e nao POST**, e a escolha e o contrato: um pedido tem no maximo uma
    avaliacao (`uq_order_reviews_order_id`), e mandar de novo TROCA a que
    estava la em vez de criar uma segunda. Quem apertou uma estrela por engano
    manda de novo; quem tem rede ruim e reenviou nao cria duas notas.

    **Sem login, de proposito.** Pedido de convidado e caso normal, e exigir
    conta aqui cortaria justamente quem mais tem o que dizer. Quem autoriza e
    o `tracking_token` desta URL — o mesmo que abre o acompanhamento do
    pedido, com 256 bits e sem rota de reemissao.

    ## Quando a rota aceita

    - o pedido esta em `completed` (409 nos outros, inclusive `cancelled` e
      `rejected`: nao houve entrega para avaliar);
    - dentro de 14 dias da entrega (409 depois disso).

    ## Os campos

    - `rating`: 1 a 5, obrigatorio. UMA nota geral — ver
      `CreateOrderReviewRequest` para por que nao ha nota separada de comida,
      entrega e embalagem.
    - `problem_tag`: opcional, e **so aceito com `rating` ate 3**. Mandar com
      nota 4 ou 5 responde 422.
    - `comment`: opcional, ate 500 caracteres.
    """
    return OrderReviewService(db).submit(restaurant_slug, tracking_token, payload)


@router.post(
    "/{restaurant_slug}/orders/track/{tracking_token}/cancel",
    response_model=OrderDetailResponse,
    responses={
        404: {"description": "Pedido nao encontrado, ou token invalido"},
        409: {
            "description": (
                "O pedido ja saiu da janela em que o cliente cancela sozinho "
                "(a partir de `preparing`), ou ja e um estado final"
            )
        },
    },
)
@limiter.limit(CANCEL_ORDER_RATE_LIMIT)
def cancel_order_by_customer(
    request: Request,
    restaurant_slug: str,
    tracking_token: str,
    payload: CustomerCancelOrderRequest | None = None,
    db: Session = Depends(get_db),
) -> OrderDetailResponse:
    """O cliente desistindo do proprio pedido, antes do preparo.

    **Ate onde ela vai: `pending` e `accepted`.** Antes do preparo ninguem
    gastou nada e desistir e barato para os dois lados. A partir de
    `preparing` o insumo ja saiu do estoque, e quem decide quem come o
    prejuizo passa a ser o lojista — a rota responde **409** e o app manda o
    cliente falar com o restaurante.

    **Sem login, de proposito.** Pedido de convidado e caso normal, e exigir
    conta aqui deixaria justamente o convidado sem saida. Quem autoriza e o
    `tracking_token` desta URL — o mesmo do acompanhamento e da avaliacao,
    com 256 bits e sem rota de reemissao.

    **O corpo e opcional, e o motivo dentro dele tambem.** Exigir
    justificativa de quem desiste de um pedido que nem comecou produz um
    campo preenchido com "a"; quem cancelou ja fica gravado no historico.

    ## O que ela faz junto, e que o app nao precisa pedir

    E a MESMA escrita do cancelamento pelo painel
    (`OrderStatusChangeService`), entao o cupom volta a ficar disponivel, o
    cashback resgatado volta para o saldo e **o pagamento online e
    estornado** — um pix pago e cancelado em seguida volta sem ninguem ligar
    para o restaurante. O estorno acontece depois do commit e nao pode
    derrubar esta resposta: se o gateway estiver fora do ar, o cancelamento
    vale e uma varredura devolve o dinheiro depois.

    **Sem `Idempotency-Key`**, e ela nao faz falta: o segundo clique chega
    com o pedido ja em `cancelled` e leva 409 da maquina de estados.
    """
    return CustomerOrderCancelService(db).cancel(restaurant_slug, tracking_token, payload)
