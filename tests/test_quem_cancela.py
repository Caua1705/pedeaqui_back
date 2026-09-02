"""Quem pode cancelar o quê, e quando — as duas metades da mesma regra.

A regra tem um eixo só: **antes do preparo, cancelar é barato; depois, alguém
come o prejuízo.** Ela aparece de dois jeitos, e os dois vivem aqui porque um
sem o outro deixa um buraco:

- o **lojista** cancela em qualquer estado, mas a partir de `preparing`
  precisa confirmar explicitamente — 428 com `confirmation_required`;
- o **cliente** cancela sozinho só até `accepted`. Depois disso, 409 e a
  conversa passa a ser com o restaurante.

O que estes testes protegem além do óbvio:

1. **A confirmação vale nas DUAS portas do painel.** `PATCH /status` aceita
   `status="cancelled"` e seria exatamente por onde o painel pularia o
   diálogo.
2. **428 e não 409.** Os 409 da rota são conflitos de estado de verdade e
   saem com texto; este é o backend pedindo uma precondição, com corpo
   tipado. Sobrepor os dois obrigaria o painel a distinguir pela mensagem.
3. **O cliente passa pela MESMA escrita do painel.** Cupom, cashback,
   histórico e estorno saem de graça — um caminho próprio seria quatro bugs
   de dinheiro por um copiar e colar.
"""

import unittest
import uuid
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

from src.api.dependencies.admin_scope import AdminScope
from src.models.order_model import Order
from src.schemas.admin_order_schema import CancelOrderRequest, UpdateOrderStatusRequest
from src.schemas.order_schema import CustomerCancelOrderRequest
from src.services.admin_order_service import AdminOrderService
from src.services.customer_order_cancel_service import CustomerOrderCancelService
from src.services.order_service import OrderService
from src.services.order_status_change_service import OrderStatusChangeService
from tests import fabricas


ADMIN = fabricas.usuario_do_painel(email="lojista@exemplo.com")
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

    def get_order_by_tracking_token(self, restaurant_id, tracking_token):
        if self.order.restaurant_id != restaurant_id or tracking_token != TOKEN:
            return None
        return self.order

    def update_status(self, order, new_status):
        order.status = new_status

    def create_status_history(self, history):
        self.history.append(history)


TOKEN = "token-do-pedido-do-cliente"


def make_order(*, status="preparing", payment_status="on_delivery", payment_flow=None):
    """Model de verdade, transiente. O motivo está no `make_order` de
    test_admin_order_cancel.py — e foi `confirm_prepared_order` que provou o
    ponto: um `SimpleNamespace` teria respondido o campo novo sozinho."""
    return Order(
        id=uuid.uuid4(),
        restaurant_id=RESTAURANT_ID,
        status=status,
        order_type="delivery",
        payment_status=payment_status,
        payment_flow=payment_flow,
        order_number=99,
        cashback_redeemed_amount=Decimal("0"),
    )


def wire(service, order):
    """Liga o dublê nas duas camadas: leitura em cima, escrita no writer."""
    repository = FakeOrderRepository(order)
    service.order_repository = repository
    service.status_change_service.order_repository = repository
    service.status_change_service.coupon_service = SimpleNamespace(
        reverse_for_order=lambda order_id: None
    )
    return service


def admin_service(order):
    return wire(AdminOrderService(FakeDb()), order)


def customer_service(order):
    service = CustomerOrderCancelService(FakeDb())
    service.restaurant_service = SimpleNamespace(
        get_active_restaurant=lambda slug: SimpleNamespace(id=RESTAURANT_ID)
    )
    return wire(service, order)


def owner_scope():
    return AdminScope(admin_user=ADMIN, restaurant_id=RESTAURANT_ID, branch_id=None)


class ConfirmacaoDoLojistaTests(unittest.TestCase):
    """A partir de `preparing` o lojista precisa de um segundo clique."""

    def test_pedido_em_preparo_exige_confirmacao(self):
        order = make_order(status="preparing")
        service = admin_service(order)

        with self.assertRaises(HTTPException) as raised:
            service.cancel_order(
                order.id,
                owner_scope(),
                CancelOrderRequest(reason="acabou a costela"),
                admin_user=ADMIN,
            )

        self.assertEqual(raised.exception.status_code, 428)
        self.assertEqual(raised.exception.detail["code"], "confirmation_required")
        # O status vai junto para o painel escrever a frase certa: "já saiu
        # para entrega" é uma conversa, "já está em preparo" é outra.
        self.assertEqual(raised.exception.detail["order_status"], "preparing")
        # E nada foi gravado: recusar tem que ser recusar.
        self.assertEqual(order.status, "preparing")
        self.assertEqual(service.order_repository.history, [])
        self.assertEqual(service.db.events, [])

    def test_com_a_confirmacao_o_cancelamento_passa(self):
        order = make_order(status="preparing")
        service = admin_service(order)

        with patch.object(OrderService, "to_order_detail_response", return_value="detail"):
            service.cancel_order(
                order.id,
                owner_scope(),
                CancelOrderRequest(reason="acabou a costela", confirm_prepared_order=True),
                admin_user=ADMIN,
            )

        self.assertEqual(order.status, "cancelled")

    def test_a_confirmacao_vale_para_ready_e_out_for_delivery(self):
        for status in ("ready", "out_for_delivery"):
            with self.subTest(status=status):
                order = make_order(status=status)
                service = admin_service(order)

                with self.assertRaises(HTTPException) as raised:
                    service.cancel_order(
                        order.id,
                        owner_scope(),
                        CancelOrderRequest(reason="cliente sumiu"),
                        admin_user=ADMIN,
                    )

                self.assertEqual(raised.exception.status_code, 428)

    def test_antes_do_preparo_nao_pede_confirmacao(self):
        # Em `pending` e `accepted` nada foi para a praça: exigir o segundo
        # clique aqui seria atrito no caso mais comum de todos.
        for status in ("pending", "accepted"):
            with self.subTest(status=status):
                order = make_order(status=status)
                service = admin_service(order)

                with patch.object(
                    OrderService, "to_order_detail_response", return_value="detail"
                ):
                    service.cancel_order(
                        order.id,
                        owner_scope(),
                        CancelOrderRequest(reason="cliente desistiu"),
                        admin_user=ADMIN,
                    )

                self.assertEqual(order.status, "cancelled")

    def test_o_patch_de_status_nao_e_a_porta_dos_fundos(self):
        # `PATCH /status` aceita status="cancelled". Sem a confirmação aqui,
        # bastava o painel usar a outra rota para pular o diálogo — e a rota
        # de cancelamento viraria uma sugestão.
        order = make_order(status="preparing")
        service = admin_service(order)

        with self.assertRaises(HTTPException) as raised:
            service.update_order_status(
                order.id,
                owner_scope(),
                UpdateOrderStatusRequest(status="cancelled", note=None),
                admin_user=ADMIN,
            )

        self.assertEqual(raised.exception.status_code, 428)
        self.assertEqual(order.status, "preparing")

    def test_recusar_nunca_pede_confirmacao(self):
        # `rejected` só é alcançável a partir de `pending`, onde nada foi
        # preparado. Pedir confirmação ali seria atrito sem motivo.
        order = make_order(status="pending")
        service = admin_service(order)

        with patch.object(OrderService, "to_order_detail_response", return_value="detail"):
            service.update_order_status(
                order.id,
                owner_scope(),
                UpdateOrderStatusRequest(status="rejected", note="fora de área"),
                admin_user=ADMIN,
            )

        self.assertEqual(order.status, "rejected")

    def test_avancar_o_status_nunca_pede_confirmacao(self):
        # A confirmação é do CANCELAMENTO. Um `if` por estado sem olhar o
        # destino faria "marcar como pronto" abrir um diálogo.
        order = make_order(status="preparing")
        service = admin_service(order)

        with patch.object(OrderService, "to_order_detail_response", return_value="detail"):
            service.update_order_status(
                order.id,
                owner_scope(),
                UpdateOrderStatusRequest(status="ready", note=None),
                admin_user=ADMIN,
            )

        self.assertEqual(order.status, "ready")


class CancelamentoPeloClienteTests(unittest.TestCase):
    def test_o_cliente_cancela_antes_do_preparo(self):
        for status in ("pending", "accepted"):
            with self.subTest(status=status):
                order = make_order(status=status)
                service = customer_service(order)

                with patch.object(
                    OrderService, "to_order_detail_response", return_value="detail"
                ):
                    service.cancel("junior-da-picanha", TOKEN)

                self.assertEqual(order.status, "cancelled")

    def test_o_cliente_nao_cancela_pedido_em_preparo(self):
        # A comida já custou dinheiro ao restaurante. Deixar o cliente
        # cancelar aqui daria a ele o poder de gerar prejuízo com um toque,
        # sem ninguém do outro lado saber por quê.
        order = make_order(status="preparing")
        service = customer_service(order)

        with self.assertRaises(HTTPException) as raised:
            service.cancel("junior-da-picanha", TOKEN)

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(order.status, "preparing")
        self.assertEqual(service.db.events, [])

    def test_o_cliente_nao_cancela_pedido_entregue(self):
        order = make_order(status="completed")
        service = customer_service(order)

        with self.assertRaises(HTTPException) as raised:
            service.cancel("junior-da-picanha", TOKEN)

        self.assertEqual(raised.exception.status_code, 409)

    def test_token_errado_e_404_e_nao_403(self):
        # 403 confirmaria que aquele pedido existe.
        order = make_order(status="pending")
        service = customer_service(order)

        with self.assertRaises(HTTPException) as raised:
            service.cancel("junior-da-picanha", "token-de-outra-pessoa")

        self.assertEqual(raised.exception.status_code, 404)

    def test_o_autor_sai_do_backend_e_nao_do_corpo(self):
        order = make_order(status="pending")
        service = customer_service(order)

        with patch.object(OrderService, "to_order_detail_response", return_value="detail"):
            service.cancel("junior-da-picanha", TOKEN)

        entry = service.order_repository.history[0]
        self.assertEqual(entry.changed_by, "cliente")
        self.assertEqual(entry.note, "Cancelado pelo cliente")

    def test_o_motivo_do_cliente_entra_precedido_do_padrao(self):
        # "mudei de ideia" sozinho na coluna não diz de quem partiu.
        order = make_order(status="pending")
        service = customer_service(order)

        with patch.object(OrderService, "to_order_detail_response", return_value="detail"):
            service.cancel(
                "junior-da-picanha", TOKEN, CustomerCancelOrderRequest(reason="mudei de ideia")
            )

        self.assertEqual(
            service.order_repository.history[0].note,
            "Cancelado pelo cliente: mudei de ideia",
        )

    def test_o_cliente_passa_pela_mesma_escrita_do_painel(self):
        # A propriedade que este arquivo existe para travar. Um cancelamento
        # pelo cliente com código próprio seria um caminho em que o cupom não
        # volta, o cashback fica retido e o pagamento não é estornado.
        order = make_order(status="accepted")
        service = customer_service(order)

        with patch.object(OrderStatusChangeService, "apply", return_value="detail") as escrita:
            service.cancel("junior-da-picanha", TOKEN)

        escrita.assert_called_once()
        self.assertEqual(escrita.call_args.kwargs["new_status"], "cancelled")
        self.assertEqual(escrita.call_args.kwargs["changed_by"], "cliente")

    def test_pix_pago_cancelado_pelo_cliente_e_estornado(self):
        # O caso que motivou a rota: o cliente paga o pix e desiste em trinta
        # segundos. Sem isto ele teria que ligar para o restaurante para o
        # lojista cancelar e o dinheiro voltar.
        order = make_order(status="accepted", payment_status="paid", payment_flow="online")
        service = customer_service(order)

        with patch(
            "src.services.order_status_change_service.PaymentRefundService"
        ) as refund_service:
            with patch.object(OrderService, "to_order_detail_response", return_value="detail"):
                service.cancel("junior-da-picanha", TOKEN)

        refund_service.return_value.refund_terminal_order.assert_called_once_with(
            order.id, RESTAURANT_ID
        )


if __name__ == "__main__":
    unittest.main()
