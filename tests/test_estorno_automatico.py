"""Estorno automatico do pedido cancelado (PaymentRefundService).

O que estes testes PROVAM:

- **a operacao e escolhida pelo estado do GATEWAY, nao pelo nosso.** Cobranca
  aprovada vira estorno; cobranca que ainda nao capturou dinheiro (pix aberto,
  cartao em analise) vira cancelamento. Chamar a errada e 4xx do Mercado
  Pago, e a copia local pode estar defasada — o webhook demora.
- **a corrida do `in_process`**: o antifraude aprova enquanto o lojista
  cancela. O nosso `payment_status` diz `in_review`, o gateway diz `approved`,
  e o desfecho certo e ESTORNAR (nao cancelar), passando por `paid` no
  historico para o pulo ficar contado.
- **o pedido pago na entrega nao gasta uma chamada sequer**, e pedido que
  ainda esta vivo NAO e estornado — as duas checagens que separam "devolver o
  dinheiro" de "devolver o dinheiro de um pedido que a cozinha esta fazendo".
- **falha do gateway nao escreve nada e nao levanta**: vira desfecho
  `falhou`, com o texto `sem estorno automatico` no log (o mesmo grep que
  estava no radar antes de existir estorno nenhum) para a varredura voltar
  nele depois.
- **estorno aceito e nao concluido nao marca `refunded`**: declarar devolvido
  um dinheiro que nao se moveu e pior que nao declarar nada.

O gateway e dublado no NIVEL DAS TRES FUNCOES da integracao
(`fetch_payment`, `cancel_payment`, `refund_payment`) — o formato do que sai
na rede e o que `test_mercadopago_gateway.py` cobre, com o `httpx.Client`
substituido.
"""

import unittest
import uuid
from decimal import Decimal
from unittest.mock import patch

from src.integrations.payment_gateway import (
    GatewayPayment,
    PaymentGatewayUnavailableError,
    RefundResult,
)
from src.models.order_model import Order
from src.services import payment_refund_service as modulo
from src.services.commission import zero_commission_for_refund
from src.services.payment_refund_service import (
    ACTION_CANCELLED,
    ACTION_FAILED,
    ACTION_NOTHING_TO_DO,
    ACTION_REFUND_IN_PROCESS,
    ACTION_REFUNDED,
    PaymentRefundService,
)


RESTAURANT_ID = uuid.uuid4()


class FakeDb:
    def __init__(self):
        self.events = []

    def commit(self):
        self.events.append("commit")

    def rollback(self):
        self.events.append("rollback")


class FakeOrderRepository:
    def __init__(self, order):
        self.order = order
        self.history = []

    def get_order_detail(self, order_id, restaurant_id):
        if self.order.id != order_id or self.order.restaurant_id != restaurant_id:
            return None
        return self.order

    def update_payment_status(self, order, payment_status, paid_at=None):
        order.payment_status = payment_status
        if paid_at is not None:
            order.paid_at = paid_at
        return order

    def create_status_history(self, history):
        self.history.append(history)
        return history


def make_order(**overrides):
    """Pedido TRANSIENTE — o model de verdade, sem sessao e sem banco.

    Nao e `SimpleNamespace` de proposito (ver CLAUDE.md): um objeto de
    atributos livres responderia qualquer campo que o teste escrevesse e
    nenhum que ele esquecesse, e este service le SETE colunas do pedido. Com
    o model, uma coluna renomeada quebra aqui em vez de quebrar em producao.

    O default e o caso central: pedido cancelado, pago por pix online, com a
    cobranca confirmada — dinheiro do cliente parado na conta do lojista.
    """
    values = {
        "id": uuid.uuid4(),
        "restaurant_id": RESTAURANT_ID,
        "status": "cancelled",
        "order_type": "delivery",
        "payment_flow": "online",
        "payment_method": "pix",
        "payment_status": "paid",
        "payment_provider": "mercadopago",
        "provider_payment_id": "mp-123",
        "order_number": 4711,
        "total": Decimal("93.00"),
        "refunded_amount": Decimal("0"),
    }
    values.update(overrides)
    return Order(**values)


def build_service(order):
    service = PaymentRefundService(FakeDb())
    service.order_repository = FakeOrderRepository(order)
    # Colaborador, nao dado nosso: o que importa e que o access_token do
    # RESTAURANTE do pedido chegue ao gateway.
    service.payment_credential_service = _FakeCredentialService()
    return service


class _FakeCredentialService:
    def get_active_credential(self, restaurant_id):
        from types import SimpleNamespace

        return SimpleNamespace(access_token=f"token-de-{restaurant_id}")


def gateway(status, refunded=Decimal("0"), raw=None):
    return GatewayPayment(
        payment_status=status,
        raw_status=raw or status,
        refunded_amount=refunded,
    )


def refund_ok(amount=Decimal("93.00")):
    return RefundResult(
        provider_refund_id="refund-1",
        amount=amount,
        settled=True,
        raw_status="approved",
    )


class EscolhaDaOperacaoTests(unittest.TestCase):
    """Qual das duas operacoes o gateway recebe, por estado."""

    def test_cobranca_aprovada_e_estornada(self):
        order = make_order()
        service = build_service(order)

        with patch.object(modulo, "fetch_payment", return_value=gateway("paid")):
            with patch.object(modulo, "refund_payment", return_value=refund_ok()) as estorno:
                with patch.object(modulo, "cancel_payment") as cancelamento:
                    outcome = service.refund_terminal_order(order.id, RESTAURANT_ID)

        self.assertEqual(outcome.action, ACTION_REFUNDED)
        self.assertTrue(outcome.resolved)
        cancelamento.assert_not_called()
        estorno.assert_called_once()
        self.assertEqual(estorno.call_args.kwargs["provider_payment_id"], "mp-123")
        self.assertEqual(order.payment_status, "refunded")
        self.assertEqual(order.refunded_amount, Decimal("93.00"))

    def test_pix_aberto_e_cancelado_e_nao_estornado(self):
        # Ninguem pagou nada: nao ha estorno, ha uma cobranca a matar. E o
        # que impede o cliente de pagar, no app do banco, um pedido que
        # ninguem vai produzir.
        order = make_order(payment_status="pending")
        service = build_service(order)

        with patch.object(modulo, "fetch_payment", return_value=gateway("pending")):
            with patch.object(modulo, "cancel_payment") as cancelamento:
                with patch.object(modulo, "refund_payment") as estorno:
                    outcome = service.refund_terminal_order(order.id, RESTAURANT_ID)

        self.assertEqual(outcome.action, ACTION_CANCELLED)
        estorno.assert_not_called()
        cancelamento.assert_called_once()
        # `failed` e o que o webhook desta MESMA cobranca escreveria: o
        # `cancelled` deles ja traduz para `failed` aqui.
        self.assertEqual(order.payment_status, "failed")

    def test_cartao_em_analise_e_cancelado(self):
        order = make_order(payment_method="credit_card", payment_status="in_review")
        service = build_service(order)

        with patch.object(modulo, "fetch_payment", return_value=gateway("in_review")):
            with patch.object(modulo, "cancel_payment") as cancelamento:
                with patch.object(modulo, "refund_payment") as estorno:
                    outcome = service.refund_terminal_order(order.id, RESTAURANT_ID)

        self.assertEqual(outcome.action, ACTION_CANCELLED)
        estorno.assert_not_called()
        cancelamento.assert_called_once()
        self.assertEqual(order.payment_status, "failed")

    def test_gateway_ja_estornado_so_sincroniza(self):
        # Alguem devolveu no painel do Mercado Pago antes de a varredura
        # chegar. Nao ha segunda devolucao a fazer — so a copia local a
        # acertar.
        order = make_order()
        service = build_service(order)

        with patch.object(
            modulo, "fetch_payment", return_value=gateway("refunded", refunded=Decimal("93.00"))
        ):
            with patch.object(modulo, "refund_payment") as estorno:
                with patch.object(modulo, "cancel_payment") as cancelamento:
                    outcome = service.refund_terminal_order(order.id, RESTAURANT_ID)

        self.assertEqual(outcome.action, ACTION_NOTHING_TO_DO)
        self.assertTrue(outcome.resolved)
        estorno.assert_not_called()
        cancelamento.assert_not_called()
        self.assertEqual(order.payment_status, "refunded")
        self.assertEqual(order.refunded_amount, Decimal("93.00"))


class CorridaDoAntifraudeTests(unittest.TestCase):
    """O `in_process` que vira `approved` no meio do cancelamento.

    E a corrida mais provavel do cartao: o antifraude pode segurar a cobranca
    por ate 48h uteis, e nenhum lojista espera 48h para recusar um pedido de
    almoco. Decidir pela copia local aqui manda `cancel` numa cobranca ja
    aprovada, que e 4xx do gateway e dinheiro que fica.
    """

    def test_aprovado_durante_a_analise_e_estornado_e_nao_cancelado(self):
        order = make_order(payment_method="credit_card", payment_status="in_review")
        service = build_service(order)

        with patch.object(modulo, "fetch_payment", return_value=gateway("paid")):
            with patch.object(modulo, "refund_payment", return_value=refund_ok()) as estorno:
                with patch.object(modulo, "cancel_payment") as cancelamento:
                    outcome = service.refund_terminal_order(order.id, RESTAURANT_ID)

        self.assertEqual(outcome.action, ACTION_REFUNDED)
        cancelamento.assert_not_called()
        estorno.assert_called_once()
        self.assertEqual(order.payment_status, "refunded")

    def test_o_pulo_de_in_review_para_refunded_passa_por_paid_no_historico(self):
        # `in_review -> refunded` nao existe no grafo, e nao deve existir: o
        # pagamento foi APROVADO antes de ser devolvido, e apagar esse passo
        # faria o historico do cliente mentir sobre o que aconteceu com o
        # dinheiro dele.
        order = make_order(payment_method="credit_card", payment_status="in_review")
        service = build_service(order)

        with patch.object(modulo, "fetch_payment", return_value=gateway("paid")):
            with patch.object(modulo, "refund_payment", return_value=refund_ok()):
                service.refund_terminal_order(order.id, RESTAURANT_ID)

        escritos = [entry.status for entry in service.order_repository.history]
        self.assertEqual(escritos, ["payment:paid", "payment:refunded"])
        self.assertEqual(order.payment_status, "refunded")


class NadaAFazerTests(unittest.TestCase):
    def test_pedido_pago_na_entrega_nao_fala_com_o_gateway(self):
        order = make_order(payment_flow="delivery", payment_status="on_delivery")
        service = build_service(order)

        with patch.object(modulo, "fetch_payment") as consulta:
            outcome = service.refund_terminal_order(order.id, RESTAURANT_ID)

        self.assertEqual(outcome.action, ACTION_NOTHING_TO_DO)
        consulta.assert_not_called()
        self.assertEqual(service.db.events, [])

    def test_pedido_vivo_nao_e_estornado(self):
        # A protecao mais importante do arquivo: devolver o dinheiro de um
        # pedido que a cozinha esta preparando e prejuizo direto do lojista.
        order = make_order(status="preparing")
        service = build_service(order)

        with patch.object(modulo, "fetch_payment") as consulta:
            with self.assertLogs("uvicorn.error", level="WARNING"):
                outcome = service.refund_terminal_order(order.id, RESTAURANT_ID)

        self.assertEqual(outcome.action, ACTION_NOTHING_TO_DO)
        consulta.assert_not_called()
        self.assertEqual(order.payment_status, "paid")

    def test_pedido_concluido_nao_e_estornado(self):
        # `completed` tambem e terminal, e e o unico terminal em que HOUVE
        # venda. Um `status in TERMINAL_ORDER_STATUSES` solto devolveria o
        # dinheiro de todo pedido entregue.
        order = make_order(status="completed")
        service = build_service(order)

        with patch.object(modulo, "fetch_payment") as consulta:
            with self.assertLogs("uvicorn.error", level="WARNING"):
                outcome = service.refund_terminal_order(order.id, RESTAURANT_ID)

        self.assertEqual(outcome.action, ACTION_NOTHING_TO_DO)
        consulta.assert_not_called()

    def test_cobranca_nunca_criada_nao_fala_com_o_gateway(self):
        # Pedido online em que o cliente fechou o checkout antes de clicar em
        # pagar: nao ha id de cobranca para consultar.
        order = make_order(payment_status="pending", provider_payment_id=None)
        service = build_service(order)

        with patch.object(modulo, "fetch_payment") as consulta:
            outcome = service.refund_terminal_order(order.id, RESTAURANT_ID)

        self.assertEqual(outcome.action, ACTION_NOTHING_TO_DO)
        consulta.assert_not_called()

    def test_pedido_ja_estornado_nao_estorna_de_novo(self):
        order = make_order(payment_status="refunded")
        service = build_service(order)

        with patch.object(modulo, "fetch_payment") as consulta:
            outcome = service.refund_terminal_order(order.id, RESTAURANT_ID)

        self.assertEqual(outcome.action, ACTION_NOTHING_TO_DO)
        consulta.assert_not_called()


class FalhaDoGatewayTests(unittest.TestCase):
    def test_gateway_fora_do_ar_nao_levanta_e_nao_escreve(self):
        order = make_order()
        service = build_service(order)

        with patch.object(
            modulo, "fetch_payment", side_effect=PaymentGatewayUnavailableError("timeout")
        ):
            with self.assertLogs("uvicorn.error", level="WARNING") as captured:
                outcome = service.refund_terminal_order(order.id, RESTAURANT_ID)

        self.assertEqual(outcome.action, ACTION_FAILED)
        self.assertFalse(outcome.resolved)
        # O pedido continua `paid` e cancelado — que e exatamente o conjunto
        # que a varredura procura. Nao ha coluna de fila justamente por isso.
        self.assertEqual(order.payment_status, "paid")
        self.assertEqual(service.order_repository.history, [])
        # O grep que ja estava no radar antes de existir estorno automatico.
        self.assertIn("sem estorno automatico", "\n".join(captured.output))

    def test_estorno_aceito_e_nao_concluido_nao_marca_refunded(self):
        order = make_order()
        service = build_service(order)
        em_processamento = RefundResult(
            provider_refund_id="refund-1",
            amount=Decimal("93.00"),
            settled=False,
            raw_status="in_process",
        )

        with patch.object(modulo, "fetch_payment", return_value=gateway("paid")):
            with patch.object(modulo, "refund_payment", return_value=em_processamento):
                with self.assertLogs("uvicorn.error", level="WARNING"):
                    outcome = service.refund_terminal_order(order.id, RESTAURANT_ID)

        self.assertEqual(outcome.action, ACTION_REFUND_IN_PROCESS)
        self.assertFalse(outcome.resolved)
        # Quem fecha e o webhook. Marcar aqui declararia devolvido um
        # dinheiro que nao se moveu.
        self.assertEqual(order.payment_status, "paid")
        self.assertEqual(order.refunded_amount, Decimal("0"))

    def test_status_do_gateway_sem_traducao_falha_alto(self):
        order = make_order()
        service = build_service(order)
        desconhecido = GatewayPayment(
            payment_status=None, raw_status="authorized_pending_capture"
        )

        with patch.object(modulo, "fetch_payment", return_value=desconhecido):
            with patch.object(modulo, "refund_payment") as estorno:
                with self.assertLogs("uvicorn.error", level="WARNING") as captured:
                    outcome = service.refund_terminal_order(order.id, RESTAURANT_ID)

        self.assertEqual(outcome.action, ACTION_FAILED)
        estorno.assert_not_called()
        self.assertIn("sem estorno automatico", "\n".join(captured.output))


class EstornoParcialAnteriorTests(unittest.TestCase):
    def test_o_valor_gravado_e_cumulativo(self):
        # O lojista ja tinha devolvido R$ 20 no painel deles; o pedido e
        # cancelado depois. O `amount` do nosso estorno e so o que faltava, e
        # `orders.refunded_amount` guarda o TOTAL devolvido — somar errado
        # aqui faria um relatorio futuro de dinheiro devolvido mentir.
        order = make_order(refunded_amount=Decimal("20.00"))
        service = build_service(order)

        with patch.object(
            modulo, "fetch_payment", return_value=gateway("paid", refunded=Decimal("20.00"))
        ):
            with patch.object(
                modulo, "refund_payment", return_value=refund_ok(amount=Decimal("73.00"))
            ):
                service.refund_terminal_order(order.id, RESTAURANT_ID)

        self.assertEqual(order.refunded_amount, Decimal("93.00"))
        self.assertEqual(order.payment_status, "refunded")


class SandboxTests(unittest.TestCase):
    """O sandbox nao guarda estado, e a queda para a copia local e o desenho.

    Nao e atalho de teste: `fetch_payment` devolve None de proposito para o
    provider que nao tem o que consultar, e sem essa queda o fluxo de
    cancelamento de pedido pago deixaria de ser demonstravel sem o Mercado
    Pago responder — que e a razao de o sandbox existir.
    """

    def test_estorno_no_sandbox_usa_o_payment_status_local(self):
        order = make_order(payment_provider="sandbox", provider_payment_id="sandbox-1")
        service = build_service(order)

        with patch.object(modulo, "fetch_payment", return_value=None):
            with patch.object(modulo, "refund_payment", return_value=refund_ok(Decimal("0"))):
                outcome = service.refund_terminal_order(order.id, RESTAURANT_ID)

        self.assertEqual(outcome.action, ACTION_REFUNDED)
        self.assertEqual(order.payment_status, "refunded")
        # Estorno TOTAL sem valor informado vale o total do pedido: "total"
        # ja diz qual e o numero, e gravar zero deixaria a coluna mentindo.
        self.assertEqual(order.refunded_amount, Decimal("93.00"))


if __name__ == "__main__":
    unittest.main()


class ComissaoNoEstornoTests(unittest.TestCase):
    """Estorno TOTAL zera a comissao — decisao de 25/08/2026.

    Ela reverteu o que este repositorio dizia antes ("as tres colunas sao um
    registro congelado, e nao um saldo"). O argumento continua verdadeiro e
    perdeu para um mais forte: cobrar comissao de venda que nao existiu e
    indefensavel.

    Na FATURA nada muda — `billable_order_conditions` ja excluia
    `payment_status='refunded'`. O que muda e o registro parar de contradizer
    a fatura: um pedido estornado tinha `commission_amount = 9,00` gravado e
    cobranca zero, e quem lesse a coluna sem conhecer o filtro chegava ao
    numero errado.
    """

    def pedido_com_comissao(self, **overrides):
        valores = {
            "commission_percent": Decimal("10.00"),
            "commission_base_amount": Decimal("90.00"),
            "commission_amount": Decimal("9.00"),
        }
        valores.update(overrides)
        return make_order(**valores)

    def test_estorno_total_zera_base_e_valor(self):
        order = self.pedido_com_comissao()
        service = build_service(order)

        with patch.object(modulo, "fetch_payment", return_value=gateway("paid")):
            with patch.object(modulo, "refund_payment", return_value=refund_ok()):
                service.refund_terminal_order(order.id, RESTAURANT_ID)

        self.assertEqual(order.payment_status, "refunded")
        self.assertEqual(order.commission_base_amount, Decimal("0.00"))
        self.assertEqual(order.commission_amount, Decimal("0.00"))

    def test_o_percentual_contratado_SOBREVIVE(self):
        # `commission_percent` nao e dinheiro: e a TAXA contratada, e ela
        # continua sendo a mesma taxa daquele restaurante naquele dia.
        # Zera-la faria o pedido estornado parecer um contrato de 0% e
        # apagaria a unica prova de qual percentual valia — que e o que
        # permite ao lojista conferir o extrato meses depois.
        order = self.pedido_com_comissao()
        service = build_service(order)

        with patch.object(modulo, "fetch_payment", return_value=gateway("paid")):
            with patch.object(modulo, "refund_payment", return_value=refund_ok()):
                service.refund_terminal_order(order.id, RESTAURANT_ID)

        self.assertEqual(order.commission_percent, Decimal("10.00"))
        # E a identidade que as tres colunas mantem continua valendo.
        self.assertEqual(
            order.commission_amount,
            order.commission_base_amount * order.commission_percent / Decimal("100"),
        )

    def test_cancelar_a_cobranca_NAO_zera_a_comissao(self):
        # Cobranca cancelada vira `failed`, nao `refunded`: ninguem pagou
        # nada e nao houve estorno. A comissao daquele pedido ja estava fora
        # do extrato pelo `status`, e zerar aqui misturaria dois fatos
        # diferentes na mesma coluna.
        order = self.pedido_com_comissao(payment_status="pending")
        service = build_service(order)

        with patch.object(modulo, "fetch_payment", return_value=gateway("pending")):
            with patch.object(modulo, "cancel_payment"):
                service.refund_terminal_order(order.id, RESTAURANT_ID)

        self.assertEqual(order.payment_status, "failed")
        self.assertEqual(order.commission_amount, Decimal("9.00"))

    def test_estorno_aceito_e_nao_concluido_nao_zera_nada(self):
        # O dinheiro nao se moveu. Zerar aqui seria tirar a comissao antes de
        # o cliente receber de volta.
        order = self.pedido_com_comissao()
        service = build_service(order)
        em_processamento = RefundResult(
            provider_refund_id="refund-1",
            amount=Decimal("93.00"),
            settled=False,
            raw_status="in_process",
        )

        with patch.object(modulo, "fetch_payment", return_value=gateway("paid")):
            with patch.object(modulo, "refund_payment", return_value=em_processamento):
                with self.assertLogs("uvicorn.error", level="WARNING"):
                    service.refund_terminal_order(order.id, RESTAURANT_ID)

        self.assertEqual(order.commission_amount, Decimal("9.00"))

    def test_zerar_duas_vezes_da_zero(self):
        # O gateway reenvia a mesma notificacao ate receber 2xx, e a varredura
        # pode passar de novo pelo mesmo pedido.
        order = self.pedido_com_comissao()
        zero_commission_for_refund(order)
        zero_commission_for_refund(order)

        self.assertEqual(order.commission_amount, Decimal("0.00"))
        self.assertEqual(order.commission_percent, Decimal("10.00"))
