"""O motoboy na listagem, contra o Postgres: o JOIN que o fake nao prova.

Tres coisas so o banco responde: que a listagem e o stream carregam a
atribuicao ABERTA (e nao a fechada, que e historico), que a pagina inteira
custa um numero fixo de consultas (sem um SELECT por pedido), e que o pedido
sem atribuicao sai com os dois campos nulos.
"""

from datetime import timedelta

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session

from src.models.courier_model import Courier, CourierAssignment
from src.repositories.order_repository import OrderRepository
from src.services.admin_order_service import AdminOrderService
from src.utils.security import utcnow
from tests import fabricas_db as fab


pytestmark = pytest.mark.db


def _entregador(db: Session, restaurante, filial, nome, telefone) -> Courier:
    courier = Courier(
        restaurant_id=restaurante.id, branch_id=filial.id, name=nome, phone=telefone, is_active=True
    )
    db.add(courier)
    db.flush()
    return courier


@pytest.fixture
def cenario(db: Session):
    restaurante = fab.criar_restaurante(db)
    filial = fab.criar_filial(db, restaurante)
    ze = _entregador(db, restaurante, filial, "Zé", "85999990001")
    tonho = _entregador(db, restaurante, filial, "Tonho", "85999990002")
    com_ze = fab.criar_pedido(db, restaurante, filial, status="ready")
    trocado = fab.criar_pedido(db, restaurante, filial, status="ready")
    sem_ninguem = fab.criar_pedido(db, restaurante, filial)
    db.add_all(
        [
            CourierAssignment(order_id=com_ze.id, courier_id=ze.id),
            # Foi do Tonho, depois passou para o Ze: so a aberta conta.
            CourierAssignment(
                order_id=trocado.id, courier_id=tonho.id, unassigned_at=utcnow() - timedelta(hours=1)
            ),
            CourierAssignment(order_id=trocado.id, courier_id=ze.id),
        ]
    )
    db.flush()
    db.expire_all()
    return {
        "restaurante": restaurante,
        "com_ze": com_ze,
        "trocado": trocado,
        "sem_ninguem": sem_ninguem,
        "ze": ze,
    }


def _contar_consultas(engine):
    contagem = {"n": 0, "sql": []}

    def _antes(conn, cursor, statement, parameters, context, executemany):
        contagem["n"] += 1
        contagem["sql"].append(statement.splitlines()[0][:90])

    event.listen(engine, "before_cursor_execute", _antes)
    return contagem, lambda: event.remove(engine, "before_cursor_execute", _antes)


def test_a_listagem_traz_o_motoboy_da_atribuicao_aberta(db: Session, cenario):
    pedidos = OrderRepository(db).list_orders_by_restaurant(cenario["restaurante"].id)
    por_id = {AdminOrderService.to_list_item(p).id: AdminOrderService.to_list_item(p) for p in pedidos}

    assert por_id[cenario["com_ze"].id].courier_name == "Zé"
    assert por_id[cenario["trocado"].id].courier_name == "Zé"
    assert por_id[cenario["trocado"].id].courier_id == cenario["ze"].id
    assert por_id[cenario["sem_ninguem"].id].courier_name is None
    assert por_id[cenario["sem_ninguem"].id].courier_id is None


def test_a_pagina_inteira_custa_um_numero_fixo_de_consultas(db: Session, cenario):
    """Sem o `selectinload`, cada `to_list_item` dispararia um SELECT da
    atribuicao e outro do entregador — um N+1 que a listagem de 50 pedidos
    transformaria em cem consultas por tela."""
    repositorio = OrderRepository(db)
    # Lido ANTES de contar: a fixture expirou a sessao, e ler o id do
    # restaurante dentro da contagem dispararia um SELECT que nao e da pagina.
    restaurant_id = cenario["restaurante"].id
    contagem, parar = _contar_consultas(db.get_bind())
    try:
        pedidos = repositorio.list_orders_by_restaurant(restaurant_id)
        itens = [AdminOrderService.to_list_item(p) for p in pedidos]
    finally:
        parar()

    assert len(itens) == 3
    # A pagina, a atribuicao aberta de todos (um IN) e os entregadores (um IN).
    assert contagem["n"] <= 3, f"{contagem['n']} consultas para uma pagina de 3 pedidos: {contagem['sql']}"


def test_o_stream_tambem_carrega_o_motoboy(db: Session, cenario):
    desde = utcnow() - timedelta(minutes=5)
    repositorio = OrderRepository(db)

    criados = repositorio.list_orders_created_since(cenario["restaurante"].id, None, desde, 50)
    contagem, parar = _contar_consultas(db.get_bind())
    try:
        nomes = {AdminOrderService.to_list_item(p).id: AdminOrderService.to_list_item(p).courier_name for p in criados}
    finally:
        parar()

    assert nomes[cenario["com_ze"].id] == "Zé"
    assert nomes[cenario["sem_ninguem"].id] is None
    assert contagem["n"] == 0, "o stream deixou a atribuicao para carregar preguicosamente"
