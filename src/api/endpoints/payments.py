"""Rotas de pagamento.

Duas: o cliente pede a cobranca do pedido dele, e o gateway avisa quando o
dinheiro entrou. A regra toda esta em PaymentService; o que a implementacao
do gateway precisa esta em src/integrations/payment_gateway.py.
"""

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from src.api.dependencies.database import get_db
from src.api.rate_limit import START_PAYMENT_RATE_LIMIT, limiter
from src.schemas.payment_schema import (
    PaymentErrorDetail,
    PaymentWebhookResponse,
    StartPaymentResponse,
)
from src.services.payment_service import PaymentService


router = APIRouter(tags=["payments"])

# O erro desta rota tem corpo proprio: o frontend precisa distinguir "tente
# de novo" de "nao adianta insistir" — ver PaymentErrorDetail.
_PAYMENT_ERROR_RESPONSES = {
    502: {"model": PaymentErrorDetail, "description": "Cobranca recusada pelo provedor"},
    503: {"model": PaymentErrorDetail, "description": "Pagamento indisponivel no momento"},
}


@router.post(
    "/restaurants/{restaurant_slug}/orders/{tracking_token}/payment",
    response_model=StartPaymentResponse,
    responses=_PAYMENT_ERROR_RESPONSES,
)
@limiter.limit(START_PAYMENT_RATE_LIMIT)
def start_payment(
    request: Request,
    restaurant_slug: str,
    tracking_token: str,
    db: Session = Depends(get_db),
) -> StartPaymentResponse:
    """Cria a cobranca do pedido no gateway.

    Autorizacao pelo token de acompanhamento, o mesmo que a consulta
    publica usa: quem tem o token e quem fez o pedido. Fica fora de
    create_order de proposito — a chamada ao gateway nao pode acontecer com
    a transacao do pedido aberta.

    Falha ao criar a cobranca responde 502 ou 503 com `detail` no formato
    de `PaymentErrorDetail` — um objeto, nao a string de sempre: sem o
    `retryable` nao ha como o frontend escolher entre oferecer "tentar de
    novo" e mandar o cliente falar com o restaurante.
    """
    return PaymentService(db).start_online_payment(restaurant_slug, tracking_token)


@router.post("/payments/webhooks/{provider}", response_model=PaymentWebhookResponse)
async def receive_payment_webhook(
    provider: str,
    request: Request,
    db: Session = Depends(get_db),
) -> PaymentWebhookResponse:
    """Notificacao do gateway.

    `async def` aqui e para conseguir o corpo CRU: a assinatura e calculada
    sobre os bytes exatos que o gateway enviou, e reserializar o JSON
    (espacos, ordem das chaves) quebraria a conferencia. Como o resto do
    projeto e sincrono, o service roda em threadpool para nao segurar o
    event loop enquanto fala com o banco.

    Sem rate limit: o gateway reenvia em rajada quando volta de uma queda, e
    devolver 429 para ele significa perder confirmacao de pagamento. A
    protecao aqui e a assinatura.
    """
    raw_body = await request.body()
    headers = dict(request.headers)
    result = await run_in_threadpool(
        PaymentService(db).handle_webhook,
        provider,
        raw_body,
        headers,
    )
    return PaymentWebhookResponse(**result)
