"""O cadastro de entregadores pelo painel, e o codigo de acesso.

O que estes testes protegem:

1. **Escopo.** A lista e a leitura respeitam restaurante e filial do token:
   gerente preso a uma loja ve so os dela, e 404 (nunca 403) para o
   entregador de outro restaurante ou da filial vizinha.
2. **O telefone e chave.** So digitos, e unico por filial entre os nao
   excluidos — 409 na repeticao.
3. **Excluir e `deleted_at`.** Some das listas, perde o acesso na hora, e o
   historico sobrevive. Desativar tambem perde o acesso e devolve os pedidos
   abertos para a fila.
4. **O acesso sai UMA vez, em claro.** Link de 256 bits e codigo de 6
   digitos, gravados so em hash; regenerar mata o par anterior na hora.
"""

import unittest
import uuid

from fastapi import HTTPException
from pydantic import ValidationError

from src.api.dependencies.admin_scope import AdminScope
from src.schemas.courier_schema import AdminCourierCreate, AdminCourierUpdate
from src.services.admin_courier_service import AdminCourierService
from src.utils.security import verify_courier_access_code, verify_courier_link_token
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


class FakeCourierRepository:
    """Respeita restaurante, filial e `deleted_at`, como o SQL real."""

    def __init__(self, couriers=()):
        self.couriers = {courier.id: courier for courier in couriers}
        self.closed_for = []

    def _visiveis(self, restaurant_id):
        return [
            courier
            for courier in self.couriers.values()
            if courier.restaurant_id == restaurant_id and courier.deleted_at is None
        ]

    def list_by_restaurant(self, restaurant_id, branch_id=None):
        couriers = self._visiveis(restaurant_id)
        if branch_id is not None:
            couriers = [courier for courier in couriers if courier.branch_id == branch_id]
        return sorted(couriers, key=lambda courier: courier.name)

    def get_by_id_and_restaurant(self, courier_id, restaurant_id):
        for courier in self._visiveis(restaurant_id):
            if courier.id == courier_id:
                return courier
        return None

    def exists_phone_in_branch(self, branch_id, phone, exclude_courier_id=None):
        return any(
            courier.branch_id == branch_id
            and courier.phone == phone
            and courier.deleted_at is None
            and courier.id != exclude_courier_id
            for courier in self.couriers.values()
        )

    def create(self, courier):
        if courier.id is None:
            courier.id = uuid.uuid4()
        self.couriers[courier.id] = courier
        return courier

    def mark_open_assignments_unassigned(self, courier_id, admin_user_id):
        self.closed_for.append((courier_id, admin_user_id))
        return 0


def _scope(restaurant_id, branch_id=None, role="owner"):
    admin = fabricas.usuario_do_painel(restaurant_id=restaurant_id, branch_id=branch_id, role=role)
    return AdminScope(admin_user=admin, restaurant_id=restaurant_id, branch_id=branch_id)


class _Base(unittest.TestCase):
    def setUp(self):
        self.restaurant_id = uuid.uuid4()
        self.centro = fabricas.filial(restaurant_id=self.restaurant_id, name="Centro")
        self.aldeota = fabricas.filial(restaurant_id=self.restaurant_id, name="Aldeota")
        self.foreign_branch = fabricas.filial(restaurant_id=uuid.uuid4())
        self.ze = fabricas.entregador(
            restaurant_id=self.restaurant_id, branch_id=self.centro.id, name="Zé", phone="85999990001"
        )
        self.tonho = fabricas.entregador(
            restaurant_id=self.restaurant_id, branch_id=self.aldeota.id, name="Tonho", phone="85999990002"
        )
        self.alheio = fabricas.entregador(
            restaurant_id=self.foreign_branch.restaurant_id, branch_id=self.foreign_branch.id
        )
        self.db = FakeDb()
        self.service = AdminCourierService(self.db)
        self.service.branch_repository = FakeBranchRepository(
            [self.centro, self.aldeota, self.foreign_branch]
        )
        self.repository = FakeCourierRepository([self.ze, self.tonho, self.alheio])
        self.service.courier_repository = self.repository
        self.owner = _scope(self.restaurant_id)
        self.gerente_do_centro = _scope(self.restaurant_id, branch_id=self.centro.id, role="manager")


class TestAsRotas(unittest.TestCase):
    def test_estao_registradas(self):
        registradas = caminhos()
        for caminho in (
            "/admin/couriers",
            "/admin/couriers/{courier_id}",
            "/admin/couriers/{courier_id}/access",
        ):
            self.assertIn(caminho, registradas)


class TestALista(_Base):
    def test_o_dono_ve_as_duas_lojas(self):
        nomes = [courier.name for courier in self.service.list_couriers(self.owner)]

        self.assertEqual(nomes, ["Tonho", "Zé"])

    def test_o_gerente_preso_ve_so_a_dele_sem_pedir(self):
        nomes = [courier.name for courier in self.service.list_couriers(self.gerente_do_centro)]

        self.assertEqual(nomes, ["Zé"])

    def test_o_gerente_preso_que_pede_a_vizinha_recebe_404(self):
        with self.assertRaises(HTTPException) as raised:
            self.service.list_couriers(self.gerente_do_centro, branch_id=self.aldeota.id)

        self.assertEqual(raised.exception.status_code, 404)

    def test_o_dono_filtra_por_filial(self):
        nomes = [
            courier.name
            for courier in self.service.list_couriers(self.owner, branch_id=self.aldeota.id)
        ]

        self.assertEqual(nomes, ["Tonho"])

    def test_a_lista_diz_quem_ja_tem_acesso(self):
        self.ze.access_link_hash = "h"
        self.ze.access_code_hash = "c"

        por_nome = {c.name: c for c in self.service.list_couriers(self.owner)}

        self.assertTrue(por_nome["Zé"].has_access)
        self.assertFalse(por_nome["Tonho"].has_access)


class TestCriar(_Base):
    def test_cria_na_filial_com_o_telefone_normalizado(self):
        response = self.service.create_courier(
            self.owner,
            AdminCourierCreate(branch_id=self.centro.id, name="Maria", phone="(85) 9 9999-0003"),
        )

        self.assertEqual(response.phone, "85999990003")
        self.assertEqual(response.branch_id, self.centro.id)
        self.assertTrue(response.is_active)
        self.assertFalse(response.has_access)
        criado = self.repository.couriers[response.id]
        self.assertEqual(criado.restaurant_id, self.restaurant_id)
        self.assertEqual(self.db.events, ["commit"])

    def test_filial_de_outro_restaurante_e_404(self):
        with self.assertRaises(HTTPException) as raised:
            self.service.create_courier(
                self.owner,
                AdminCourierCreate(branch_id=self.foreign_branch.id, name="X", phone="85999990009"),
            )

        self.assertEqual(raised.exception.status_code, 404)
        self.assertEqual(self.db.events, [])

    def test_gerente_preso_nao_cria_na_vizinha(self):
        with self.assertRaises(HTTPException) as raised:
            self.service.create_courier(
                self.gerente_do_centro,
                AdminCourierCreate(branch_id=self.aldeota.id, name="X", phone="85999990009"),
            )

        self.assertEqual(raised.exception.status_code, 404)

    def test_telefone_repetido_na_mesma_filial_e_409(self):
        with self.assertRaises(HTTPException) as raised:
            self.service.create_courier(
                self.owner,
                AdminCourierCreate(branch_id=self.centro.id, name="Outro Zé", phone="85 99999-0001"),
            )

        self.assertEqual(raised.exception.status_code, 409)

    def test_e_o_mesmo_telefone_em_outra_filial_e_outro_cadastro(self):
        response = self.service.create_courier(
            self.owner,
            AdminCourierCreate(branch_id=self.aldeota.id, name="Zé da Aldeota", phone="85999990001"),
        )

        self.assertEqual(response.branch_id, self.aldeota.id)

    def test_telefone_curto_e_recusado_pelo_schema(self):
        with self.assertRaises(ValidationError):
            AdminCourierCreate(branch_id=uuid.uuid4(), name="X", phone="1234")

    def test_campo_desconhecido_e_recusado(self):
        """`extra="forbid"`: `access_code` no corpo de criacao seria alguem
        tentando ESCOLHER o codigo, e ele e sorteado pelo servidor."""
        with self.assertRaises(ValidationError):
            AdminCourierCreate(
                branch_id=uuid.uuid4(), name="X", phone="85999990009", access_code="123456"
            )


class TestLerEEditar(_Base):
    def test_le_o_proprio(self):
        response = self.service.get_courier(self.owner, self.ze.id)

        self.assertEqual(response.name, "Zé")

    def test_entregador_de_outro_restaurante_e_404(self):
        with self.assertRaises(HTTPException) as raised:
            self.service.get_courier(self.owner, self.alheio.id)

        self.assertEqual(raised.exception.status_code, 404)

    def test_gerente_preso_nao_alcanca_o_da_vizinha(self):
        with self.assertRaises(HTTPException) as raised:
            self.service.get_courier(self.gerente_do_centro, self.tonho.id)

        self.assertEqual(raised.exception.status_code, 404)

    def test_os_404_sao_iguais(self):
        with self.assertRaises(HTTPException) as alheio:
            self.service.get_courier(self.owner, self.alheio.id)
        with self.assertRaises(HTTPException) as inexistente:
            self.service.get_courier(self.owner, uuid.uuid4())

        self.assertEqual(alheio.exception.detail, inexistente.exception.detail)

    def test_edita_nome_e_telefone(self):
        response = self.service.update_courier(
            self.owner, self.ze.id, AdminCourierUpdate(name="Zé Novo", phone="(85) 99999-0007")
        )

        self.assertEqual(response.name, "Zé Novo")
        self.assertEqual(self.ze.phone, "85999990007")

    def test_trocar_para_telefone_de_outro_da_mesma_filial_e_409(self):
        outro = fabricas.entregador(
            restaurant_id=self.restaurant_id, branch_id=self.centro.id, phone="85999990005"
        )
        self.repository.create(outro)

        with self.assertRaises(HTTPException) as raised:
            self.service.update_courier(self.owner, self.ze.id, AdminCourierUpdate(phone="85999990005"))

        self.assertEqual(raised.exception.status_code, 409)

    def test_manter_o_proprio_telefone_nao_e_conflito(self):
        self.service.update_courier(self.owner, self.ze.id, AdminCourierUpdate(phone="85999990001"))

    def test_desativar_devolve_os_pedidos_abertos_para_a_fila(self):
        """Inativo nao marca mais nada, entao um pedido que ficasse com ele
        ficaria preso. A atribuicao aberta e fechada e o painel reatribui."""
        self.service.update_courier(self.owner, self.ze.id, AdminCourierUpdate(is_active=False))

        self.assertFalse(self.ze.is_active)
        self.assertEqual(self.repository.closed_for, [(self.ze.id, self.owner.admin_user.id)])

    def test_reativar_nao_mexe_em_atribuicao_nem_no_acesso(self):
        self.ze.is_active = False
        self.ze.access_link_hash = "h"

        self.service.update_courier(self.owner, self.ze.id, AdminCourierUpdate(is_active=True))

        self.assertTrue(self.ze.is_active)
        self.assertEqual(self.ze.access_link_hash, "h")
        self.assertEqual(self.repository.closed_for, [])


class TestExcluir(_Base):
    def test_excluir_marca_deleted_at_revoga_e_devolve_os_pedidos(self):
        self.ze.access_link_hash = "h"
        self.ze.access_code_hash = "c"

        self.service.delete_courier(self.owner, self.ze.id)

        self.assertIsNotNone(self.ze.deleted_at)
        self.assertIsNone(self.ze.access_link_hash)
        self.assertIsNone(self.ze.access_code_hash)
        self.assertEqual(self.repository.closed_for, [(self.ze.id, self.owner.admin_user.id)])
        self.assertEqual(self.db.events, ["commit"])

    def test_excluido_some_da_lista_e_da_leitura(self):
        self.service.delete_courier(self.owner, self.ze.id)

        nomes = [courier.name for courier in self.service.list_couriers(self.owner)]
        self.assertEqual(nomes, ["Tonho"])
        with self.assertRaises(HTTPException) as raised:
            self.service.get_courier(self.owner, self.ze.id)
        self.assertEqual(raised.exception.status_code, 404)

    def test_excluir_de_outro_restaurante_e_404(self):
        with self.assertRaises(HTTPException) as raised:
            self.service.delete_courier(self.owner, self.alheio.id)

        self.assertEqual(raised.exception.status_code, 404)
        self.assertIsNone(self.alheio.deleted_at)


class TestOAcesso(_Base):
    def test_gera_link_e_codigo_e_grava_so_os_hashes(self):
        response = self.service.generate_access(self.owner, self.ze.id)

        self.assertEqual(response.courier_id, self.ze.id)
        self.assertGreaterEqual(len(response.link_token), 43)
        self.assertRegex(response.access_code, r"^\d{6}$")
        self.assertIsNotNone(response.access_generated_at)
        self.assertNotIn(response.link_token, (self.ze.access_link_hash or ""))
        self.assertNotIn(response.access_code, (self.ze.access_code_hash or ""))
        self.assertTrue(verify_courier_link_token(response.link_token, self.ze.access_link_hash))
        self.assertTrue(
            verify_courier_access_code(
                response.access_code, response.link_token, self.ze.access_code_hash
            )
        )
        self.assertEqual(self.db.events, ["commit"])

    def test_regenerar_mata_o_par_anterior(self):
        antigo = self.service.generate_access(self.owner, self.ze.id)

        novo = self.service.generate_access(self.owner, self.ze.id)

        self.assertNotEqual(antigo.link_token, novo.link_token)
        self.assertFalse(verify_courier_link_token(antigo.link_token, self.ze.access_link_hash))
        self.assertTrue(verify_courier_link_token(novo.link_token, self.ze.access_link_hash))

    def test_o_codigo_so_vale_com_o_proprio_link(self):
        """A chave do HMAC e o link: o mesmo codigo com outro link nao confere.
        E o que faz um dump sem o link nao servir para forca bruta."""
        acesso = self.service.generate_access(self.owner, self.ze.id)

        self.assertFalse(
            verify_courier_access_code(acesso.access_code, "outro-link", self.ze.access_code_hash)
        )

    def test_dois_entregadores_nao_recebem_o_mesmo_link(self):
        um = self.service.generate_access(self.owner, self.ze.id)
        outro = self.service.generate_access(self.owner, self.tonho.id)

        self.assertNotEqual(um.link_token, outro.link_token)

    def test_inativo_ou_de_outro_restaurante_nao_ganha_acesso(self):
        self.ze.is_active = False
        with self.assertRaises(HTTPException) as inativo:
            self.service.generate_access(self.owner, self.ze.id)
        with self.assertRaises(HTTPException) as alheio:
            self.service.generate_access(self.owner, self.alheio.id)

        self.assertEqual(inativo.exception.status_code, 409)
        self.assertEqual(alheio.exception.status_code, 404)
