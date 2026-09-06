"""A taxa do entregador: a formula pura, e a rota da filial que a escreve.

Tres coisas que estes testes protegem:

1. **Nulo nunca vira zero.** Filial sem taxa configurada produz snapshot
   NULO na atribuicao, e nao R$ 0,00 — zero e um numero que soma no
   historico que o dono usa para pagar o motoboy.
2. **A distancia que falta nao derruba a conta.** Pedido precificado sem
   rota (Google fora do ar) tem `delivery_distance_km` nulo; a taxa sai so
   da base, e o historico mostra a distancia nula ao lado.
3. **A rota e da filial, no escopo do token.** Mesmo 404 para filial de outro
   restaurante e para a filial vizinha de um gerente preso a uma loja.
"""

import unittest
import uuid
from decimal import Decimal

from fastapi import HTTPException
from pydantic import ValidationError

from src.api.dependencies.admin_scope import AdminScope
from src.schemas.courier_schema import AdminBranchCourierFeeUpdate
from src.services.admin_courier_service import AdminCourierService
from src.services.courier_fee import calculate_courier_fee
from tests import fabricas
from tests.rotas_do_app import caminhos


class TestAFormula(unittest.TestCase):
    def test_sem_configuracao_nenhuma_e_nulo_e_nao_zero(self):
        self.assertIsNone(calculate_courier_fee(None, None, Decimal("4.20")))

    def test_so_a_base_e_o_motoboy_pago_por_corrida(self):
        self.assertEqual(calculate_courier_fee(Decimal("7"), None, Decimal("4.20")), Decimal("7.00"))

    def test_so_o_por_km_tambem_conta(self):
        self.assertEqual(
            calculate_courier_fee(None, Decimal("1.50"), Decimal("4")), Decimal("6.00")
        )

    def test_base_mais_km_arredonda_para_duas_casas(self):
        # 5 + 4.2 * 1.25 = 10.25
        self.assertEqual(
            calculate_courier_fee(Decimal("5"), Decimal("1.25"), Decimal("4.2")),
            Decimal("10.25"),
        )
        # 5 + 4.27 * 1.25 = 10.3375 -> 10.34 (ROUND_HALF_UP)
        self.assertEqual(
            calculate_courier_fee(Decimal("5"), Decimal("1.25"), Decimal("4.27")),
            Decimal("10.34"),
        )

    def test_sem_distancia_conta_so_a_base(self):
        """Pedido precificado em contingencia nao tem `delivery_distance_km`.
        A parte que nao depende de distancia continua valendo."""
        self.assertEqual(
            calculate_courier_fee(Decimal("7"), Decimal("1.50"), None), Decimal("7.00")
        )

    def test_sem_distancia_e_so_por_km_e_nulo(self):
        """Nao ha o que multiplicar, e inventar zero seria a armadilha 11:
        um numero que parece decisao e e ausencia de dado."""
        self.assertIsNone(calculate_courier_fee(None, Decimal("1.50"), None))

    def test_o_resultado_e_decimal(self):
        self.assertIsInstance(calculate_courier_fee(Decimal("7"), None, 4.2), Decimal)


class FakeDb:
    def __init__(self):
        self.events = []

    def commit(self):
        self.events.append("commit")

    def rollback(self):
        self.events.append("rollback")


class FakeBranchRepository:
    """Respeita o filtro por restaurante, como o `WHERE` real."""

    def __init__(self, branches):
        self.branches = {branch.id: branch for branch in branches}

    def get_active_by_id_and_restaurant(self, branch_id, restaurant_id):
        branch = self.branches.get(branch_id)
        if branch is None or branch.restaurant_id != restaurant_id or not branch.is_active:
            return None
        return branch


def _scope(restaurant_id, branch_id=None, role="owner"):
    admin = fabricas.usuario_do_painel(restaurant_id=restaurant_id, branch_id=branch_id, role=role)
    return AdminScope(admin_user=admin, restaurant_id=restaurant_id, branch_id=branch_id)


class TestARotaDaFilial(unittest.TestCase):
    def setUp(self):
        self.restaurant_id = uuid.uuid4()
        self.branch = fabricas.filial(restaurant_id=self.restaurant_id)
        self.other_branch = fabricas.filial(restaurant_id=self.restaurant_id, name="Aldeota")
        self.foreign_branch = fabricas.filial(restaurant_id=uuid.uuid4())
        self.db = FakeDb()
        self.service = AdminCourierService(self.db)
        self.service.branch_scope.branch_repository = FakeBranchRepository(
            [self.branch, self.other_branch, self.foreign_branch]
        )

    def test_as_rotas_estao_registradas(self):
        self.assertIn("/admin/branches/{branch_id}/courier-fee", caminhos())

    def test_filial_nasce_sem_taxa_e_a_leitura_diz_isso(self):
        response = self.service.get_courier_fee(_scope(self.restaurant_id), self.branch.id)

        self.assertEqual(response.branch_id, self.branch.id)
        self.assertIsNone(response.courier_fee_base)
        self.assertIsNone(response.courier_fee_per_km)

    def test_o_dono_grava_a_taxa_com_duas_casas(self):
        response = self.service.update_courier_fee(
            _scope(self.restaurant_id),
            self.branch.id,
            AdminBranchCourierFeeUpdate(courier_fee_base=Decimal("7.005"), courier_fee_per_km=1),
        )

        self.assertEqual(self.branch.courier_fee_base, Decimal("7.01"))
        self.assertEqual(self.branch.courier_fee_per_km, Decimal("1.00"))
        self.assertEqual(response.courier_fee_base, 7.01)
        self.assertEqual(response.courier_fee_per_km, 1.0)
        self.assertEqual(self.db.events, ["commit"])

    def test_campo_ausente_nao_e_tocado_e_nulo_explicito_apaga(self):
        self.branch.courier_fee_base = Decimal("7.00")
        self.branch.courier_fee_per_km = Decimal("1.00")

        self.service.update_courier_fee(
            _scope(self.restaurant_id),
            self.branch.id,
            AdminBranchCourierFeeUpdate(courier_fee_per_km=None),
        )

        self.assertEqual(self.branch.courier_fee_base, Decimal("7.00"))
        self.assertIsNone(self.branch.courier_fee_per_km)

    def test_taxa_negativa_e_recusada_pelo_schema(self):
        with self.assertRaises(ValidationError):
            AdminBranchCourierFeeUpdate(courier_fee_base=Decimal("-1"))

    def test_e_a_recusa_acima_e_do_valor(self):
        AdminBranchCourierFeeUpdate(courier_fee_base=Decimal("0"))

    def test_filial_de_outro_restaurante_e_404(self):
        with self.assertRaises(HTTPException) as raised:
            self.service.get_courier_fee(_scope(self.restaurant_id), self.foreign_branch.id)

        self.assertEqual(raised.exception.status_code, 404)

    def test_filial_vizinha_de_um_gerente_preso_e_404(self):
        scope = _scope(self.restaurant_id, branch_id=self.branch.id, role="manager")

        with self.assertRaises(HTTPException) as raised:
            self.service.update_courier_fee(
                scope, self.other_branch.id, AdminBranchCourierFeeUpdate(courier_fee_base=1)
            )

        self.assertEqual(raised.exception.status_code, 404)
        self.assertEqual(self.db.events, [])

    def test_os_dois_404_sao_iguais(self):
        with self.assertRaises(HTTPException) as foreign:
            self.service.get_courier_fee(_scope(self.restaurant_id), self.foreign_branch.id)
        with self.assertRaises(HTTPException) as unknown:
            self.service.get_courier_fee(_scope(self.restaurant_id), uuid.uuid4())

        self.assertEqual(foreign.exception.detail, unknown.exception.detail)
