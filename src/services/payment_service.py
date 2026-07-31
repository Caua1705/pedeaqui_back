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
    PaymentProviderNotConfiguredError,
    PaymentProviderUnknownError,
    PaymentWebhookPayloadError,
    create_payment,
    parse_webhook_event,
    verify_webhook_signature,
)
from src.models.order_status_history_model import OrderStatusHistory
from src.repositories.order_repository import OrderRepository
from src.schemas.payment_schema import StartPaymentResponse
from src.services.idempotency_service import IdempotencyService
from src.services.order_state_machine import (
    ensure_payment_transition_allowed,
    payment_history_status,
)
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
        )

        try:
            order = self.order_repository.get_order_by_tracking_token(restaurant_id, tracking_token)
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

    def handle_webhook(
        self,
        provider: str,
        raw_body: bytes,
        headers: dict[str, str],
    ) -> dict[str, Any]:
        self._verify_signature(provider, raw_body, headers)

        try:
            event = parse_webhook_event(provider=provider, raw_body=raw_body)
        except PaymentWebhookPayloadError as exc:
            # 200 de proposito: reenviar nao conserta um corpo que nao
            # entendemos, e 5xx colocaria o gateway em retentativa por horas.
            logger.warning("[Pagamento] webhook ignorado provider=%s motivo=%s", provider, exc)
            return {"status": "ignored", "reason": "payload"}
        except PaymentProviderNotConfiguredError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
            ) from exc

        order = self.order_repository.get_order_by_provider_payment(
            provider,
            event.provider_payment_id,
        )
        if order is None:
            logger.warning(
                "[Pagamento] webhook sem pedido correspondente provider=%s payment_id=%s",
                provider,
                event.provider_payment_id,
            )
            return {"status": "ignored", "reason": "unknown_payment"}

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

    def _verify_signature(self, provider: str, raw_body: bytes, headers: dict[str, str]) -> None:
        try:
            valid = verify_webhook_signature(
                provider=provider,
                raw_body=raw_body,
                headers=headers,
                secret=self._webhook_secret(provider),
            )
        except PaymentProviderUnknownError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
            ) from exc
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
    ):
        try:
            return create_payment(
                provider=settings.PAYMENT_PROVIDER,
                order_id=order_id,
                amount=amount,
                payment_method=payment_method or "other",
                description=description,
            )
        except (PaymentProviderNotConfiguredError, PaymentProviderUnknownError) as exc:
            # 503 e nao 500: e configuracao/indisponibilidade do gateway, e o
            # cliente pode tentar de novo depois. O pedido continua de pe.
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
            ) from exc

    @staticmethod
    def _webhook_secret(provider: str) -> str | None:
        if provider == "mercadopago":
            return settings.MERCADOPAGO_WEBHOOK_SECRET
        return settings.PAYMENT_WEBHOOK_SECRET
