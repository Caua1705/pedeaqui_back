"""A atribuicao ponta a ponta, contra o Postgres.

O que so aparece com banco: o indice parcial de "uma ativa por pedido"
convivendo com a reatribuicao feita pelo service (fecha a antiga ANTES de
abrir a nova, na mesma transacao), e a taxa lida da linha real de
`branches`.
"""

from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from src.api.dependencies.admin_scope import AdminScope
from src.models.admin_user_model import AdminUser
from src.models.courier_model import Courier
from src.schemas.courier_schema import AdminAssignOrdersRequest
from src.services.admin_courier_service import AdminCourierService
from tests import fabricas_db as fab


pytestmark = pytest.mark.db


def _dono(db: Session, restaurante) -> AdminScope:
    admin = AdminUser(
        restaurant_id=restaurante.id,
        name="Dono",
        email=f"dono-{restaurante.slug}@exemplo.com",
        password_hash="$2b$12$" + "x" * 53,
        role="owner",
        is_active=True,
    )
    db.add(admin)
    db.flush()
    return AdminScope(admin_user=admin, restaurant_id=restaurante.id, branch_id=None)


def _entregador(db: Session, restaurante, filial, nome, telefone) -> Courier:
    courier = Courier(
        restaurant_id=restaurante.id, branch_id=filial.id, name=nome, phone=telefone, is_active=True
    )
    db.add(courier)
    db.flush()
    return courier


def test_atribui_reatribui_e_a_taxa_sai_da_filial(db: Session):
    restaurante = fab.criar_restaurante(db)
    filial = fab.criar_filial(db, restaurante)
    filial.courier_fee_base = Decimal("6.00")
    filial.courier_fee_per_km = Decimal("1.50")
    pedido = fab.criar_pedido(db, restaurante, filial, status="ready")
    pedido.delivery_distance_km = Decimal("3.00")
    db.flush()
    ze = _entregador(db, restaurante, filial, "Zé", "85999990001")
    tonho = _entregador(db, restaurante, filial, "Tonho", "85999990002")
    dono = _dono(db, restaurante)
    service = AdminCourierService(db)

    primeira = service.assign_orders(dono, ze.id, AdminAssignOrdersRequest(order_ids=[pedido.id]))
    segunda = service.assign_orders(dono, tonho.id, AdminAssignOrdersRequest(order_ids=[pedido.id]))

    assert primeira.items[0].ok
    # 6 + 3 x 1.5 = 10.50
    assert primeira.items[0].assignment.courier_fee_snapshot == 10.5
    assert segunda.items[0].ok
    quem = service.get_order_courier(dono, pedido.id)
    assert quem.courier.id == tonho.id
    assert [a.order_id for a in service.list_open_assignments(dono, ze.id)] == []
    assert [a.order_id for a in service.list_open_assignments(dono, tonho.id)] == [pedido.id]


def test_desatribuir_libera_o_pedido(db: Session):
    restaurante = fab.criar_restaurante(db)
    filial = fab.criar_filial(db, restaurante)
    pedido = fab.criar_pedido(db, restaurante, filial, status="ready")
    ze = _entregador(db, restaurante, filial, "Zé", "85999990001")
    dono = _dono(db, restaurante)
    service = AdminCourierService(db)
    service.assign_orders(dono, ze.id, AdminAssignOrdersRequest(order_ids=[pedido.id]))

    service.unassign_order(dono, pedido.id)

    assert service.get_order_courier(dono, pedido.id).assignment is None
    # E pode ser atribuido de novo: o indice parcial so vale para a ativa.
    de_novo = service.assign_orders(dono, ze.id, AdminAssignOrdersRequest(order_ids=[pedido.id]))
    assert de_novo.items[0].ok
