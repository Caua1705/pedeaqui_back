"""Rotas de pagamento.

Duas: o cliente pede a cobranca do pedido dele, e o gateway avisa quando o
dinheiro entrou. A regra toda esta em PaymentService; o que a implementacao
do gateway precisa esta em src/integrations/payment_gateway.py.
"""

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from src.api.dependencies.customer_auth import get_optional_current_customer
from src.api.dependencies.database import get_db
from src.api.rate_limit import START_PAYMENT_RATE_LIMIT, limiter
from src.models.customer_model import Customer
from src.schemas.payment_schema import (
    PaymentConfigResponse,
    PaymentErrorResponse,
    PaymentWebhookResponse,
    StartPaymentRequest,
    StartPaymentResponse,
)
from src.services.payment_service import PaymentService


router = APIRouter(tags=["payments"])

# O erro desta rota tem corpo proprio: o frontend precisa distinguir "tente
# de novo" de "nao adianta insistir" — ver PaymentErrorDetail.
#
# O model e PaymentErrorResponse (com o envelope `detail`) e nao
# PaymentErrorDetail: HTTPException entrega {"detail": {...}}, e anunciar o
# detail na raiz faria o frontend escrever o parser contra um formato que a
# rota nunca devolve.
_PAYMENT_ERROR_RESPONSES = {
    400: {"model": PaymentErrorResponse, "description": "Cobranca de cartao sem o token do navegador"},
    401: {"model": PaymentErrorResponse, "description": "Cartao exige cliente autenticado"},
    502: {"model": PaymentErrorResponse, "description": "Cobranca recusada pelo provedor"},
    503: {"model": PaymentErrorResponse, "description": "Pagamento indisponivel no momento"},
}


@router.get(
    "/restaurants/{restaurant_slug}/payment-config",
    response_model=PaymentConfigResponse,
)
def get_payment_config(
    restaurant_slug: str,
    db: Session = Depends(get_db),
) -> PaymentConfigResponse:
    """O que o navegador precisa para tokenizar um cartao.

    Publica, como o cardapio, e sem segredo nenhum: a `public_key` e o unico
    dado do gateway que o proprio Mercado Pago manda expor no frontend. O
    `access_token` e o `webhook_secret` do mesmo restaurante sao cifrados em
    repouso e nao passam por esta rota.

    Sem rate limit proprio: e uma leitura barata e sem efeito, chamada uma vez
    por abertura da tela de pagamento.
    """
    return PaymentService(db).get_payment_config(restaurant_slug)


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
    payload: StartPaymentRequest | None = None,
    db: Session = Depends(get_db),
    current_customer: Customer | None = Depends(get_optional_current_customer),
) -> StartPaymentResponse:
    """Cria a cobranca do pedido no gateway.

    Autorizacao pelo token de acompanhamento, o mesmo que a consulta
    publica usa: quem tem o token e quem fez o pedido. Fica fora de
    create_order de proposito — a chamada ao gateway nao pode acontecer com
    a transacao do pedido aberta.

    **O login e OPCIONAL na rota e obrigatorio para CARTAO**, e a diferenca e
    proposital: o token de acompanhamento continua sendo a autorizacao (pix
    de convidado tem que seguir funcionando sem conta), mas a cobranca de
    cartao precisa de um e-mail de pagador de verdade para a analise
    antifraude do gateway — e quem recusa e o service, com
    `login_required`, nao esta rota.

    `payload` e opcional pelo mesmo motivo: pix continua sendo um POST sem
    corpo, exatamente como antes de o cartao existir.

    Falha ao criar a cobranca responde 502 ou 503 com `detail` no formato
    de `PaymentErrorDetail` — um objeto, nao a string de sempre: sem o
    `retryable` nao ha como o frontend escolher entre oferecer "tentar de
    novo" e mandar o cliente falar com o restaurante.
    """
    return PaymentService(db).start_online_payment(
        restaurant_slug,
        tracking_token,
        payload,
        current_customer,
    )


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
