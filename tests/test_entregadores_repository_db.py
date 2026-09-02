"""`CourierRepository` contra o Postgres: o WHERE de verdade.

O fake da suite rapida prova que o parametro chegou; so aqui se prova que o
SQL recorta — restaurante, filial, `deleted_at` — e que a consulta pelo
link nao devolve o excluido nem o de outro hash.
"""

from datetime import timedelta

import pytest
from sqlalchemy.orm import Session

from src.models.courier_model import Courier, CourierAssignment
from src.repositories.courier_repository import CourierRepository
from src.utils.security import hash_courier_link_token, utcnow
from tests import fabricas_db as fab


pytestmark = pytest.mark.db


def _entregador(db: Session, restaurante, filial, nome="Zé", telefone="85999990000", **extras) -> Courier:
    courier = Courier(
        restaurant_id=restaurante.id,
        branch_id=filial.id,
        name=nome,
        phone=telefone,
        is_active=True,
        **extras,
    )
    db.add(courier)
    db.flush()
    return courier


@pytest.fixture
def cenario(db: Session):
    a = fab.criar_restaurante(db, "A")
    centro = fab.criar_filial(db, a, "Centro")
    aldeota = fab.criar_filial(db, a, "Aldeota")
    b = fab.criar_restaurante(db, "B")
    filial_b = fab.criar_filial(db, b)
    ze = _entregador(db, a, centro, "Zé", "85999990001")
    tonho = _entregador(db, a, aldeota, "Tonho", "85999990002")
    alheio = _entregador(db, b, filial_b, "Alheio", "85999990003")
    excluido = _entregador(db, a, centro, "Excluído", "85999990004", deleted_at=utcnow())
    return {
        "a": a, "b": b, "centro": centro, "aldeota": aldeota,
        "ze": ze, "tonho": tonho, "alheio": alheio, "excluido": excluido,
        "repo": CourierRepository(db),
    }


class TestALista:
    def test_recorta_por_restaurante_e_esconde_o_excluido(self, cenario):
        nomes = [c.name for c in cenario["repo"].list_by_restaurant(cenario["a"].id)]

        assert nomes == ["Tonho", "Zé"]

    def test_recorta_por_filial(self, cenario):
        nomes = [
            c.name
            for c in cenario["repo"].list_by_restaurant(cenario["a"].id, branch_id=cenario["centro"].id)
        ]

        assert nomes == ["Zé"]


class TestALeitura:
    def test_pelo_id_exige_o_restaurante(self, cenario):
        repo = cenario["repo"]

        assert repo.get_by_id_and_restaurant(cenario["ze"].id, cenario["a"].id) is not None
        assert repo.get_by_id_and_restaurant(cenario["ze"].id, cenario["b"].id) is None
        assert repo.get_by_id_and_restaurant(cenario["excluido"].id, cenario["a"].id) is None

    def test_pelo_link_so_o_nao_excluido(self, cenario, db: Session):
        repo = cenario["repo"]
        cenario["ze"].access_link_hash = hash_courier_link_token("link-do-ze")
        cenario["excluido"].access_link_hash = hash_courier_link_token("link-do-excluido")
        db.flush()

        assert repo.get_by_link_hash(hash_courier_link_token("link-do-ze")) is cenario["ze"]
        assert repo.get_by_link_hash(hash_courier_link_token("link-do-excluido")) is None
        assert repo.get_by_link_hash(hash_courier_link_token("link-que-nao-existe")) is None


class TestOTelefone:
    def test_existe_na_filial_entre_os_nao_excluidos(self, cenario):
        repo = cenario["repo"]

        assert repo.exists_phone_in_branch(cenario["centro"].id, "85999990001")
        # O excluido nao segura o telefone.
        assert not repo.exists_phone_in_branch(cenario["centro"].id, "85999990004")
        # A outra filial nao conta.
        assert not repo.exists_phone_in_branch(cenario["aldeota"].id, "85999990001")
        # O proprio, na edicao, nao conta como conflito.
        assert not repo.exists_phone_in_branch(
            cenario["centro"].id, "85999990001", exclude_courier_id=cenario["ze"].id
        )


class TestAsAtribuicoes:
    def test_fechar_as_abertas_do_entregador_nao_toca_nas_dos_outros(self, cenario, db: Session):
        repo = cenario["repo"]
        pedido_1 = fab.criar_pedido(db, cenario["a"], cenario["centro"])
        pedido_2 = fab.criar_pedido(db, cenario["a"], cenario["centro"])
        pedido_3 = fab.criar_pedido(db, cenario["a"], cenario["aldeota"])
        repo.create_assignment(CourierAssignment(order_id=pedido_1.id, courier_id=cenario["ze"].id))
        antiga = repo.create_assignment(
            CourierAssignment(
                order_id=pedido_2.id,
                courier_id=cenario["ze"].id,
                unassigned_at=utcnow() - timedelta(days=1),
            )
        )
        repo.create_assignment(CourierAssignment(order_id=pedido_3.id, courier_id=cenario["tonho"].id))

        fechadas = repo.mark_open_assignments_unassigned(cenario["ze"].id, None)

        assert fechadas == 1
        assert repo.get_open_assignment_of_order(pedido_1.id) is None
        assert repo.get_open_assignment_of_order(pedido_3.id) is not None
        assert antiga.unassigned_at < utcnow() - timedelta(hours=23)

    def test_a_lista_do_entregador_e_so_dele_e_so_das_abertas(self, cenario, db: Session):
        repo = cenario["repo"]
        pedido_1 = fab.criar_pedido(db, cenario["a"], cenario["centro"])
        pedido_2 = fab.criar_pedido(db, cenario["a"], cenario["centro"])
        pedido_3 = fab.criar_pedido(db, cenario["a"], cenario["aldeota"])
        repo.create_assignment(CourierAssignment(order_id=pedido_1.id, courier_id=cenario["ze"].id))
        repo.create_assignment(
            CourierAssignment(order_id=pedido_2.id, courier_id=cenario["ze"].id, unassigned_at=utcnow())
        )
        repo.create_assignment(CourierAssignment(order_id=pedido_3.id, courier_id=cenario["tonho"].id))

        abertas = repo.list_open_orders_by_courier(cenario["ze"].id)

        assert [order.id for _, order in abertas] == [pedido_1.id]
        assert repo.get_open_order_of_courier(cenario["ze"].id, pedido_3.id) is None
        assert repo.get_open_order_of_courier(cenario["ze"].id, pedido_1.id) is not None
