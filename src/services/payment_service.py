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
    MERCADOPAGO_PROVIDER,
    PaymentGatewayError,
    PaymentNotFoundError,
    PaymentProviderNotConfiguredError,
    PaymentProviderUnknownError,
    PaymentWebhookPayloadError,
    create_payment,
    extract_provider_payment_id,
    parse_webhook_event,
    verify_webhook_signature,
)
from src.models.order_status_history_model import OrderStatusHistory
from src.repositories.customer_repository import CustomerRepository
from src.repositories.order_repository import OrderRepository
from src.schemas.payment_schema import StartPaymentResponse
from src.services.idempotency_service import IdempotencyService
from src.services.order_state_machine import (
    ensure_payment_transition_allowed,
    payment_history_status,
)
from src.services.payment_credential_service import ActivePaymentCredential, PaymentCredentialService
from src.services.restaurant_service import RestaurantService
from src.utils.security import utcnow


logger = logging.getLogger("uvicorn.error")

WEBHOOK_ROUTE = "POST /payments/webhooks/{provider}"

# Estados de pagamento em que faz sentido criar (ou recriar) uma cobranca.
PAYABLE_STATUSES = ("pending", "failed")


class PaymentService:
    def __init__(self, db: Session):
        self.db = db
        self.order_repository = OrderRepository(db)
        self.restaurant_service = RestaurantService(db)
        self.idempotency_service = IdempotencyService(db)
        self.payment_credential_service = PaymentCredentialService(db)
        self.customer_repository = CustomerRepository(db)

    def start_online_payment(
        self,
        restaurant_slug: str,
        tracking_token: str,
    ) -> StartPaymentResponse:
        """Cria a cobranca do pedido no gateway.

        Autorizacao pelo token de acompanhamento: quem tem o token e quem
        criou o pedido. Nao ha login obrigatorio aqui porque pedido de
        convidado tambem paga.
        """
        restaurant = self.restaurant_service.get_active_restaurant(restaurant_slug)
        order = self.order_repository.get_order_by_tracking_token(restaurant.id, tracking_token)
        if order is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Pedido não encontrado"
            )
        if order.payment_flow != "online":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Este pedido e pago na entrega e nao tem cobranca online.",
            )
        if order.payment_status not in PAYABLE_STATUSES:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Pagamento em '{order.payment_status}': nao ha o que cobrar.",
            )

        # Valores copiados ANTES do commit: depois dele o objeto do
        # SQLAlchemy esta expirado e cada atributo lido dispara um SELECT
        # novo — exatamente o que estamos tentando evitar aqui.
        restaurant_id = restaurant.id
        order_id = order.id
        amount = order.total
        payment_method = order.payment_method
        order_number = order.order_number

        # A cobranca e sempre em nome do restaurante do pedido: busca a
        # credencial dele ANTES de falar com o gateway, nunca uma constante
        # global. `access_token` fica None quando o provider e "sandbox"
        # (que nao precisa de credencial nenhuma) ou quando o restaurante
        # ainda nao cadastrou a credencial do ambiente ativo — nesse
        # segundo caso, create_payment e quem recusa com 503.
        access_token = None
        payer_email = None
        if settings.PAYMENT_PROVIDER == MERCADOPAGO_PROVIDER:
            credential = self.payment_credential_service.get_active_credential(restaurant_id)
            if credential is not None:
                access_token = credential.access_token
            # So resolve e-mail quando de fato vai chamar o Mercado Pago: o
            # sandbox nao usa, e e uma consulta a mais no banco por nada.
            payer_email = self._resolve_payer_email(order)

        # Fecha a transacao de leitura ANTES de falar com o gateway. Sem
        # isso a conexao de banco fica presa durante um I/O externo que pode
        # levar segundos — com o pool cheio, a API inteira trava esperando
        # um gateway lento.
        self.db.commit()

        intent = self._create_payment_at_gateway(
            order_id=order_id,
            amount=amount,
            payment_method=payment_method,
            description=f"Pedido #{order_number}",
            access_token=access_token,
            payer_email=payer_email,
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
            if order.payment_status not in PAYABLE_STATUSES:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Pagamento em '{order.payment_status}': nao ha o que cobrar.",
                )
            self.order_repository.attach_payment_intent(
                order,
                provider=intent.provider,
                provider_payment_id=intent.provider_payment_id,
            )
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

        return StartPaymentResponse(
            provider=intent.provider,
            provider_payment_id=intent.provider_payment_id,
            payment_status="pending",
            checkout_url=intent.checkout_url,
            qr_code=intent.qr_code,
        )

    def _resolve_payer_email(self, order) -> str:
        """E-mail exigido pelo Mercado Pago para criar cobranca pix.

        O pedido nao guarda e-mail nenhum (so nome e telefone — ver
        Order.customer_name_snapshot/customer_phone_snapshot). Quem fez
        login tem e-mail em customers; quem pediu como convidado nao tem
        nenhum cadastrado em lugar algum. Para o convidado, usamos um
        e-mail sintetico nosso, derivado do numero do pedido: o Mercado
        Pago so usa esse campo para validar o payer da cobranca pix, nao
        para mandar comunicacao nenhuma a ele.
        """
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
            # Reenvio depois de ja aplicado, ou duas notificacoes do mesmo
            # evento. Nada a fazer, e nao e erro.
            return {"status": "already_applied", "payment_status": order.payment_status}

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

        try:
            self.order_repository.update_payment_status(
                order,
                payment_status=event.payment_status,
                paid_at=utcnow() if event.payment_status == "paid" else None,
            )
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
        # O pedido NAO e aceito automaticamente aqui: pagar nao obriga o
        # lojista a aceitar. O que o pagamento faz e liberar o botao —
        # ensure_payment_allows_order_status passa a deixar.
        return response

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
                detail="Assinatura do webhook invalida",
            )

    def _create_payment_at_gateway(
        self,
        *,
        order_id: uuid.UUID,
        amount,
        payment_method: str | None,
        description: str,
        access_token: str | None,
        payer_email: str | None = None,
        application_fee=None,
    ):
        try:
            return create_payment(
                provider=settings.PAYMENT_PROVIDER,
                order_id=order_id,
                amount=amount,
                payment_method=payment_method or "other",
                description=description,
                access_token=access_token,
                payer_email=payer_email,
                application_fee=application_fee,
            )
        except (PaymentProviderNotConfiguredError, PaymentProviderUnknownError, PaymentGatewayError) as exc:
            # 503 e nao 500: e configuracao/indisponibilidade do gateway, e o
            # cliente pode tentar de novo depois. O pedido continua de pe.
            # PaymentGatewayError cobre timeout, credencial recusada e
            # qualquer status de erro que o Mercado Pago tenha devolvido.
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
            ) from exc
