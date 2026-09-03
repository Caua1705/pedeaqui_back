"""A porta do ENTREGADOR: link + codigo, a lista dele, saiu/entregue, historico.

O que estes testes protegem:

1. **Quem autentica e o PAR.** Link sem codigo e 401; codigo errado e 401;
   link que nao abre cadastro nenhum (inexistente, regenerado, excluido,
   inativo) e 404 — os quatro iguais, para o link nao virar oraculo.
2. **A quarta porta do writer.** `out_for_delivery` e `completed` passam por
   `OrderStatusChangeService.apply` com `changed_by="entregador:<nome>"`; o
   entregador so anda `ready -> out_for_delivery -> completed`, e nada
   mais — nem cancelar, nem voltar.
3. **Ele so enxerga o que e dele.** A lista e o historico saem por
   `courier.id`; pedido atribuido a outro e 404 (nao 403).
4. **O lote e por item.** O terceiro pedido em estado errado nao desfaz os
   dois que ja sairam.
"""

import unittest
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

from src.schemas.courier_schema import CourierOrdersStatusRequest, CourierStatusErrorCode
from src.services.courier_delivery_service import CourierDeliveryService
from src.services.order_service import OrderService
from src.services.order_state_machine import ensure_courier_can_set
from src.utils.security import hash_courier_access_code, hash_courier_link_token
from tests import fabricas
from tests.rotas_do_app import caminhos


LINK = "link-do-ze-com-256-bits-de-mentira"
CODIGO = "123456"


class FakeDb:
    def __init__(self):
        self.events = []

    def commit(self):
        self.events.append("commit")

    def rollback(self):
        self.events.append("rollback")


class FakeOrderRepository:
    """O repositorio que o WRITER usa. Guarda os pedidos por id e o historico."""

    def __init__(self, orders):
        self.orders = {order.id: order for order in orders}
        self.history = []

    def get_order_detail(self, order_id, restaurant_id):
        order = self.orders.get(order_id)
        if order is None or order.restaurant_id != restaurant_id:
            return None
        return order

    def update_status(self, order, new_status):
        order.status = new_status

    def create_status_history(self, history):
        self.history.append(history)


class FakeCourierRepository:
    def __init__(self, couriers, assignments, orders):
        self.couriers = list(couriers)
        self.assignments = list(assignments)
        self.orders = {order.id: order for order in orders}
        self.history_calls = []

    def get_by_link_hash(self, link_hash):
        for courier in self.couriers:
            if courier.access_link_hash == link_hash and courier.deleted_at is None:
                return courier
        return None

    def _abertas(self, courier_id, exclude_statuses):
        return [
            (a, self.orders[a.order_id])
            for a in self.assignments
            if a.courier_id == courier_id
            and a.unassigned_at is None
            and self.orders[a.order_id].status not in exclude_statuses
        ]

    def list_open_orders_by_courier(self, courier_id, exclude_statuses=()):
        return self._abertas(courier_id, exclude_statuses)

    def get_open_order_of_courier(self, courier_id, order_id):
        for assignment, order in self._abertas(courier_id, ()):
            if order.id == order_id:
                return assignment, order
        return None

    def list_deliveries_by_courier(self, courier_id, start_at, end_at):
        self.history_calls.append((courier_id, start_at, end_at))
        return [
            (a, self.orders[a.order_id], datetime(2026, 9, 1, 20, tzinfo=timezone.utc))
            for a in self.assignments
            if a.courier_id == courier_id and self.orders[a.order_id].status == "completed"
        ]


class FakeBranchRepository:
    def __init__(self, branches):
        self.branches = {b.id: b for b in branches}

    def get_by_id_and_restaurant(self, branch_id, restaurant_id):
        branch = self.branches.get(branch_id)
        if branch is None or branch.restaurant_id != restaurant_id:
            return None
        return branch


def _pedido(restaurant_id, branch_id, **extras):
    campos = {
        "restaurant_id": restaurant_id,
        "branch_id": branch_id,
        "order_type": "delivery",
        "status": "ready",
        "payment_flow": "delivery",
        "payment_status": "on_delivery",
        "payment_method": "cash",
        "total": Decimal("52.90"),
        "address_street": "Rua das Flores",
        "address_number": "200",
        "address_neighborhood": "Varjota",
        "address_complement": "ap 301",
        "address_reference": "perto da padaria",
        "cashback_redeemed_amount": Decimal("0"),
    }
    campos.update(extras)
    return fabricas.pedido(**campos)


class _Base(unittest.TestCase):
    def setUp(self):
        self.restaurant_id = uuid.uuid4()
        self.filial = fabricas.filial(restaurant_id=self.restaurant_id, name="Centro")
        self.ze = fabricas.entregador(
            restaurant_id=self.restaurant_id,
            branch_id=self.filial.id,
            name="Zé",
            access_link_hash=hash_courier_link_token(LINK),
            access_code_hash=hash_courier_access_code(CODIGO, LINK),
        )
        self.tonho = fabricas.entregador(
            restaurant_id=self.restaurant_id, branch_id=self.filial.id, name="Tonho", phone="85999990002"
        )
        self.pronto = _pedido(self.restaurant_id, self.filial.id)
        self.na_rua = _pedido(self.restaurant_id, self.filial.id, status="out_for_delivery")
        self.em_preparo = _pedido(self.restaurant_id, self.filial.id, status="preparing")
        self.pago_online = _pedido(
            self.restaurant_id, self.filial.id, payment_flow="online", payment_status="paid", payment_method="pix"
        )
        self.do_tonho = _pedido(self.restaurant_id, self.filial.id)
        self.entregue = _pedido(self.restaurant_id, self.filial.id, status="completed")
        orders = [self.pronto, self.na_rua, self.em_preparo, self.pago_online, self.do_tonho, self.entregue]
        assignments = [
            fabricas.atribuicao(order_id=self.pronto.id, courier_id=self.ze.id, courier_fee_snapshot=Decimal("9.20")),
            fabricas.atribuicao(order_id=self.na_rua.id, courier_id=self.ze.id),
            fabricas.atribuicao(order_id=self.em_preparo.id, courier_id=self.ze.id),
            fabricas.atribuicao(order_id=self.pago_online.id, courier_id=self.ze.id, courier_fee_snapshot=None),
            fabricas.atribuicao(order_id=self.do_tonho.id, courier_id=self.tonho.id),
            fabricas.atribuicao(order_id=self.entregue.id, courier_id=self.ze.id, courier_fee_snapshot=Decimal("7.00")),
        ]
        self.db = FakeDb()
        self.service = CourierDeliveryService(self.db)
        self.repository = FakeCourierRepository([self.ze, self.tonho], assignments, orders)
        self.service.courier_repository = self.repository
        self.service.branch_repository = FakeBranchRepository([self.filial])
        # O writer e o de verdade, com os dubles nas DUAS camadas (armadilha
        # 25.1): a leitura no service da porta e a escrita no writer.
        self.order_repository = FakeOrderRepository(orders)
        self.service.status_change_service.order_repository = self.order_repository
        self.service.status_change_service.coupon_service = SimpleNamespace(reverse_for_order=lambda o: None)
        self.creditados = []
        self.service.status_change_service.cashback_service = SimpleNamespace(
            refund_redemption=lambda o: None,
            credit_for_order=lambda o: self.creditados.append(o.id),
        )


class TestAsRotas(unittest.TestCase):
    def test_estao_registradas(self):
        registradas = caminhos()
        for caminho in (
            "/courier/{link_token}/me",
            "/courier/{link_token}/orders",
            "/courier/{link_token}/orders/out-for-delivery",
            "/courier/{link_token}/orders/{order_id}/delivered",
            "/courier/{link_token}/history",
        ):
            self.assertIn(caminho, registradas)


class TestQuemAutentica(_Base):
    def test_o_par_certo_abre_o_cadastro(self):
        self.assertIs(self.service.authenticate(LINK, CODIGO), self.ze)

    def test_link_desconhecido_e_404(self):
        with self.assertRaises(HTTPException) as raised:
            self.service.authenticate("outro-link", CODIGO)

        self.assertEqual(raised.exception.status_code, 404)

    def test_codigo_errado_e_401(self):
        with self.assertRaises(HTTPException) as raised:
            self.service.authenticate(LINK, "000000")

        self.assertEqual(raised.exception.status_code, 401)

    def test_codigo_ausente_e_401(self):
        with self.assertRaises(HTTPException) as raised:
            self.service.authenticate(LINK, None)

        self.assertEqual(raised.exception.status_code, 401)

    def test_inativo_e_excluido_sao_404_iguais_ao_desconhecido(self):
        """O link "morre": quem o tem nao consegue saber se foi
        desativado, excluido ou nunca existiu."""
        with self.assertRaises(HTTPException) as desconhecido:
            self.service.authenticate("outro-link", CODIGO)
        self.ze.is_active = False
        with self.assertRaises(HTTPException) as inativo:
            self.service.authenticate(LINK, CODIGO)
        self.ze.is_active = True
        self.ze.deleted_at = datetime.now(timezone.utc)
        with self.assertRaises(HTTPException) as excluido:
            self.service.authenticate(LINK, CODIGO)

        self.assertEqual(inativo.exception.status_code, 404)
        self.assertEqual(excluido.exception.status_code, 404)
        self.assertEqual(inativo.exception.detail, desconhecido.exception.detail)
        self.assertEqual(excluido.exception.detail, desconhecido.exception.detail)

    def test_sem_acesso_gerado_nada_abre(self):
        self.tonho.access_link_hash = None
        with self.assertRaises(HTTPException) as raised:
            self.service.authenticate("", CODIGO)

        self.assertEqual(raised.exception.status_code, 404)

    def test_o_me_diz_quem_ele_e_e_de_que_loja(self):
        me = self.service.me(self.ze)

        self.assertEqual(me.name, "Zé")
        self.assertEqual(me.branch_name, "Centro")


class TestALista(_Base):
    def test_so_os_dele_abertos_e_nao_terminais(self):
        pedidos = self.service.list_orders(self.ze)

        ids = {p.order_id for p in pedidos}
        self.assertEqual(ids, {self.pronto.id, self.na_rua.id, self.em_preparo.id, self.pago_online.id})
        self.assertNotIn(self.do_tonho.id, ids)
        self.assertNotIn(self.entregue.id, ids)

    def test_o_que_o_motoboy_precisa_ver(self):
        por_id = {p.order_id: p for p in self.service.list_orders(self.ze)}
        pronto = por_id[self.pronto.id]

        self.assertEqual(pronto.order_number, self.pronto.order_number)
        self.assertEqual(pronto.status, "ready")
        self.assertEqual(pronto.customer_name, "Fulano de Tal")
        self.assertEqual(pronto.customer_phone, "85999990000")
        self.assertEqual(pronto.address_street, "Rua das Flores")
        self.assertEqual(pronto.address_reference, "perto da padaria")
        self.assertEqual(pronto.payment_method, "cash")
        self.assertFalse(pronto.is_paid)
        self.assertEqual(pronto.amount_to_collect, 52.9)
        self.assertEqual(pronto.total, 52.9)
        self.assertEqual(pronto.courier_fee, 9.2)
        self.assertTrue(pronto.can_leave)

    def test_pago_online_nao_tem_o_que_receber(self):
        por_id = {p.order_id: p for p in self.service.list_orders(self.ze)}
        pago = por_id[self.pago_online.id]

        self.assertTrue(pago.is_paid)
        self.assertEqual(pago.amount_to_collect, 0.0)
        self.assertIsNone(pago.courier_fee)

    def test_em_preparo_ainda_nao_pode_sair(self):
        por_id = {p.order_id: p for p in self.service.list_orders(self.ze)}

        self.assertFalse(por_id[self.em_preparo.id].can_leave)
        self.assertFalse(por_id[self.na_rua.id].can_leave)
        self.assertTrue(por_id[self.na_rua.id].can_deliver)
        self.assertFalse(por_id[self.pronto.id].can_deliver)


class TestARegraDaPorta(unittest.TestCase):
    def test_so_as_duas_arestas(self):
        ensure_courier_can_set("ready", "out_for_delivery")
        ensure_courier_can_set("out_for_delivery", "completed")

    def test_o_resto_e_409(self):
        for atual, novo in (
            ("preparing", "out_for_delivery"),
            ("ready", "completed"),
            ("out_for_delivery", "ready"),
            ("ready", "cancelled"),
            ("completed", "out_for_delivery"),
        ):
            with self.subTest(atual=atual, novo=novo):
                with self.assertRaises(HTTPException) as raised:
                    ensure_courier_can_set(atual, novo)
                self.assertEqual(raised.exception.status_code, 409)


class TestSaiuParaEntrega(_Base):
    def _sair(self, *orders):
        with patch.object(OrderService, "to_order_detail_response", side_effect=lambda o: o):
            return self.service.mark_out_for_delivery(
                self.ze, CourierOrdersStatusRequest(order_ids=[o.id for o in orders])
            )

    def test_escreve_pelo_writer_com_a_assinatura_do_entregador(self):
        response = self._sair(self.pronto)

        item = response.items[0]
        self.assertTrue(item.ok)
        self.assertEqual(self.pronto.status, "out_for_delivery")
        self.assertEqual(item.order.status, "out_for_delivery")
        gravado = self.order_repository.history[-1]
        self.assertEqual(gravado.status, "out_for_delivery")
        self.assertEqual(gravado.changed_by, "entregador:Zé")
        self.assertEqual(self.db.events, ["commit"])

    def test_o_lote_e_por_item_e_nao_desfaz_o_que_saiu(self):
        response = self._sair(self.pronto, self.em_preparo, self.do_tonho, self.na_rua)

        por_id = {item.order_id: item for item in response.items}
        self.assertTrue(por_id[self.pronto.id].ok)
        self.assertEqual(por_id[self.em_preparo.id].error, CourierStatusErrorCode.WRONG_STATUS)
        self.assertIn("preparing", por_id[self.em_preparo.id].message)
        self.assertEqual(por_id[self.do_tonho.id].error, CourierStatusErrorCode.NOT_FOUND)
        self.assertEqual(por_id[self.na_rua.id].error, CourierStatusErrorCode.WRONG_STATUS)
        self.assertEqual(self.pronto.status, "out_for_delivery")
        self.assertEqual(self.em_preparo.status, "preparing")
        self.assertEqual(self.do_tonho.status, "ready")

    def test_a_ordem_da_resposta_e_a_do_corpo(self):
        response = self._sair(self.em_preparo, self.pronto)

        self.assertEqual([i.order_id for i in response.items], [self.em_preparo.id, self.pronto.id])


class TestEntregue(_Base):
    def _entregar(self, order):
        with patch.object(OrderService, "to_order_detail_response", side_effect=lambda o: o):
            return self.service.mark_delivered(self.ze, order.id)

    def test_completa_e_credita_o_cashback_de_graca(self):
        response = self._entregar(self.na_rua)

        self.assertEqual(response.status, "completed")
        self.assertEqual(self.na_rua.status, "completed")
        self.assertEqual(self.order_repository.history[-1].changed_by, "entregador:Zé")
        self.assertEqual(self.creditados, [self.na_rua.id])

    def test_pronto_que_nao_saiu_nao_e_entregue(self):
        with self.assertRaises(HTTPException) as raised:
            self._entregar(self.pronto)

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(self.pronto.status, "ready")

    def test_pedido_de_outro_e_404(self):
        with self.assertRaises(HTTPException) as raised:
            self._entregar(self.do_tonho)

        self.assertEqual(raised.exception.status_code, 404)

    def test_pedido_ja_entregue_e_404_e_nao_409(self):
        """Terminal sai da lista dele; para a porta ele deixou de existir.
        409 diria "voce ja entregou", e o segundo toque no botao merece a
        mesma resposta que um id inventado."""
        with self.assertRaises(HTTPException) as raised:
            self._entregar(self.entregue)

        self.assertEqual(raised.exception.status_code, 404)


class TestOHistorico(_Base):
    def test_soma_as_taxas_do_periodo_e_conta_as_sem_taxa(self):
        historico = self.service.history(self.ze, date(2026, 9, 1), date(2026, 9, 1))

        self.assertEqual(historico.deliveries_count, 1)
        self.assertEqual(historico.fee_total, 7.0)
        self.assertEqual(historico.deliveries_without_fee, 0)
        self.assertEqual(historico.deliveries[0].order_number, self.entregue.order_number)
        self.assertEqual(historico.deliveries[0].courier_fee, 7.0)
        self.assertEqual(historico.deliveries[0].delivered_at, datetime(2026, 9, 1, 20, tzinfo=timezone.utc))

    def test_o_recorte_vai_ao_repositorio_no_fuso_da_operacao(self):
        self.service.history(self.ze, date(2026, 9, 1), date(2026, 9, 2))

        courier_id, start_at, end_at = self.repository.history_calls[0]
        self.assertEqual(courier_id, self.ze.id)
        # 00:00 de 1/9 em Fortaleza (UTC-3) e 03:00 UTC; o fim e o comeco de 3/9.
        self.assertEqual(start_at.astimezone(timezone.utc), datetime(2026, 9, 1, 3, tzinfo=timezone.utc))
        self.assertEqual(end_at.astimezone(timezone.utc), datetime(2026, 9, 3, 3, tzinfo=timezone.utc))

    def test_sem_datas_e_o_dia_de_hoje(self):
        hoje = date(2026, 9, 3)
        self.service.clock = lambda: datetime(2026, 9, 3, 15, tzinfo=timezone.utc)

        self.service.history(self.ze, None, None)

        _, start_at, end_at = self.repository.history_calls[0]
        self.assertEqual(start_at.date(), hoje)
        self.assertEqual((end_at - start_at).days, 1)

    def test_periodo_invertido_ou_longo_demais_e_400(self):
        with self.assertRaises(HTTPException) as invertido:
            self.service.history(self.ze, date(2026, 9, 2), date(2026, 9, 1))
        with self.assertRaises(HTTPException) as longo:
            self.service.history(self.ze, date(2026, 1, 1), date(2026, 9, 1))

        self.assertEqual(invertido.exception.status_code, 400)
        self.assertEqual(longo.exception.status_code, 400)

    def test_sem_taxa_conta_como_entrega_e_nao_como_zero(self):
        self.pago_online.status = "completed"

        historico = self.service.history(self.ze, date(2026, 9, 1), date(2026, 9, 1))

        self.assertEqual(historico.deliveries_count, 2)
        self.assertEqual(historico.fee_total, 7.0)
        self.assertEqual(historico.deliveries_without_fee, 1)
