"""Devolve o dinheiro do pedido que terminou sem virar venda.

Ate 25/08/2026 **nada aqui estornava sozinho**: cancelar um pedido pago
gravava o cancelamento, escrevia um warning com o texto `sem estorno
automatico` e pronto. O dinheiro do cliente ficava na conta do restaurante
ate alguem abrir o painel do Mercado Pago e devolver a mao. Os dois lados
dessa corrida tinham log e nenhum tinha conserto:

  - o lojista cancela um pedido JA PAGO;
  - o lojista recusa e o pagamento entra DEPOIS (o webhook chega num pedido
    ja terminal, e a escrita acontece de proposito — recusa-la esconderia o
    dinheiro que de fato entrou).

Este service e o conserto dos dois. Ele e chamado pelo
`AdminOrderService` (depois de gravar o cancelamento) e pelo
`PaymentService` (depois de aplicar o webhook), e faz a MESMA coisa nas
duas pontas.

===========================================================================
DUAS OPERACOES DIFERENTES, e o gateway escolhe qual — nao nos
===========================================================================

"Estornar" e uma palavra so para duas coisas que o Mercado Pago separa:

  - **cancelar** a cobranca que ainda nao capturou dinheiro (o QR do pix
    aberto, o cartao em analise do antifraude). Ninguem pagou nada, e nada
    volta: a cobranca morre.
  - **estornar** a cobranca aprovada. Ai ha dinheiro voltando de verdade.

Chamar a errada e 4xx do gateway. E qual delas cabe depende do estado que o
pagamento tem NO GATEWAY agora, que nao e necessariamente
`orders.payment_status`: o webhook pode estar em voo, e a janela e maior
justamente no caso que mais interessa (o cartao em analise, que pode ser
aprovado a qualquer momento das 48h uteis).

**Por isso este service pergunta antes de agir** (`fetch_payment`). E uma
chamada a mais por cancelamento de pedido online — que e um evento raro — e
compra a unica coisa que a copia local nao da: a verdade do momento.

===========================================================================
A ORDEM: cancelar primeiro, devolver depois
===========================================================================

Este service roda **depois** de o cancelamento estar commitado, e nunca
dentro da transacao dele. Duas razoes, e nenhuma e estilo:

1. **A decisao do lojista nao pode depender do Mercado Pago responder.**
   Com o estorno dentro da transacao, o gateway fora do ar faria o
   cancelamento inteiro voltar atras — o lojista clica em "cancelar", ve um
   erro, e o pedido continua na cozinha.
2. **I/O externo com transacao aberta prende conexao do pool.** E a mesma
   razao pela qual `PaymentService.start_online_payment` commita antes de
   falar com o gateway.

O preco dessa ordem e explicito: existe uma janela em que o pedido esta
cancelado e o dinheiro ainda nao voltou. Essa janela e **exatamente** o
conjunto que `OrderRepository.list_orders_awaiting_refund` sabe consultar, e
e o que `scripts/estorna_pedidos_cancelados.py` varre — sem coluna nova
nenhuma, porque "pedido cancelado com cobranca online viva" ja e uma
descricao completa do que falta fazer.
"""

import logging
import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.orm import Session

from src.core.constants import PAYMENT_STATUSES_WITH_LIVE_CHARGE
from src.integrations.payment_gateway import (
    MERCADOPAGO_PROVIDER,
    GatewayPayment,
    PaymentGatewayError,
    PaymentProviderNotConfiguredError,
    PaymentProviderUnknownError,
    cancel_payment,
    fetch_payment,
    refund_payment,
)
from src.models.order_status_history_model import OrderStatusHistory
from src.services.commission import zero_commission_for_refund
from src.repositories.order_repository import (
    NON_BILLABLE_ORDER_STATUSES,
    OrderRepository,
)
from src.services.order_state_machine import (
    payment_history_status,
    payment_transition_is_allowed,
)
from src.services.payment_credential_service import PaymentCredentialService
from src.utils.money import to_decimal


logger = logging.getLogger("uvicorn.error")

# O que foi feito. Sai no log e nos contadores da varredura, entao vale ser
# legivel por quem le o log, nao so pelo codigo.
ACTION_NOTHING_TO_DO = "nada_a_devolver"
ACTION_CANCELLED = "cobranca_cancelada"
ACTION_REFUNDED = "estornado"
ACTION_REFUND_IN_PROCESS = "estorno_em_processamento"
ACTION_FAILED = "falhou"

# O grep que ja estava no radar antes de existir estorno automatico. Mantido
# PALAVRA POR PALAVRA de proposito: quem tem alerta montado em cima dele
# continua sendo avisado quando sobra dinheiro parado, e agora a linha sai
# so quando a tentativa automatica falhou — que e uma frequencia muito
# menor, e por isso mais digna de atencao.
STUCK_MONEY_LOG = "sem estorno automatico"


@dataclass(frozen=True)
class RefundOutcome:
    """O desfecho de uma tentativa de devolver o dinheiro de um pedido."""

    action: str
    # False quando sobrou cobranca viva do lado do gateway e alguem (a
    # varredura, ou uma pessoa) precisa voltar nela. `ACTION_NOTHING_TO_DO`
    # e resolvido: nao havia o que devolver.
    resolved: bool
    detail: str | None = None


class PaymentRefundService:
    def __init__(self, db: Session):
        self.db = db
        self.order_repository = OrderRepository(db)
        self.payment_credential_service = PaymentCredentialService(db)

    def refund_terminal_order(
        self,
        order_id: uuid.UUID,
        restaurant_id: uuid.UUID,
    ) -> RefundOutcome:
        """Encerra a cobranca de um pedido que terminou sem virar venda.

        `restaurant_id` e exigido junto e nao e burocracia: e o que permite
        usar `get_order_detail`, que filtra por restaurante, em vez de abrir
        no repositorio uma leitura de pedido por id solto — a leitura que
        vazava pedido entre lojistas antes de aquele filtro existir. Os dois
        chamadores ja tem o valor em maos.

        Nunca levanta por causa do gateway. Falar com o Mercado Pago e a
        parte que falha por conta propria (timeout, 5xx, credencial
        revogada), e aqui isso e um DESFECHO — `ACTION_FAILED`, que a
        varredura vai retentar —, nao uma excecao para subir ate uma
        resposta HTTP que ja foi decidida. Erro de banco continua subindo.
        """
        order = self.order_repository.get_order_detail(order_id, restaurant_id)
        if order is None:
            return RefundOutcome(ACTION_NOTHING_TO_DO, resolved=True, detail="pedido inexistente")

        recusa = self._nothing_to_settle(order)
        if recusa is not None:
            return recusa

        # Copiados ANTES do commit: depois dele o objeto do SQLAlchemy esta
        # expirado e cada atributo lido dispara um SELECT novo.
        provider = order.payment_provider
        provider_payment_id = order.provider_payment_id
        local_status = order.payment_status
        access_token = self._access_token(provider, restaurant_id)

        # Fecha a transacao de leitura ANTES de falar com o gateway, pelo
        # mesmo motivo de PaymentService.start_online_payment: a chamada
        # pode levar segundos, e a conexao presa nesse tempo e conexao a
        # menos para atender pedido.
        self.db.commit()

        try:
            snapshot = fetch_payment(
                provider=provider,
                access_token=access_token,
                provider_payment_id=provider_payment_id,
            )
            return self._settle(
                order_id=order_id,
                restaurant_id=restaurant_id,
                provider=provider,
                provider_payment_id=provider_payment_id,
                access_token=access_token,
                snapshot=snapshot,
                local_status=local_status,
            )
        except (
            PaymentGatewayError,
            PaymentProviderNotConfiguredError,
            PaymentProviderUnknownError,
        ) as exc:
            logger.warning(
                "[Pagamento] %s order_id=%s payment_status=%s provider=%s motivo=%s",
                STUCK_MONEY_LOG,
                order_id,
                local_status,
                provider,
                exc,
            )
            return RefundOutcome(ACTION_FAILED, resolved=False, detail=str(exc))

    def _nothing_to_settle(self, order) -> RefundOutcome | None:
        """As quatro razoes para nao haver o que devolver neste pedido.

        Todas sao leitura do que ja esta em maos, e ficam ANTES de qualquer
        chamada ao gateway: o caso esmagadoramente mais comum (pedido pago
        na entrega) nao paga nem uma consulta.
        """
        if order.status not in NON_BILLABLE_ORDER_STATUSES:
            # **Terminal nao basta, e `completed` e a razao.** Ele tambem e
            # um estado final, e e o unico em que HOUVE venda — um
            # `status in TERMINAL_ORDER_STATUSES` aqui devolveria o dinheiro
            # de todo pedido entregue. O conjunto certo e o mesmo que sai da
            # comissao, e ele mora num lugar so de proposito: e o que faz a
            # varredura procurar exatamente o que este service trata.
            #
            # Pedido ainda VIVO cai aqui tambem, e recusar em silencio
            # esconderia o bug de quem chamou: estornar um pedido que a
            # cozinha esta preparando e prejuizo direto do lojista.
            logger.warning(
                "[Pagamento] estorno pedido para pedido que nao foi cancelado "
                "order_id=%s status=%s",
                order.id,
                order.status,
            )
            return RefundOutcome(
                ACTION_NOTHING_TO_DO, resolved=True, detail=f"pedido em '{order.status}'"
            )
        if order.payment_flow != "online":
            return RefundOutcome(ACTION_NOTHING_TO_DO, resolved=True, detail="pago na entrega")
        if order.payment_status not in PAYMENT_STATUSES_WITH_LIVE_CHARGE:
            return RefundOutcome(
                ACTION_NOTHING_TO_DO,
                resolved=True,
                detail=f"pagamento em '{order.payment_status}'",
            )
        if not order.provider_payment_id or not order.payment_provider:
            # Pedido online cuja cobranca nunca chegou a ser criada: o
            # cliente fechou o checkout antes de clicar em pagar.
            return RefundOutcome(
                ACTION_NOTHING_TO_DO, resolved=True, detail="cobranca nunca criada"
            )
        return None

    def _access_token(self, provider: str, restaurant_id: uuid.UUID) -> str | None:
        """A credencial DO RESTAURANTE do pedido, nunca uma global.

        `None` no sandbox (que nao usa credencial nenhuma) e no restaurante
        sem credencial cadastrada para o ambiente ativo — nesse segundo caso
        quem recusa e o proprio gateway, com
        PaymentProviderNotConfiguredError, e a recusa vira `ACTION_FAILED`
        para a varredura retentar depois de o lojista cadastrar.
        """
        if provider != MERCADOPAGO_PROVIDER:
            return None
        credential = self.payment_credential_service.get_active_credential(restaurant_id)
        return credential.access_token if credential is not None else None

    def _settle(
        self,
        *,
        order_id: uuid.UUID,
        restaurant_id: uuid.UUID,
        provider: str,
        provider_payment_id: str,
        access_token: str | None,
        snapshot: GatewayPayment | None,
        local_status: str,
    ) -> RefundOutcome:
        """Escolhe a operacao pelo estado do pagamento e executa.

        O estado vem do gateway quando ele sabe dizer, e da copia local
        quando nao (`snapshot is None` — o sandbox, que nao guarda estado
        proprio; ver `fetch_payment`).
        """
        status_no_gateway = local_status if snapshot is None else snapshot.payment_status

        if status_no_gateway is None:
            # Status que o Mercado Pago inventou depois desta linha ser
            # escrita. Nao se adivinha operacao com dinheiro: falha alto e a
            # varredura volta nela.
            logger.warning(
                "[Pagamento] %s order_id=%s motivo=status do gateway sem traducao (%s)",
                STUCK_MONEY_LOG,
                order_id,
                snapshot.raw_status,
            )
            return RefundOutcome(
                ACTION_FAILED, resolved=False, detail=f"status sem traducao: {snapshot.raw_status}"
            )

        if status_no_gateway in ("failed", "refunded"):
            # Ja resolvido do lado de la — cobranca morta, ou dinheiro
            # devolvido no painel deles. So falta a copia local concordar.
            self._record(
                order_id=order_id,
                restaurant_id=restaurant_id,
                new_payment_status=status_no_gateway,
                note=f"estado lido no gateway: {status_no_gateway}",
                provider=provider,
                refunded_total=None if snapshot is None else snapshot.refunded_amount,
            )
            return RefundOutcome(
                ACTION_NOTHING_TO_DO, resolved=True, detail=f"gateway ja em '{status_no_gateway}'"
            )

        if status_no_gateway in ("pending", "in_review"):
            return self._cancel(
                order_id=order_id,
                restaurant_id=restaurant_id,
                provider=provider,
                provider_payment_id=provider_payment_id,
                access_token=access_token,
                status_no_gateway=status_no_gateway,
            )

        return self._refund(
            order_id=order_id,
            restaurant_id=restaurant_id,
            provider=provider,
            provider_payment_id=provider_payment_id,
            access_token=access_token,
            snapshot=snapshot,
            local_status=local_status,
        )

    def _cancel(
        self,
        *,
        order_id: uuid.UUID,
        restaurant_id: uuid.UUID,
        provider: str,
        provider_payment_id: str,
        access_token: str | None,
        status_no_gateway: str,
    ) -> RefundOutcome:
        """Mata a cobranca que ainda nao capturou dinheiro.

        Nao ha estorno aqui: ninguem pagou nada. O que isto compra e o
        cliente **nao conseguir mais pagar** um pedido que ninguem vai
        produzir — sem isto, o pix aberto de um pedido cancelado continua
        pagavel no app do banco, e o webhook chega num pedido ja terminal.
        """
        cancel_payment(
            provider=provider,
            access_token=access_token,
            provider_payment_id=provider_payment_id,
        )
        # `failed` e o que o webhook desta mesma cobranca escreveria: o
        # `cancelled` deles ja traduz para `failed` aqui. Escrever outra
        # coisa faria a notificacao que chega em seguida discordar de nos.
        self._record(
            order_id=order_id,
            restaurant_id=restaurant_id,
            new_payment_status="failed",
            note=f"cobranca cancelada no gateway (estava em {status_no_gateway})",
            provider=provider,
            refunded_total=None,
        )
        logger.info(
            "[Pagamento] cobranca cancelada no gateway order_id=%s de=%s provider=%s",
            order_id,
            status_no_gateway,
            provider,
        )
        return RefundOutcome(ACTION_CANCELLED, resolved=True)

    def _refund(
        self,
        *,
        order_id: uuid.UUID,
        restaurant_id: uuid.UUID,
        provider: str,
        provider_payment_id: str,
        access_token: str | None,
        snapshot: GatewayPayment | None,
        local_status: str,
    ) -> RefundOutcome:
        """Devolve o dinheiro de uma cobranca aprovada."""
        result = refund_payment(
            provider=provider,
            access_token=access_token,
            provider_payment_id=provider_payment_id,
        )
        ja_devolvido = Decimal("0") if snapshot is None else snapshot.refunded_amount
        refunded_total = ja_devolvido + result.amount

        if not result.settled:
            # O gateway aceitou o pedido de estorno e ainda nao concluiu.
            # Marcar `refunded` agora seria declarar devolvido um dinheiro
            # que nao se moveu; quem fecha e o webhook.
            logger.warning(
                "[Pagamento] estorno aceito e nao concluido order_id=%s status=%s provider=%s",
                order_id,
                result.raw_status,
                provider,
            )
            return RefundOutcome(
                ACTION_REFUND_IN_PROCESS, resolved=False, detail=result.raw_status
            )

        if local_status != "paid":
            # Chegamos aqui com o pagamento em `pending` ou `in_review` e o
            # gateway respondendo `approved`: a cobranca foi aprovada entre o
            # cancelamento e agora. `in_review -> refunded` nao existe no
            # grafo, e nao deve existir — o dinheiro ENTROU antes de voltar, e
            # pular esse passo faria o historico do cliente mentir sobre o que
            # aconteceu com ele. Sem esta linha o estorno acontece no gateway
            # e a copia local fica parada em `in_review`, que e a pior das
            # duas metades.
            self._record(
                order_id=order_id,
                restaurant_id=restaurant_id,
                new_payment_status="paid",
                note="aprovado no gateway durante o cancelamento do pedido",
                provider=provider,
                refunded_total=None,
            )

        self._record(
            order_id=order_id,
            restaurant_id=restaurant_id,
            new_payment_status="refunded",
            note=f"estorno total no gateway (refund_id={result.provider_refund_id})",
            provider=provider,
            refunded_total=refunded_total,
        )
        logger.info(
            "[Pagamento] pedido cancelado estornado order_id=%s valor=%s provider=%s",
            order_id,
            refunded_total,
            provider,
        )
        return RefundOutcome(ACTION_REFUNDED, resolved=True)

    def _record(
        self,
        *,
        order_id: uuid.UUID,
        restaurant_id: uuid.UUID,
        new_payment_status: str,
        note: str,
        provider: str,
        refunded_total: Decimal | None,
    ) -> None:
        """Grava o desfecho no pedido e no historico, numa transacao so.

        O pedido e **relido** aqui, e nao reaproveitado do comeco do metodo:
        entre a leitura e agora houve uma chamada externa de ate dez
        segundos, e um webhook pode ter chegado no meio dela.

        Uma transicao de pagamento que o grafo nao permite nao e erro para
        levantar: e o webhook tendo ganhado a corrida e ja escrito o mesmo
        desfecho. O dinheiro esta certo nos dois casos.
        """
        try:
            order = self.order_repository.get_order_detail(order_id, restaurant_id)
            if order is None:
                self.db.rollback()
                return

            if new_payment_status == "refunded" and not refunded_total:
                # Estorno TOTAL cujo valor o gateway nao informou — a
                # resposta deles pode vir sem `amount`, e o sandbox nao
                # movimenta dinheiro nenhum para ter valor a informar.
                # "Total" ja diz qual e o valor: o do pedido.
                refunded_total = to_decimal(order.total)

            if refunded_total is not None and refunded_total > to_decimal(order.refunded_amount):
                # Cumulativo, nunca substituicao: um estorno parcial feito
                # antes no painel deles ja esta gravado aqui, e o valor do
                # nosso estorno e so a parte que faltava.
                order.refunded_amount = refunded_total

            if not payment_transition_is_allowed(order.payment_status, new_payment_status):
                logger.info(
                    "[Pagamento] estado do pagamento ja estava em '%s' order_id=%s",
                    order.payment_status,
                    order_id,
                )
                self.db.commit()
                return

            self.order_repository.update_payment_status(
                order,
                payment_status=new_payment_status,
                paid_at=None,
            )
            if new_payment_status == "refunded":
                # Venda que nao existiu nao gera comissao. Na MESMA transacao
                # da mudanca de estado de proposito: as duas descrevem o
                # mesmo fato, e um rollback que levasse uma sem a outra
                # deixaria o registro contradizendo o dinheiro.
                zero_commission_for_refund(order)
            self.order_repository.create_status_history(
                OrderStatusHistory(
                    order_id=order_id,
                    status=payment_history_status(new_payment_status),
                    changed_by=f"gateway:{provider}",
                    note=note,
                )
            )
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
