"""A UNICA escrita de status de pedido do sistema.

Tres portas chegam aqui, e e de proposito que seja uma escrita so:

  - `PATCH /admin/orders/{id}/status` — o lojista movendo o pedido;
  - `PATCH /admin/orders/{id}/cancel` — o lojista cancelando, com motivo;
  - `POST  /restaurants/{slug}/orders/track/{token}/cancel` — o CLIENTE
    desistindo antes do preparo.

A regra que este arquivo existe para proteger ja estava escrita quando so
havia as duas primeiras: *"cancelar nao e uma segunda escrita de status: ele
delega para o mesmo metodo. Duas escritas independentes seriam a chance de a
maquina valer numa e nao na outra."* A terceira porta e o teste dessa regra —
um cancelamento pelo cliente com codigo proprio seria um caminho em que o
cupom nao volta, o cashback fica retido, o historico nao registra quem
cancelou, ou o pagamento nao e estornado. Quatro bugs de dinheiro por um
copiar e colar.

**O que este service NAO faz: autorizar.** Ele recebe o pedido JA carregado e
JA autorizado por quem chamou, porque as tres portas autorizam de formas
incomparaveis — escopo de lojista pelo token no painel, `tracking_token` no
app do cliente. Misturar as duas aqui dentro criaria um metodo com um `if`
decidindo de quem e o pedido, que e exatamente onde esse tipo de coisa
costuma vazar.

**Quem pode cancelar em que estado tambem fica de fora**, pelo mesmo motivo:
e regra da PORTA, nao da escrita. `ensure_customer_can_cancel` roda no
service do cliente, e a confirmacao de pedido em preparo roda no do painel.
O que este arquivo garante e que, decidido o cancelamento, ele acontece
sempre igual.
"""

import logging
from uuid import UUID

from sqlalchemy.orm import Session

from src.models.order_status_history_model import OrderStatusHistory
from src.repositories.order_repository import OrderRepository
from src.schemas.order_schema import OrderDetailResponse
from src.services.cashback_service import CashbackService
from src.services.coupon_service import CouponService
from src.services.idempotency_service import IdempotencyService
from src.services.order_service import OrderService
from src.services.order_state_machine import (
    ensure_order_transition_allowed,
    ensure_payment_allows_order_status,
)
from src.services.payment_refund_service import PaymentRefundService
from src.services.whatsapp_notification_service import WhatsAppOrderNotifier


logger = logging.getLogger("uvicorn.error")

# Os dois estados terminais em que o pedido NAO virou venda. Cupom volta,
# cashback volta, e o dinheiro do gateway volta.
REVERSING_STATUSES = ("cancelled", "rejected")

# O estado em que o cashback do pedido e creditado.
COMPLETED_STATUS = "completed"


class OrderStatusChangeService:
    def __init__(self, db: Session):
        self.db = db
        self.order_repository = OrderRepository(db)
        self.coupon_service = CouponService(db)
        self.cashback_service = CashbackService(db)
        self.idempotency_service = IdempotencyService(db)

    def apply(
        self,
        *,
        order,
        restaurant_id: UUID,
        new_status: str,
        note: str | None,
        changed_by: str,
        requester: str,
        route: str,
        idempotency_key: str | None,
    ) -> OrderDetailResponse:
        """Grava a mudanca de status: validacao, escrita, historico e estorno.

        `route` e `requester` entram no escopo da idempotencia para que a
        mesma `Idempotency-Key` reenviada por outra porta (ou por outra
        pessoa) nao seja tratada como repeticao de uma so.

        `changed_by` vem de QUEM CHAMOU e nunca do corpo da requisicao: o
        campo ja foi texto livre enviado pelo cliente, e o historico do
        pedido registrava o autor que o painel quisesse escrever.
        """
        order_id = order.id

        # Sem isto, cada reenvio (clique duplo, retry de rede) empilhava uma
        # linha nova em order_status_history para o mesmo status, sujando o
        # historico que o cliente ve.
        replayed = self.idempotency_service.begin(
            scope=IdempotencyService.build_scope(
                restaurant_id=restaurant_id,
                route=route,
                requester=requester,
            ),
            key=idempotency_key,
            request_fingerprint=IdempotencyService.fingerprint({
                "order_id": str(order_id),
                "status": new_status,
                "note": note,
            }),
        )
        if replayed is not None:
            return IdempotencyService.parse_stored_response(OrderDetailResponse, replayed)

        # A validacao da transicao vem DEPOIS do replay de proposito. Um
        # reenvio da mesma chave chega com o pedido ja no status de destino;
        # validando antes, o retry legitimo morreria com "o pedido ja esta em
        # accepted" em vez de devolver a resposta gravada.
        ensure_order_transition_allowed(order.status, new_status, order.order_type)
        ensure_payment_allows_order_status(new_status, order.payment_status)

        try:
            self.order_repository.update_status(order, new_status)
            if new_status in REVERSING_STATUSES:
                self.coupon_service.reverse_for_order(order_id)
                # O cashback resgatado volta para o saldo pelo mesmo motivo
                # do cupom: sem isto o cliente cancela e PERDE o dinheiro.
                self.cashback_service.refund_redemption(order)
            if new_status == COMPLETED_STATUS:
                # O credito acontece AQUI, e nao no pago nem no aceite:
                # `completed` e o unico estado terminal em que houve venda, e
                # e o que faz o estorno de credito ser excecao em vez de
                # rotina. Ver `docs/cashback.md`, secao 1.
                self.cashback_service.credit_for_order(order)
            self.order_repository.create_status_history(
                OrderStatusHistory(
                    order_id=order_id,
                    status=new_status,
                    changed_by=changed_by,
                    note=note,
                )
            )
            if self.idempotency_service.has_reservation:
                # Recarrega antes do commit para gravar a mesma resposta que
                # o chamador vai receber.
                self.idempotency_service.complete(
                    response_body=OrderService.to_order_detail_response(
                        self.order_repository.get_order_detail(order_id, restaurant_id)
                    ).model_dump(mode="json"),
                    order_id=order_id,
                )
            self.db.commit()
            order = self.order_repository.get_order_detail(order_id, restaurant_id)
        except Exception:
            self.db.rollback()
            raise

        # FORA do try, e depois do commit: o estorno fala com o Mercado Pago,
        # e a decisao de cancelar nao pode depender de o gateway responder.
        # Estivesse aqui dentro, um timeout deles desfaria o cancelamento
        # inteiro — quem clicou veria um erro e o pedido continuaria na
        # cozinha. O motivo completo esta no docstring de PaymentRefundService.
        if new_status in REVERSING_STATUSES and order.payment_flow == "online":
            self._refund_terminal_order(order_id, restaurant_id)
            order = self.order_repository.get_order_detail(order_id, restaurant_id)

        # Mesmo lugar e mesmo motivo do estorno: falar com a Meta nao pode
        # fazer parte da transacao do pedido. E aqui esta a razao de o aviso
        # morar NESTE metodo e nao em cada porta: sao quatro portas de escrita
        # de status (painel, cancelamento, cliente, entregador), e quatro
        # copias seriam tres chances de o cliente deixar de ser avisado.
        self._notify_customer_on_whatsapp(order, restaurant_id)

        return OrderService.to_order_detail_response(order)

    def _notify_customer_on_whatsapp(self, order, restaurant_id: UUID) -> None:
        """Avisa o cliente do status novo, sem poder derrubar a resposta.

        O `except` largo e o mesmo do estorno, e pelo mesmo motivo: a mudanca
        de status **ja esta gravada**, e transformar uma falha daqui em 500
        diria ao lojista que o aceite nao aconteceu — e ele clicaria de novo.

        O que e falha PREVISTA (a Meta recusando, o telefone que nao vira
        E.164) nao chega aqui: vira uma linha `failed` em `whatsapp_messages`,
        que e onde "o cliente nao foi avisado" fica visivel depois. Este
        `except` e para o que nao esta previsto.
        """
        try:
            WhatsAppOrderNotifier(self.db).notify(order=order, restaurant_id=restaurant_id)
        except Exception:
            logger.exception(
                "[WhatsApp] falha inesperada ao avisar o cliente pedido=#%s",
                order.order_number,
            )

    def _refund_terminal_order(self, order_id: UUID, restaurant_id: UUID) -> None:
        """Devolve o dinheiro do pedido recem-cancelado, sem poder derrubar
        a resposta.

        A checagem de `payment_flow == "online"` fica no chamador de
        proposito, e nao aqui: ela e leitura de um campo ja em maos, e evita
        abrir uma transacao de leitura no caso esmagadoramente mais comum —
        o pedido pago na entrega, em que nao ha cobranca nenhuma. O service
        confere de novo, porque ele tambem e chamado pela varredura.

        O `except` largo nao esconde falha de gateway: essa ja vira
        `ACTION_FAILED` la dentro, com log proprio e retentativa pela
        varredura. Ele existe para o que nao esta previsto — o cancelamento
        JA esta gravado, e transformar um erro daqui em 500 diria a quem
        clicou que o cancelamento nao aconteceu, o que e falso e o faria
        tentar de novo.
        """
        try:
            PaymentRefundService(self.db).refund_terminal_order(order_id, restaurant_id)
        except Exception:
            logger.exception(
                "[Pagamento] falha inesperada ao estornar pedido cancelado order_id=%s",
                order_id,
            )
