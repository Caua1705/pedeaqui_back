"""O que a Cloud API da Meta manda e o que ela recebe. So transporte.

Nao ha banco aqui e nao ha decisao: quem decide e o service. Este modulo
traduz bytes em objetos nossos e objetos nossos em chamadas HTTP.

## A assinatura e conferida ANTES do roteamento, e isso e o contrario do pagamento

No Mercado Pago o segredo do webhook e do RESTAURANTE, entao nao da para
conferir a assinatura sem antes descobrir de quem e o pagamento — por isso
`PaymentService.handle_webhook` acha o pedido primeiro e so depois verifica.

Aqui o segredo e o **App Secret do nosso app da Meta**, um so para a
aplicacao inteira: da para conferir a primeira coisa, antes de tocar no banco.
E e o que se faz. Nada acontece antes da assinatura.

Ela e calculada sobre os **bytes crus** do corpo (`X-Hub-Signature-256:
sha256=<hex>`): reserializar o JSON muda espacos e ordem de chaves e derruba
a conferencia. E a comparacao e `hmac.compare_digest`, nunca `!=` — armadilha
18.

## Um POST pode trazer varios numeros

`entry[].changes[]`, e cada `change` tem o seu
`value.metadata.phone_number_id`. Duas lojas do mesmo WABA cabem no mesmo
POST, e tratar a requisicao como se fosse de um numero so creditaria as
mensagens de uma loja a outra — sem erro nenhum.

Por isso a leitura devolve uma LISTA de mudancas, e o roteamento e por
mudanca.

## O texto da mensagem recebida nao e lido

O que interessa desta rodada e QUE o cliente escreveu (abre a janela de 24h),
nao O QUE ele escreveu. Texto de pessoa e dado pessoal com prazo (armadilha
38), e nao ha quem o leia: ele nao entra no processo.
"""

import hashlib
import hmac
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


logger = logging.getLogger("uvicorn.error")

SIGNATURE_HEADER = "x-hub-signature-256"
SIGNATURE_PREFIX = "sha256="

# O `object` que a Meta carimba no webhook do WhatsApp. Ela usa o MESMO
# formato para Instagram, Messenger e outros produtos: um `object` diferente
# chegando aqui e assinatura do nosso app com o produto errado no painel.
WHATSAPP_WEBHOOK_OBJECT = "whatsapp_business_account"


class WhatsAppNotConfiguredError(Exception):
    """Falta configuracao nossa (App Secret, verify token, chave de cifra)."""


class WhatsAppWebhookPayloadError(Exception):
    """Corpo que nao da para entender como webhook do WhatsApp."""


@dataclass(frozen=True)
class WhatsAppInboundMessage:
    """Uma mensagem do CLIENTE. Sem o texto, de proposito."""

    from_phone: str
    sent_at: datetime


@dataclass(frozen=True)
class WhatsAppStatusUpdate:
    """O desfecho de uma mensagem NOSSA, que chega depois do envio."""

    wamid: str
    status: str
    # Texto e nao numero: e o que se cita num chamado da Meta, nao numero
    # para contas.
    error_code: str | None


@dataclass(frozen=True)
class WhatsAppChange:
    """Tudo que chegou para UM numero neste POST."""

    phone_number_id: str
    display_phone_number: str
    inbound: tuple[WhatsAppInboundMessage, ...]
    statuses: tuple[WhatsAppStatusUpdate, ...]


def verify_webhook_signature(
    *,
    raw_body: bytes,
    headers: Mapping[str, str],
    app_secret: str | None,
) -> bool:
    """Confere que o POST veio mesmo da Meta.

    `False` quer dizer "nao veio da Meta" e vira 401. Configuracao faltando
    NAO e `False`: e `WhatsAppNotConfiguredError`, e vira 503 — quem tem que
    agir e quem opera, e nao quem mandou.
    """
    if not app_secret:
        raise WhatsAppNotConfiguredError(
            "WHATSAPP_APP_SECRET nao configurada: o webhook do WhatsApp nao "
            "pode ser verificado."
        )

    received = _header(headers, SIGNATURE_HEADER)
    if not received or not received.startswith(SIGNATURE_PREFIX):
        return False

    expected = hmac.new(app_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, received.removeprefix(SIGNATURE_PREFIX))


def parse_webhook_changes(raw_body: bytes) -> list[WhatsAppChange]:
    """Traduz o corpo do webhook em uma mudanca POR NUMERO.

    Lista vazia e resposta normal: a Meta manda notificacao de campo que nao
    interessa, e um POST sem nada para nos nao e erro.

    Mudanca sem `phone_number_id` e descartada em vez de derrubar o corpo
    inteiro — sem ele nao ha como rotear, e recusar o POST por causa de uma
    mudanca torta perderia as outras, que estao boas.
    """
    envelope = _load_json_object(raw_body)
    if envelope.get("object") != WHATSAPP_WEBHOOK_OBJECT:
        raise WhatsAppWebhookPayloadError(
            f"Webhook com object={envelope.get('object')!r}, esperado "
            f"{WHATSAPP_WEBHOOK_OBJECT!r}."
        )

    mudancas = []
    for entry in _as_list(envelope.get("entry")):
        for change in _as_list(entry.get("changes")):
            mudanca = _parse_change(change)
            if mudanca is not None:
                mudancas.append(mudanca)
    return mudancas


def _parse_change(change: Any) -> WhatsAppChange | None:
    if not isinstance(change, dict):
        return None
    value = change.get("value")
    if not isinstance(value, dict):
        return None

    metadata = value.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    phone_number_id = metadata.get("phone_number_id")
    if not phone_number_id:
        return None

    inbound = []
    for item in _as_list(value.get("messages")):
        mensagem = _parse_inbound(item)
        if mensagem is not None:
            inbound.append(mensagem)

    statuses = []
    for item in _as_list(value.get("statuses")):
        atualizacao = _parse_status(item)
        if atualizacao is not None:
            statuses.append(atualizacao)

    return WhatsAppChange(
        phone_number_id=str(phone_number_id),
        display_phone_number=str(metadata.get("display_phone_number") or ""),
        inbound=tuple(inbound),
        statuses=tuple(statuses),
    )


def _parse_inbound(message: Any) -> WhatsAppInboundMessage | None:
    if not isinstance(message, dict):
        return None
    from_phone = message.get("from")
    sent_at = _instant_from_unix(message.get("timestamp"))
    if not from_phone or sent_at is None:
        # Sem remetente nao ha janela a abrir; sem instante, abrir usando o
        # relogio daqui esticaria a janela de um webhook atrasado. Falha
        # fechada: o proximo envio sai como template, que sempre e aceito.
        return None
    return WhatsAppInboundMessage(from_phone=str(from_phone), sent_at=sent_at)


def _parse_status(status: Any) -> WhatsAppStatusUpdate | None:
    if not isinstance(status, dict):
        return None
    wamid = status.get("id")
    nome = status.get("status")
    if not wamid or not nome:
        return None
    return WhatsAppStatusUpdate(
        wamid=str(wamid),
        status=str(nome),
        error_code=_first_error_code(status.get("errors")),
    )


def _first_error_code(errors: Any) -> str | None:
    for erro in _as_list(errors):
        code = erro.get("code")
        if code is not None:
            return str(code)
    return None


def _instant_from_unix(timestamp: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(timestamp), tz=timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _load_json_object(raw_body: bytes) -> dict:
    try:
        envelope = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise WhatsAppWebhookPayloadError("Corpo do webhook nao e JSON valido.") from exc
    if not isinstance(envelope, dict):
        raise WhatsAppWebhookPayloadError("Corpo do webhook nao e um objeto JSON.")
    return envelope


def _as_list(value: Any) -> list[dict]:
    """So os itens que sao dicionario. A Meta acrescenta campo, nao troca tipo
    — mas um `null` no meio da lista nao pode derrubar o POST inteiro."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _header(headers: Mapping[str, str], name: str) -> str | None:
    # Nome de header e case-insensitive no HTTP, e o dict que chega do
    # Starlette pode vir com qualquer capitalizacao dependendo do cliente.
    for key, value in headers.items():
        if key.lower() == name:
            return value
    return None
