"""Auditoria: o que o papel `attendant` de fato alcanca.

A pergunta que este arquivo responde e a do `config.ini` do agente de
impressao, que guarda e-mail e senha em texto puro na maquina do balcao: se
alguem ler aquele arquivo, o que ganha? O `README` do agente recomenda um
usuario `attendant` preso a filial, entao o estrago e exatamente o que um
`attendant` consegue fazer.

Arquivo de auditoria: nasceu para produzir evidencia.
"""

import uuid
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from main import app
from src.api.dependencies.database import get_db
from src.models.admin_user_model import AdminUser
from src.models.restaurant_setting_model import RestaurantSetting
from src.services.admin_auth_service import AdminAuthService
from tests import fabricas_db as fab


pytestmark = pytest.mark.db

PERIODO = "start_date=2026-06-01&end_date=2026-08-12"


def _admin(db: Session, restaurante, filial, role: str) -> AdminUser:
    admin = AdminUser(
        restaurant_id=restaurante.id,
        branch_id=filial.id,
        name=f"Usuario {role}",
        email=f"{uuid.uuid4().hex[:10]}@exemplo.com",
        password_hash="$2b$12$" + "x" * 53,
        role=role,
        is_active=True,
    )
    db.add(admin)
    db.flush()
    return admin


@pytest.fixture
def cliente_http(db: Session):
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


@pytest.fixture
def loja(db: Session):
    restaurante = fab.criar_restaurante(db, "Loja da Auditoria")
    matriz = fab.criar_filial(db, restaurante, "Matriz")
    outra = fab.criar_filial(db, restaurante, "Filial 2")
    categoria = fab.criar_categoria(db, restaurante)
    produto = fab.criar_produto(db, restaurante, categoria, preco=Decimal("100.00"))
    cliente = fab.criar_cliente(db, nome="Cliente Com Telefone")
    fab.criar_pedido(
        db, restaurante, matriz, cliente, status="completed", total=Decimal("100.00")
    )
    # Faturamento da OUTRA filial, com status que entra no relatorio.
    fab.criar_pedido(
        db, restaurante, outra, cliente, status="completed", total=Decimal("777.00")
    )
    db.add(RestaurantSetting(restaurant_id=restaurante.id, min_order_value=Decimal("0.00")))
    db.flush()

    atendente = _admin(db, restaurante, matriz, "attendant")
    db.expire_all()
    return {
        "restaurante": restaurante,
        "matriz": matriz,
        "outra": outra,
        "produto": produto,
        "cliente": cliente,
        "auth": {"Authorization": f"Bearer {AdminAuthService.create_access_token(atendente)}"},
    }


def test_attendant_edita_preco_de_produto(cliente_http, loja):
    r = cliente_http.patch(
        f"/admin/products/{loja['produto'].id}",
        json={"price": "0.01"},
        headers=loja["auth"],
    )
    assert r.status_code == 200, (r.status_code, r.text[:300])
    assert r.json()["price"] == 0.01


def test_attendant_le_a_lista_de_clientes_com_telefone(cliente_http, loja):
    r = cliente_http.get("/admin/customers", headers=loja["auth"])
    assert r.status_code == 200, (r.status_code, r.text[:300])
    assert "customer_phone" in r.text
    assert "customer_name" in r.text


def test_attendant_le_os_relatorios_financeiros(cliente_http, loja):
    for caminho in (
        f"/admin/reports/summary?{PERIODO}",
        f"/admin/reports/commission?{PERIODO}",
        f"/admin/reports/sales-by-day?{PERIODO}",
        f"/admin/reports/products?{PERIODO}",
    ):
        r = cliente_http.get(caminho, headers=loja["auth"])
        assert r.status_code == 200, (caminho, r.status_code, r.text[:300])


def test_attendant_muda_as_configuracoes_do_restaurante(cliente_http, loja):
    r = cliente_http.patch(
        "/admin/settings",
        json={"min_order_value": "999.00"},
        headers=loja["auth"],
    )
    assert r.status_code == 200, (r.status_code, r.text[:300])


def test_relatorio_ignora_a_filial_e_soma_o_restaurante_inteiro(cliente_http, loja):
    """O relatorio NAO tem recorte de filial — e decisao registrada.

    O cabecalho de `src/api/endpoints/admin_reports.py` diz que as rotas de
    relatorio usam `get_current_admin` e nao `get_admin_scope` de proposito:
    "o relatorio e do RESTAURANTE, nao da filial". Este teste registra o que
    isso significa na pratica — um `attendant` preso a Matriz le o
    faturamento das duas filiais, e o pedido de 877,00 (100 + 777) prova.

    Nao e um bug; e o tamanho do estrago do `config.ini` do agente de
    impressao, que guarda a senha de um usuario desses em texto puro.
    """
    r = cliente_http.get(f"/admin/reports/summary?{PERIODO}", headers=loja["auth"])
    assert r.status_code == 200, r.text[:300]
    assert "877" in r.text, (
        "O relatorio deixou de somar o restaurante inteiro — o comentario de "
        "admin_reports.py precisa ser revisto: " + r.text[:400]
    )
