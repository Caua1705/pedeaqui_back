"""Validacoes de aceite do pedido (Fase 2, bloco D).

Tudo aqui existia no banco e nao era lido na criacao do pedido: a loja podia
estar fechada, com retirada desligada ou receber uma forma de pagamento que
nao aceita, e o pedido entrava assim mesmo. Quem bloqueava era o frontend —
ou seja, ninguem.

Desde a revisao 20260818_0025 as tres chaves (`is_open`, `accepts_delivery`,
`accepts_pickup`) sao da FILIAL, e por isso elas moram no objeto `branch`
destes dubles e nao mais no de `restaurant_settings`. Um teste que as
colocasse de volta em settings passaria a nao provar nada: o service nem
olharia para elas.
"""

import unittest
import uuid
from decimal import Decimal
from types import SimpleNamespace

from fastapi import HTTPException

from src.schemas.order_schema import CreateOrderRequest
from src.services.order_service import OrderService


class FakeDb:
    def __init__(self):
        self.events = []

    def commit(self):
        self.events.append("commit")

    def rollback(self):
        self.events.append("rollback")

    def refresh(self, value):
        pass


class FakeOrderRepository:
    def __init__(self):
        self.orders = []

    def create_order(self, order):
        order.id = uuid.uuid4()
        order.order_number = 500
        self.orders.append(order)

    def create_order_items(self, items):
        for item in items:
            item.id = uuid.uuid4()

    def create_order_item_options(self, options):
        pass

    def create_status_history(self, history):
        pass


def build_service(
    *,
    is_open=True,
    accepts_pickup=True,
    accepts_delivery=True,
    branch_is_open=True,
    enabled_payment_methods=(("cash", "delivery"),),
):
    branch = SimpleNamespace(
        id=uuid.uuid4(),
        is_open=is_open,
        accepts_delivery=accepts_delivery,
        accepts_pickup=accepts_pickup,
        # Nulos: esta filial nao sobrescreve nada e herda os padroes do
        # restaurante, que e o estado em que toda filial nasce.
        min_order_value=None,
        service_fee_enabled=None,
        service_fee_amount=None,
        estimated_delivery_time_min=None,
        estimated_delivery_time_max=None,
        default_delivery_fee=None,
    )
    product_id = uuid.uuid4()
    product = SimpleNamespace(
        id=product_id,
        code="P1",
        name="Picanha",
        description=None,
        price=Decimal("50.00"),
        option_groups=[],
    )

    service = OrderService(FakeDb())
    service.restaurant_service = SimpleNamespace(
        get_active_restaurant=lambda slug: SimpleNamespace(id=uuid.uuid4())
    )
    service.branch_repository = SimpleNamespace(
        get_active_by_id_and_restaurant=lambda branch_id, restaurant_id: branch,
        list_enabled_payment_methods=lambda branch_id: [
            SimpleNamespace(method_type=method, payment_flow=flow)
            for method, flow in enabled_payment_methods
        ],
    )
    service.branch_hours_service = SimpleNamespace(
        ensure_branch_is_open=_open_branch if branch_is_open else _closed_branch
    )
    service.menu_repository = SimpleNamespace(
        get_settings=lambda restaurant_id: SimpleNamespace(
            min_order_value=Decimal("0"),
            service_fee_enabled=False,
            service_fee_amount=Decimal("0"),
            estimated_delivery_time_min=None,
            estimated_delivery_time_max=None,
            default_delivery_fee=None,
            platform_commission_percent=Decimal("10.00"),
        )
    )
    service.product_repository = SimpleNamespace(
        list_active_by_ids=lambda restaurant_id, ids: [product]
    )
    service.order_repository = FakeOrderRepository()
    return service, branch, product_id


def _open_branch(branch_id):
    return SimpleNamespace(prep_time_min=20, prep_time_max=30)


def _closed_branch(branch_id):
    raise HTTPException(status_code=400, detail="A loja esta fechada neste horario")


def build_payload(branch, product_id, *, order_type="pickup", payment_method="cash"):
    body = {
        "branch_id": str(branch.id),
        "order_type": order_type,
        "customer": {"name": "Ana", "phone": "85999999999"},
        "items": [{"product_id": str(product_id), "quantity": 1}],
    }
    if payment_method is not None:
        body["payment_method"] = payment_method
    return CreateOrderRequest.model_validate(body)


class StoreAvailabilityTests(unittest.TestCase):
    def test_closed_branch_does_not_accept_order(self):
        service, branch, product_id = build_service(is_open=False)

        with self.assertRaises(HTTPException) as raised:
            service.create_order("junior", build_payload(branch, product_id))

        self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(service.order_repository.orders, [])

    def test_pickup_disabled_refuses_pickup_order(self):
        service, branch, product_id = build_service(accepts_pickup=False)

        with self.assertRaises(HTTPException) as raised:
            service.create_order("junior", build_payload(branch, product_id))

        self.assertEqual(raised.exception.status_code, 400)

    def test_pickup_disabled_does_not_block_delivery_order(self):
        # A checagem e por tipo de pedido: desligar retirada nao pode
        # derrubar a entrega junto.
        service, branch, product_id = build_service(accepts_pickup=False)
        service._estimate_delivery = lambda *args, **kwargs: None
        payload = build_payload(branch, product_id, order_type="delivery")
        payload.address = SimpleNamespace(
            street="Rua A", number="1", neighborhood="Centro",
            complement=None, reference=None, city="Fortaleza", state="CE", zipcode=None,
        )

        service.create_order("junior", payload)

        self.assertEqual(len(service.order_repository.orders), 1)

    def test_branch_closed_now_refuses_even_with_the_switch_on(self):
        # Duas coisas diferentes na MESMA filial: `is_open` e a pausa manual,
        # o horario e o cadastro da semana. Pedido as 3h da manha nao passa
        # nem com a chave ligada.
        service, branch, product_id = build_service(branch_is_open=False)

        with self.assertRaises(HTTPException) as raised:
            service.create_order("junior", build_payload(branch, product_id))

        self.assertEqual(raised.exception.status_code, 400)

    def test_the_pause_of_one_branch_does_not_travel_to_another(self):
        """O motivo desta migracao inteira, num teste.

        Antes as tres chaves eram de `restaurant_settings` e o mesmo objeto
        respondia por todas as filiais: pausar uma pausava a rede. Aqui as
        duas filiais dividem o mesmo restaurante e o mesmo `settings`, e so a
        pausada recusa.
        """
        service, pausada, product_id = build_service(is_open=False)
        aberta = SimpleNamespace(
            id=uuid.uuid4(),
            is_open=True,
            accepts_delivery=True,
            accepts_pickup=True,
            min_order_value=None,
            service_fee_enabled=None,
            service_fee_amount=None,
            estimated_delivery_time_min=None,
            estimated_delivery_time_max=None,
            default_delivery_fee=None,
        )
        filiais = {pausada.id: pausada, aberta.id: aberta}
        service.branch_repository.get_active_by_id_and_restaurant = (
            lambda branch_id, restaurant_id: filiais[branch_id]
        )

        with self.assertRaises(HTTPException):
            service.create_order("junior", build_payload(pausada, product_id))

        service.create_order("junior", build_payload(aberta, product_id))
        self.assertEqual(len(service.order_repository.orders), 1)


class PaymentMethodTests(unittest.TestCase):
    def test_order_without_payment_method_is_refused(self):
        service, branch, product_id = build_service()

        with self.assertRaises(HTTPException) as raised:
            service.create_order("junior", build_payload(branch, product_id, payment_method=None))

        self.assertEqual(raised.exception.status_code, 400)

    def test_payment_method_outside_the_platform_list_is_refused(self):
        # Era texto livre de 50 caracteres gravado direto no pedido.
        service, branch, product_id = build_service()

        with self.assertRaises(HTTPException) as raised:
            service.create_order("junior", build_payload(branch, product_id, payment_method="banana"))

        self.assertEqual(raised.exception.status_code, 400)

    def test_method_the_branch_does_not_enable_is_refused(self):
        service, branch, product_id = build_service(
            enabled_payment_methods=(("cash", "delivery"),)
        )

        with self.assertRaises(HTTPException) as raised:
            service.create_order("junior", build_payload(branch, product_id, payment_method="pix"))

        self.assertEqual(raised.exception.status_code, 400)

    def test_disabled_method_of_the_branch_is_refused(self):
        # list_enabled_payment_methods ja filtra enabled=false; o teste
        # documenta que o service confia nessa lista e nao em outra.
        service, branch, product_id = build_service(enabled_payment_methods=())

        with self.assertRaises(HTTPException):
            service.create_order("junior", build_payload(branch, product_id))


class PaymentFlowTests(unittest.TestCase):
    def test_pay_on_delivery_order_is_born_ready_for_the_shopkeeper(self):
        service, branch, product_id = build_service(
            enabled_payment_methods=(("cash", "delivery"),)
        )

        response = service.create_order("junior", build_payload(branch, product_id))

        order = service.order_repository.orders[0]
        self.assertEqual(order.payment_flow, "delivery")
        self.assertEqual(order.payment_status, "on_delivery")
        self.assertEqual(response.payment_status, "on_delivery")

    def test_online_order_is_born_owing(self):
        service, branch, product_id = build_service(
            enabled_payment_methods=(("pix", "online"),)
        )

        response = service.create_order(
            "junior", build_payload(branch, product_id, payment_method="pix")
        )

        order = service.order_repository.orders[0]
        self.assertEqual(order.payment_flow, "online")
        self.assertEqual(order.payment_status, "pending")
        self.assertEqual(order.status, "pending")
        self.assertEqual(response.payment_flow, "online")

    def test_method_offered_in_both_flows_is_treated_as_online(self):
        # Ambiguidade real de configuracao (pix pelo gateway e pix na
        # entrega). Escolhemos o caminho restritivo: exigir pagamento antes
        # de mandar para a cozinha.
        service, branch, product_id = build_service(
            enabled_payment_methods=(("pix", "delivery"), ("pix", "online"))
        )

        service.create_order("junior", build_payload(branch, product_id, payment_method="pix"))

        self.assertEqual(service.order_repository.orders[0].payment_flow, "online")


if __name__ == "__main__":
    unittest.main()
