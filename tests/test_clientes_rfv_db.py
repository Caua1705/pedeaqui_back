"""A classificacao RFV pela HTTP, contra o Postgres.

`test_customer_segment.py` prova a REGRA e `test_admin_customers.py` prova o
encanamento. Sobram duas coisas que so o banco decide, e as duas sao
silenciosas quando erram:

1. **O `FILTER` do SQL.** `billable_orders_count` e `total_spent` tem que
   contar o mesmo conjunto. Se divergirem, o ticket medio sai um pouco menor
   e nada acusa — nao ha excecao, nao ha log, so um numero errado ao lado de
   um total certo.
2. **O recorte manda na classificacao.** O mesmo cliente pode ser `fiel` no
   restaurante e `perdido` na filial, e as duas leituras estao certas. E
   consequencia da consulta, e nao de uma regra escrita em algum lugar — o
   tipo de coisa que ninguem adivinha lendo o service.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from main import app
from src.api.dependencies.database import get_db
from src.models.admin_user_model import AdminUser
from src.services.admin_auth_service import AdminAuthService
from tests import fabricas_db as fab


pytestmark = pytest.mark.db

TELEFONE = "85988887777"


@pytest.fixture
def cliente_http(db: Session):
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


def _admin(db: Session, restaurante, filial, role: str = "owner") -> AdminUser:
    usuario = AdminUser(
        restaurant_id=restaurante.id,
        branch_id=filial.id if filial is not None else None,
        name=f"Usuario {role}",
        email=f"{role}-{fab._sufixo()}@exemplo.com",
        password_hash="$2b$12$" + "x" * 53,
        role=role,
        is_active=True,
    )
    db.add(usuario)
    db.flush()
    return usuario


def _como(usuario: AdminUser) -> dict:
    return {"Authorization": f"Bearer {AdminAuthService.create_access_token(usuario)}"}


def _linha(resposta, telefone: str = TELEFONE) -> dict:
    assert resposta.status_code == 200, (resposta.status_code, resposta.text[:300])
    linhas = [item for item in resposta.json()["items"] if item["customer_phone"] == telefone]
    assert len(linhas) == 1, resposta.json()["items"]
    return linhas[0]


# Datas RELATIVAS ao relogio, e nao literais: a classificacao compara o
# `created_at` gravado com o `utcnow()` da requisicao. Data fixa aqui
# envelheceria de rotulo em rotulo sem ninguem mexer no codigo.
AGORA = datetime.now(timezone.utc)


def _ha(dias: float) -> datetime:
    return AGORA - timedelta(days=dias)


def test_pedido_cancelado_conta_no_total_e_fica_de_fora_do_ticket(cliente_http, db):
    """Tres pedidos, um cancelado. Os dois contadores precisam discordar —
    e o ticket tem que dividir pelo menor deles."""
    restaurante = fab.criar_restaurante(db)
    filial = fab.criar_filial(db, restaurante)
    for dias, status, total in [
        (20, "completed", Decimal("60.00")),
        (10, "cancelled", Decimal("999.00")),
        (2, "completed", Decimal("40.00")),
    ]:
        fab.criar_pedido(
            db,
            restaurante,
            filial,
            status=status,
            total=total,
            created_at=_ha(dias),
            customer_phone_snapshot=TELEFONE,
        )
    dono = _admin(db, restaurante, None)
    db.flush()

    linha = _linha(cliente_http.get("/admin/customers", headers=_como(dono)))

    assert linha["orders_count"] == 3
    assert linha["billable_orders_count"] == 2
    # O cancelado de R$ 999 nao entra em nenhum dos dois numeros de dinheiro.
    assert linha["total_spent"] == 100.00
    # 100 / 2, e nunca 100 / 3 = 33,33.
    assert linha["average_ticket"] == 50.00


def test_a_classificacao_segue_o_recorte_da_consulta(cliente_http, db):
    """Semanal no Centro, um pedido so na Aldeota ha seis meses.

    O dono, olhando o restaurante inteiro, ve um cliente fiel. O gerente da
    Aldeota ve um cliente perdido. **As duas leituras estao certas**, e e a
    da Aldeota que serve ao gatilho de reativacao: quem vai chamar de volta e
    a loja, e chamar de volta quem nunca pediu ali nao e reativacao, e
    aquisicao — outro texto e outra oferta.
    """
    restaurante = fab.criar_restaurante(db)
    centro = fab.criar_filial(db, restaurante, nome="Centro")
    aldeota = fab.criar_filial(db, restaurante, nome="Aldeota")

    # Doze semanas de pedidos no Centro, o ultimo ha tres dias.
    for semana in range(12):
        fab.criar_pedido(
            db,
            restaurante,
            centro,
            status="completed",
            created_at=_ha(3 + semana * 7),
            customer_phone_snapshot=TELEFONE,
        )
    # E uma unica visita a Aldeota, ha seis meses.
    fab.criar_pedido(
        db,
        restaurante,
        aldeota,
        status="completed",
        created_at=_ha(180),
        customer_phone_snapshot=TELEFONE,
    )

    dono = _admin(db, restaurante, None)
    gerente_da_aldeota = _admin(db, restaurante, aldeota, role="manager")
    db.flush()

    do_restaurante = _linha(cliente_http.get("/admin/customers", headers=_como(dono)))
    da_aldeota = _linha(
        cliente_http.get(
            f"/admin/customers?branch_id={aldeota.id}", headers=_como(dono)
        )
    )
    # O gerente preso a filial nem precisa pedir o recorte: ele ja vem.
    do_gerente = _linha(cliente_http.get("/admin/customers", headers=_como(gerente_da_aldeota)))

    assert do_restaurante["segment"] == "fiel"
    assert do_restaurante["orders_count"] == 13

    assert da_aldeota["segment"] == "perdido"
    assert da_aldeota["orders_count"] == 1

    assert do_gerente == da_aldeota


def test_cliente_de_um_pedido_recente_nasce_novo(cliente_http, db):
    """O caso mais comum da tela de um restaurante que acabou de subir."""
    restaurante = fab.criar_restaurante(db)
    filial = fab.criar_filial(db, restaurante)
    fab.criar_pedido(
        db,
        restaurante,
        filial,
        status="completed",
        created_at=_ha(1),
        customer_phone_snapshot=TELEFONE,
    )
    dono = _admin(db, restaurante, None)
    db.flush()

    linha = _linha(cliente_http.get("/admin/customers", headers=_como(dono)))

    assert linha["segment"] == "novo"
    assert linha["days_since_last_order"] == 1
