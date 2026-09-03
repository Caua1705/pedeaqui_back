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
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

from fastapi import HTTPException

from tests import fabricas

from src.schemas.order_schema import AddressInput, CreateOrderRequest
from src.services.order_service import OrderService
from src.utils.security import utcnow


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
    # DADO e tipo real (armadilha do `serves_people`); COLABORADOR continua
    # `SimpleNamespace`, que e para o que ele serve. A filial, o produto, o
    # restaurante e as configuracoes abaixo sao dado; os `service.*_repository`
    # sao colaborador.
    branch = fabricas.filial(
        is_open=is_open,
        accepts_delivery=accepts_delivery,
        accepts_pickup=accepts_pickup,
    )
    product = fabricas.produto(code="P1")
    product_id = product.id

    service = OrderService(FakeDb())
    service.restaurant_service = SimpleNamespace(
        get_active_restaurant=lambda slug: fabricas.restaurante()
    )
    service.branch_repository = SimpleNamespace(
        get_active_by_id_and_restaurant=lambda branch_id, restaurant_id: branch,
        list_enabled_payment_methods=lambda branch_id: [
            fabricas.forma_de_pagamento(method_type=method, payment_flow=flow)
            for method, flow in enabled_payment_methods
        ],
    )
    service.branch_hours_service = SimpleNamespace(
        ensure_branch_is_open=_open_branch if branch_is_open else _closed_branch
    )
    service.menu_repository = SimpleNamespace(
        get_settings=lambda restaurant_id: fabricas.configuracoes()
    )
    service.product_repository = SimpleNamespace(
        list_active_by_ids=lambda restaurant_id, ids: [product]
    )
    service.order_repository = FakeOrderRepository()
    return service, branch, product_id


def _open_branch(branch_id):
    # `ensure_branch_is_open` devolve a FAIXA de funcionamento, um
    # `BranchBusinessHour` — e e dela que saem os dois tempos de preparo.
    return fabricas.horario(prep_time_min=20, prep_time_max=30)


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
        payload.address = AddressInput(
            street="Rua A", number="1", neighborhood="Centro", city="Fortaleza", state="CE",
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
        aberta = fabricas.filial(is_open=True)
        filiais = {pausada.id: pausada, aberta.id: aberta}
        service.branch_repository.get_active_by_id_and_restaurant = (
            lambda branch_id, restaurant_id: filiais[branch_id]
        )

        with self.assertRaises(HTTPException):
            service.create_order("junior", build_payload(pausada, product_id))

        service.create_order("junior", build_payload(aberta, product_id))
        self.assertEqual(len(service.order_repository.orders), 1)


def delivery_payload(branch, product_id, **kwargs):
    payload = build_payload(branch, product_id, order_type="delivery", **kwargs)
    payload.address = AddressInput(
        street="Rua A", number="1", neighborhood="Centro", city="Fortaleza", state="CE",
    )
    return payload


def with_estimate(service, delivery_fee):
    """Substitui a estimativa por uma taxa fixa.

    O que se testa aqui e a regra de frete gratis, que roda DEPOIS de a taxa
    existir. De onde a taxa veio (rota do Google, contingencia, token
    reaproveitado) e assunto de test_delivery_estimate.
    """
    service._estimate_delivery = lambda *args, **kwargs: fabricas.estimativa_de_entrega(
        delivery_fee=delivery_fee,
    )


class FreteGratisTests(unittest.TestCase):
    """A campanha aplicada no PEDIDO, que e o unico lugar onde ela decide.

    A estimativa nao sabe o subtotal — ela e feita antes de existir carrinho
    fechado —, entao ela publica o TETO e o servidor decide aqui, com o
    subtotal que ele mesmo calculou. Uma rota que recebesse o subtotal do
    cliente para responder "gratis ou nao" seria preco vindo do cliente por
    outra porta.

    O produto deste arquivo custa R$ 50,00.
    """

    def _service(self, teto, **kwargs):
        service, branch, product_id = build_service(**kwargs)
        branch.free_delivery_enabled = True
        branch.free_delivery_min_order_value = teto
        with_estimate(service, 7.0)
        return service, branch, product_id

    def test_acima_do_teto_a_taxa_e_zerada(self):
        service, branch, product_id = self._service(Decimal("40.00"))

        service.create_order("junior", delivery_payload(branch, product_id))

        pedido = service.order_repository.orders[0]
        self.assertEqual(pedido.delivery_fee, Decimal("0.00"))
        # Quanto foi perdoado fica gravado: sem capturar na escrita, "quanto
        # essa campanha me custou em agosto" nao tem resposta depois.
        self.assertEqual(pedido.delivery_fee_waived, Decimal("7.00"))
        self.assertEqual(pedido.total, Decimal("50.00"))

    def test_no_teto_exato_a_entrega_ja_e_gratis(self):
        # ">=", e nao ">": "acima de R$ 50" na tela do lojista e lido por
        # todo cliente como "50 fecha o pedido com entrega gratis".
        service, branch, product_id = self._service(Decimal("50.00"))

        service.create_order("junior", delivery_payload(branch, product_id))

        self.assertEqual(service.order_repository.orders[0].delivery_fee, Decimal("0.00"))

    def test_abaixo_do_teto_a_taxa_continua_cheia(self):
        service, branch, product_id = self._service(Decimal("60.00"))

        service.create_order("junior", delivery_payload(branch, product_id))

        pedido = service.order_repository.orders[0]
        self.assertEqual(pedido.delivery_fee, Decimal("7.00"))
        self.assertEqual(pedido.delivery_fee_waived, Decimal("0.00"))

    def test_a_campanha_desligada_nao_zera_nada(self):
        service, branch, product_id = self._service(Decimal("40.00"))
        branch.free_delivery_enabled = False

        service.create_order("junior", delivery_payload(branch, product_id))

        self.assertEqual(service.order_repository.orders[0].delivery_fee, Decimal("7.00"))

    def test_ligada_sem_teto_cadastrado_nao_da_entrega_de_graca(self):
        """`enabled` sem valor nao e "gratis sempre": nao ha o que comparar.

        E a armadilha 11 escrita de outro jeito — na duvida, o lado que nao
        gasta o dinheiro do lojista.
        """
        service, branch, product_id = self._service(None)

        service.create_order("junior", delivery_payload(branch, product_id))

        self.assertEqual(service.order_repository.orders[0].delivery_fee, Decimal("7.00"))

    def test_a_comparacao_e_com_o_subtotal_e_nao_com_o_total(self):
        """Incluir a propria taxa faria o frete gratis se autoconceder.

        Com teto de R$ 55 e produto de R$ 50, o pedido so alcanca 57 SOMANDO
        a taxa de 7 que ele esta tentando nao pagar.
        """
        service, branch, product_id = self._service(Decimal("55.00"))

        service.create_order("junior", delivery_payload(branch, product_id))

        self.assertEqual(service.order_repository.orders[0].delivery_fee, Decimal("7.00"))

    def test_na_retirada_nao_ha_taxa_a_perdoar(self):
        service, branch, product_id = self._service(Decimal("40.00"))
        service._estimate_delivery = lambda *args, **kwargs: None

        service.create_order("junior", build_payload(branch, product_id))

        pedido = service.order_repository.orders[0]
        self.assertEqual(pedido.delivery_fee, Decimal("0.00"))
        # Zero perdoado, e nao zero perdoado "de sete": a retirada nunca teve
        # taxa, e registrar perdao aqui poluiria o relatorio da campanha.
        self.assertEqual(pedido.delivery_fee_waived, Decimal("0.00"))


class PausaDaEntregaTests(unittest.TestCase):
    def test_pedido_de_entrega_e_recusado_enquanto_a_pausa_vale(self):
        service, branch, product_id = build_service()
        branch.delivery_paused_until = utcnow() + timedelta(minutes=40)
        with_estimate(service, 7.0)

        with self.assertRaises(HTTPException) as raised:
            service.create_order("junior", delivery_payload(branch, product_id))

        self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(service.order_repository.orders, [])

    def test_a_pausa_nao_atrapalha_a_retirada(self):
        # Pausar a entrega nao fecha a loja: quem esta no balcao continua
        # vendendo.
        service, branch, product_id = build_service()
        branch.delivery_paused_until = utcnow() + timedelta(minutes=40)

        service.create_order("junior", build_payload(branch, product_id))

        self.assertEqual(len(service.order_repository.orders), 1)

    def test_a_recusa_acontece_antes_da_estimativa(self):
        """O buraco que esta checagem fecha.

        Pedido que chega com `delivery_estimate_token` valido reaproveita a
        estimativa guardada e NAO passa por `DeliveryEstimateService.estimate()`
        — onde a pausa tambem e conferida. Sem a checagem no proprio pedido, o
        cliente que estimou as 18h55 fecharia as 19h05 com a entrega pausada
        desde as 19h, e a loja receberia justamente o pedido que pausou para
        nao receber.

        O dublê levanta se for chamado: a recusa tem que vir antes.
        """
        service, branch, product_id = build_service()
        branch.delivery_paused_until = utcnow() + timedelta(minutes=40)

        def nao_deveria_ser_chamado(*args, **kwargs):
            raise AssertionError("a pausa tinha que recusar antes da estimativa")

        service._estimate_delivery = nao_deveria_ser_chamado

        with self.assertRaises(HTTPException):
            service.create_order("junior", delivery_payload(branch, product_id))

    def test_pausa_vencida_volta_a_aceitar_pedido_sozinha(self):
        service, branch, product_id = build_service()
        branch.delivery_paused_until = utcnow() - timedelta(minutes=1)
        with_estimate(service, 7.0)

        service.create_order("junior", delivery_payload(branch, product_id))

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
