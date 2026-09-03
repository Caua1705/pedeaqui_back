"""Atribuir e desatribuir pedidos a um entregador, pelo painel.

O que estes testes protegem:

1. **A resposta e por item, e a escrita e uma so.** Um lote com um pedido
   de retirada no meio nao derruba os outros — cada item diz `ok` ou o
   motivo —, e nada e gravado sem o commit no fim.
2. **A taxa e congelada na atribuicao**, da configuracao da filial sobre a
   distancia que o pedido ja tinha. Reatribuir e uma linha nova.
3. **Escopo.** Pedido de outro restaurante ou da filial vizinha e
   `not_found`; pedido de outra filial que o mesmo lojista enxerga e
   `other_branch` — o motoboy do Centro nao sai com o pedido da Aldeota.
4. **Um pedido, um motoboy.** Atribuir a outro fecha a atribuicao anterior;
   atribuir ao mesmo e no-op.
"""

import unittest
import uuid
from decimal import Decimal

from fastapi import HTTPException

from src.api.dependencies.admin_scope import AdminScope
from src.schemas.courier_schema import AdminAssignOrdersRequest, AssignmentErrorCode
from src.services.admin_courier_service import AdminCourierService
from tests import fabricas
from tests.rotas_do_app import caminhos


class FakeDb:
    def __init__(self):
        self.events = []

    def commit(self):
        self.events.append("commit")

    def rollback(self):
        self.events.append("rollback")


class FakeBranchRepository:
    def __init__(self, branches):
        self.branches = {branch.id: branch for branch in branches}

    def get_active_by_id_and_restaurant(self, branch_id, restaurant_id):
        branch = self.branches.get(branch_id)
        if branch is None or branch.restaurant_id != restaurant_id:
            return None
        return branch


class FakeOrderRepository:
    def __init__(self, orders):
        self.orders = {order.id: order for order in orders}

    def get_order_detail(self, order_id, restaurant_id):
        order = self.orders.get(order_id)
        if order is None or order.restaurant_id != restaurant_id:
            return None
        return order


class FakeCourierRepository:
    def __init__(self, couriers, assignments=()):
        self.couriers = {courier.id: courier for courier in couriers}
        self.assignments = list(assignments)

    def get_by_id_and_restaurant(self, courier_id, restaurant_id):
        courier = self.couriers.get(courier_id)
        if courier is None or courier.restaurant_id != restaurant_id or courier.deleted_at:
            return None
        return courier

    def get_open_assignment_of_order(self, order_id):
        for assignment in self.assignments:
            if assignment.order_id == order_id and assignment.unassigned_at is None:
                return assignment
        return None

    def create_assignment(self, assignment):
        if assignment.id is None:
            assignment.id = uuid.uuid4()
        self.assignments.append(assignment)
        return assignment

    def mark_assignment_unassigned(self, assignment, admin_user_id, now):
        assignment.unassigned_at = now
        assignment.unassigned_by_admin_user_id = admin_user_id

    def list_open_orders_by_courier(self, courier_id, exclude_statuses=()):
        return [
            (assignment, self.orders_by_id[assignment.order_id])
            for assignment in self.assignments
            if assignment.courier_id == courier_id
            and assignment.unassigned_at is None
            and self.orders_by_id[assignment.order_id].status not in exclude_statuses
        ]


def _scope(restaurant_id, branch_id=None, role="owner"):
    admin = fabricas.usuario_do_painel(restaurant_id=restaurant_id, branch_id=branch_id, role=role)
    return AdminScope(admin_user=admin, restaurant_id=restaurant_id, branch_id=branch_id)


def _pedido(restaurant_id, branch_id, **extras):
    campos = {
        "restaurant_id": restaurant_id,
        "branch_id": branch_id,
        "order_type": "delivery",
        "status": "ready",
        "payment_status": "on_delivery",
        "delivery_distance_km": Decimal("4.20"),
    }
    campos.update(extras)
    return fabricas.pedido(**campos)


class _Base(unittest.TestCase):
    def setUp(self):
        self.restaurant_id = uuid.uuid4()
        self.centro = fabricas.filial(
            restaurant_id=self.restaurant_id,
            name="Centro",
            courier_fee_base=Decimal("5.00"),
            courier_fee_per_km=Decimal("1.00"),
        )
        self.aldeota = fabricas.filial(restaurant_id=self.restaurant_id, name="Aldeota")
        self.ze = fabricas.entregador(restaurant_id=self.restaurant_id, branch_id=self.centro.id, name="Zé")
        self.tonho = fabricas.entregador(
            restaurant_id=self.restaurant_id, branch_id=self.centro.id, name="Tonho", phone="85999990002"
        )
        self.pedido = _pedido(self.restaurant_id, self.centro.id)
        self.pedido_da_aldeota = _pedido(self.restaurant_id, self.aldeota.id)
        self.pedido_alheio = _pedido(uuid.uuid4(), uuid.uuid4())
        self.retirada = _pedido(self.restaurant_id, self.centro.id, order_type="pickup")
        self.entregue = _pedido(self.restaurant_id, self.centro.id, status="completed")
        self.sem_rota = _pedido(self.restaurant_id, self.centro.id, delivery_distance_km=None)

        self.db = FakeDb()
        self.service = AdminCourierService(self.db)
        self.service.branch_repository = FakeBranchRepository([self.centro, self.aldeota])
        self.service.order_repository = FakeOrderRepository(
            [self.pedido, self.pedido_da_aldeota, self.pedido_alheio, self.retirada, self.entregue, self.sem_rota]
        )
        self.repository = FakeCourierRepository([self.ze, self.tonho])
        self.repository.orders_by_id = self.service.order_repository.orders
        self.service.courier_repository = self.repository
        self.owner = _scope(self.restaurant_id)

    def _atribuir(self, courier, *orders, scope=None):
        return self.service.assign_orders(
            scope or self.owner,
            courier.id,
            AdminAssignOrdersRequest(order_ids=[order.id for order in orders]),
        )


class TestAsRotas(unittest.TestCase):
    def test_estao_registradas(self):
        registradas = caminhos()
        self.assertIn("/admin/couriers/{courier_id}/assignments", registradas)
        self.assertIn("/admin/orders/{order_id}/courier", registradas)


class TestAtribuir(_Base):
    def test_atribui_e_congela_a_taxa_da_filial_sobre_a_distancia_do_pedido(self):
        response = self._atribuir(self.ze, self.pedido)

        item = response.items[0]
        self.assertTrue(item.ok)
        self.assertIsNone(item.error)
        self.assertEqual(item.assignment.courier_id, self.ze.id)
        self.assertEqual(item.assignment.order_id, self.pedido.id)
        # 5 + 4.20 x 1 = 9.20
        self.assertEqual(item.assignment.courier_fee_snapshot, 9.2)
        self.assertEqual(item.assignment.distance_km_snapshot, 4.2)
        gravada = self.repository.assignments[0]
        self.assertEqual(gravada.courier_fee_snapshot, Decimal("9.20"))
        self.assertEqual(gravada.assigned_by_admin_user_id, self.owner.admin_user.id)
        self.assertEqual(self.db.events, ["commit"])

    def test_sem_rota_conta_so_a_base_e_a_distancia_fica_nula(self):
        response = self._atribuir(self.ze, self.sem_rota)

        self.assertEqual(response.items[0].assignment.courier_fee_snapshot, 5.0)
        self.assertIsNone(response.items[0].assignment.distance_km_snapshot)

    def test_filial_sem_taxa_congela_nulo_e_nao_zero(self):
        self.centro.courier_fee_base = None
        self.centro.courier_fee_per_km = None

        response = self._atribuir(self.ze, self.pedido)

        self.assertTrue(response.items[0].ok)
        self.assertIsNone(response.items[0].assignment.courier_fee_snapshot)

    def test_a_taxa_mudada_depois_nao_muda_a_corrida(self):
        self._atribuir(self.ze, self.pedido)
        self.centro.courier_fee_base = Decimal("50.00")

        self.assertEqual(self.repository.assignments[0].courier_fee_snapshot, Decimal("9.20"))

    def test_o_lote_responde_por_item_e_grava_so_os_bons(self):
        response = self._atribuir(self.ze, self.pedido, self.retirada, self.entregue, self.pedido_alheio)

        por_pedido = {item.order_id: item for item in response.items}
        self.assertTrue(por_pedido[self.pedido.id].ok)
        self.assertEqual(por_pedido[self.retirada.id].error, AssignmentErrorCode.NOT_DELIVERY)
        self.assertEqual(por_pedido[self.entregue.id].error, AssignmentErrorCode.ORDER_CLOSED)
        self.assertEqual(por_pedido[self.pedido_alheio.id].error, AssignmentErrorCode.NOT_FOUND)
        self.assertEqual(len(self.repository.assignments), 1)
        self.assertEqual(self.db.events, ["commit"])

    def test_a_ordem_da_resposta_e_a_do_pedido(self):
        response = self._atribuir(self.ze, self.retirada, self.pedido)

        self.assertEqual([item.order_id for item in response.items], [self.retirada.id, self.pedido.id])

    def test_pedido_de_outra_filial_e_other_branch(self):
        """O dono enxerga a Aldeota, entao nao e 404: e a regra de que o
        motoboy do Centro nao sai com o pedido da outra cozinha."""
        response = self._atribuir(self.ze, self.pedido_da_aldeota)

        self.assertEqual(response.items[0].error, AssignmentErrorCode.OTHER_BRANCH)
        self.assertEqual(self.repository.assignments, [])

    def test_gerente_preso_a_outra_filial_nao_alcanca_o_motoboy(self):
        gerente = _scope(self.restaurant_id, branch_id=self.aldeota.id, role="manager")

        with self.assertRaises(HTTPException) as raised:
            self._atribuir(self.ze, self.pedido, scope=gerente)

        self.assertEqual(raised.exception.status_code, 404)

    def test_atribuir_ao_mesmo_de_novo_e_no_op(self):
        self._atribuir(self.ze, self.pedido)

        response = self._atribuir(self.ze, self.pedido)

        self.assertTrue(response.items[0].ok)
        self.assertEqual(len(self.repository.assignments), 1)

    def test_atribuir_a_outro_fecha_a_anterior_e_abre_outra(self):
        self._atribuir(self.ze, self.pedido)

        response = self._atribuir(self.tonho, self.pedido)

        self.assertTrue(response.items[0].ok)
        antiga, nova = self.repository.assignments
        self.assertIsNotNone(antiga.unassigned_at)
        self.assertEqual(antiga.unassigned_by_admin_user_id, self.owner.admin_user.id)
        self.assertIsNone(nova.unassigned_at)
        self.assertEqual(nova.courier_id, self.tonho.id)

    def test_inativo_nao_recebe_pedido(self):
        self.ze.is_active = False

        with self.assertRaises(HTTPException) as raised:
            self._atribuir(self.ze, self.pedido)

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(self.db.events, [])

    def test_pedido_ja_saiu_para_entrega_ainda_pode_trocar_de_motoboy(self):
        """`out_for_delivery` nao e terminal: o motoboy quebrou a moto e
        outro assume. So os terminais fecham a porta."""
        self.pedido.status = "out_for_delivery"

        response = self._atribuir(self.ze, self.pedido)

        self.assertTrue(response.items[0].ok)


class TestLerEDesatribuir(_Base):
    def test_a_lista_do_entregador_traz_as_abertas_com_o_pedido(self):
        self._atribuir(self.ze, self.pedido, self.sem_rota)

        abertas = self.service.list_open_assignments(self.owner, self.ze.id)

        self.assertEqual([a.order_id for a in abertas], [self.pedido.id, self.sem_rota.id])
        self.assertEqual(abertas[0].order_number, self.pedido.order_number)
        self.assertEqual(abertas[0].order_status, "ready")

    def test_o_pedido_diz_quem_esta_com_ele(self):
        self._atribuir(self.ze, self.pedido)

        response = self.service.get_order_courier(self.owner, self.pedido.id)

        self.assertEqual(response.courier.id, self.ze.id)
        self.assertEqual(response.courier.name, "Zé")
        self.assertEqual(response.assignment.order_id, self.pedido.id)

    def test_pedido_sem_motoboy_responde_nulo_e_nao_404(self):
        """404 e para o pedido que o lojista nao alcanca; "ninguem ainda" e
        um estado normal do pedido, e a tela precisa distinguir os dois."""
        response = self.service.get_order_courier(self.owner, self.pedido.id)

        self.assertIsNone(response.assignment)
        self.assertIsNone(response.courier)

    def test_pedido_alheio_e_404(self):
        with self.assertRaises(HTTPException) as raised:
            self.service.get_order_courier(self.owner, self.pedido_alheio.id)

        self.assertEqual(raised.exception.status_code, 404)

    def test_desatribui_e_fecha_a_linha(self):
        self._atribuir(self.ze, self.pedido)
        self.db.events.clear()

        self.service.unassign_order(self.owner, self.pedido.id)

        self.assertIsNotNone(self.repository.assignments[0].unassigned_at)
        self.assertEqual(self.db.events, ["commit"])
        self.assertIsNone(self.service.get_order_courier(self.owner, self.pedido.id).assignment)

    def test_desatribuir_pedido_sem_motoboy_e_409(self):
        with self.assertRaises(HTTPException) as raised:
            self.service.unassign_order(self.owner, self.pedido.id)

        self.assertEqual(raised.exception.status_code, 409)

    def test_gerente_preso_nao_desatribui_pedido_da_vizinha(self):
        self._atribuir(self.ze, self.pedido)
        gerente = _scope(self.restaurant_id, branch_id=self.aldeota.id, role="manager")

        with self.assertRaises(HTTPException) as raised:
            self.service.unassign_order(gerente, self.pedido.id)

        self.assertEqual(raised.exception.status_code, 404)
        self.assertIsNone(self.repository.assignments[0].unassigned_at)
