"""A consulta do relatorio de entregadores contra o Postgres.

O que so o banco prova: que a soma le a atribuicao ABERTA da entrega
concluida (nao a fechada, que e de um motoboy trocado), que o instante e o
da linha `completed` do historico, que o recorte de filial e de restaurante
valem, e que corrida sem taxa conta como entrega e nao como zero.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from src.models.courier_model import Courier, CourierAssignment
from src.models.order_status_history_model import OrderStatusHistory
from src.repositories.courier_repository import CourierRepository
from src.utils.security import utcnow
from tests import fabricas_db as fab


pytestmark = pytest.mark.db


def _entregador(db, restaurante, filial, nome, telefone, **extras) -> Courier:
    courier = Courier(
        restaurant_id=restaurante.id, branch_id=filial.id, name=nome, phone=telefone, is_active=True, **extras
    )
    db.add(courier)
    db.flush()
    return courier


def _entrega_concluida(db, restaurante, filial, courier, taxa, quando, fechada=False) -> None:
    pedido = fab.criar_pedido(db, restaurante, filial, status="completed")
    db.add(CourierAssignment(
        order_id=pedido.id, courier_id=courier.id, courier_fee_snapshot=taxa,
        unassigned_at=quando if fechada else None,
    ))
    db.add(OrderStatusHistory(order_id=pedido.id, status="completed", changed_by="x", created_at=quando))
    db.flush()


def test_soma_por_entregador_com_os_recortes(db: Session):
    a = fab.criar_restaurante(db, "A")
    centro = fab.criar_filial(db, a, "Centro")
    aldeota = fab.criar_filial(db, a, "Aldeota")
    b = fab.criar_restaurante(db, "B")
    filial_b = fab.criar_filial(db, b)
    ze = _entregador(db, a, centro, "Zé", "85999990001")
    tonho = _entregador(db, a, aldeota, "Tonho", "85999990002")
    excluido = _entregador(db, a, centro, "Excluído", "85999990003", deleted_at=utcnow())
    alheio = _entregador(db, b, filial_b, "Alheio", "85999990004")
    agora = utcnow()
    _entrega_concluida(db, a, centro, ze, Decimal("8.00"), agora - timedelta(hours=1))
    _entrega_concluida(db, a, centro, ze, None, agora - timedelta(hours=2))
    # Fora do periodo.
    _entrega_concluida(db, a, centro, ze, Decimal("8.00"), agora - timedelta(days=10))
    # Trocado antes da entrega: a atribuicao fechada nao e dele.
    _entrega_concluida(db, a, centro, ze, Decimal("8.00"), agora - timedelta(hours=3), fechada=True)
    _entrega_concluida(db, a, aldeota, tonho, Decimal("5.50"), agora - timedelta(hours=1))
    _entrega_concluida(db, a, centro, excluido, Decimal("7.00"), agora - timedelta(hours=1))
    _entrega_concluida(db, b, filial_b, alheio, Decimal("9.00"), agora - timedelta(hours=1))
    repo = CourierRepository(db)

    linhas = repo.totals_by_courier(a.id, None, agora - timedelta(days=1), agora + timedelta(days=1))
    por_nome = {linha["name"]: linha for linha in linhas}

    assert set(por_nome) == {"Zé", "Tonho", "Excluído"}
    assert por_nome["Zé"]["deliveries_count"] == 2
    assert por_nome["Zé"]["deliveries_without_fee"] == 1
    assert por_nome["Zé"]["fee_total"] == Decimal("8.00")
    assert por_nome["Tonho"]["fee_total"] == Decimal("5.50")
    assert por_nome["Excluído"]["deleted_at"] is not None

    so_centro = repo.totals_by_courier(a.id, centro.id, agora - timedelta(days=1), agora + timedelta(days=1))
    assert {linha["name"] for linha in so_centro} == {"Zé", "Excluído"}
