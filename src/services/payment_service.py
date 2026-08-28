"""Regras de pagamento do pedido.

Duas operacoes:

- `start_online_payment`: cria a cobranca no gateway e guarda o id dele no
  pedido. E chamada pelo cliente depois de o pedido existir, e nao dentro
  de create_order, para que a chamada externa nao aconteca com a transacao
  do pedido aberta.

- `handle_webhook`: recebe a notificacao do gateway, confere a assinatura e
  aplica a mudanca de estado do pagamento. Idempotente pela mesma tabela da
  Fase 1 (`idempotency_keys`), usando o id do EVENTO como chave — gateways
  reenviam a mesma notificacao ate receber 2xx, e sem isso o mesmo
  pagamento entraria varias vezes no historico.

Politica de resposta do webhook: quase tudo que nao e "assinatura invalida"
responde 2xx. Erro 5xx faz o gateway reenviar em backoff por horas, e
reenviar nao conserta corpo malformado nem pagamento que nao existe aqui.
O que precisa de atencao humana vai para o log como warning.
"""

import logging
import uuid
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from src.core.config import settings
from src.integrations.payment_gateway import (
    MERCADOPAGO_CARD_MIN_AMOUNT,
    MERCADOPAGO_PROVIDER,
    CardPaymentInput,
    PaymentGatewayCredentialError,
    PaymentGatewayError,
    PaymentGatewayUnavailableError,
    PaymentNotFoundError,
    PaymentProviderNotConfiguredError,
    PaymentProviderUnknownError,
    PaymentWebhookPayloadError,
    create_payment,
    extract_provider_payment_id,
    parse_webhook_event,
    verify_webhook_signature,
)
from src.models.customer_model import Customer
from src.models.order_status_history_model import OrderStatusHistory
from src.repositories.customer_repository import CustomerRepository
from src.repositories.customer_saved_card_repository import CustomerSavedCardRepository
from src.repositories.order_repository import OrderRepository
from src.schemas.payment_schema import (
    PaymentConfigResponse,
    PaymentErrorCode,
    PaymentErrorDetail,
    StartPaymentRequest,
    StartPaymentResponse,
)
from src.services.commission import zero_commission_for_refund
from src.services.idempotency_service import IdempotencyService
from src.services.order_state_machine import (
    PAYMENT_HISTORY_PREFIX,
    TERMINAL_ORDER_STATUSES,
    ensure_payment_transition_allowed,
    payment_history_status,
)
from src.services.payment_credential_service import ActivePaymentCredential, PaymentCredentialService
from src.services.payment_refund_service import PaymentRefundService
from src.services.restaurant_service import RestaurantService
from src.utils.money import to_decimal
from src.utils.security import utcnow


logger = logging.getLogger("uvicorn.error")

WEBHOOK_ROUTE = "POST /payments/webhooks/{provider}"

# Estados de pagamento em que faz sentido criar (ou recriar) uma cobranca.
# `in_review` fica de FORA: com o antifraude analisando nao ha o que retentar,
# e uma segunda cobranca criaria duas analises para o mesmo pedido.
PAYABLE_STATUSES = ("pending", "failed")

# Formas de pagamento que exigem token gerado no navegador. `debit_card` NAO
# entra na v1: no Mercado Pago ele tem fluxo proprio (debito virtual) e 3DS
# obrigatorio de fato — parece vir de graca junto com o credito, e nao vem.
CARD_PAYMENT_METHODS = ("credit_card",)

# Os desfechos possiveis moram em PaymentErrorCode (payment_schema): a lista
# tem que sair no /openapi.json para o frontend escrever um texto por caso,
# e duas listas separadas sairiam de sincronia na primeira adicao.
#
# Mensagens prontas para o cliente ler. "Erro interno" nao diz a ninguem se
# vale esperar um minuto ou se e melhor ligar para o restaurante.
_GATEWAY_UNAVAILABLE_MESSAGE = (
    "Não foi possível gerar o pagamento agora. Tente de novo em alguns instantes."
)
_PAYMENT_UNAVAILABLE_MESSAGE = (
    "O pagamento online deste restaurante está indisponível no momento. "
    "Fale com o restaurante para combinar o pagamento."
)
_PAYMENT_REJECTED_MESSAGE = (
    "O provedor de pagamento recusou esta cobrança. "
    "Fale com o restaurante para concluir o pedido."
)
_LOGIN_REQUIRED_MESSAGE = (
    "Para pagar com cartão é preciso entrar na sua conta. "
    "Você também pode pagar com Pix ou na entrega."
)
_CARD_TOKEN_REQUIRED_MESSAGE = (
    "Não foi possível ler os dados do cartão. Preencha novamente."
)
_SAVED_CARD_NOT_FOUND_MESSAGE = (
    "Este cartão não está mais salvo na sua conta. "
    "Escolha outro cartão ou cadastre novamente."
)
_MINIMO_DO_CARTAO_EM_REAIS = f"{MERCADOPAGO_CARD_MIN_AMOUNT:.2f}".replace(".", ",")
_AMOUNT_BELOW_MINIMUM_MESSAGE = (
    f"Pedidos abaixo de R$ {_MINIMO_DO_CARTAO_EM_REAIS} não podem ser pagos com cartão. "
    "Você pode pagar com Pix ou na entrega."
)

# Mensagem propria para os codigos de erro do Mercado Pago que o CLIENTE pode
# ver, no mesmo espirito do que o front ja fazia com o `status_detail`.
#
# O `provider_error_code` sempre atravessou para a resposta, mas sozinho ele
# so serve para citar num chamado — quem esta com o pedido fechado le a
# mensagem, e ate 28/08/2026 ela era a mesma frase generica para todos. Foi
# assim que um pudim de R$ 0,01 virou "o provedor recusou esta cobranca":
# verdade, e inutil.
#
# A lista e CURTA de proposito, e o criterio das duas colunas nao e o mesmo:
#
#   1. o codigo tem que ser alcancavel pelo corpo que NOS montamos
#      (_mercadopago_body). "transaction_amount nao pode ser nulo" nao entra:
#      a coluna e NOT NULL e nao ha caminho ate ele;
#   2. a mensagem tem que mudar o que a PESSOA faz a seguir. Codigo que so
#      muda o diagnostico interno fica de fora e cai na frase generica, que
#      continua certa para ele — traduzir tudo trocaria uma frase inutil por
#      varias frases inuteis.
#
# Os textos originais sao os da tabela publicada pelo proprio Mercado Pago em
# mercadopago/cart-magento2, `src/MercadoPago/Core/Helper/Response.php`
# (PAYMENT_CREATION_ERRORS). Codigo desconhecido cai no _PAYMENT_REJECTED_MESSAGE.
#
# `retryable` NAO e decidido aqui, e isso e deliberado: ele sai do tipo da
# excecao (a familia de PaymentGatewayError), que e o que de fato diz se
# repetir a MESMA chamada tem chance. Deixar um codigo virar o `retryable`
# faria a mesma pergunta ter duas respostas em lugares diferentes.
_MERCADOPAGO_REJECTION_MESSAGES = {
    # "Already posted the same request in the last minute."
    "2001": (
        "Já recebemos esta tentativa de pagamento. "
        "Aguarde alguns instantes antes de tentar de novo."
    ),
    # "Customer not found." — o `payer.id` do cartao salvo nao existe nesta
    # conta do Mercado Pago (loja trocou de credencial, cadastro removido la).
    "2002": (
        "Este cartão salvo não vale mais neste restaurante. "
        "Escolha outro cartão ou cadastre novamente."
    ),
    # "Card Token not found." — o token do navegador tem vida curta e e de
    # uso unico; tela aberta ha muito tempo cai aqui.
    "2006": (
        "Os dados do cartão expiraram antes da cobrança. "
        "Preencha o cartão novamente."
    ),
    # "The customer can't be equal to the collector."
    "2060": (
        "Não é possível pagar com um cartão da mesma conta que recebe o pagamento. "
        "Use outro cartão."
    ),
    # "Invalid card_id for this payment_method_id."
    "3026": (
        "Não foi possível usar este cartão salvo. "
        "Escolha outro cartão ou cadastre novamente."
    ),
    # "Invalid payment_method_id." — a bandeira nao bate com o cartao.
    "3028": (
        "A bandeira deste cartão não foi reconhecida. "
        "Confira os dados ou use outro cartão."
    ),
    # "Invalid transaction_amount." — o caso do pedido de R$ 0,01. Continua
    # aqui mesmo com a trava de _ensure_amount_is_chargeable_on_card: ela usa
    # um piso nosso, e quem manda no piso e eles (ver MERCADOPAGO_CARD_MIN_AMOUNT).
    "4037": (
        f"O valor deste pedido não pode ser cobrado no cartão "
        f"(o mínimo é R$ {_MINIMO_DO_CARTAO_EM_REAIS}). "
        "Você pode pagar com Pix ou na entrega."
    ),
}


def _rejection_message(provider_error_code: str | None) -> str:
    """A frase que o cliente le quando o gateway recusou a cobranca."""
    if provider_error_code is None:
        return _PAYMENT_REJECTED_MESSAGE
    return _MERCADOPAGO_REJECTION_MESSAGES.get(provider_error_code, _PAYMENT_REJECTED_MESSAGE)


class PaymentService:
    def __init__(self, db: Session):
        self.db = db
        self.order_repository = OrderRepository(db)
        self.restaurant_service = RestaurantService(db)
        self.idempotency_service = IdempotencyService(db)
        self.payment_credential_service = PaymentCredentialService(db)
        self.customer_repository = CustomerRepository(db)
        self.saved_card_repository = CustomerSavedCardRepository(db)

    def get_payment_config(self, restaurant_slug: str) -> PaymentConfigResponse:
        """O que o navegador precisa para tokenizar um cartao deste restaurante.

        Publica, como o cardapio: a `public_key` e o unico dado do gateway que
        o Mercado Pago manda expor no frontend, e e por isso que ela e a unica
        coluna da credencial guardada em texto puro. Nada cifrado sai daqui.

        `card_enabled` responde a pergunta que o front faria de qualquer jeito
        — e responde ANTES de o cliente digitar o cartao, em vez de depois,
        com um 503.
        """
        restaurant = self.restaurant_service.get_active_restaurant(restaurant_slug)

        if settings.PAYMENT_PROVIDER != MERCADOPAGO_PROVIDER:
            # O sandbox nao processa cartao (ver SANDBOX_SUPPORTED_PAYMENT_METHODS)
            # e nao tem chave publica nenhuma para dar.
            return PaymentConfigResponse(
                provider=settings.PAYMENT_PROVIDER,
                public_key=None,
                card_enabled=False,
            )

        credential = self.payment_credential_service.get_active_credential(restaurant.id)
        return PaymentConfigResponse(
            provider=settings.PAYMENT_PROVIDER,
            public_key=credential.public_key if credential is not None else None,
            card_enabled=credential is not None,
        )

    def start_online_payment(
        self,
        restaurant_slug: str,
        tracking_token: str,
        payload: StartPaymentRequest | None = None,
        current_customer: Customer | None = None,
    ) -> StartPaymentResponse:
        """Cria a cobranca do pedido no gateway.

        Autorizacao pelo token de acompanhamento: quem tem o token e quem
        criou o pedido. Pedido de convidado tambem paga — mas so por pix; ver
        _resolve_card_input.

        `payload` so e necessario para cartao (traz o token gerado no
        navegador). Pix continua sendo um POST sem corpo, e isso e de
        proposito: o corpo opcional nao muda o contrato de quem ja integrou.

        Falha ao criar a cobranca sai como PaymentErrorDetail, separando o
        que vale tentar de novo do que nao vale — ver _create_payment_at_gateway.
        """
        restaurant = self.restaurant_service.get_active_restaurant(restaurant_slug)
        order = self.order_repository.get_order_by_tracking_token(restaurant.id, tracking_token)
        if order is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Pedido não encontrado"
            )
        # Pedido que ja acabou nao aceita cobranca nova, e esta linha custou
        # dinheiro de verdade antes de existir. `payment_status` sozinho nao
        # responde por ela: um pedido CANCELADO com a cobranca em `failed`
        # continua casando com PAYABLE_STATUSES, entao o cliente que voltasse
        # ao link do pedido criava uma cobranca — e pagava — por um pedido
        # que ninguem ia produzir. O dinheiro voltava depois, pelo webhook
        # (`_refund_payment_on_terminal_order`), mas a TARIFA do gateway nao
        # volta: R$ 3,58 no cartao, que o lojista come, mais um cliente
        # cobrado e estornado sem entender por que.
        #
        # A varredura `cancela_pedidos_sem_pagamento.py` transformou isso de
        # raro em rotina — todo pedido abandonado depois de uma recusa passa
        # a ser um pedido cancelado com `payment_status='failed'`.
        if order.status in TERMINAL_ORDER_STATUSES:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Pedido em '{order.status}' não recebe pagamento.",
            )
        if order.payment_flow != "online":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Este pedido é pago na entrega e não tem cobrança online.",
            )
        if order.payment_status not in PAYABLE_STATUSES:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Pagamento em '{order.payment_status}': não há o que cobrar.",
            )

        # Valores copiados ANTES do commit: depois dele o objeto do
        # SQLAlchemy esta expirado e cada atributo lido dispara um SELECT
        # novo — exatamente o que estamos tentando evitar aqui.
        restaurant_id = restaurant.id
        order_id = order.id
        amount = order.total
        payment_method = order.payment_method
        order_number = order.order_number

        # Uma cobranca RECUSADA nao se "retenta": ela se SUBSTITUI. Reenviar
        # a chave de idempotencia da tentativa recusada faria o gateway
        # devolver a propria cobranca recusada de volta, e o pedido nunca
        # mais teria como ser pago. Informar qual cobranca esta sendo
        # substituida e o que faz a proxima nascer com chave nova — ver
        # payment_gateway._mercadopago_idempotency_key.
        #
        # Com o pagamento ainda "pending" e o CONTRARIO: fica None de
        # proposito, para a chave se repetir e um segundo clique em "pagar"
        # devolver o mesmo pix em vez de abrir um segundo.
        previous_payment_id = (
            order.provider_payment_id if order.payment_status == "failed" else None
        )

        # A cobranca e sempre em nome do restaurante do pedido: busca a
        # credencial dele ANTES de falar com o gateway, nunca uma constante
        # global. `access_token` fica None quando o provider e "sandbox"
        # (que nao precisa de credencial nenhuma) ou quando o restaurante
        # ainda nao cadastrou a credencial do ambiente ativo — nesse
        # segundo caso, create_payment e quem recusa com 503.
        # Cartao e resolvido ANTES do commit e antes do gateway: recusar aqui
        # custa uma resposta, recusar depois custa uma cobranca criada.
        card = self._resolve_card_input(
            payment_method, payload, current_customer, restaurant_id, order_number
        )
        self._ensure_amount_is_chargeable_on_card(card, amount, order_number)

        access_token = None
        payer_email = None
        if settings.PAYMENT_PROVIDER == MERCADOPAGO_PROVIDER:
            credential = self.payment_credential_service.get_active_credential(restaurant_id)
            if credential is not None:
                access_token = credential.access_token
            # So resolve e-mail quando de fato vai chamar o Mercado Pago: o
            # sandbox nao usa, e e uma consulta a mais no banco por nada.
            payer_email = self._resolve_payer_email(order, current_customer)

        # Fecha a transacao de leitura ANTES de falar com o gateway. Sem
        # isso a conexao de banco fica presa durante um I/O externo que pode
        # levar segundos — com o pool cheio, a API inteira trava esperando
        # um gateway lento.
        self.db.commit()

        intent = self._create_payment_at_gateway(
            order_id=order_id,
            amount=amount,
            payment_method=payment_method,
            order_number=order_number,
            description=f"Pedido #{order_number}",
            access_token=access_token,
            payer_email=payer_email,
            previous_payment_id=previous_payment_id,
            card=card,
            # application_fee (corte da plataforma no split) fica de fora:
            # e um campo opcional que so passa a ser preenchido quando
            # existir contrato de marketplace com o restaurante.
        )

        try:
            order = self.order_repository.get_order_by_tracking_token(restaurant_id, tracking_token)
            if order is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail="Pedido não encontrado"
                )
            # Reconferido depois do I/O: um webhook pode ter chegado
            # enquanto esperavamos o gateway responder.
            #
            # A trava de pedido TERMINAL de propósito NAO se repete aqui, e a
            # assimetria e o ponto: a cobranca ja existe no gateway, e sair
            # sem `attach_payment_intent` a deixa orfa — sem
            # `provider_payment_id` gravado, nem o estorno automatico nem a
            # varredura conseguem acha-la, e o dinheiro do cliente fica
            # invisivel deste lado. Gravando, o pedido cai em
            # `list_orders_awaiting_refund` (terminal + cobranca viva) e a
            # maquina que ja existe devolve o dinheiro sozinha. A corrida que
            # isto cobre e o cancelamento acontecendo DURANTE a chamada ao
            # gateway, de segundos; o caso comum — cliente voltando ao link
            # de um pedido ja cancelado — morre na trava la de cima.
            if order.payment_status not in PAYABLE_STATUSES:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Pagamento em '{order.payment_status}': não há o que cobrar.",
                )
            self.order_repository.attach_payment_intent(
                order,
                provider=intent.provider,
                provider_payment_id=intent.provider_payment_id,
                payment_status=intent.payment_status,
            )
            self._record_synchronous_verdict(order, intent)
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

        return StartPaymentResponse(
            provider=intent.provider,
            provider_payment_id=intent.provider_payment_id,
            payment_status=intent.payment_status,
            checkout_url=intent.checkout_url,
            qr_code=intent.qr_code,
            status_detail=intent.raw_status_detail,
        )

    def _record_synchronous_verdict(self, order, intent) -> None:
        """Grava no historico o desfecho que veio no PROPRIO POST.

        Pix nao passa por aqui: a cobranca nasce `pending` e quem escreve o
        historico e o webhook. Cartao responde na hora, e sem esta linha o
        pedido teria `payment_status` mudado sem nenhum evento explicando
        quando nem por quem — o oposto do que a tabela existe para dar.

        `paid_at` tambem sai daqui pelo mesmo motivo: um cartao aprovado sem
        ele ficaria pago sem hora de pagamento.
        """
        if intent.payment_status == "pending":
            return

        if intent.payment_status == "paid":
            order.paid_at = utcnow()
        self.order_repository.create_status_history(
            OrderStatusHistory(
                order_id=order.id,
                status=payment_history_status(intent.payment_status),
                changed_by=f"gateway:{intent.provider}",
                note=f"status do gateway: {intent.raw_status} ({intent.raw_status_detail})",
            )
        )

    def _resolve_card_input(
        self,
        payment_method: str | None,
        payload: StartPaymentRequest | None,
        current_customer: Customer | None,
        restaurant_id: uuid.UUID,
        order_number: int | None = None,
    ) -> CardPaymentInput | None:
        """Monta o que so o cartao precisa, ou recusa o pedido de cobranca.

        **Cartao exige login, e nao e por comodidade.** O `payer.email` de um
        pedido de convidado seria o sintetico derivado do numero do pedido
        (ver _resolve_payer_email) — inocuo no pix, que nao valida pagador, e
        caro no cartao, onde ele entra na analise antifraude e ja rendeu
        recusa direta do gateway em teste. Convidado paga por pix ou na
        entrega.
        """
        if payment_method not in CARD_PAYMENT_METHODS:
            return None
        if current_customer is None:
            raise self._payment_error(
                status_code=status.HTTP_401_UNAUTHORIZED,
                code=PaymentErrorCode.LOGIN_REQUIRED,
                message=_LOGIN_REQUIRED_MESSAGE,
                retryable=False,
                cause=ValueError("cobranca de cartao sem cliente autenticado"),
                order_number=order_number,
            )
        if payload is None or payload.card is None:
            raise self._payment_error(
                status_code=status.HTTP_400_BAD_REQUEST,
                code=PaymentErrorCode.CARD_TOKEN_REQUIRED,
                message=_CARD_TOKEN_REQUIRED_MESSAGE,
                retryable=False,
                cause=ValueError("cobranca de cartao sem token do navegador"),
                order_number=order_number,
            )

        card = payload.card
        if card.saved_card_id is None:
            return CardPaymentInput(
                token=card.token,
                payment_method_id=card.payment_method_id,
                issuer_id=card.issuer_id,
                payer_document_type=card.payer_document_type,
                payer_document_number=card.payer_document_number,
            )

        saved = self._resolve_saved_card(
            card.saved_card_id, current_customer, restaurant_id, order_number
        )
        return CardPaymentInput(
            token=card.token,
            # A bandeira sai do que foi GRAVADO no cadastro, e nao do corpo:
            # o cliente nao tem por que escolher a bandeira de um cartao que
            # ja esta salvo, e o valor do banco nao pode divergir do cartao.
            payment_method_id=saved.brand,
            issuer_id=card.issuer_id,
            payer_document_type=card.payer_document_type,
            payer_document_number=card.payer_document_number,
            provider_customer_id=saved.profile.provider_customer_id,
        )

    def _ensure_amount_is_chargeable_on_card(
        self,
        card: CardPaymentInput | None,
        amount,
        order_number: int,
    ) -> None:
        """Recusa aqui o pedido abaixo do piso que o gateway cobra no cartao.

        **O caso concreto: um pudim de R$ 0,01.** O Mercado Pago recusou com
        `400 Invalid transaction_amount` (codigo 4037), o que virava um 502
        `payment_rejected` com a frase generica "o provedor recusou esta
        cobranca" — verdadeira e inutil, porque nao diz que o problema e o
        VALOR e nao o cartao. O cliente redigitava o cartao, tentava outro, e
        nenhum ia funcionar.

        Recusar antes de chamar o gateway compra duas coisas que a traducao
        do 4037 sozinha nao compra: uma chamada externa a menos no caminho de
        um desfecho ja conhecido, e um codigo proprio (`amount_below_minimum`)
        que o front liga ao botao certo — "pagar com Pix" — em vez de a um
        "tentar de novo" que nunca vai dar.

        **Nao substitui a traducao do 4037**, e as duas convivem de proposito:
        o piso e deles e pode subir sem aviso (ver MERCADOPAGO_CARD_MIN_AMOUNT).
        Quando subir, esta trava fica permissiva demais e a recusa volta a
        acontecer la — com mensagem propria, e nao com a frase generica de
        antes.

        Pix nao passa por aqui (`card is None`): a cobranca de R$ 0,01 em pix
        o Mercado Pago aceita, e um piso inventado por nos recusaria pedido
        que hoje funciona.
        """
        if card is None:
            return
        if settings.PAYMENT_PROVIDER != MERCADOPAGO_PROVIDER:
            return
        if to_decimal(amount) >= MERCADOPAGO_CARD_MIN_AMOUNT:
            return

        raise self._payment_error(
            status_code=status.HTTP_400_BAD_REQUEST,
            code=PaymentErrorCode.AMOUNT_BELOW_MINIMUM,
            message=_AMOUNT_BELOW_MINIMUM_MESSAGE,
            retryable=False,
            cause=ValueError(
                f"total {to_decimal(amount)} abaixo do minimo do cartao "
                f"({MERCADOPAGO_CARD_MIN_AMOUNT})"
            ),
            order_number=order_number,
        )

    def _resolve_saved_card(
        self,
        saved_card_id: uuid.UUID,
        current_customer: Customer,
        restaurant_id: uuid.UUID,
        order_number: int | None = None,
    ):
        """O cartao salvo desta pessoa NESTE restaurante, ou 404.

        As duas checagens sao a autorizacao inteira, e nenhuma e dispensavel:

        - **dono** — o repositorio ja casa o cartao com o `customer_id` do
          token, entao um UUID de cartao alheio nao resolve;
        - **restaurante** — um `card_id` so existe dentro da conta do
          Mercado Pago que o emitiu. Cobrar na loja B um cartao salvo na
          loja A daria 404 do gateway no meio do checkout, com a cobranca ja
          em andamento; recusar aqui custa uma resposta.

        Divergencia responde **404, e nao 403** — 403 confirmaria que aquele
        cartao existe em outra conta ou em outra loja.
        """
        saved = self.saved_card_repository.get_card_of_customer(
            current_customer.id, saved_card_id
        )
        if saved is None or saved.profile.restaurant_id != restaurant_id:
            raise self._payment_error(
                status_code=status.HTTP_404_NOT_FOUND,
                code=PaymentErrorCode.SAVED_CARD_NOT_FOUND,
                message=_SAVED_CARD_NOT_FOUND_MESSAGE,
                retryable=False,
                cause=ValueError("cartao salvo inexistente para este cliente/restaurante"),
                order_number=order_number,
            )
        # O ambiente do perfil nao e conferido aqui de proposito: virar
        # MERCADOPAGO_ENVIRONMENT muda a conta do gateway, e o perfil do
        # ambiente antigo simplesmente deixa de ser encontrado pelo
        # SavedCardService — a lista do cliente vem vazia e ele recadastra.
        return saved

    def _resolve_payer_email(self, order, current_customer: Customer | None = None) -> str:
        """E-mail exigido pelo Mercado Pago para criar a cobranca.

        O pedido nao guarda e-mail nenhum (so nome e telefone — ver
        Order.customer_name_snapshot/customer_phone_snapshot), entao ele sai
        de `customers` quando ha cliente, e de um sintetico quando nao ha.

        A ordem de preferencia importa e nao e obvia: quem esta pagando AGORA
        vem antes do dono do pedido. Um pedido pode ter nascido de convidado e
        a pessoa ter entrado na conta antes de pagar — e nesse caso o e-mail
        de verdade existe, mesmo com `order.customer_id` nulo.

        **O sintetico e para pix e so.** No cartao ele entra na analise
        antifraude do gateway e ja rendeu recusa direta em teste; por isso
        _resolve_card_input exige login antes de chegar aqui.
        """
        if current_customer is not None and current_customer.email:
            return current_customer.email
        if order.customer_id is not None:
            customer = self.customer_repository.get_by_id(order.customer_id)
            if customer is not None and customer.email:
                return customer.email
        return f"pedido-{order.order_number}@pederapidex.com"

    def handle_webhook(
        self,
        provider: str,
        raw_body: bytes,
        headers: dict[str, str],
    ) -> dict[str, Any]:
        """Confere a assinatura e aplica a mudanca de estado do pagamento.

        A assinatura do Mercado Pago e por RESTAURANTE (ver
        RestaurantPaymentCredential.webhook_secret_encrypted) — nao existe
        mais uma unica MERCADOPAGO_WEBHOOK_SECRET global para verificar
        "antes de tudo", porque antes de saber qual segredo usar precisamos
        saber de qual restaurante e o pagamento, e isso so vem do PROPRIO
        corpo do webhook (data.id). Ordem adotada:

          1. extract_provider_payment_id  so le o id do gateway no corpo,
             NENHUMA chamada externa nem ao banco. Corpo malformado ou
             provider desconhecido nunca chega a etapa 2.
          2. OrderRepository.get_order_by_provider_payment  SELECT indexado
             e local pelo (provider, provider_payment_id) lido no passo 1.
             So devolve o restaurante_id; nao muda nada nem custa uma
             chamada paga. Id que nao bate com pedido nenhum nosso para
             tudo aqui (ignored/unknown_payment), sem verificar assinatura
             nenhuma — nao ha o que proteger quando nao ha pedido para
             mudar de estado.
          3. resolve a credencial do restaurante achado no passo 2 e,
             SO ENTAO, verifica a assinatura com o webhook_secret DELE.
          4. so com assinatura valida e que este metodo segue para
             parse_webhook_event, que para o Mercado Pago faz o GET
             /v1/payments/{id} — a chamada de verdade, paga, ao gateway.

        O que a ordem antiga protegia (nao gastar uma consulta FORJADA na
        API do Mercado Pago) continua protegido: o unico passo que fala com
        o Mercado Pago de verdade (passo 4) continua atras da assinatura
        verificada. Os passos 1 e 2, que agora rodam antes, sao leitura
        local e barata (parse de JSON e SELECT por indice unico) — a unica
        coisa que um corpo forjado com um data.id chutado consegue provocar
        e um SELECT que nao acha nada. Nao ha efeito colateral, e nenhum
        dado sensivel novo e exposto: ja existia "ignored/unknown_payment"
        como resposta publica para esse caso antes desta mudanca.
        """
        try:
            provider_payment_id = extract_provider_payment_id(provider=provider, raw_body=raw_body)
        except PaymentProviderUnknownError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except PaymentWebhookPayloadError as exc:
            # 200 de proposito: reenviar nao conserta um corpo que nao
            # entendemos, e 5xx colocaria o gateway em retentativa por horas.
            logger.warning("[Pagamento] webhook ignorado provider=%s motivo=%s", provider, exc)
            return {"status": "ignored", "reason": "payload"}

        order = self.order_repository.get_order_by_provider_payment(
            provider,
            provider_payment_id,
        )
        if order is None:
            logger.warning(
                "[Pagamento] webhook sem pedido correspondente provider=%s payment_id=%s",
                provider,
                provider_payment_id,
            )
            return {"status": "ignored", "reason": "unknown_payment"}

        # So a partir daqui sabemos de qual restaurante e o pagamento. Busca
        # a credencial dele UMA VEZ SO: da o segredo para conferir a
        # assinatura (a seguir) e, se ela bater, o access_token para o GET
        # de status mais abaixo — nao ha necessidade de duas consultas ao
        # banco para a mesma linha.
        credential = None
        if provider == MERCADOPAGO_PROVIDER:
            credential = self.payment_credential_service.get_active_credential(order.restaurant_id)

        self._verify_signature(provider, credential, raw_body, headers)

        access_token = credential.access_token if credential is not None else None

        try:
            event = parse_webhook_event(provider=provider, raw_body=raw_body, access_token=access_token)
        except PaymentWebhookPayloadError as exc:
            logger.warning("[Pagamento] webhook ignorado provider=%s motivo=%s", provider, exc)
            return {"status": "ignored", "reason": "payload"}
        except PaymentNotFoundError as exc:
            # Nao retentavel: um 404 do gateway para o mesmo id nao vira
            # outra coisa se o Mercado Pago reenviar a notificacao de novo.
            logger.warning(
                "[Pagamento] pagamento nao encontrado no gateway provider=%s payment_id=%s motivo=%s",
                provider,
                provider_payment_id,
                exc,
            )
            return {"status": "ignored", "reason": "payment_not_found"}
        except PaymentProviderNotConfiguredError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
            ) from exc
        except PaymentGatewayError as exc:
            # Timeout, 5xx ou credencial recusada na consulta de status:
            # 503 para o gateway reenviar depois, sem perder o evento.
            logger.warning(
                "[Pagamento] falha ao consultar status no gateway provider=%s payment_id=%s motivo=%s",
                provider,
                provider_payment_id,
                exc,
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
            ) from exc

        if order.payment_status == event.payment_status:
            # "Mesmo status" deixou de significar "nada aconteceu".
            #
            # No Mercado Pago, ESTORNO PARCIAL mantem o pagamento em
            # `approved` — que traduz para `paid`, que e onde o pedido ja
            # esta. Antes disto o metodo retornava aqui e o dinheiro
            # devolvido nao existia do nosso lado: nem coluna, nem historico,
            # nem log. Com pix quase nao acontece; com cartao e o caso comum.
            return self._apply_partial_refund(order, event, provider)

        replayed = self.idempotency_service.begin(
            scope=IdempotencyService.build_scope(
                restaurant_id=order.restaurant_id,
                route=WEBHOOK_ROUTE,
                requester=f"gateway:{provider}",
            ),
            key=event.event_id,
            request_fingerprint=IdempotencyService.fingerprint({
                "provider": provider,
                "payment_id": event.provider_payment_id,
                "status": event.payment_status,
            }),
        )
        if replayed is not None:
            return replayed

        try:
            ensure_payment_transition_allowed(order.payment_status, event.payment_status)
        except HTTPException as exc:
            # Transicao impossivel (um "pending" chegando depois de "paid",
            # por exemplo). Nao e retentavel: registra e encerra com 2xx.
            self.db.rollback()
            logger.warning(
                "[Pagamento] transicao de pagamento recusada order_id=%s de=%s para=%s detalhe=%s",
                order.id,
                order.payment_status,
                event.payment_status,
                exc.detail,
            )
            return {"status": "ignored", "reason": "invalid_transition"}

        # Lidos ANTES do commit: depois dele o objeto do SQLAlchemy esta
        # expirado e cada atributo relido dispara um SELECT novo.
        order_status = order.status
        order_id = order.id
        restaurant_id = order.restaurant_id

        try:
            self.order_repository.update_payment_status(
                order,
                payment_status=event.payment_status,
                paid_at=utcnow() if event.payment_status == "paid" else None,
            )
            # Vale tambem no estorno TOTAL: `refunded` diz que voltou tudo,
            # mas nao diz quanto — e o relatorio que um dia somar dinheiro
            # devolvido precisa do numero, nao do rotulo.
            order.refunded_amount = event.refunded_amount
            if event.payment_status == "refunded":
                # MESMA funcao que o estorno automatico usa, e e o ponto:
                # estorno feito no painel do Mercado Pago tem que zerar a
                # comissao igual ao que sai daqui. Duas copias da regra
                # fariam o extrato depender de por onde o lojista devolveu.
                zero_commission_for_refund(order)
            self.order_repository.create_status_history(
                OrderStatusHistory(
                    order_id=order.id,
                    # Prefixo "payment:" para o evento de dinheiro nao se
                    # confundir com status operacional na mesma tabela.
                    status=payment_history_status(event.payment_status),
                    changed_by=f"gateway:{provider}",
                    note=f"status do gateway: {event.raw_status}",
                )
            )
            response = {
                "status": "processed",
                "order_id": str(order.id),
                "payment_status": event.payment_status,
            }
            if self.idempotency_service.has_reservation:
                self.idempotency_service.complete(response_body=response, order_id=order.id)
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

        logger.info(
            "[Pagamento] webhook aplicado order_id=%s payment_status=%s provider=%s",
            order.id,
            event.payment_status,
            provider,
        )
        self._refund_payment_on_terminal_order(
            order_id, restaurant_id, order_status, event.payment_status
        )
        # O pedido NAO e aceito automaticamente aqui: pagar nao obriga o
        # lojista a aceitar. O que o pagamento faz e liberar o botao —
        # ensure_payment_allows_order_status passa a deixar.
        return response

    def _apply_partial_refund(self, order, event, provider: str) -> dict:
        """Grava o estorno parcial, que nao muda `payment_status`.

        O sinal e o VALOR, nunca o status: o gateway mantem o pagamento em
        `approved` e so aumenta o total ja devolvido. Comparar com o que esta
        gravado torna o metodo idempotente de graca — o gateway reenvia a
        mesma notificacao ate receber 2xx, e reaplicar o mesmo total nao e
        um segundo estorno.

        **A comissao NAO e mexida, e e decisao tomada.** A plataforma cobra
        sobre a venda que aconteceu; devolucao por erro do lojista e custo
        dele. A coluna existe para a decisao contraria continuar possivel —
        o valor so existe do lado do gateway, e sem grava-lo agora nao ha
        como reconstitui-lo depois.
        """
        gravado = to_decimal(order.refunded_amount)
        if event.refunded_amount <= gravado:
            return {"status": "already_applied", "payment_status": order.payment_status}

        try:
            order.refunded_amount = event.refunded_amount
            self.order_repository.create_status_history(
                OrderStatusHistory(
                    order_id=order.id,
                    # Nao e um PAYMENT_STATUS: e um evento de dinheiro que o
                    # estado do pagamento nao consegue expressar.
                    status=f"{PAYMENT_HISTORY_PREFIX}partially_refunded",
                    changed_by=f"gateway:{provider}",
                    note=f"estornado ate agora: {event.refunded_amount}",
                )
            )
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

        logger.warning(
            "[Pagamento] estorno parcial registrado order_id=%s estornado=%s "
            "payment_status=%s provider=%s",
            order.id,
            event.refunded_amount,
            order.payment_status,
            provider,
        )
        return {
            "status": "processed",
            "order_id": str(order.id),
            "payment_status": order.payment_status,
        }

    def _refund_payment_on_terminal_order(
        self,
        order_id: uuid.UUID,
        restaurant_id: uuid.UUID,
        order_status: str,
        payment_status: str,
    ) -> None:
        """Devolve o dinheiro que entrou num pedido que ja acabou.

        A corrida: o lojista recusa (ou cancela) enquanto o pagamento ainda
        esta em voo, e o webhook chega depois. `handle_webhook` valida so a
        transicao de PAGAMENTO, e `pending -> paid` e valida qualquer que
        seja o status operacional — o pedido termina `rejected` + `paid`. A
        escrita acontece de proposito: recusa-la esconderia o dinheiro que de
        fato entrou.

        Ate 25/08/2026 isto era so um `logger.warning`, porque nao havia
        estorno automatico nenhum. O dinheiro do cliente ficava na conta do
        restaurante ate alguem notar — e o extrato nao denunciava, porque
        `cancelled` e `rejected` ja estao fora da comissao.

        E o MESMO service do cancelamento pelo painel, chamado da outra
        ponta da corrida: as duas ordens de eventos terminam no mesmo lugar
        e devolvem o mesmo dinheiro. Ver PaymentRefundService.

        Roda DEPOIS do commit de proposito. A resposta ao webhook nao pode
        depender do estorno dar certo: o gateway reenvia em backoff por
        horas quem nao responde 2xx, e reenviar nao conserta um estorno que
        falhou — quem conserta e a varredura.
        """
        if payment_status != "paid":
            return
        if order_status not in TERMINAL_ORDER_STATUSES:
            return
        try:
            PaymentRefundService(self.db).refund_terminal_order(order_id, restaurant_id)
        except Exception:
            # A cobranca ja esta gravada como paga; o 2xx para o gateway vale
            # mais que este estorno, que a varredura retenta.
            logger.exception(
                "[Pagamento] falha inesperada ao estornar pagamento em pedido ja %s "
                "order_id=%s",
                order_status,
                order_id,
            )

    def _verify_signature(
        self,
        provider: str,
        credential: ActivePaymentCredential | None,
        raw_body: bytes,
        headers: dict[str, str],
    ) -> None:
        # `provider` ja e conhecido aqui (extract_provider_payment_id, chamado
        # antes deste metodo em handle_webhook, teria levantado
        # PaymentProviderUnknownError para um provider desconhecido) — nao ha
        # PaymentProviderUnknownError a tratar neste ponto.
        if provider == MERCADOPAGO_PROVIDER:
            # Segredo do RESTAURANTE do pedido (RestaurantPaymentCredential),
            # nunca uma variavel global. None quando o restaurante nao tem
            # credencial cadastrada para o ambiente ativo, ou tem credencial
            # mas ainda nao cadastrou a Assinatura secreta do webhook.
            secret = credential.webhook_secret if credential is not None else None
        else:
            secret = settings.PAYMENT_WEBHOOK_SECRET

        try:
            valid = verify_webhook_signature(
                provider=provider,
                raw_body=raw_body,
                headers=headers,
                secret=secret,
            )
        except PaymentProviderNotConfiguredError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
            ) from exc
        if not valid:
            # 401 e nao 200: assinatura invalida e a unica hipotese em que
            # alguem esta tentando marcar pedido como pago sem pagar.
            logger.warning("[Pagamento] webhook com assinatura invalida provider=%s", provider)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Assinatura do webhook inválida",
            )

    def _create_payment_at_gateway(
        self,
        *,
        order_id: uuid.UUID,
        amount,
        payment_method: str | None,
        order_number: int,
        description: str,
        access_token: str | None,
        payer_email: str | None = None,
        previous_payment_id: str | None = None,
        card: CardPaymentInput | None = None,
        application_fee=None,
    ):
        try:
            return create_payment(
                provider=settings.PAYMENT_PROVIDER,
                order_id=order_id,
                amount=amount,
                payment_method=payment_method or "other",
                description=description,
                order_number=order_number,
                access_token=access_token,
                payer_email=payer_email,
                previous_payment_id=previous_payment_id,
                card=card,
                application_fee=application_fee,
            )
        # A ordem dos except importa: as duas primeiras sao subclasses de
        # PaymentGatewayError e sao os casos que se distinguem dele.
        except PaymentGatewayUnavailableError as exc:
            # Timeout, falha de rede ou 5xx deles: o UNICO caso em que
            # tentar de novo daqui a pouco tem chance de funcionar.
            raise self._payment_error(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                code=PaymentErrorCode.GATEWAY_UNAVAILABLE,
                message=_GATEWAY_UNAVAILABLE_MESSAGE,
                retryable=True,
                cause=exc,
                order_number=order_number,
            ) from exc
        except PaymentGatewayCredentialError as exc:
            # Token invalido, revogado ou de outra conta. Insistir nao troca
            # a credencial — quem resolve e o lojista, no painel dele.
            raise self._payment_error(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                code=PaymentErrorCode.PAYMENT_UNAVAILABLE,
                message=_PAYMENT_UNAVAILABLE_MESSAGE,
                retryable=False,
                cause=exc,
                order_number=order_number,
            ) from exc
        except PaymentGatewayError as exc:
            # 400/422: o gateway entendeu e RECUSOU a cobranca. 502 e nao
            # 503 — 503 diz "volte depois", e aqui voltar depois com a mesma
            # cobranca da no mesmo.
            #
            # A MENSAGEM sai do codigo deles quando ele e um dos que o cliente
            # consegue agir a respeito; nos outros continua a frase generica.
            # O `code` da resposta NAO muda por isso: payment_rejected e o
            # desfecho, e o front ja o consome — quem detalha e a mensagem.
            raise self._payment_error(
                status_code=status.HTTP_502_BAD_GATEWAY,
                code=PaymentErrorCode.PAYMENT_REJECTED,
                message=_rejection_message(exc.provider_error_code),
                retryable=False,
                cause=exc,
                provider_error_code=exc.provider_error_code,
                order_number=order_number,
            ) from exc
        except (PaymentProviderNotConfiguredError, PaymentProviderUnknownError) as exc:
            # Restaurante sem credencial para o ambiente ativo, metodo nao
            # suportado, provider mal configurado. Nunca chegamos a falar com
            # o gateway; 503 continua (o pedido segue de pe), mas sem pedir
            # ao cliente que tente de novo.
            raise self._payment_error(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                code=PaymentErrorCode.PAYMENT_UNAVAILABLE,
                message=_PAYMENT_UNAVAILABLE_MESSAGE,
                retryable=False,
                cause=exc,
                order_number=order_number,
            ) from exc

    def _payment_error(
        self,
        *,
        status_code: int,
        code: PaymentErrorCode,
        message: str,
        retryable: bool,
        cause: Exception,
        provider_error_code: str | None = None,
        order_number: int | None = None,
    ) -> HTTPException:
        """Monta o erro que o cliente recebe e manda o motivo para o log.

        O motivo tecnico fica NO LOG e nao na resposta: "Mercado Pago com
        erro interno (status 500)" nao ajuda quem so quer pagar o lanche, e
        a mensagem crua do gateway ainda pode ecoar o e-mail de quem pagou.
        O que atravessa para o cliente e o codigo, a mensagem escrita para
        ele e o `retryable`.

        **`pedido=#N` abre a linha porque sem ele o log nao responde a
        pergunta que sempre se faz dele.** Quem investiga chega com o numero
        do pedido na mao — e do lojista, do cliente ou do painel que ele
        vem — e ate 28/08/2026 nao havia por onde entrar: nem esta linha nem
        a do gateway (`[Pagamento][mercadopago] erro ...`, que e a que traz o
        `message` e o `cause` que o Mercado Pago mandou) citavam pedido
        nenhum. Descobrir o que eles responderam para UM pedido virava
        correlacao por horario, num log onde toda cobranca daquele minuto
        parece igual.

        O numero e o do lojista (`orders.order_number`), e nao o `id`, de
        proposito: o `id` ninguem tem na mao.

        Esta linha **nao substitui** a do gateway — ela nao carrega o texto
        deles, e nao deve carregar (ver _call_mercadopago). As duas e que se
        carimbam com o mesmo `pedido=#N`, e e isso que faz um `grep` unico
        devolver a chamada e o desfecho dela sem depender de as linhas
        sairem coladas — o que, com varias cobrancas em voo, elas nao saem.
        """
        logger.warning(
            "[Pagamento] cobranca nao criada pedido=#%s code=%s retryable=%s "
            "provider_code=%s motivo=%s",
            order_number if order_number is not None else "-",
            code.value,
            retryable,
            provider_error_code or "-",
            cause,
        )
        detail = PaymentErrorDetail(
            code=code,
            message=message,
            retryable=retryable,
            provider_error_code=provider_error_code,
        )
        # mode="json" para o `code` sair como a string do enum: o dict vai
        # direto para o corpo da resposta e para as asserts dos testes.
        return HTTPException(status_code=status_code, detail=detail.model_dump(mode="json"))
