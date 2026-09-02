"""O resgate dentro da criação do pedido, e o crédito dentro da mudança de
status.

Sem banco: aqui o que se prova é a AMARRAÇÃO — quem é chamado, em que ordem,
e com que efeito no total e na comissão. As semânticas do razão (soma, sinal,
idempotência pelo índice) são testadas contra o Postgres em
`test_cashback_credito_e_resgate_db.py`.
"""

import unittest
import uuid
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

from src.schemas.order_schema import CreateOrderRequest
from src.services.admin_order_service import AdminOrderService
from src.models.order_model import Order
from src.services.order_service import OrderService
from tests import fabricas


class FakeDb:
    def commit(self):
        pass

    def rollback(self):
        pass

    def refresh(self, value):
        pass


class FakeOrderRepository:
    def __init__(self):
        self.orders = []

    def create_order(self, order):
        order.id = uuid.uuid4()
        order.order_number = 77
        self.orders.append(order)

    def create_order_items(self, items):
        for item in items:
            item.id = uuid.uuid4()

    def create_order_item_options(self, options):
        pass

    def create_status_history(self, history):
        pass


class FakeCashbackService:
    """O saldo já resolvido: aqui interessa o quanto e o registro."""

    def __init__(self, quanto=Decimal("0")):
        self.quanto = quanto
        self.registrados = []
        self.consultas = []

    def amount_to_redeem(self, *, customer, restaurant_id, branch_id, momento, teto):
        self.consultas.append(teto)
        return min(self.quanto, teto)

    def register_redemption(self, order, amount):
        self.registrados.append((order.id, amount))
        return None


def build_service(*, saldo=Decimal("0"), coupon_discount=None, ordem=None):
    branch = fabricas.filial(is_open=True, accepts_delivery=True, accepts_pickup=True)
    product_id = uuid.uuid4()
    product = fabricas.produto(id=product_id, code="P1")

    service = OrderService(FakeDb())
    service.restaurant_service = SimpleNamespace(
        get_active_restaurant=lambda slug: fabricas.restaurante()
    )
    service.branch_repository = SimpleNamespace(
        get_active_by_id_and_restaurant=lambda branch_id, restaurant_id: branch,
        list_enabled_payment_methods=lambda branch_id: [
            fabricas.forma_de_pagamento(method_type="cash", payment_flow="delivery"),
        ],
    )
    service.branch_hours_service = SimpleNamespace(ensure_branch_is_open=lambda branch_id: None)
    service.menu_repository = SimpleNamespace(
        get_settings=lambda restaurant_id: fabricas.configuracoes(
            service_fee_enabled=False,
            service_fee_amount=Decimal("0"),
            platform_commission_percent=Decimal("10.00"),
        )
    )
    service.product_repository = SimpleNamespace(
        list_active_by_ids=lambda restaurant_id, ids: [product]
    )
    service.order_repository = FakeOrderRepository()
    service.cashback_service = FakeCashbackService(saldo)
    service.customer_repository = SimpleNamespace(
        lock_customer=lambda customer_id: (ordem is not None) and ordem.append("cliente")
    )
    cupom = SimpleNamespace(id=uuid.uuid4(), code="SAVE", discount_type="fixed")

    def travar_cupom(**kwargs):
        if ordem is not None:
            ordem.append("cupom")
        return cupom, coupon_discount

    # O dublê é ligado SEMPRE, e não só quando o teste tem cupom. Desde a
    # auto-aplicação (28/08/2026), `create_order` chama o CouponService
    # mesmo sem cupom no corpo — para descobrir se há campanha automática —,
    # e o service de verdade sobre um `FakeDb` estoura num `scalars` que
    # nada aqui montou, longe de onde este dublê é lido.
    service.coupon_service = SimpleNamespace(
        lock_and_validate_for_order=travar_cupom,
        auto_apply_for_order=lambda **kwargs: None,
        create_redemption=lambda *args, **kwargs: None,
    )

    body = {
        "branch_id": str(branch.id),
        "order_type": "pickup",
        "payment_method": "cash",
        "customer": {"name": "Ana", "phone": "85999999999"},
        "items": [{"product_id": str(product_id), "quantity": 2}],
        "use_cashback": True,
    }
    if coupon_discount is not None:
        body["coupon_code"] = "SAVE"
    return service, CreateOrderRequest.model_validate(body)


def cliente():
    return fabricas.cliente(name="Ana", phone="85999999999")


class ResgateNoPedidoTests(unittest.TestCase):
    def test_o_resgate_entra_no_total_e_na_base_da_comissao(self):
        """A decisão comercial da frente, em números.

        Pedido de R$ 100 com R$ 10 de cashback: o cliente paga R$ 90 e a
        comissão incide sobre R$ 90. É a MESMA regra do cupom — duas
        mecânicas para dois descontos parecidos seria impossível de explicar
        ao lojista.
        """
        service, payload = build_service(saldo=Decimal("10.00"))

        service.create_order("junior", payload, cliente())

        order = service.order_repository.orders[0]
        self.assertEqual(order.cashback_redeemed_amount, Decimal("10.00"))
        self.assertEqual(order.discount_total, Decimal("10.00"))
        self.assertEqual(order.total, Decimal("90.00"))
        self.assertEqual(order.commission_base_amount, Decimal("90.00"))
        self.assertEqual(order.commission_amount, Decimal("9.00"))

    def test_o_teto_do_resgate_e_o_subtotal_menos_o_cupom(self):
        """O cashback nunca desconta taxa, e nunca leva o total abaixo de
        zero. Com R$ 20 de cupom sobre R$ 100, sobram R$ 80 de teto."""
        service, payload = build_service(saldo=Decimal("500.00"), coupon_discount=Decimal("20.00"))

        service.create_order("junior", payload, cliente())

        self.assertEqual(service.cashback_service.consultas, [Decimal("80.00")])
        order = service.order_repository.orders[0]
        self.assertEqual(order.total, Decimal("0.00"))
        self.assertEqual(order.commission_base_amount, Decimal("0.00"))

    def test_o_cliente_e_travado_ANTES_do_cupom(self):
        """Ordem fixa de lock em todo caminho.

        Duas transações que peguem os mesmos dois recursos em ordens opostas
        fecham ciclo, e o Postgres mata uma por deadlock — no checkout, no
        horário de pico.
        """
        ordem = []
        service, payload = build_service(
            saldo=Decimal("10.00"), coupon_discount=Decimal("5.00"), ordem=ordem
        )

        service.create_order("junior", payload, cliente())

        self.assertEqual(ordem, ["cliente", "cupom"])

    def test_o_resgate_e_registrado_no_razao_com_o_pedido_ja_criado(self):
        service, payload = build_service(saldo=Decimal("10.00"))

        service.create_order("junior", payload, cliente())

        order = service.order_repository.orders[0]
        self.assertEqual(service.cashback_service.registrados, [(order.id, Decimal("10.00"))])

    def test_convidado_com_use_cashback_recebe_401(self):
        """Mesma resposta que o cupom dá, e pelo mesmo motivo: sem conta não
        há saldo. As duas mecânicas de desconto não podem divergir em nada
        que o app precise explicar."""
        service, payload = build_service(saldo=Decimal("10.00"))

        with self.assertRaises(HTTPException) as raised:
            service.create_order("junior", payload)

        self.assertEqual(raised.exception.status_code, 401)

    def test_sem_use_cashback_nao_ha_consulta_de_saldo(self):
        service, payload = build_service(saldo=Decimal("10.00"))
        payload.use_cashback = False

        service.create_order("junior", payload, cliente())

        self.assertEqual(service.cashback_service.consultas, [])
        self.assertEqual(service.order_repository.orders[0].cashback_redeemed_amount, Decimal("0"))

    def test_saldo_zerado_nao_e_erro(self):
        """Loja sem campanha, saldo abaixo do mínimo, cliente novo: nada
        disso é culpa de quem está pedindo, e o pedido fecha normal."""
        service, payload = build_service(saldo=Decimal("0"))

        service.create_order("junior", payload, cliente())

        order = service.order_repository.orders[0]
        self.assertEqual(order.cashback_redeemed_amount, Decimal("0"))
        self.assertEqual(order.total, Decimal("100.00"))


class ImpressaoDigitalTests(unittest.TestCase):
    def test_use_cashback_ENTRA_no_fingerprint(self):
        """Campo que muda o PEDIDO entra na assinatura, e tem que entrar.

        `use_cashback` muda o total, então a mesma chave com este campo
        diferente é conflito de verdade — e recusar (422, pedindo chave nova)
        é a resposta certa. O preço é 24h de 422 para chaves em voo no deploy
        (armadilha 37).
        """
        _, com = build_service()
        _, sem = build_service()
        sem.use_cashback = False

        self.assertNotEqual(
            OrderService._idempotency_fingerprint(com),
            OrderService._idempotency_fingerprint(sem),
        )


class CreditoNaMudancaDeStatusTests(unittest.TestCase):
    """O crédito e a devolução são chamados nos status certos.

    Sem banco: o que importa aqui é que `completed` credita e que
    `cancelled`/`rejected` devolvem — a conta em si é testada contra o
    Postgres.
    """

    def build(self, order):
        service = AdminOrderService(FakeDb())
        repository = SimpleNamespace(
            get_order_detail=lambda order_id, restaurant_id: order,
            update_status=lambda current, novo: setattr(current, "status", novo),
            create_status_history=lambda history: None,
        )
        service.creditados = []
        service.devolvidos = []
        # A leitura é do AdminOrderService e a escrita é do
        # OrderStatusChangeService que ele delega; o cashback mora no writer,
        # que é o mesmo caminho do cancelamento pelo cliente.
        service.order_repository = repository
        service.status_change_service.order_repository = repository
        service.status_change_service.coupon_service = SimpleNamespace(
            reverse_for_order=lambda order_id: None
        )
        service.status_change_service.cashback_service = SimpleNamespace(
            credit_for_order=lambda pedido: service.creditados.append(pedido.id),
            refund_redemption=lambda pedido: service.devolvidos.append(pedido.id),
        )
        return service

    def pedido(self, status="out_for_delivery"):
        # Model de verdade, transiente. O motivo esta no docstring do
        # `make_order` de test_admin_order_cancel.py.
        return Order(
            id=uuid.uuid4(),
            restaurant_id=uuid.uuid4(),
            branch_id=uuid.uuid4(),
            status=status,
            order_type="delivery",
            payment_status="on_delivery",
            order_number=99,
            cashback_redeemed_amount=Decimal("0"),
        )

    def aplicar(self, service, order, novo_status):
        """O `patch` da resposta e o mesmo de `test_admin_order_cancel`: o
        pedido dublê não tem itens nem histórico, e montar o `OrderDetail`
        aqui seria construir meia resposta para não olhar para ela."""
        from src.api.dependencies.admin_scope import AdminScope

        with patch.object(OrderService, "to_order_detail_response", return_value="detail"):
            self._chamar(service, order, novo_status, AdminScope)

    def _chamar(self, service, order, novo_status, AdminScope):
        service._apply_status_change(
            order_id=order.id,
            scope=AdminScope(
                admin_user=SimpleNamespace(id=uuid.uuid4(), email="lojista@exemplo.com"),
                restaurant_id=order.restaurant_id,
                branch_id=None,
            ),
            new_status=novo_status,
            note=None,
            admin_user=SimpleNamespace(id=uuid.uuid4(), email="lojista@exemplo.com"),
            idempotency_key=None,
            route="teste",
            # Cancelar pedido em preparo exige confirmação explícita desde
            # 25/08/2026 (a comida já foi feita). Aqui ela é ruído: o assunto
            # do teste é o cashback, e sem isto ele morreria com 428 no setup.
            confirm_prepared_order=True,
        )

    def test_concluir_credita(self):
        order = self.pedido()
        service = self.build(order)

        self.aplicar(service, order, "completed")

        self.assertEqual(service.creditados, [order.id])
        self.assertEqual(service.devolvidos, [])

    def test_cancelar_devolve_e_nao_credita(self):
        order = self.pedido(status="preparing")
        service = self.build(order)

        self.aplicar(service, order, "cancelled")

        self.assertEqual(service.devolvidos, [order.id])
        self.assertEqual(service.creditados, [])

    def test_recusar_devolve(self):
        order = self.pedido(status="pending")
        service = self.build(order)

        self.aplicar(service, order, "rejected")

        self.assertEqual(service.devolvidos, [order.id])


if __name__ == "__main__":
    unittest.main()
