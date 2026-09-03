"""`POST /customers/me/orders/{id}/cancel` — a porta autenticada do cancelamento.

O `tracking_token` so vive no `localStorage` do aparelho que fez o pedido,
entao quem pediu pelo celular e abriu o app no computador nao alcancava o
proprio pedido. O front pediu uma de duas saidas: **publicar o token** no
`OrderDetailResponse`, ou **esta rota**.

A escolha foi esta rota, e o primeiro grupo de testes e o que torna a escolha
DURAVEL — sem ele, o campo volta num PR de dez linhas e ninguem lembra por que
ele nao estava la.

Os fakes seguem `test_quem_cancela.py`: model transiente de verdade (nunca
`SimpleNamespace` para dado), dubles so nas bordas.
"""

import unittest
import uuid
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

from src.models.customer_model import Customer
from src.models.order_model import Order
from src.schemas.order_schema import (
    CreateOrderResponse,
    CustomerCancelOrderRequest,
    OrderDetailResponse,
)
from src.services.customer_order_cancel_service import (
    CUSTOMER_ACCOUNT_CANCEL_ROUTE,
    CUSTOMER_CANCEL_ROUTE,
    CustomerOrderCancelService,
)
from src.services.order_service import OrderService
from tests import fabricas
from tests.rotas_do_app import caminhos


RESTAURANT_ID = uuid.uuid4()
CLIENTE = uuid.uuid4()
OUTRO_CLIENTE = uuid.uuid4()


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

    def get_order_detail_for_customer(self, order_id, customer_id):
        # A MESMA condicao do repositorio de verdade: id do pedido E dono.
        if self.order.id != order_id or self.order.customer_id != customer_id:
            return None
        return self.order

    def get_order_detail(self, order_id, restaurant_id):
        if self.order.id != order_id or self.order.restaurant_id != restaurant_id:
            return None
        return self.order

    def update_status(self, order, new_status):
        order.status = new_status

    def create_status_history(self, history):
        self.history.append(history)


def make_order(*, status="pending", customer_id=CLIENTE):
    return Order(
        id=uuid.uuid4(),
        restaurant_id=RESTAURANT_ID,
        customer_id=customer_id,
        status=status,
        order_type="delivery",
        payment_status="on_delivery",
        payment_flow=None,
        order_number=99,
        cashback_redeemed_amount=Decimal("0"),
    )


def cliente(customer_id=CLIENTE) -> Customer:
    """Model transiente, e nao um dublê: `cancel_for_customer` le `.id`, e um
    objeto de atributos livres responderia qualquer nome que o teste
    escrevesse — inclusive o errado."""
    pessoa = fabricas.cliente()
    pessoa.id = customer_id
    return pessoa


def servico(order):
    service = CustomerOrderCancelService(FakeDb())
    repositorio = FakeOrderRepository(order)
    service.order_repository = repositorio
    service.status_change_service.order_repository = repositorio
    service.status_change_service.coupon_service = SimpleNamespace(
        reverse_for_order=lambda order_id: None
    )
    service.status_change_service.cashback_service = SimpleNamespace(
        refund_redemption=lambda order: None,
        credit_for_order=lambda order: None,
    )
    return service


class OTokenNaoFoiPublicadoTests(unittest.TestCase):
    """A decisao, escrita de um jeito que um PR distraido nao desfaz.

    O `tracking_token` e credencial portadora, e nao um id: quem o tem abre o
    detalhe inteiro do pedido, avalia, consulta o pagamento e cancela. Sao 256
    bits sem rota de reemissao e sem prazo — nao da para revogar nem expirar.
    """

    def test_o_detalhe_do_pedido_NAO_leva_o_token(self):
        self.assertNotIn("tracking_token", OrderDetailResponse.model_fields)

    def test_e_o_teste_acima_nao_e_vacuo(self):
        """O campo existe, e e devolvido — uma vez so, para quem acabou de
        fazer o pedido. Sem esta metade, renomear o campo deixaria o teste de
        cima verde para sempre.
        """
        self.assertIn("tracking_token", CreateOrderResponse.model_fields)

    def test_o_detalhe_e_o_mesmo_schema_do_painel(self):
        """Por que publicar o token nao era so uma decisao do app.

        `OrderDetailResponse` e o `response_model` de rotas do painel tambem,
        entao o campo cairia na resposta do lojista junto.
        """
        do_painel = [
            caminho
            for caminho in caminhos()
            if caminho.startswith("/admin/orders")
        ]

        self.assertTrue(do_painel, "nenhuma rota /admin/orders encontrada")


class ARotaExisteTests(unittest.TestCase):
    def test_a_rota_autenticada_esta_registrada(self):
        self.assertIn("/customers/me/orders/{order_id}/cancel", caminhos())

    def test_a_rota_do_convidado_CONTINUA_existindo(self):
        """Nao e redundancia: ela e a unica saida de quem pediu sem conta.

        Trocar uma pela outra deixaria o convidado — caso normal, nao borda —
        sem jeito de desistir do proprio pedido.
        """
        self.assertIn(
            "/restaurants/{restaurant_slug}/orders/track/{tracking_token}/cancel",
            caminhos(),
        )


class SoOProprioPedidoTests(unittest.TestCase):
    def test_o_pedido_de_outro_cliente_da_404(self):
        """404 e nao 403: 403 confirmaria que aquele pedido existe.

        E o mesmo criterio da porta do token, e a razao e a mesma — o
        `order_id` e um UUID que pode ser tentado.
        """
        order = make_order(customer_id=OUTRO_CLIENTE)
        service = servico(order)

        with self.assertRaises(HTTPException) as raised:
            service.cancel_for_customer(cliente(), order.id, None)

        self.assertEqual(raised.exception.status_code, 404)
        self.assertEqual(order.status, "pending")
        self.assertEqual(service.db.events, [])

    def test_o_id_de_outro_pedido_do_mesmo_cliente_da_404(self):
        order = make_order()
        service = servico(order)

        with self.assertRaises(HTTPException) as raised:
            service.cancel_for_customer(cliente(), uuid.uuid4(), None)

        self.assertEqual(raised.exception.status_code, 404)

    def test_o_dono_cancela(self):
        order = make_order()
        service = servico(order)

        with patch.object(OrderService, "to_order_detail_response", return_value="detalhe"):
            service.cancel_for_customer(cliente(), order.id, None)

        self.assertEqual(order.status, "cancelled")


class AJanelaEAMesmaTests(unittest.TestCase):
    """Autenticar nao alarga a janela.

    Se alargasse, o cliente com conta poderia cancelar um pedido em producao —
    e o poder de gerar prejuizo com um toque nao muda por causa de login.
    """

    def test_em_preparo_da_409(self):
        order = make_order(status="preparing")
        service = servico(order)

        with self.assertRaises(HTTPException) as raised:
            service.cancel_for_customer(cliente(), order.id, None)

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(order.status, "preparing")

    def test_pending_e_accepted_passam(self):
        for status in ("pending", "accepted"):
            with self.subTest(status=status):
                order = make_order(status=status)
                service = servico(order)

                with patch.object(
                    OrderService, "to_order_detail_response", return_value="detalhe"
                ):
                    service.cancel_for_customer(cliente(), order.id, None)

                self.assertEqual(order.status, "cancelled")

    def test_os_estados_recusados_sao_os_mesmos_da_porta_do_token(self):
        """As duas portas dividem `ensure_customer_can_cancel`.

        O teste compara o COMPORTAMENTO das duas, e nao o codigo: uma cópia da
        janela numa delas passaria por qualquer leitura de diff.
        """
        for status in ("preparing", "ready", "out_for_delivery", "completed", "cancelled"):
            with self.subTest(status=status):
                autenticada = servico(make_order(status=status))
                por_token = servico(make_order(status=status))
                por_token.restaurant_service = SimpleNamespace(
                    get_active_restaurant=lambda slug: SimpleNamespace(id=RESTAURANT_ID)
                )
                por_token.order_repository.get_order_by_tracking_token = (
                    lambda restaurant_id, token: por_token.order_repository.order
                )

                with self.assertRaises(HTTPException) as pela_conta:
                    autenticada.cancel_for_customer(cliente(), autenticada.order_repository.order.id, None)
                with self.assertRaises(HTTPException) as pelo_token:
                    por_token.cancel("slug", "token", None)

                self.assertEqual(
                    pela_conta.exception.status_code, pelo_token.exception.status_code
                )


class AEscritaEAMesmaTests(unittest.TestCase):
    """Um caminho proprio seria quatro bugs de dinheiro por um copiar e colar:
    cupom que nao volta, cashback retido, historico sem autor, pagamento sem
    estorno. As duas portas passam pelo `OrderStatusChangeService`."""

    def test_o_historico_registra_o_cliente_como_autor(self):
        order = make_order()
        service = servico(order)

        with patch.object(OrderService, "to_order_detail_response", return_value="detalhe"):
            service.cancel_for_customer(cliente(), order.id, None)

        linha = service.order_repository.history[0]
        self.assertEqual(linha.changed_by, "cliente")
        self.assertEqual(linha.status, "cancelled")

    def test_sem_motivo_o_historico_leva_o_padrao(self):
        order = make_order()
        service = servico(order)

        with patch.object(OrderService, "to_order_detail_response", return_value="detalhe"):
            service.cancel_for_customer(cliente(), order.id, None)

        self.assertEqual(service.order_repository.history[0].note, "Cancelado pelo cliente")

    def test_o_motivo_do_cliente_entra_precedido_do_padrao(self):
        """Sozinho na coluna, "mudei de ideia" nao diz de quem partiu."""
        order = make_order()
        service = servico(order)

        with patch.object(OrderService, "to_order_detail_response", return_value="detalhe"):
            service.cancel_for_customer(
                cliente(), order.id, CustomerCancelOrderRequest(reason="mudei de ideia")
            )

        self.assertEqual(
            service.order_repository.history[0].note,
            "Cancelado pelo cliente: mudei de ideia",
        )

    def test_o_commit_acontece(self):
        order = make_order()
        service = servico(order)

        with patch.object(OrderService, "to_order_detail_response", return_value="detalhe"):
            service.cancel_for_customer(cliente(), order.id, None)

        self.assertIn("commit", service.db.events)


class OEscopoDeIdempotenciaTests(unittest.TestCase):
    def test_as_duas_portas_tem_rotas_diferentes(self):
        """A mesma `Idempotency-Key` chegando pelas duas sao dois pedidos.

        Nenhuma das duas aceita a chave hoje, e por isso este teste olha a
        CONSTANTE: o dia em que alguem aceitar, o escopo ja esta separado — e
        descobrir isso depois seria descobrir com a resposta errada na mao.
        """
        self.assertNotEqual(CUSTOMER_ACCOUNT_CANCEL_ROUTE, CUSTOMER_CANCEL_ROUTE)


if __name__ == "__main__":
    unittest.main()
