"""Ponto de extensao do gateway de pagamento.

===========================================================================
E AQUI QUE O MERCADO PAGO ENTRA. Sao TRES funcoes, e nada fora deste
arquivo precisa mudar quando as credenciais chegarem:

  1. create_payment          — cria a cobranca e devolve o id do gateway
  2. verify_webhook_signature — confere que a notificacao veio do gateway
  3. parse_webhook_event      — traduz o corpo do gateway para o daqui

Cada uma tem um bloco `if provider == "mercadopago"` com o passo a passo
do que escrever. Depois de escrever os tres, mude PAYMENT_PROVIDER para
"mercadopago" no .env e pronto.
===========================================================================

Por que uma camada propria e nao chamar o SDK do Mercado Pago direto dos
services: o resto do sistema fala "paid", "failed", "refunded"
(PAYMENT_STATUSES). O gateway fala "approved", "rejected", "in_process",
"charged_back". A traducao mora aqui, em um lugar so, e trocar de gateway
um dia nao vira uma caca ao "approved" espalhado pelo codigo.

O provider "sandbox" e uma implementacao real e completa, sem chamada
externa: ele existe para o fluxo inteiro (criar cobranca, receber webhook
assinado, confirmar pagamento) ser testavel e demonstravel antes de
existir credencial. Nao e mock de teste — roda em desenvolvimento.
"""

import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass
from decimal import Decimal

from src.core.constants import PAYMENT_STATUSES


SANDBOX_PROVIDER = "sandbox"
MERCADOPAGO_PROVIDER = "mercadopago"

# Header em que o sandbox espera a assinatura do corpo. O Mercado Pago usa
# `x-signature` com outro formato; ver verify_webhook_signature.
SANDBOX_SIGNATURE_HEADER = "x-webhook-signature"


class PaymentProviderUnknownError(Exception):
    """Provider que nao existe (erro de rota ou de configuracao)."""


class PaymentProviderNotConfiguredError(Exception):
    """Provider conhecido, mas sem credencial ou sem implementacao ainda."""


class PaymentWebhookPayloadError(Exception):
    """Corpo do webhook ilegivel ou sem os campos obrigatorios."""


@dataclass(frozen=True)
class PaymentIntent:
    """O que o gateway devolve ao criar uma cobranca."""

    provider: str
    provider_payment_id: str
    # Para onde mandar o cliente. Pix costuma vir com qr_code em vez de url.
    checkout_url: str | None = None
    qr_code: str | None = None


@dataclass(frozen=True)
class PaymentWebhookEvent:
    """Uma notificacao do gateway, ja traduzida."""

    # Id do EVENTO (nao do pagamento). E a chave de idempotencia do webhook:
    # gateways reenviam a mesma notificacao ate receber 2xx.
    event_id: str
    provider_payment_id: str
    # Ja em PAYMENT_STATUSES: "paid", "failed" ou "refunded".
    payment_status: str
    # O status cru do gateway, guardado so para log/depuracao.
    raw_status: str | None = None


def create_payment(
    *,
    provider: str,
    order_id: uuid.UUID,
    amount: Decimal,
    payment_method: str,
    description: str,
    access_token: str | None = None,
    application_fee: Decimal | None = None,
) -> PaymentIntent:
    """Cria a cobranca no gateway.

    `access_token` e a credencial DO RESTAURANTE (resolvida pelo
    PaymentCredentialService a partir do restaurant_id do pedido, nunca de
    uma variavel global) — a cobranca precisa ser criada em nome da conta
    dele. Nao existe token de fallback: gateway que precisa de credencial e
    nao recebeu uma vira PaymentProviderNotConfiguredError.

    `application_fee` e o corte da plataforma no split de pagamento do
    Mercado Pago. Fica opcional e hoje ninguem preenche — nao ha contrato de
    marketplace assinado ainda. Quando existir, quem chama passa o valor e a
    implementacao do Mercado Pago (abaixo) inclui no corpo da requisicao.
    """
    if provider == SANDBOX_PROVIDER:
        return _create_sandbox_payment(order_id)

    if provider == MERCADOPAGO_PROVIDER:
        if not access_token:
            raise PaymentProviderNotConfiguredError(
                "Restaurante sem credencial do Mercado Pago cadastrada para "
                "o ambiente atual: ver scripts/register_restaurant_payment_credential.py"
            )

        # >>> PLUGUE AQUI (1/3) <<<
        #
        # POST https://api.mercadopago.com/v1/payments
        #   Authorization: Bearer {access_token}
        #       ^ credencial do restaurante, nao uma constante do .env: e a
        #         conta dele que recebe o dinheiro.
        #   X-Idempotency-Key: {order_id}
        #       ^ o Mercado Pago tem idempotencia propria. Usar o order_id
        #         garante que um retry nosso nao gere duas cobrancas.
        #   corpo: transaction_amount=float(amount),
        #          payment_method_id derivado de `payment_method`
        #          ("pix", "credit_card"...), description, payer,
        #          notification_url apontando para
        #          POST /payments/webhooks/mercadopago,
        #          application_fee=float(application_fee) SE application_fee
        #              nao for None — omitir o campo quando for, e nao mandar
        #              zero (zero e uma resposta, ausencia e outra).
        #
        # Da resposta, montar:
        #   PaymentIntent(
        #       provider=MERCADOPAGO_PROVIDER,
        #       provider_payment_id=str(resposta["id"]),
        #       checkout_url=resposta["point_of_interaction"]["transaction_data"]["ticket_url"],
        #       qr_code=resposta["point_of_interaction"]["transaction_data"]["qr_code"],
        #   )
        #
        # Use httpx (ja e dependencia) com timeout explicito, no mesmo
        # padrao de src/integrations/google_maps_routes_client.py. Erro de
        # rede deve virar PaymentProviderNotConfiguredError ou uma excecao
        # propria — o PaymentService transforma em 503.
        raise PaymentProviderNotConfiguredError(
            "Mercado Pago ainda nao implementado: ver create_payment em "
            "src/integrations/payment_gateway.py"
        )

    raise PaymentProviderUnknownError(f"Provider de pagamento desconhecido: {provider}")


def verify_webhook_signature(
    *,
    provider: str,
    raw_body: bytes,
    headers: dict[str, str],
    secret: str | None,
) -> bool:
    """Confere que a notificacao veio mesmo do gateway.

    Recebe o corpo CRU de proposito: a assinatura e calculada sobre os bytes
    exatos que o gateway enviou. Reserializar o JSON muda espacos e ordem
    de chaves e derruba a conferencia.
    """
    if provider == SANDBOX_PROVIDER:
        return _verify_sandbox_signature(raw_body, headers, secret)

    if provider == MERCADOPAGO_PROVIDER:
        # >>> PLUGUE AQUI (2/3) <<<
        #
        # O Mercado Pago manda dois headers:
        #   x-signature: "ts=1704908010,v1=618c85345248dd820d5fd456117c2ab2ef8eda45..."
        #   x-request-id: "<uuid>"
        #
        # Passos:
        #   1. quebrar x-signature em ts e v1;
        #   2. montar o manifest exatamente nesta forma (inclusive os ";"):
        #        f"id:{data_id};request-id:{x_request_id};ts:{ts};"
        #      onde data_id e o `data.id` do corpo, em MINUSCULAS se vier
        #      alfanumerico;
        #   3. hmac_sha256(manifest, settings.MERCADOPAGO_WEBHOOK_SECRET);
        #   4. comparar com v1 usando hmac.compare_digest (nunca ==, para
        #      nao vazar o segredo por tempo de comparacao).
        #
        # Rejeitar tambem quando `ts` estiver velho demais (5 minutos e um
        # bom teto) — sem isso, uma notificacao valida capturada hoje pode
        # ser reenviada por terceiros amanha.
        raise PaymentProviderNotConfiguredError(
            "Assinatura do Mercado Pago ainda nao implementada: ver "
            "verify_webhook_signature em src/integrations/payment_gateway.py"
        )

    raise PaymentProviderUnknownError(f"Provider de pagamento desconhecido: {provider}")


def parse_webhook_event(*, provider: str, raw_body: bytes) -> PaymentWebhookEvent:
    """Traduz o corpo do webhook para o vocabulario da aplicacao."""
    if provider == SANDBOX_PROVIDER:
        return _parse_sandbox_event(raw_body)

    if provider == MERCADOPAGO_PROVIDER:
        # >>> PLUGUE AQUI (3/3) <<<
        #
        # ATENCAO: o webhook do Mercado Pago NAO traz o status do pagamento.
        # Ele avisa "o pagamento X mudou" (o `data.id` do envelope e o
        # provider_payment_id). O status confiavel exige uma consulta:
        #
        #   GET https://api.mercadopago.com/v1/payments/{data.id}
        #     Authorization: Bearer {access_token da conta DO RESTAURANTE}
        #
        # Nao ha credencial global para essa chamada: o `access_token` e o
        # do restaurante dono do pagamento, e o webhook so chega com o
        # data.id. A ordem tem que ser: (1) achar o pedido pelo
        # provider_payment_id via OrderRepository.get_order_by_provider_payment,
        # (2) resolver a credencial ativa desse order.restaurant_id com
        # PaymentCredentialService, (3) so entao fazer o GET acima. Por isso
        # esta funcao vai precisar deixar de ser pura — o PaymentService e
        # quem tem acesso ao banco para os passos 1 e 2.
        #
        # Nunca confie em status vindo dentro do corpo do webhook: o corpo
        # e so um aviso, e quem manda e a API.
        #
        # Traducao de `status` da consulta para PAYMENT_STATUSES:
        #   approved                      -> "paid"
        #   rejected / cancelled          -> "failed"
        #   refunded / charged_back       -> "refunded"
        #   pending / in_process / authorized -> ignore o evento (devolva
        #       algo que o PaymentService trate como "sem mudanca"; hoje o
        #       caminho mais simples e levantar PaymentWebhookPayloadError,
        #       que vira 200 "ignored" sem retentativa infinita).
        #
        # event_id: use o `id` do envelope da notificacao. Ele e o que se
        # repete quando o Mercado Pago reenvia, e e o que da idempotencia.
        raise PaymentProviderNotConfiguredError(
            "Traducao do webhook do Mercado Pago ainda nao implementada: ver "
            "parse_webhook_event em src/integrations/payment_gateway.py"
        )

    raise PaymentProviderUnknownError(f"Provider de pagamento desconhecido: {provider}")


def sign_sandbox_payload(raw_body: bytes, secret: str) -> str:
    """Assinatura que o sandbox espera no header.

    Publica de proposito: e o que permite disparar um webhook de teste com
    curl sem reimplementar o HMAC na mao.
    """
    return hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()


def _create_sandbox_payment(order_id: uuid.UUID) -> PaymentIntent:
    # Id derivado do pedido, nao aleatorio: chamar duas vezes para o mesmo
    # pedido devolve a mesma cobranca, que e como um gateway com
    # idempotencia se comporta.
    return PaymentIntent(
        provider=SANDBOX_PROVIDER,
        provider_payment_id=f"sandbox-{order_id}",
        checkout_url=None,
        qr_code=None,
    )


def _verify_sandbox_signature(
    raw_body: bytes,
    headers: dict[str, str],
    secret: str | None,
) -> bool:
    if not secret:
        # Sem segredo nao existe verificacao possivel. Aceitar "porque e
        # sandbox" deixaria uma rota publica capaz de marcar qualquer
        # pedido como pago.
        raise PaymentProviderNotConfiguredError(
            "PAYMENT_WEBHOOK_SECRET nao configurada: o webhook do sandbox "
            "nao pode ser verificado."
        )
    received = _header(headers, SANDBOX_SIGNATURE_HEADER)
    if not received:
        return False
    return hmac.compare_digest(sign_sandbox_payload(raw_body, secret), received)


def _parse_sandbox_event(raw_body: bytes) -> PaymentWebhookEvent:
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise PaymentWebhookPayloadError("Corpo do webhook nao e JSON valido") from exc
    if not isinstance(payload, dict):
        raise PaymentWebhookPayloadError("Corpo do webhook nao e um objeto JSON")

    event_id = payload.get("event_id")
    provider_payment_id = payload.get("payment_id")
    payment_status = payload.get("status")
    if not event_id or not provider_payment_id or not payment_status:
        raise PaymentWebhookPayloadError(
            "Webhook do sandbox exige event_id, payment_id e status"
        )
    if payment_status not in PAYMENT_STATUSES:
        raise PaymentWebhookPayloadError(f"Status desconhecido: {payment_status}")

    return PaymentWebhookEvent(
        event_id=str(event_id),
        provider_payment_id=str(provider_payment_id),
        payment_status=payment_status,
        raw_status=payment_status,
    )


def _header(headers: dict[str, str], name: str) -> str | None:
    # Nome de header e case-insensitive no HTTP, e o dict que chega do
    # Starlette pode vir com qualquer capitalizacao dependendo do cliente.
    for key, value in headers.items():
        if key.lower() == name:
            return value
    return None
