"""Integracao com o gateway de pagamento.

===========================================================================
SEIS FUNCOES fazem a ponte com o Mercado Pago; nada fora deste arquivo
precisa mudar quando o gateway ou a versao da API mudar:

  1. create_payment           — cria a cobranca e devolve o id do gateway
  2. fetch_payment            — le o estado ATUAL de uma cobranca
  3. cancel_payment           — mata cobranca que ainda nao capturou dinheiro
  4. refund_payment           — devolve dinheiro que ja foi capturado
  5. verify_webhook_signature — confere que a notificacao veio do gateway
  6. parse_webhook_event      — traduz o corpo do gateway para o daqui
===========================================================================

**As funcoes 3 e 4 nao sao a mesma coisa com nomes diferentes**, e escolher
a errada e um erro do gateway na cara do cliente:

  - `cancel_payment` (`PUT /v1/payments/{id}` com `status=cancelled`) so
    vale para cobranca que AINDA NAO capturou dinheiro — `pending` (o QR do
    pix gerado e nao pago) e `in_process` (o cartao em analise). Nao ha
    dinheiro voltando: a cobranca deixa de existir.
  - `refund_payment` (`POST /v1/payments/{id}/refunds`) so vale para
    cobranca APROVADA. Ai ha dinheiro de verdade voltando.

Nenhuma das duas funciona no estado da outra, e QUAL delas cabe depende do
estado que o pagamento tem no gateway AGORA — que nao e necessariamente o
que `orders.payment_status` guarda, porque o webhook pode ainda estar em
voo. E por isso que a funcao 2 existe: quem vai encerrar uma cobranca
pergunta antes, em vez de decidir pela copia local. Ver
`PaymentRefundService`.

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
Checkout Transparente, com pix e cartao de credito.

**Os dois nao sao o mesmo fluxo com outro nome**, e as diferencas moram
todas aqui:

  - pix e ASSINCRONO: a cobranca nasce `pending` e o veredito chega por
    webhook. Cartao e SINCRONO — o proprio POST responde aprovado, recusado
    ou em analise. Por isso PaymentIntent carrega `payment_status`: ignorar
    a resposta transformava cartao recusado em pedido pendente eterno.
  - cartao passa por ANTIFRAUDE, e pix nao. Dai o estado `in_review`
    (`in_process` no vocabulario deles), que pode durar ate 48h uteis.
  - o numero do cartao NUNCA passa por aqui: o front tokeniza com o SDK
    deles e manda so o token (ver CardPaymentInput).

Duas particularidades do pix na API deles:

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
import re
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal
from urllib.parse import quote

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
MERCADOPAGO_SUPPORTED_PAYMENT_METHODS = ("pix", "credit_card")

# O sandbox NAO simula cartao, e a recusa e o ponto.
#
# Ele ignorava `payment_method` inteiro: `_create_sandbox_payment` so recebia
# o `order_id`. Com cartao isso significaria intent valido, webhook marcando
# como pago e COMANDA IMPRIMINDO — o fluxo inteiro parecendo funcionar sem
# dinheiro nenhum ter existido, que e a pior demonstracao possivel de um meio
# de pagamento. Cartao so se testa contra a credencial de teste do Mercado
# Pago; aqui ele falha alto.
SANDBOX_SUPPORTED_PAYMENT_METHODS = ("pix",)

# Parcelamento fica FORA da v1: a taxa que o restaurante contratou e a de
# cartao a vista, e parcelado sem juros sai do bolso de quem recebe. Habilitar
# parcelas sem essa decisao comercial mudaria o que o lojista recebe sem ele
# ter pedido.
CARD_INSTALLMENTS = 1
# Tolerancia da assinatura do webhook: uma notificacao capturada e reenviada
# por terceiros horas depois nao pode passar so porque o HMAC bate.
MERCADOPAGO_SIGNATURE_MAX_AGE_SECONDS = 300

# Teto do texto de erro que vai para o log. O Mercado Pago as vezes devolve
# uma descricao longa; o que importa esta no comeco dela, e uma linha de log
# de tamanho imprevisivel atrapalha quem for ler o arquivo depois.
MERCADOPAGO_ERROR_TEXT_MAX_CHARS = 300

# O UNICO dado do pagador que mandamos para eles e o e-mail (ver o corpo
# montado em create_payment), e a mensagem de erro deles ecoa de volta o
# valor recusado ("fulano@x.com is invalid"). Mascarar na saida do log: o
# codigo e a descricao do erro sao o que se depura, o e-mail de quem pagou
# nao.
_EMAIL_PATTERN = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

# approved/rejected/etc. -> vocabulario da casa (PAYMENT_STATUSES). Status
# que nao aparece aqui (pending, in_process, authorized, e qualquer coisa
# nova que o Mercado Pago inventar) nao produz mudanca — ver parse_webhook_event.
_MERCADOPAGO_STATUS_TRANSLATION = {
    "approved": "paid",
    "rejected": "failed",
    "cancelled": "failed",
    # Antifraude segurou para analise. So aparece com CARTAO — pix nao passa
    # por analise. Antes disto nao tinha traducao e virava
    # PaymentWebhookPayloadError, ou seja: o "em analise" ficava
    # indistinguivel de corpo malformado no log.
    "in_process": "in_review",
    "refunded": "refunded",
    "charged_back": "refunded",
}

# O mesmo dicionario nao serve para ler o ESTADO de um pagamento, e a
# diferenca esta no `pending`:
#
#   no WEBHOOK  `pending` nao produz mudanca — o pedido ja esta pending, e
#               traduzi-lo faria uma transicao de X para X.
#   no ESTADO   `pending` e um estado legitimo (o pix esperando ser pago), e
#               precisa ter nome.
#
# Duas funcoes leem o estado e por isso usam este: `_mercadopago_intent` (a
# resposta da criacao) e `fetch_payment` (a consulta ao pagamento). As duas
# perguntam "em que pe esta esta cobranca", nao "o que mudou".
_MERCADOPAGO_PAYMENT_STATUS_TRANSLATION = {
    "pending": "pending",
    "in_process": "in_review",
    "approved": "paid",
    "rejected": "failed",
    "cancelled": "failed",
    # Os dois so aparecem para quem CONSULTA um pagamento antigo — cobranca
    # recem-criada nunca nasce assim. Sem eles, `fetch_payment` devolveria
    # "status sem traducao" para um pagamento ja estornado, e o estorno
    # automatico trataria como falha o caso em que nao ha mais nada a fazer.
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
    resposta. Erro DELES (ou da chamada), nao um bug de configuracao nossa.

    `provider_error_code` e o identificador que o gateway devolveu no corpo
    do erro quando devolveu algum ("bad_request", "2062", ...). E o codigo do
    catalogo DELES, nunca a mensagem crua: a mensagem pode ecoar o e-mail de
    quem pagou e por isso fica so no log, enquanto o codigo pode ser
    mostrado ao cliente e citado num chamado de suporte.
    """

    def __init__(self, message: str, *, provider_error_code: str | None = None):
        super().__init__(message)
        self.provider_error_code = provider_error_code


class PaymentGatewayUnavailableError(PaymentGatewayError):
    """Timeout, falha de rede ou 5xx do gateway. Vale tentar de novo depois."""


class PaymentGatewayCredentialError(PaymentGatewayError):
    """Gateway respondeu 401/403: token invalido, revogado ou de outra
    conta. Nao adianta tentar de novo sem trocar a credencial."""


class PaymentNotFoundError(PaymentGatewayError):
    """Gateway respondeu 404: o id de pagamento informado nao existe la."""


@dataclass(frozen=True)
class CardPaymentInput:
    """O que so o cartao precisa, gerado no NAVEGADOR.

    O numero do cartao nunca passa por este backend: o front tokeniza com o
    SDK do Mercado Pago e manda so o `token`, que e de uso unico e vida
    curta. Se algum dia aparecer PAN, CVV ou validade nesta classe, a
    integracao saiu do padrao de tokenizacao e o perimetro de PCI mudou.
    """

    token: str
    # Bandeira que o SDK resolveu ("visa", "master", "elo"). NAO e o
    # `payment_method` da casa ("credit_card") — sao vocabularios diferentes
    # e o Mercado Pago quer o dele.
    payment_method_id: str
    issuer_id: str | None = None
    # CPF do portador. Atravessa para o gateway e NAO e persistido em lugar
    # nenhum daqui.
    payer_document_type: str | None = None
    payer_document_number: str | None = None
    # Preenchido SO na cobranca com cartao salvo: e o "customer" do Mercado
    # Pago dono do cartao. Sem ele o gateway recusa um token que nasceu de
    # um `card_id`, porque o cartao pertence ao customer e nao ao avulso.
    provider_customer_id: str | None = None


@dataclass(frozen=True)
class PaymentIntent:
    """O que o gateway devolve ao criar uma cobranca.

    `payment_status` e o campo que o pix nunca precisou: a cobranca pix nasce
    sempre `pending` e o veredito chega por webhook, entao ler a resposta era
    inutil. **Cartao responde no proprio POST** — aprovado, recusado ou em
    analise —, e um `rejected` dentro de um HTTP 201 nao e excecao para o
    codigo. Sem este campo, cartao recusado viraria pedido pendente eterno.
    """

    provider: str
    provider_payment_id: str
    # Ja em PAYMENT_STATUSES.
    payment_status: str
    # Para onde mandar o cliente. Pix costuma vir com qr_code em vez de url.
    checkout_url: str | None = None
    qr_code: str | None = None
    # Status cru do gateway, para log e para o front distinguir motivos de
    # recusa ("cc_rejected_insufficient_amount" pede coisa diferente do
    # cliente que "cc_rejected_bad_filled_security_code").
    raw_status: str | None = None
    raw_status_detail: str | None = None


@dataclass(frozen=True)
class GatewayPayment:
    """Em que pe uma cobranca esta NO GATEWAY, agora.

    Existe para quem vai encerrar uma cobranca nao decidir pela copia local:
    `orders.payment_status` e o ultimo webhook que chegou, e o webhook pode
    estar em voo. Cancelar uma cobranca que acabou de ser aprovada, ou
    estornar uma que nunca foi paga, sao os dois erros que essa defasagem
    produz — e os dois voltam como 4xx do gateway.
    """

    # Ja em PAYMENT_STATUSES. None quando o status deles nao tem traducao
    # aqui (um estado novo que eles inventem): quem chama nao age no escuro.
    payment_status: str | None
    raw_status: str | None
    # Quanto ja voltou para o cliente, no total. Diferente de zero num
    # pagamento que ainda esta `paid` significa estorno PARCIAL — feito no
    # painel deles, ou por esta plataforma.
    refunded_amount: Decimal = Decimal("0")


@dataclass(frozen=True)
class RefundResult:
    """O que o gateway devolveu ao mandar o dinheiro de volta."""

    provider_refund_id: str | None
    amount: Decimal
    # True so quando o gateway diz que o dinheiro JA saiu da conta do
    # restaurante. O estorno deles tem status proprio, e `in_process` existe:
    # tratar isso como concluido marcaria o pedido como `refunded` antes de
    # o dinheiro ter se movido. Quando for False, quem confirma e o webhook.
    settled: bool
    raw_status: str | None = None


@dataclass(frozen=True)
class SavedCardData:
    """O que o gateway devolve ao pendurar um cartao num customer.

    E deliberadamente magro: id opaco, bandeira, quatro digitos e validade.
    A resposta do Mercado Pago traz mais campos (primeiros seis digitos,
    nome do portador, dados do emissor); **nada disso e lido aqui**, para
    nao existir caminho pelo qual um dado a mais chegue ao banco por
    descuido de quem escrever o INSERT depois.
    """

    provider_card_id: str
    brand: str
    last_four_digits: str
    expiration_month: int | None = None
    expiration_year: int | None = None


@dataclass(frozen=True)
class MercadopagoError:
    """O que o Mercado Pago conta sobre um erro no CORPO da resposta.

    O status HTTP sozinho nao diagnostica nada: um 500 deles pode ser
    instabilidade do lado deles, o `payer.email` recusado ou a chave de
    idempotencia repetida com um corpo diferente — e as tres coisas so se
    distinguem pelo `error`/`message`/`cause` que vem no corpo.
    """

    # Slug generico: "bad_request", "internal_error", ...
    error: str | None
    # Texto livre deles, ja com o e-mail do pagador mascarado e truncado.
    message: str | None
    # `cause` deles, achatado em "code=X description=Y; code=..." — e o campo
    # mais especifico que eles tem sobre o motivo da recusa.
    causes: str | None
    # O identificador mais especifico disponivel (primeiro `cause.code`, ou o
    # `error` quando nao ha causa nenhuma). E o que vai para o cliente.
    code: str | None


@dataclass(frozen=True)
class PaymentWebhookEvent:
    """Uma notificacao do gateway, ja traduzida."""

    # Id do EVENTO (nao do pagamento). E a chave de idempotencia do webhook:
    # gateways reenviam a mesma notificacao ate receber 2xx.
    event_id: str
    provider_payment_id: str
    # Ja em PAYMENT_STATUSES: "paid", "in_review", "failed" ou "refunded".
    payment_status: str
    # O status cru do gateway, guardado so para log/depuracao.
    raw_status: str | None = None
    # Quanto do pagamento ja voltou para o cliente, no total.
    #
    # E o UNICO sinal de estorno PARCIAL: no Mercado Pago ele mantem o
    # pagamento em `approved`, entao `payment_status` continua "paid" e o
    # webhook, sozinho, nao tem como saber que dinheiro voltou.
    refunded_amount: Decimal = Decimal("0")


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
    previous_payment_id: str | None = None,
    card: CardPaymentInput | None = None,
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

    `previous_payment_id` e a cobranca que esta tentativa vem SUBSTITUIR —
    preenchido so quando a anterior foi recusada. E o que faz a chave de
    idempotencia mudar e uma cobranca nova nascer; ver
    _mercadopago_idempotency_key.
    """
    if provider == SANDBOX_PROVIDER:
        return _create_sandbox_payment(order_id, payment_method)

    if provider == MERCADOPAGO_PROVIDER:
        if not access_token:
            raise PaymentProviderNotConfiguredError(
                "Restaurante sem credencial do Mercado Pago cadastrada para "
                "o ambiente atual: ver scripts/register_restaurant_payment_credential.py"
            )
        if payment_method not in MERCADOPAGO_SUPPORTED_PAYMENT_METHODS:
            raise PaymentProviderNotConfiguredError(
                f"Mercado Pago: metodo de pagamento '{payment_method}' nao suportado "
                f"(aceitos: {', '.join(MERCADOPAGO_SUPPORTED_PAYMENT_METHODS)})"
            )
        if not payer_email:
            raise PaymentProviderNotConfiguredError(
                "Mercado Pago exige e-mail do pagador e nenhum foi informado"
            )

        body = _mercadopago_body(
            amount=amount,
            description=description,
            payment_method=payment_method,
            payer_email=payer_email,
            card=card,
            application_fee=application_fee,
        )
        payload = _call_mercadopago(
            method="POST",
            path="/v1/payments",
            access_token=access_token,
            json_body=body,
            extra_headers={
                "X-Idempotency-Key": _mercadopago_idempotency_key(
                    order_id, body, previous_payment_id
                )
            },
        )
        return _mercadopago_intent(payload)

    raise PaymentProviderUnknownError(f"Provider de pagamento desconhecido: {provider}")


def _mercadopago_body(
    *,
    amount: Decimal,
    description: str,
    payment_method: str,
    payer_email: str,
    card: CardPaymentInput | None,
    application_fee: Decimal | None,
) -> dict:
    """Corpo do POST /v1/payments, que e diferente por metodo.

    Duas montagens separadas e nao uma com `if` dentro: os corpos tem campos
    obrigatorios distintos, e uma funcao unica com metade dos campos
    condicionais esconderia justamente o que muda entre eles.
    """
    body = {
        "transaction_amount": float(amount),
        "description": description,
        "payer": {"email": payer_email},
    }
    if application_fee is not None:
        body["application_fee"] = float(application_fee)

    if payment_method == "pix":
        body["payment_method_id"] = "pix"
        return body

    if card is None:
        raise PaymentProviderNotConfiguredError(
            "Cobranca de cartao exige o token gerado no navegador e nenhum "
            "foi informado"
        )

    # `payment_method_id` aqui e a BANDEIRA ("visa", "master"), resolvida
    # pelo SDK no navegador — nao o "credit_card" do nosso vocabulario.
    body["payment_method_id"] = card.payment_method_id
    body["token"] = card.token
    body["installments"] = CARD_INSTALLMENTS
    # Captura automatica. `binary_mode` fica FALSO de proposito: verdadeiro
    # forcaria approved/rejected e mataria o "em analise" junto — e mataria
    # tambem o desafio 3DS, se um dia ele entrar.
    body["capture"] = True
    body["binary_mode"] = False
    if card.issuer_id:
        body["issuer_id"] = card.issuer_id
    if card.payer_document_type and card.payer_document_number:
        body["payer"]["identification"] = {
            "type": card.payer_document_type,
            "number": card.payer_document_number,
        }
    # Cartao SALVO: o pagador deixa de ser um e-mail avulso e passa a ser o
    # customer dono do cartao. O `token` continua obrigatorio e continua
    # vindo do navegador — a diferenca e que ele nasceu de um `card_id` mais
    # o CVV, em vez do numero digitado. Sem estes dois campos o Mercado Pago
    # recusa esse token: para ele o cartao pertence ao customer.
    if card.provider_customer_id:
        body["payer"]["type"] = "customer"
        body["payer"]["id"] = card.provider_customer_id
    return body


def _mercadopago_intent(payload: dict) -> PaymentIntent:
    """Le a resposta da criacao — INCLUSIVE o veredito.

    O `status` era descartado aqui. Para pix nao fazia diferenca (a cobranca
    nasce sempre `pending`), mas cartao responde no proprio POST, e ignorar a
    resposta transformava cartao recusado em pedido pendente para sempre.
    """
    transaction_data = (payload.get("point_of_interaction") or {}).get("transaction_data") or {}
    raw_status = payload.get("status")
    payment_status = _MERCADOPAGO_PAYMENT_STATUS_TRANSLATION.get(raw_status)

    if payment_status is None:
        # Status que eles inventaram depois desta linha ser escrita. `pending`
        # e a queda SEGURA: o pedido continua cobravel, o lojista continua
        # bloqueado, e o webhook corrige quando o desfecho de verdade chegar.
        # Qualquer outra escolha erra para o lado de liberar comida.
        logger.warning(
            "[Pagamento][mercadopago] status de criacao sem traducao: %s", raw_status
        )
        payment_status = "pending"

    return PaymentIntent(
        provider=MERCADOPAGO_PROVIDER,
        provider_payment_id=str(payload["id"]),
        payment_status=payment_status,
        checkout_url=transaction_data.get("ticket_url"),
        qr_code=transaction_data.get("qr_code"),
        raw_status=raw_status,
        raw_status_detail=payload.get("status_detail"),
    )


def fetch_payment(
    *,
    provider: str,
    access_token: str | None,
    provider_payment_id: str,
) -> GatewayPayment | None:
    """Le o estado atual da cobranca no gateway.

    `None` significa **"este provider nao sabe dizer"**, e nao "nao existe".
    O sandbox nao guarda estado nenhum: a unica coisa que muda o pagamento
    dele e um webhook que nos mesmos disparamos, entao a copia local JA e a
    verdade e nao ha o que consultar. Quem chama trata o None caindo para
    `orders.payment_status` — ver PaymentRefundService.

    Pagamento que o gateway nao encontra levanta PaymentNotFoundError, que e
    coisa diferente e nao pode virar `None`: id gravado aqui que nao existe
    la e divergencia de dado, e precisa aparecer.
    """
    if provider == SANDBOX_PROVIDER:
        return None

    if provider != MERCADOPAGO_PROVIDER:
        raise PaymentProviderUnknownError(f"Provider de pagamento desconhecido: {provider}")

    if not access_token:
        raise PaymentProviderNotConfiguredError(
            "Restaurante sem credencial do Mercado Pago cadastrada para o "
            "ambiente atual: impossivel consultar o pagamento"
        )

    payload = _call_mercadopago(
        method="GET",
        path=f"/v1/payments/{provider_payment_id}",
        access_token=access_token,
    )
    raw_status = payload.get("status")
    return GatewayPayment(
        payment_status=_MERCADOPAGO_PAYMENT_STATUS_TRANSLATION.get(raw_status),
        raw_status=raw_status,
        refunded_amount=_refunded_amount(payload),
    )


def cancel_payment(
    *,
    provider: str,
    access_token: str | None,
    provider_payment_id: str,
) -> None:
    """Mata uma cobranca que ainda NAO capturou dinheiro.

    E o caminho do pix com o QR gerado e nao pago, e do cartao ainda em
    analise do antifraude. **Nao ha dinheiro voltando** — a cobranca deixa
    de existir, e o cliente nao consegue mais paga-la. Para cobranca ja
    aprovada quem serve e `refund_payment`, e o Mercado Pago recusa esta
    chamada com 4xx nesse caso (ver o docstring do modulo).

    Cancelar o pix aberto de um pedido cancelado nao e higiene: sem isso o
    cliente paga, do lado dele, um pedido que ninguem vai produzir — e o
    webhook chega num pedido ja terminal, que e a corrida tratada em
    `PaymentService._refund_payment_on_terminal_order`.

    Sucesso e a ausencia de excecao. Nao ha o que devolver: o unico desfecho
    possivel de um cancelamento aceito e a cobranca cancelada.
    """
    if provider == SANDBOX_PROVIDER:
        # Nao ha cobranca de verdade para matar. Retornar (em vez de
        # levantar) e o que mantem o fluxo inteiro demonstravel no sandbox,
        # que e a razao de ele existir.
        return

    if provider != MERCADOPAGO_PROVIDER:
        raise PaymentProviderUnknownError(f"Provider de pagamento desconhecido: {provider}")

    if not access_token:
        raise PaymentProviderNotConfiguredError(
            "Restaurante sem credencial do Mercado Pago cadastrada para o "
            "ambiente atual: impossivel cancelar a cobranca"
        )

    _call_mercadopago(
        method="PUT",
        path=f"/v1/payments/{provider_payment_id}",
        access_token=access_token,
        json_body={"status": "cancelled"},
    )


def refund_payment(
    *,
    provider: str,
    access_token: str | None,
    provider_payment_id: str,
) -> RefundResult:
    """Devolve INTEGRALMENTE o dinheiro de uma cobranca aprovada.

    Corpo vazio de proposito: no Mercado Pago um `POST .../refunds` sem
    `amount` estorna o que restar do pagamento. Mandar o valor calculado
    daqui abriria a chance de divergir do que eles capturaram de fato — o
    total do pedido nao e necessariamente o valor da cobranca depois de um
    estorno parcial feito no painel deles.

    **Estorno parcial nao tem funcao aqui**, e a ausencia e deliberada:
    quem cancela um pedido devolve o pedido inteiro. Devolucao de parte do
    valor continua sendo operacao do painel do Mercado Pago, e chega aqui
    pelo webhook (`PaymentService._apply_partial_refund`).

    A chave de idempotencia sai do id do PAGAMENTO e e estavel para sempre:
    ao contrario da criacao de cobranca (ver _mercadopago_idempotency_key),
    aqui repetir a chamada NUNCA e uma operacao nova. Um timeout na ida
    seguido de um retry nao pode virar dois estornos.
    """
    if provider == SANDBOX_PROVIDER:
        # Mesmo motivo do cancel: sem isto o sandbox nao consegue demonstrar
        # o cancelamento de pedido pago, que e justamente o fluxo novo.
        return RefundResult(
            provider_refund_id=f"sandbox-refund-{provider_payment_id}",
            amount=Decimal("0"),
            settled=True,
            raw_status="approved",
        )

    if provider != MERCADOPAGO_PROVIDER:
        raise PaymentProviderUnknownError(f"Provider de pagamento desconhecido: {provider}")

    if not access_token:
        raise PaymentProviderNotConfiguredError(
            "Restaurante sem credencial do Mercado Pago cadastrada para o "
            "ambiente atual: impossivel estornar o pagamento"
        )

    payload = _call_mercadopago(
        method="POST",
        path=f"/v1/payments/{provider_payment_id}/refunds",
        access_token=access_token,
        json_body={},
        extra_headers={"X-Idempotency-Key": f"refund:{provider_payment_id}"},
    )
    return _mercadopago_refund_result(payload)


def _mercadopago_refund_result(payload: dict) -> RefundResult:
    """Le a resposta do estorno — inclusive quando ela nao e um desfecho.

    O estorno deles tem status proprio (`approved`, `in_process`,
    `rejected`), e so `approved` significa que o dinheiro saiu. Um
    `in_process` lido como sucesso marcaria o pedido `refunded` com o
    dinheiro ainda na conta do restaurante; o webhook e quem fecha esse
    caso.

    Status ausente conta como NAO concluido, pela mesma regra que
    `_mercadopago_intent` usa para status sem traducao: a queda segura e a
    que nao declara dinheiro devolvido sem prova.
    """
    raw_status = payload.get("status")
    if raw_status is not None and raw_status != "approved":
        logger.warning(
            "[Pagamento][mercadopago] estorno aceito mas nao concluido status=%s",
            raw_status,
        )
    return RefundResult(
        provider_refund_id=str(payload["id"]) if payload.get("id") else None,
        amount=_decimal_or_zero(payload.get("amount"), "amount"),
        settled=raw_status == "approved",
        raw_status=raw_status,
    )


def find_or_create_gateway_customer(*, access_token: str, email: str) -> str:
    """O id do "customer" do Mercado Pago para este e-mail, criando se preciso.

    BUSCA ANTES DE CRIAR, e a ordem nao e otimizacao: o Mercado Pago recusa
    um segundo customer com o mesmo e-mail na mesma conta. Criar primeiro e
    tratar o erro funcionaria, mas obrigaria a distinguir "ja existe" de
    "e-mail invalido" pelo texto da mensagem deles — que muda sem aviso.

    A busca depois do POST cobre a CORRIDA: duas abas salvando um cartao ao
    mesmo tempo, a primeira cria e a segunda leva o 400. Aqui a segunda
    busca de novo e acha o que a primeira criou, em vez de estourar um erro
    que o cliente nao tem como resolver.
    """
    existing = _search_gateway_customer(access_token=access_token, email=email)
    if existing is not None:
        return existing

    try:
        payload = _call_mercadopago(
            method="POST",
            path="/v1/customers",
            access_token=access_token,
            json_body={"email": email},
        )
    except PaymentGatewayError:
        recovered = _search_gateway_customer(access_token=access_token, email=email)
        if recovered is None:
            raise
        return recovered

    customer_id = payload.get("id")
    if not customer_id:
        raise PaymentGatewayUnavailableError(
            "Mercado Pago criou o customer sem devolver id"
        )
    return str(customer_id)


def _search_gateway_customer(*, access_token: str, email: str) -> str | None:
    """None quando a conta do restaurante ainda nao conhece este e-mail."""
    payload = _call_mercadopago(
        method="GET",
        path=f"/v1/customers/search?email={quote(email)}",
        access_token=access_token,
    )
    results = payload.get("results") or []
    if not results:
        return None
    found = results[0].get("id")
    return str(found) if found else None


def save_card(*, access_token: str, provider_customer_id: str, token: str) -> SavedCardData:
    """Pendura no customer o cartao que o navegador tokenizou.

    `token` e de uso unico e vida curta, e e o UNICO jeito de o cartao
    chegar aqui: o numero foi digitado no formulario do SDK deles, no
    navegador, e nunca passou por este processo.

    **Isto nao cobra nada e nao valida saldo.** O cartao so e testado de
    verdade na primeira cobranca — decisao tomada em 25/08/2026, e o motivo
    esta em docs/cartao-salvo.md.
    """
    payload = _call_mercadopago(
        method="POST",
        path=f"/v1/customers/{provider_customer_id}/cards",
        access_token=access_token,
        json_body={"token": token},
    )
    return _saved_card_data(payload)


def delete_saved_card(
    *, access_token: str, provider_customer_id: str, provider_card_id: str
) -> None:
    """Apaga o cartao na conta do restaurante no Mercado Pago.

    404 e tratado como SUCESSO: o cartao ja nao esta la, que e exatamente o
    estado que se queria. Levantar erro faria uma remocao repetida — dois
    cliques, um retry de rede — travar para sempre uma linha que o cliente
    quer ver sumir.
    """
    try:
        _call_mercadopago(
            method="DELETE",
            path=f"/v1/customers/{provider_customer_id}/cards/{provider_card_id}",
            access_token=access_token,
        )
    except PaymentNotFoundError:
        logger.info(
            "[Pagamento][mercadopago] cartao ja nao existia no gateway card_id=%s",
            provider_card_id,
        )


def _saved_card_data(payload: dict) -> SavedCardData:
    """Le SO os cinco campos que a tabela guarda, e recusa o resto.

    A resposta deles traz `first_six_digits`, nome do portador e dados do
    emissor. Nenhum e lido: o que nao e extraido aqui nao tem como ser
    gravado por engano depois.
    """
    provider_card_id = payload.get("id")
    last_four = payload.get("last_four_digits")
    brand = (payload.get("payment_method") or {}).get("id")
    if not provider_card_id or not last_four or not brand:
        raise PaymentGatewayUnavailableError(
            "Mercado Pago salvou o cartao sem id, bandeira ou ultimos digitos"
        )
    return SavedCardData(
        provider_card_id=str(provider_card_id),
        brand=str(brand),
        last_four_digits=str(last_four),
        expiration_month=payload.get("expiration_month"),
        expiration_year=payload.get("expiration_year"),
    )


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


def _create_sandbox_payment(order_id: uuid.UUID, payment_method: str) -> PaymentIntent:
    # A recusa por metodo tem que estar AQUI e nao so no ramo do Mercado
    # Pago: o sandbox e o provider PADRAO (PAYMENT_PROVIDER), e antes disto
    # ele nem recebia `payment_method` — devolvia intent valido para
    # `credit_card`, o webhook marcava como pago e a comanda imprimia. O
    # fluxo inteiro parecia funcionar sem dinheiro nenhum ter existido.
    if payment_method not in SANDBOX_SUPPORTED_PAYMENT_METHODS:
        raise PaymentProviderNotConfiguredError(
            f"Sandbox nao simula '{payment_method}': cartao so se testa contra a "
            "credencial de teste do Mercado Pago (PAYMENT_PROVIDER=mercadopago)"
        )
    # Id derivado do pedido, nao aleatorio: chamar duas vezes para o mesmo
    # pedido devolve a mesma cobranca, que e como um gateway com
    # idempotencia se comporta.
    return PaymentIntent(
        provider=SANDBOX_PROVIDER,
        provider_payment_id=f"sandbox-{order_id}",
        payment_status="pending",
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
        # `secret` vem de RestaurantPaymentCredential.webhook_secret_encrypted
        # (por restaurante), resolvido por quem chama — nao existe mais uma
        # MERCADOPAGO_WEBHOOK_SECRET global para citar aqui.
        raise PaymentProviderNotConfiguredError(
            "Segredo do webhook do Mercado Pago nao cadastrado para este "
            "restaurante: o webhook nao pode ser verificado."
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
        # Opcional no corpo do sandbox, para o estorno parcial ser
        # exercitavel sem depender do Mercado Pago responder — e o mesmo
        # motivo de o sandbox existir.
        refunded_amount=_refunded_amount(
            {"transaction_amount_refunded": payload.get("refunded_amount")}
        ),
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
        # pending/authorized, ou um status novo que o Mercado Pago venha a
        # inventar: nao ha mudanca de estado nosso para aplicar agora. Nao e
        # retentavel — o proprio gateway manda um novo webhook quando o
        # status mudar de verdade.
        raise PaymentWebhookPayloadError(
            f"Status do Mercado Pago sem traducao aplicavel: {raw_status}"
        )

    return PaymentWebhookEvent(
        event_id=str(event_id),
        provider_payment_id=str(provider_payment_id),
        payment_status=payment_status,
        raw_status=raw_status,
        refunded_amount=_refunded_amount(payload),
    )


def _refunded_amount(payload: dict) -> Decimal:
    """Quanto ja voltou para o cliente, segundo o gateway.

    `transaction_amount_refunded` e o UNICO sinal de estorno parcial: ele
    mantem o pagamento em `approved`, entao sem ler este campo o webhook
    conclui "ja estava paid, nada a fazer" e o dinheiro devolvido nao existe
    do nosso lado.

    Campo ausente e zero, nao erro: pagamento nunca estornado pode
    simplesmente nao traze-lo, e um estorno que a gente nao consegue ler nao
    pode derrubar a confirmacao de um pagamento que a gente consegue.
    """
    return _decimal_or_zero(
        payload.get("transaction_amount_refunded"), "transaction_amount_refunded"
    )


def _decimal_or_zero(raw, field_name: str) -> Decimal:
    """Numero de dinheiro vindo do gateway, ou zero.

    Compartilhada pelos dois campos de valor que lemos deles
    (`transaction_amount_refunded` na consulta e `amount` no estorno) porque
    a regra e a mesma: campo ausente ou ilegivel vale zero e rende um aviso,
    nunca uma excecao. Um valor que a gente nao consegue ler nao pode
    derrubar a operacao que a gente conseguiu fazer.
    """
    if raw is None:
        return Decimal("0")
    try:
        return Decimal(str(raw))
    except (ArithmeticError, TypeError, ValueError):
        logger.warning("[Pagamento][mercadopago] %s ilegivel: %r", field_name, raw)
        return Decimal("0")


def _call_mercadopago(
    *,
    method: str,
    path: str,
    access_token: str,
    json_body: dict | None = None,
    extra_headers: dict[str, str] | None = None,
) -> dict:
    """POST/GET/PUT/DELETE autenticado na API do Mercado Pago.

    Nunca loga o access_token nem o corpo de uma resposta de SUCESSO: o
    primeiro e credencial da conta do restaurante, o segundo traz o dado de
    quem pagou. Chamada que deu certo rende uma linha so (metodo, path,
    status, latencia).

    Chamada que deu ERRADO rende uma segunda linha com o `error`, o
    `message` e o `cause` que eles mandaram — sem isso o log tem o status
    HTTP e mais nada, e "status=500" nao distingue instabilidade deles de
    `payer.email` recusado de chave de idempotencia repetida. O e-mail do
    pagador sai mascarado desse texto (ver _redact_payer_data); o
    access_token nunca esteve nele.
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

    if status_code >= 400:
        error = _read_mercadopago_error(response)
        # A linha que faltava. O painel de "Atividade" deles tem o detalhe de
        # cada chamada, mas depender dele significa nao conseguir depurar uma
        # falha sem sair do nosso log — e sem o codigo da causa nao ha nem o
        # que informar num chamado de suporte.
        logger.warning(
            "[Pagamento][mercadopago] erro method=%s path=%s status=%s "
            "error=%s code=%s message=%s cause=%s",
            method,
            path,
            status_code,
            error.error or "-",
            error.code or "-",
            error.message or "-",
            error.causes or "-",
        )
        # A mensagem da excecao leva o CODIGO, nunca o texto deles: ele chega
        # ao cliente e pode ecoar o e-mail do pagador que mandamos.
        raise _mercadopago_error_for_status(status_code, method, path, error)

    # Corpo vazio e resposta VALIDA para o DELETE de cartao (eles ora
    # devolvem o recurso apagado, ora um 204 pelado). Tratar isso como erro
    # faria uma remocao bem-sucedida virar 502 na cara do cliente, e a
    # linha continuaria no nosso banco.
    if not response.content:
        return {}

    try:
        return response.json()
    except ValueError as exc:
        raise PaymentGatewayUnavailableError(
            "Mercado Pago respondeu um corpo que nao e JSON"
        ) from exc


def _mercadopago_idempotency_key(
    order_id: uuid.UUID,
    body: dict,
    previous_payment_id: str | None,
) -> str:
    """Chave de idempotencia desta TENTATIVA de cobranca.

    O Mercado Pago devolve a MESMA cobranca quando a mesma chave chega de
    novo. Isso e exatamente o que se quer num retry — um timeout na ida, ou
    o cliente clicando duas vezes em "pagar", nao pode virar duas cobrancas
    pix abertas para o mesmo pedido. So que a chave era `str(order_id)`,
    constante para sempre, e isso tratava como "a mesma cobranca" duas
    coisas que nao sao:

      - RETRY DE UMA COBRANCA RECUSADA. Reenviar a chave da tentativa
        recusada devolve a propria cobranca recusada, e o pedido nunca mais
        teria como ser pago. Dai `previous_payment_id`: quem chama informa
        qual cobranca esta sendo substituida, e a chave muda.
      - TENTATIVA COM O CORPO DIFERENTE. Entre uma tentativa e outra o total
        do pedido pode ter mudado, ou o cliente fez login e o `payer.email`
        deixou de ser o sintetico de convidado. Mesma chave com corpo
        diferente e CONFLITO de idempotencia, nao retry — e conflito e uma
        das coisas que o Mercado Pago responde com erro. Dai o corpo inteiro
        entrar no calculo: corpo diferente, chave diferente, cobranca nova.

    O que continua igual: com o pagamento ainda `pending` e o mesmo corpo, a
    chave se repete e a cobranca volta a mesma — a garantia original.

    O `order_id` fica no comeco da chave de proposito: e o que permite achar
    a tentativa no painel de "Atividade" deles a partir do pedido.
    """
    material = json.dumps(
        {"body": body, "previous_payment_id": previous_payment_id},
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]
    return f"{order_id}:{digest}"


def _mercadopago_error_for_status(
    status_code: int,
    method: str,
    path: str,
    error: MercadopagoError,
) -> PaymentGatewayError:
    """Traduz o status de erro deles na excecao que descreve o que fazer."""
    if status_code in (401, 403):
        return PaymentGatewayCredentialError(
            f"Mercado Pago recusou a credencial do restaurante (status {status_code})",
            provider_error_code=error.code,
        )
    if status_code == 404:
        return PaymentNotFoundError(
            f"Mercado Pago: recurso nao encontrado ({method} {path})",
            provider_error_code=error.code,
        )
    if status_code >= 500:
        return PaymentGatewayUnavailableError(
            f"Mercado Pago com erro interno (status {status_code}, code={error.code or '-'})",
            provider_error_code=error.code,
        )
    # 400/422 etc.: a requisicao que MONTAMOS foi recusada (dado invalido,
    # chave de idempotencia em conflito).
    return PaymentGatewayError(
        f"Mercado Pago recusou a requisicao (status {status_code}, code={error.code or '-'})",
        provider_error_code=error.code,
    )


def _read_mercadopago_error(response) -> MercadopagoError:
    """Le `error`, `message` e `cause` do corpo de erro deles.

    Defensiva de proposito: corpo de erro e justamente o que menos segue
    contrato. Qualquer coisa que nao de para ler vira None e a chamada segue
    — deixar de levantar a excecao certa porque o corpo do erro veio
    estranho seria trocar um problema por outro pior.
    """
    try:
        body = response.json()
    except ValueError:
        return MercadopagoError(error=None, message=None, causes=None, code=None)
    if not isinstance(body, dict):
        return MercadopagoError(error=None, message=None, causes=None, code=None)

    first_cause_code, causes = _format_mercadopago_causes(body.get("cause"))
    error = body.get("error")
    message = body.get("message")
    return MercadopagoError(
        error=str(error) if error is not None else None,
        message=_redact_payer_data(str(message)) if message is not None else None,
        causes=causes,
        # `cause[].code` e especifico ("2062"); `error` e o balde generico
        # ("bad_request"). Prefere-se o especifico quando existe.
        code=first_cause_code or (str(error) if error is not None else None),
    )


def _format_mercadopago_causes(cause) -> tuple[str | None, str | None]:
    """Achata `cause` em (codigo da primeira, texto de todas).

    Eles mandam `cause` ora como lista de objetos, ora como um objeto so,
    ora com a descricao em texto puro — os tres formatos aparecem na
    documentacao e nas respostas reais.
    """
    if isinstance(cause, dict):
        cause = [cause]
    if not isinstance(cause, list):
        return None, None

    first_code = None
    parts = []
    for item in cause:
        if isinstance(item, dict):
            code = item.get("code")
            description = item.get("description")
        else:
            code, description = None, item
        if first_code is None and code is not None:
            first_code = str(code)
        parts.append(
            f"code={code if code is not None else '-'} "
            f"description={_redact_payer_data(str(description))}"
        )
    return first_code, "; ".join(parts) or None


def _redact_payer_data(text: str) -> str:
    """Tira do texto o que identifica o pagador, antes de ele ir para o log.

    O unico dado do pagador que este arquivo manda para o Mercado Pago e o
    `payer.email` (ver o corpo montado em create_payment) — e e justamente
    ele que volta ecoado na mensagem quando e recusado. Mascarar so o
    e-mail, e nao tudo que pareca um identificador, e proposital: mascarar
    numeros levaria junto o id do pagamento e o codigo do erro, que sao
    exatamente o que se precisa ler no log.
    """
    redacted = _EMAIL_PATTERN.sub("[email]", text)
    if len(redacted) > MERCADOPAGO_ERROR_TEXT_MAX_CHARS:
        return redacted[:MERCADOPAGO_ERROR_TEXT_MAX_CHARS] + "..."
    return redacted


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
