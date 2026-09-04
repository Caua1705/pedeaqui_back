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

import httpx

from src.core.config import settings


logger = logging.getLogger("uvicorn.error")

SIGNATURE_HEADER = "x-hub-signature-256"
SIGNATURE_PREFIX = "sha256="

# O `object` que a Meta carimba no webhook do WhatsApp. Ela usa o MESMO
# formato para Instagram, Messenger e outros produtos: um `object` diferente
# chegando aqui e assinatura do nosso app com o produto errado no painel.
WHATSAPP_WEBHOOK_OBJECT = "whatsapp_business_account"

# O campo do webhook que carrega evento de CONTA. Roteado pelo WABA, e nao
# pelo numero: ver `parse_account_updates`.
ACCOUNT_UPDATE_FIELD = "account_update"


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
class WhatsAppAccountUpdate:
    """Um evento da CONTA (WABA), nao de um numero.

    `PARTNER_REMOVED` e o que importa: o lojista desconectou a Cloud API pelo
    aplicativo dele, e todos os numeros daquele WABA pararam junto.
    """

    waba_id: str
    event: str
    # So vem quando o lojista usava o aplicativo E a Cloud API
    # (`disconnection_info` e condicional na documentacao da Meta).
    reason: str | None


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
    _ensure_whatsapp_object(envelope)

    mudancas = []
    for entry in _as_list(envelope.get("entry")):
        for change in _as_list(entry.get("changes")):
            mudanca = _parse_change(change)
            if mudanca is not None:
                mudancas.append(mudanca)
    return mudancas


def parse_account_updates(raw_body: bytes) -> list[WhatsAppAccountUpdate]:
    """Le os eventos de CONTA do mesmo corpo. E a SEGUNDA chave de roteamento.

    `account_update` **nao tem `metadata.phone_number_id`** — conferido na
    documentacao da Meta. Ele e da conta inteira, e o que o identifica e o
    WABA: `value.waba_info.waba_id` quando vem, e `entry[].id`, que e o WABA,
    sempre.

    Por isso ele tem funcao propria em vez de sair de `parse_webhook_changes`:
    aquela exige `phone_number_id` e **descartaria o evento em silencio** — e
    silencio e exatamente o que este webhook existe para acabar. Duas
    perguntas diferentes, duas funcoes que se leem inteiras; a alternativa
    seria uma funcao com um `if` no meio decidindo qual chave usar.

    As duas leem o MESMO corpo e cada uma so enxerga o que e dela, entao um
    POST com mensagem e evento de conta juntos e atendido pelas duas.
    """
    envelope = _load_json_object(raw_body)
    _ensure_whatsapp_object(envelope)

    eventos = []
    for entry in _as_list(envelope.get("entry")):
        for change in _as_list(entry.get("changes")):
            evento = _parse_account_update(change, entry_id=entry.get("id"))
            if evento is not None:
                eventos.append(evento)
    return eventos


def _parse_account_update(change: Any, *, entry_id: Any) -> WhatsAppAccountUpdate | None:
    if not isinstance(change, dict) or change.get("field") != ACCOUNT_UPDATE_FIELD:
        return None
    value = change.get("value")
    if not isinstance(value, dict):
        return None

    event = value.get("event")
    if not event:
        return None

    waba_info = value.get("waba_info")
    waba_info = waba_info if isinstance(waba_info, dict) else {}
    waba_id = waba_info.get("waba_id") or entry_id
    if not waba_id:
        return None

    desconexao = value.get("disconnection_info")
    desconexao = desconexao if isinstance(desconexao, dict) else {}
    motivo = desconexao.get("reason")

    return WhatsAppAccountUpdate(
        waba_id=str(waba_id),
        event=str(event),
        reason=str(motivo) if motivo else None,
    )


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


def _ensure_whatsapp_object(envelope: dict) -> None:
    if envelope.get("object") != WHATSAPP_WEBHOOK_OBJECT:
        raise WhatsAppWebhookPayloadError(
            f"Webhook com object={envelope.get('object')!r}, esperado "
            f"{WHATSAPP_WEBHOOK_OBJECT!r}."
        )


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


# --- Envio ---------------------------------------------------------------
#
# Duas funcoes parecidas em vez de uma generica com um `if` dentro: elas
# diferem no unico lugar que importa (o corpo), e quem le uma le a chamada
# inteira sem destrinchar a outra. E, mais importante, a DECISAO entre as
# duas nao mora aqui — mora no service, que e quem sabe da janela de 24h.


class WhatsAppSendError(Exception):
    """Base do que da errado ao mandar.

    `retryable` sai do TIPO da excecao, NUNCA do codigo de erro da Meta. E a
    regra da armadilha 49: "repetir a mesma chamada tem chance?" ja tem dono,
    e deixar um codigo deles responder isso faria a mesma pergunta ter duas
    respostas em lugares diferentes.
    """

    retryable = False

    def __init__(self, message: str, *, error_code: str | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code


class WhatsAppTransportError(WhatsAppSendError):
    """A chamada nao chegou a ter resposta (timeout, DNS, conexao)."""

    retryable = True


class WhatsAppRejectedError(WhatsAppSendError):
    """A Meta respondeu, e a resposta nao e um envio.

    Inclui o 4xx com codigo (`132001` template inexistente, `131047` janela
    fechada) e o 200 sem `wamid` — que e resposta, mas nao e mensagem: sem
    `wamid` o webhook de status nao acha a linha depois, e chamar isso de
    sucesso gravaria um envio que ninguem consegue acompanhar.
    """


def send_template_message(
    *,
    access_token: str,
    phone_number_id: str,
    to: str,
    template_name: str,
    language: str,
    parameters: tuple[str, ...],
) -> str:
    """Manda um template APROVADO. Devolve o `wamid`.

    Sempre permitido, dentro ou fora da janela de 24h — e por isso e o unico
    caminho dos avisos de pedido, que chegam a quem nunca escreveu para a
    loja.

    Os parametros entram na ORDEM em que o template os declara (`{{1}}`,
    `{{2}}`, ...). A Meta nao os nomeia: trocar dois de lugar manda o numero
    do pedido onde vai o nome do cliente, sem erro nenhum.
    """
    corpo = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": language},
            "components": [
                {
                    "type": "body",
                    "parameters": [{"type": "text", "text": valor} for valor in parameters],
                }
            ],
        },
    }
    return _post_message(access_token=access_token, phone_number_id=phone_number_id, corpo=corpo)


def send_text_message(
    *,
    access_token: str,
    phone_number_id: str,
    to: str,
    body: str,
) -> str:
    """Manda texto livre. Devolve o `wamid`.

    So funciona DENTRO da janela de 24h. Quem confere isso e o service, antes
    de chegar aqui — fora dela a Meta responde `131047`, e descobrir pela
    resposta significa um cliente nao avisado.
    """
    corpo = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": body},
    }
    return _post_message(access_token=access_token, phone_number_id=phone_number_id, corpo=corpo)


def _post_message(*, access_token: str, phone_number_id: str, corpo: dict) -> str:
    url = (
        f"{settings.WHATSAPP_GRAPH_API_BASE_URL.rstrip('/')}"
        f"/{settings.WHATSAPP_GRAPH_API_VERSION}/{phone_number_id}/messages"
    )
    try:
        with httpx.Client(timeout=settings.WHATSAPP_TIMEOUT_SECONDS) as client:
            resposta = client.post(
                url,
                json=corpo,
                # O token no cabecalho, nunca na URL: query string entra em
                # log de proxy e em historico, e este e credencial do lojista.
                headers={"Authorization": f"Bearer {access_token}"},
            )
    except httpx.HTTPError as exc:
        raise WhatsAppTransportError(f"WhatsApp indisponivel: {exc}") from exc

    return _wamid_da_resposta(resposta)


def _wamid_da_resposta(resposta) -> str:
    try:
        resposta.raise_for_status()
    except httpx.HTTPStatusError as exc:
        codigo, mensagem = _erro_da_meta(resposta)
        raise WhatsAppRejectedError(
            f"WhatsApp recusou a mensagem: {mensagem}", error_code=codigo
        ) from exc

    try:
        payload = resposta.json()
    except ValueError as exc:
        raise WhatsAppRejectedError("WhatsApp respondeu algo que nao e JSON.") from exc

    mensagens = payload.get("messages") if isinstance(payload, dict) else None
    wamid = mensagens[0].get("id") if mensagens else None
    if not wamid:
        raise WhatsAppRejectedError("WhatsApp respondeu sem `wamid`: nao ha mensagem a seguir.")
    return str(wamid)


def _erro_da_meta(resposta) -> tuple[str | None, str]:
    try:
        payload = resposta.json()
    except ValueError:
        return None, f"HTTP {resposta.status_code}"

    erro = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(erro, dict):
        return None, f"HTTP {resposta.status_code}"

    codigo = erro.get("code")
    return (
        str(codigo) if codigo is not None else None,
        str(erro.get("message") or f"HTTP {resposta.status_code}"),
    )
