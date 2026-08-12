"""Auditoria: isolamento entre restaurantes, ponta a ponta, contra o Postgres.

Diferente de `test_admin_tenant_isolation.py`, que usa um repositorio falso e
confere que o `restaurant_id` e REPASSADO, aqui o teste sobe o app inteiro
(TestClient), grava dois restaurantes de verdade no banco de teste e tenta ler
e escrever recurso do restaurante B com o token do restaurante A, rota por
rota. E o teste que responde "e se alguem esqueceu o WHERE".

Arquivo de auditoria: nasceu para produzir evidencia, nao para cobrir regra
de negocio.
"""

import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from main import app
from src.api.dependencies.database import get_db
from src.models.admin_user_model import AdminUser
from src.models.customer_model import Customer
from src.models.restaurant_setting_model import RestaurantSetting
from src.services.admin_auth_service import AdminAuthService
from src.utils.security import create_signed_token
from tests import fabricas_db as fab


pytestmark = pytest.mark.db


def _token_de_cliente(cliente: Customer) -> str:
    """O mesmo token que POST /auth/login emite (src/services/auth_service.py)."""
    return create_signed_token(
        subject=str(cliente.id),
        purpose="customer_access",
        expires_delta=timedelta(minutes=60),
        extra={"type": "customer"},
    )


def _admin(db: Session, restaurante, filial, role: str = "owner") -> AdminUser:
    admin = AdminUser(
        restaurant_id=restaurante.id,
        branch_id=filial.id,
        name=f"Dono {role}",
        email=f"{uuid.uuid4().hex[:10]}@exemplo.com",
        password_hash="$2b$12$" + "x" * 53,
        role=role,
        is_active=True,
    )
    db.add(admin)
    db.flush()
    return admin


class Tenant:
    """Um restaurante completo: filial, cardapio, pedido, cliente, lojista."""

    def __init__(self, db: Session, nome: str):
        self.restaurante = fab.criar_restaurante(db, nome)
        self.filial = fab.criar_filial(db, self.restaurante)
        self.categoria = fab.criar_categoria(db, self.restaurante)
        self.produto = fab.criar_produto(db, self.restaurante, self.categoria)
        self.grupo = fab.criar_grupo_de_opcoes(db, self.produto)
        self.opcao = fab.criar_opcao(db, self.grupo)
        self.cliente = fab.criar_cliente(db)
        # O token em claro fica no objeto do teste porque o banco não o tem:
        # `orders` só guarda o hash desde a revisão 0016.
        self.tracking_token = f"token-de-{self.restaurante.slug}"
        self.pedido = fab.criar_pedido(
            db,
            self.restaurante,
            self.filial,
            self.cliente,
            tracking_token=self.tracking_token,
        )
        self.admin = _admin(db, self.restaurante, self.filial)
        db.add(
            RestaurantSetting(
                restaurant_id=self.restaurante.id,
                min_order_value=Decimal("0.00"),
            )
        )
        db.flush()
        self.token = AdminAuthService.create_access_token(self.admin)

    @property
    def auth(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}


@pytest.fixture
def cliente_http(db: Session):
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


@pytest.fixture
def a_e_b(db: Session):
    par = Tenant(db, "Restaurante A"), Tenant(db, "Restaurante B")
    # As fabricas so dao `flush`, entao as colunas com server_default (os
    # varios NUMERIC de desconto) ficam com o default do Python — `0`, int —
    # em vez do Decimal que o banco devolve. Expirar forca a releitura.
    db.expire_all()
    return par


def _criar_recursos_por_rota(client: TestClient, t: Tenant) -> dict:
    """Setor de impressao e forma de pagamento so existem via POST."""
    ids = {}
    r = client.post(
        f"/admin/branches/{t.filial.id}/printing-sectors",
        json={"name": "Cozinha"},
        headers=t.auth,
    )
    assert r.status_code in (200, 201), (r.status_code, r.text)
    ids["printing_sector_id"] = r.json()["id"]

    r = client.post(
        f"/admin/branches/{t.filial.id}/payment-methods",
        json={
            "payment_flow": "delivery",
            "method_type": "cash",
            "label": "Dinheiro",
        },
        headers=t.auth,
    )
    assert r.status_code in (200, 201), (r.status_code, r.text)
    ids["payment_method_id"] = r.json()["id"]
    return ids


def _rotas_de_detalhe(t: Tenant, extras: dict) -> list[tuple[str, str, dict | None]]:
    """(metodo, caminho, corpo) para todo recurso identificado por id."""
    b = t.filial.id
    return [
        ("GET", f"/admin/branches/{b}", None),
        ("PATCH", f"/admin/branches/{b}", {"name": "invadido"}),
        ("GET", f"/admin/branches/{b}/business-hours", None),
        ("PUT", f"/admin/branches/{b}/business-hours", {"periods": []}),
        ("GET", f"/admin/branches/{b}/payment-methods", None),
        (
            "POST",
            f"/admin/branches/{b}/payment-methods",
            {"payment_flow": "delivery", "method_type": "pix", "label": "Pix"},
        ),
        ("PATCH", f"/admin/branches/{b}/prep-time", {"delta_minutes": 5}),
        ("GET", f"/admin/branches/{b}/printing-sectors", None),
        ("POST", f"/admin/branches/{b}/printing-sectors", {"name": "Invadida"}),
        ("PATCH", f"/admin/categories/{t.categoria.id}", {"name": "invadida"}),
        (
            "PATCH",
            f"/admin/categories/{t.categoria.id}/printing-sector",
            {"printing_sector_id": None},
        ),
        ("PATCH", f"/admin/option-groups/{t.grupo.id}", {"name": "invadido"}),
        (
            "POST",
            f"/admin/option-groups/{t.grupo.id}/options",
            {"name": "Invadida", "additional_price": 0},
        ),
        ("PATCH", f"/admin/options/{t.opcao.id}", {"name": "invadida"}),
        ("GET", f"/admin/orders/{t.pedido.id}", None),
        ("PATCH", f"/admin/orders/{t.pedido.id}/cancel", {"reason": "invadido"}),
        ("GET", f"/admin/orders/{t.pedido.id}/print-jobs", None),
        ("PATCH", f"/admin/orders/{t.pedido.id}/status", {"status": "accepted"}),
        (
            "PATCH",
            f"/admin/payment-methods/{extras['payment_method_id']}",
            {"label": "invadido"},
        ),
        ("DELETE", f"/admin/payment-methods/{extras['payment_method_id']}", None),
        (
            "PATCH",
            f"/admin/printing-sectors/{extras['printing_sector_id']}",
            {"name": "invadido"},
        ),
        ("GET", f"/admin/products/{t.produto.id}", None),
        ("PATCH", f"/admin/products/{t.produto.id}", {"name": "invadido"}),
        ("PATCH", f"/admin/products/{t.produto.id}/availability", {"is_available": False}),
        ("GET", f"/admin/products/{t.produto.id}/option-groups", None),
        (
            "POST",
            f"/admin/products/{t.produto.id}/option-groups",
            {"name": "Invadido", "min_select": 0, "max_select": 1},
        ),
        (
            "PATCH",
            f"/admin/products/{t.produto.id}/printing-sector",
            {"printing_sector_id": None},
        ),
        (
            "PATCH",
            "/admin/categories/reorder",
            {"category_ids": [str(t.categoria.id)]},
        ),
        (
            "PATCH",
            "/admin/products/reorder",
            {"category_id": str(t.categoria.id), "product_ids": [str(t.produto.id)]},
        ),
    ]


def test_token_de_a_nao_alcanca_nenhum_recurso_de_b(cliente_http, a_e_b, db):
    a, b = a_e_b
    extras_b = _criar_recursos_por_rota(cliente_http, b)

    vazamentos = []
    inconclusivos = []
    for metodo, caminho, corpo in _rotas_de_detalhe(b, extras_b):
        resposta = cliente_http.request(metodo, caminho, json=corpo, headers=a.auth)
        if resposta.status_code == 404:
            continue
        if resposta.status_code == 422:
            inconclusivos.append((metodo, caminho, resposta.text[:200]))
            continue
        vazamentos.append((metodo, caminho, resposta.status_code, resposta.text[:300]))

    assert not vazamentos, "VAZAMENTO ENTRE RESTAURANTES:\n" + "\n".join(map(str, vazamentos))
    assert not inconclusivos, "422 (contrato) — teste inconclusivo:\n" + "\n".join(
        map(str, inconclusivos)
    )


def test_listagens_de_a_nao_mostram_nada_de_b(cliente_http, a_e_b):
    a, b = a_e_b
    corpo_b = {
        str(b.restaurante.id),
        str(b.filial.id),
        str(b.produto.id),
        str(b.categoria.id),
        str(b.pedido.id),
        str(b.cliente.id),
        b.cliente.phone,
        b.cliente.email,
    }

    listagens = [
        "/admin/orders",
        "/admin/orders/status-counts",
        "/admin/products",
        "/admin/categories",
        "/admin/customers",
        "/admin/coupons",
        "/admin/branches",
        "/admin/settings",
        "/admin/reports/summary?start_date=2026-06-01&end_date=2026-08-12",
        "/admin/reports/sales-by-day?start_date=2026-06-01&end_date=2026-08-12",
        "/admin/reports/products?start_date=2026-06-01&end_date=2026-08-12",
        "/admin/reports/payment-methods?start_date=2026-06-01&end_date=2026-08-12",
        "/admin/reports/cancellations?start_date=2026-06-01&end_date=2026-08-12",
        "/admin/reports/commission?start_date=2026-06-01&end_date=2026-08-12",
    ]

    vazamentos = []
    for caminho in listagens:
        resposta = cliente_http.get(caminho, headers=a.auth)
        assert resposta.status_code == 200, (caminho, resposta.status_code, resposta.text[:200])
        texto = resposta.text
        achados = [valor for valor in corpo_b if valor and valor in texto]
        if achados:
            vazamentos.append((caminho, achados))

    assert not vazamentos, "LISTAGEM VAZOU DADO DE OUTRO RESTAURANTE:\n" + "\n".join(
        map(str, vazamentos)
    )


def test_cliente_nao_le_pedido_de_outro_cliente(cliente_http, a_e_b, db):
    a, b = a_e_b
    token_cliente_a = _token_de_cliente(a.cliente)
    auth = {"Authorization": f"Bearer {token_cliente_a}"}

    resposta = cliente_http.get(f"/customers/me/orders/{b.pedido.id}", headers=auth)
    assert resposta.status_code == 404, (resposta.status_code, resposta.text[:300])

    lista = cliente_http.get("/customers/me/orders", headers=auth)
    assert lista.status_code == 200
    assert str(b.pedido.id) not in lista.text


def test_tracking_token_de_um_pedido_nao_serve_em_outro_restaurante(cliente_http, a_e_b):
    a, b = a_e_b
    resposta = cliente_http.get(
        f"/restaurants/{a.restaurante.slug}/orders/track/{b.tracking_token}"
    )
    assert resposta.status_code == 404, (resposta.status_code, resposta.text[:300])


def test_atendente_preso_a_filial_nao_le_pedido_de_outra_filial(cliente_http, db, a_e_b):
    a, _ = a_e_b
    outra_filial = fab.criar_filial(db, a.restaurante, "Filial 2")
    pedido_outra = fab.criar_pedido(db, a.restaurante, outra_filial)

    atendente = _admin(db, a.restaurante, a.filial, role="attendant")
    auth = {"Authorization": f"Bearer {AdminAuthService.create_access_token(atendente)}"}

    detalhe = cliente_http.get(f"/admin/orders/{pedido_outra.id}", headers=auth)
    assert detalhe.status_code == 404, (detalhe.status_code, detalhe.text[:300])

    lista = cliente_http.get("/admin/orders", headers=auth)
    assert lista.status_code == 200
    assert str(pedido_outra.id) not in lista.text


def test_token_de_cliente_nao_abre_rota_de_lojista(cliente_http, a_e_b):
    a, _ = a_e_b
    token_cliente = _token_de_cliente(a.cliente)
    auth = {"Authorization": f"Bearer {token_cliente}"}

    for caminho in ("/admin/orders", "/admin/products", "/admin/settings", "/admin/customers"):
        resposta = cliente_http.get(caminho, headers=auth)
        assert resposta.status_code == 401, (caminho, resposta.status_code, resposta.text[:200])


def test_token_de_lojista_nao_abre_rota_de_cliente(cliente_http, a_e_b):
    a, _ = a_e_b
    for caminho in ("/customers/me", "/customers/me/orders", "/customers/me/addresses"):
        resposta = cliente_http.get(caminho, headers=a.auth)
        assert resposta.status_code == 401, (caminho, resposta.status_code, resposta.text[:200])


def test_ticket_de_stream_nao_vale_como_token_do_painel(cliente_http, a_e_b):
    a, _ = a_e_b
    ticket = cliente_http.post("/admin/orders/stream-ticket", headers=a.auth)
    assert ticket.status_code == 200, ticket.text
    valor = ticket.json()["ticket"]

    resposta = cliente_http.get(
        "/admin/orders", headers={"Authorization": f"Bearer {valor}"}
    )
    assert resposta.status_code == 401, (resposta.status_code, resposta.text[:200])
