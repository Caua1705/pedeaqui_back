"""Integracao com o gateway de pagamento.

===========================================================================
TRES FUNCOES fazem a ponte com o Mercado Pago; nada fora deste arquivo
precisa mudar quando o gateway ou a versao da API mudar:

  1. create_payment           — cria a cobranca e devolve o id do gateway
  2. verify_webhook_signature — confere que a notificacao veio do gateway
  3. parse_webhook_event      — traduz o corpo do gateway para o daqui
===========================================================================

Por que uma camada propria e nao chamar o SDK do Mercado Pago direto dos
services: o resto do sistema fala "paid", "failed", "refunded"
(PAYMENT_STATUSES). O gateway fala "approved", "rejected", "in_process",
"charged_back". A traducao mora aqui, em um lugar so, e trocar de gateway
um dia nao vira uma caca ao "approved" espalhado pelo codigo.

O provider "sandbox" e uma implementacao real e completa, sem chamada
externa: ele existe para o fluxo inteiro (criar cobranca, receber webhook
assinado, confirmar pagamento) ser testavel e demonstravel sem depender do
Mercado Pago responder. Nao e mock de teste — roda em desenvolvimento.

O provider "mercadopago" chama a API de Pagamentos (v1) de verdade —
Checkout Transparente, hoje so pix (cartao fica para depois: exige token de
cartao gerado no frontend com o SDK deles, fluxo bem diferente do pix). Duas
particularidades do pix na API deles:

  - o corpo exige `payer.email`. O pedido aqui nao guarda e-mail (so nome e
    telefone — ver Order.customer_name_snapshot/customer_phone_snapshot);
    quem resolve isso e o PaymentService antes de chamar create_payment (ver
    PaymentService._resolve_payer_email).
  - o webhook NUNCA traz o status. Ele so avisa "o pagamento X mudou"; o
    status de verdade vem de um GET separado, autenticado com a credencial
    do restaurante DONO do pagamento — dai parse_webhook_event receber
    access_token, e o PaymentService ter que achar o pedido (e por ele, o
    restaurante) ANTES de poder fazer essa consulta. Ver extract_provider_payment_id.
"""

import hashlib
import hmac
import json
import logging
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal

import httpx

from src.core.constants import PAYMENT_STATUSES


logger = logging.getLogger("uvicorn.error")

SANDBOX_PROVIDER = "sandbox"
MERCADOPAGO_PROVIDER = "mercadopago"

# Header em que o sandbox espera a assinatura do corpo. O Mercado Pago usa
# `x-signature` com outro formato; ver _verify_mercadopago_signature.
SANDBOX_SIGNATURE_HEADER = "x-webhook-signature"

MERCADOPAGO_API_BASE_URL = "https://api.mercadopago.com"
MERCADOPAGO_TIMEOUT_SECONDS = 10.0
MERCADOPAGO_SIGNATURE_HEADER = "x-signature"
MERCADOPAGO_REQUEST_ID_HEADER = "x-request-id"
# Piloto so aceita pix. Cartao exige token gerado no frontend (SDK do
# Mercado Pago) e fluxo de parcelas — outra integracao, fica para depois.
MERCADOPAGO_SUPPORTED_PAYMENT_METHODS = ("pix",)
# Tolerancia da assinatura do webhook: uma notificacao capturada e reenviada
# por terceiros horas depois nao pode passar so porque o HMAC bate.
MERCADOPAGO_SIGNATURE_MAX_AGE_SECONDS = 300

# approved/rejected/etc. -> vocabulario da casa (PAYMENT_STATUSES). Status
# que nao aparece aqui (pending, in_process, authorized, e qualquer coisa
# nova que o Mercado Pago inventar) nao produz mudanca — ver parse_webhook_event.
_MERCADOPAGO_STATUS_TRANSLATION = {
    "approved": "paid",
    "rejected": "failed",
    "cancelled": "failed",
    "refunded": "refunded",
    "charged_back": "refunded",
}


class PaymentProviderUnknownError(Exception):
    """Provider que nao existe (erro de rota ou de configuracao)."""


class PaymentProviderNotConfiguredError(Exception):
    """Provider conhecido, mas sem credencial, sem dado obrigatorio ou sem
    suporte ao metodo de pagamento pedido."""


class PaymentWebhookPayloadError(Exception):
    """Corpo do webhook ilegivel, sem os campos obrigatorios, ou com status
    que nao produz mudanca de estado (pending/in_process/authorized)."""


class PaymentGatewayError(Exception):
    """O gateway respondeu, mas recusou ou nao foi possivel entender a
    resposta. Erro DELES (ou da chamada), nao um bug de configuracao nossa."""


class PaymentGatewayUnavailableError(PaymentGatewayError):
    """Timeout, falha de rede ou 5xx do gateway. Vale tentar de novo depois."""


class PaymentGatewayCredentialError(PaymentGatewayError):
    """Gateway respondeu 401/403: token invalido, revogado ou de outra
    conta. Nao adianta tentar de novo sem trocar a credencial."""


class PaymentNotFoundError(PaymentGatewayError):
    """Gateway respondeu 404: o id de pagamento informado nao existe la."""


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
    payer_email: str | None = None,
) -> PaymentIntent:
    """Cria a cobranca no gateway.

    `access_token` e a credencial DO RESTAURANTE (resolvida pelo
    PaymentCredentialService a partir do restaurant_id do pedido, nunca de
    uma variavel global) — a cobranca precisa ser criada em nome da conta
    dele. Nao existe token de fallback: gateway que precisa de credencial e
    nao recebeu uma vira PaymentProviderNotConfiguredError.

    `application_fee` e o corte da plataforma no split de pagamento do
    Mercado Pago. Fica opcional e hoje ninguem preenche — nao ha contrato de
    marketplace assinado ainda. So entra no corpo da requisicao quando
    diferente de None.

    `payer_email` e exigido pela API deles para pix. Quem resolve o valor
    (e-mail do cliente logado, ou um sintetico para convidado) e
    PaymentService, nao esta funcao — aqui so se recusa a prosseguir sem ele.
    """
    if provider == SANDBOX_PROVIDER:
        return _create_sandbox_payment(order_id)

    if provider == MERCADOPAGO_PROVIDER:
        if not access_token:
            raise PaymentProviderNotConfiguredError(
                "Restaurante sem credencial do Mercado Pago cadastrada para "
                "o ambiente atual: ver scripts/register_restaurant_payment_credential.py"
            )
        if payment_method not in MERCADOPAGO_SUPPORTED_PAYMENT_METHODS:
            raise PaymentProviderNotConfiguredError(
                f"Mercado Pago: metodo de pagamento '{payment_method}' ainda nao "
                f"suportado (piloto so aceita {', '.join(MERCADOPAGO_SUPPORTED_PAYMENT_METHODS)})"
            )
        if not payer_email:
            raise PaymentProviderNotConfiguredError(
                "Mercado Pago exige e-mail do pagador para cobranca pix e "
                "nenhum foi informado"
            )

        body = {
            "transaction_amount": float(amount),
            "description": description,
            "payment_method_id": "pix",
            "payer": {"email": payer_email},
        }
        if application_fee is not None:
            body["application_fee"] = float(application_fee)

        payload = _call_mercadopago(
            method="POST",
            path="/v1/payments",
            access_token=access_token,
            json_body=body,
            # O Mercado Pago tem idempotencia propria por esse header: um
            # retry nosso (timeout na ida, por exemplo) reusa o order_id e
            # nao gera uma segunda cobranca la.
            extra_headers={"X-Idempotency-Key": str(order_id)},
        )

        transaction_data = (payload.get("point_of_interaction") or {}).get("transaction_data") or {}
        return PaymentIntent(
            provider=MERCADOPAGO_PROVIDER,
            provider_payment_id=str(payload["id"]),
            checkout_url=transaction_data.get("ticket_url"),
            qr_code=transaction_data.get("qr_code"),
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
        return _verify_mercadopago_signature(raw_body, headers, secret)

    raise PaymentProviderUnknownError(f"Provider de pagamento desconhecido: {provider}")


def parse_webhook_event(
    *,
    provider: str,
    raw_body: bytes,
    access_token: str | None = None,
) -> PaymentWebhookEvent:
    """Traduz o corpo do webhook para o vocabulario da aplicacao.

    `access_token` so e usado pelo Mercado Pago (o sandbox ja traz o status
    no proprio corpo). E a credencial do restaurante DONO do pagamento — o
    PaymentService resolve isso ANTES de chamar esta funcao, usando
    extract_provider_payment_id + OrderRepository para achar de qual
    restaurante se trata sem precisar decifrar credencial nenhuma so para
    identificar o pedido.
    """
    if provider == SANDBOX_PROVIDER:
        return _parse_sandbox_event(raw_body)

    if provider == MERCADOPAGO_PROVIDER:
        return _parse_mercadopago_event(raw_body, access_token)

    raise PaymentProviderUnknownError(f"Provider de pagamento desconhecido: {provider}")


def extract_provider_payment_id(*, provider: str, raw_body: bytes) -> str:
    """Le so o id do pagamento no gateway, sem chamar nada externo.

    Existe para o Mercado Pago: o webhook so tem o data.id, e so DEPOIS de
    achar o pedido correspondente (e por ele, o restaurante) e que da para
    saber qual credencial usar no GET que parse_webhook_event precisa fazer.
    Separar essa leitura de parse_webhook_event evita decifrar credencial
    nenhuma para um payment_id que nem pedido nosso e.
    """
    envelope = _load_json_object(raw_body)

    if provider == SANDBOX_PROVIDER:
        provider_payment_id = envelope.get("payment_id")
    elif provider == MERCADOPAGO_PROVIDER:
        data = envelope.get("data")
        provider_payment_id = data.get("id") if isinstance(data, dict) else None
    else:
        raise PaymentProviderUnknownError(f"Provider de pagamento desconhecido: {provider}")

    if not provider_payment_id:
        raise PaymentWebhookPayloadError("Webhook sem id do pagamento no gateway")
    return str(provider_payment_id)


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


def _verify_mercadopago_signature(
    raw_body: bytes,
    headers: dict[str, str],
    secret: str | None,
) -> bool:
    if not secret:
        raise PaymentProviderNotConfiguredError(
            "MERCADOPAGO_WEBHOOK_SECRET nao configurada: o webhook do "
            "Mercado Pago nao pode ser verificado."
        )

    signature_header = _header(headers, MERCADOPAGO_SIGNATURE_HEADER)
    request_id = _header(headers, MERCADOPAGO_REQUEST_ID_HEADER)
    if not signature_header or not request_id:
        return False

    ts, v1 = _parse_mercadopago_signature_header(signature_header)
    if not ts or not v1:
        return False
    if not _mercadopago_timestamp_is_fresh(ts):
        return False

    # Corpo ilegivel = assinatura invalida (False), nunca uma excecao: quem
    # chama (_verify_signature no PaymentService) so sabe tratar
    # PaymentProviderUnknownError/PaymentProviderNotConfiguredError vindo
    # daqui, e um corpo malformado nao e nem uma coisa nem outra.
    try:
        envelope = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return False
    data = envelope.get("data") if isinstance(envelope, dict) else None
    data_id = data.get("id") if isinstance(data, dict) else None
    if not data_id:
        return False

    # MINUSCULAS: e assim que o Mercado Pago manda normalizar o id no
    # manifest quando ele vem alfanumerico (o nosso e sempre numerico, mas
    # normalizar sempre custa nada e evita depender disso continuar assim).
    manifest = f"id:{str(data_id).lower()};request-id:{request_id};ts:{ts};"
    expected = hmac.new(secret.encode("utf-8"), manifest.encode("utf-8"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, v1)


def _parse_mercadopago_signature_header(header_value: str) -> tuple[str | None, str | None]:
    ts = None
    v1 = None
    for part in header_value.split(","):
        key, _, value = part.strip().partition("=")
        if key == "ts":
            ts = value.strip()
        elif key == "v1":
            v1 = value.strip()
    return ts, v1


def _mercadopago_timestamp_is_fresh(ts: str) -> bool:
    try:
        ts_seconds = int(ts)
    except (TypeError, ValueError):
        return False
    return (time.time() - ts_seconds) <= MERCADOPAGO_SIGNATURE_MAX_AGE_SECONDS


def _parse_sandbox_event(raw_body: bytes) -> PaymentWebhookEvent:
    payload = _load_json_object(raw_body)

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


def _parse_mercadopago_event(raw_body: bytes, access_token: str | None) -> PaymentWebhookEvent:
    if not access_token:
        raise PaymentProviderNotConfiguredError(
            "Restaurante sem credencial do Mercado Pago cadastrada para o "
            "ambiente atual: impossivel consultar o status do pagamento"
        )

    envelope = _load_json_object(raw_body)
    event_id = envelope.get("id")
    data = envelope.get("data")
    provider_payment_id = data.get("id") if isinstance(data, dict) else None
    if not event_id or not provider_payment_id:
        raise PaymentWebhookPayloadError(
            "Webhook do Mercado Pago sem id do evento ou do pagamento"
        )

    # O corpo do webhook NAO traz o status — so avisa que o pagamento
    # mudou. O status confiavel e sempre o desta consulta, nunca o que
    # vier (ou nao vier) no corpo da notificacao.
    payload = _call_mercadopago(
        method="GET",
        path=f"/v1/payments/{provider_payment_id}",
        access_token=access_token,
    )
    raw_status = payload.get("status")
    payment_status = _MERCADOPAGO_STATUS_TRANSLATION.get(raw_status)
    if payment_status is None:
        # pending/in_process/authorized, ou um status novo que o Mercado
        # Pago venha a inventar: nao ha mudanca de estado nosso para
        # aplicar agora. Nao e retentavel — o proprio gateway manda um
        # novo webhook quando o status mudar de verdade.
        raise PaymentWebhookPayloadError(
            f"Status do Mercado Pago sem traducao aplicavel: {raw_status}"
        )

    return PaymentWebhookEvent(
        event_id=str(event_id),
        provider_payment_id=str(provider_payment_id),
        payment_status=payment_status,
        raw_status=raw_status,
    )


def _call_mercadopago(
    *,
    method: str,
    path: str,
    access_token: str,
    json_body: dict | None = None,
    extra_headers: dict[str, str] | None = None,
) -> dict:
    """POST/GET autenticado na API do Mercado Pago.

    Nunca loga o access_token nem o corpo da resposta: o primeiro e
    credencial da conta do restaurante, o segundo pode trazer dado de quem
    pagou. O que fica no log e so o suficiente para depurar uma falha
    (metodo, path, status, latencia) — para o resto, o Mercado Pago tem o
    proprio painel de "Atividade" com o detalhe de cada chamada.
    """
    headers = {"Authorization": f"Bearer {access_token}"}
    if extra_headers:
        headers.update(extra_headers)

    started_at = time.perf_counter()
    status_code = 0
    try:
        with httpx.Client(timeout=MERCADOPAGO_TIMEOUT_SECONDS) as client:
            response = client.request(
                method,
                f"{MERCADOPAGO_API_BASE_URL}{path}",
                json=json_body,
                headers=headers,
            )
            status_code = response.status_code
    except httpx.TimeoutException as exc:
        raise PaymentGatewayUnavailableError(
            f"Mercado Pago nao respondeu em {MERCADOPAGO_TIMEOUT_SECONDS}s ({method} {path})"
        ) from exc
    except httpx.HTTPError as exc:
        raise PaymentGatewayUnavailableError(
            f"Falha de rede ao chamar o Mercado Pago ({method} {path})"
        ) from exc
    finally:
        logger.info(
            "[Pagamento][mercadopago] method=%s path=%s status=%s latency_ms=%.2f",
            method,
            path,
            status_code,
            (time.perf_counter() - started_at) * 1000,
        )

    if status_code in (401, 403):
        raise PaymentGatewayCredentialError(
            f"Mercado Pago recusou a credencial do restaurante (status {status_code})"
        )
    if status_code == 404:
        raise PaymentNotFoundError(f"Mercado Pago: recurso nao encontrado ({method} {path})")
    if status_code >= 500:
        raise PaymentGatewayUnavailableError(
            f"Mercado Pago com erro interno (status {status_code})"
        )
    if status_code >= 400:
        # 400/422 etc.: a requisicao que MONTAMOS foi recusada (dado
        # invalido). Nao inclui o corpo na mensagem — pode ecoar de volta
        # o e-mail do pagador que mandamos.
        raise PaymentGatewayError(
            f"Mercado Pago recusou a requisicao (status {status_code})"
        )

    try:
        return response.json()
    except ValueError as exc:
        raise PaymentGatewayUnavailableError(
            "Mercado Pago respondeu um corpo que nao e JSON"
        ) from exc


def _load_json_object(raw_body: bytes) -> dict:
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise PaymentWebhookPayloadError("Corpo do webhook nao e JSON valido") from exc
    if not isinstance(payload, dict):
        raise PaymentWebhookPayloadError("Corpo do webhook nao e um objeto JSON")
    return payload


def _header(headers: dict[str, str], name: str) -> str | None:
    # Nome de header e case-insensitive no HTTP, e o dict que chega do
    # Starlette pode vir com qualquer capitalizacao dependendo do cliente.
    for key, value in headers.items():
        if key.lower() == name:
            return value
    return None
