"""Troca de senha do lojista e revogacao dos tokens dele.

Antes desta mudanca, um token de lojista roubado valia 12h e nao havia como
mata-lo: `admin_users` nao tinha `password_changed_at`, e nao existia rota
para o lojista trocar a propria senha. A unica alavanca era trocar
`ADMIN_AUTH_SECRET`, que desloga todo mundo e para todo agente de impressao.

Estes testes travam as duas pontas: a rota existe e funciona, e o token
emitido antes da troca para de valer — inclusive o ticket do stream SSE.
"""

import time
import uuid
from datetime import timedelta

from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import pytest

from main import app
from src.api.dependencies.database import get_db
from src.core.constants import MIN_ADMIN_PASSWORD_LENGTH
from src.models.admin_user_model import AdminUser
from src.services.admin_auth_service import AdminAuthService
from src.utils.security import hash_password, utcnow
from tests import fabricas_db as fab


pytestmark = pytest.mark.db

SENHA_ATUAL = "senha-antiga-comprida"
SENHA_NOVA = "senha-nova-bem-comprida"


@pytest.fixture
def cliente_http(db: Session):
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


@pytest.fixture
def lojista(db: Session):
    restaurante = fab.criar_restaurante(db, "Loja da Senha")
    filial = fab.criar_filial(db, restaurante)
    admin = AdminUser(
        restaurant_id=restaurante.id,
        branch_id=filial.id,
        name="Dono",
        email=f"{uuid.uuid4().hex[:10]}@exemplo.com",
        password_hash=hash_password(SENHA_ATUAL),
        role="owner",
        is_active=True,
    )
    db.add(admin)
    db.flush()
    db.expire_all()
    return admin


def _auth(admin: AdminUser) -> dict:
    return {"Authorization": f"Bearer {AdminAuthService.create_access_token(admin)}"}


def test_troca_de_senha_funciona(cliente_http, lojista):
    resposta = cliente_http.patch(
        "/admin/auth/password",
        json={
            "current_password": SENHA_ATUAL,
            "new_password": SENHA_NOVA,
            "confirm_password": SENHA_NOVA,
        },
        headers=_auth(lojista),
    )
    assert resposta.status_code == 200, resposta.text
    assert "message" in resposta.json()

    entrar = cliente_http.post(
        "/admin/auth/login",
        json={"email": lojista.email, "password": SENHA_NOVA},
    )
    assert entrar.status_code == 200, entrar.text


def test_senha_atual_errada_e_recusada(cliente_http, lojista):
    resposta = cliente_http.patch(
        "/admin/auth/password",
        json={
            "current_password": "nao-e-a-senha",
            "new_password": SENHA_NOVA,
            "confirm_password": SENHA_NOVA,
        },
        headers=_auth(lojista),
    )
    assert resposta.status_code == 400, resposta.text
    assert resposta.json()["detail"] == "Senha atual incorreta"


def test_senha_curta_e_recusada_no_minimo_de_lojista(cliente_http, lojista):
    """12, e nao os 8 do cliente. Ver MIN_ADMIN_PASSWORD_LENGTH."""
    curta = "a" * (MIN_ADMIN_PASSWORD_LENGTH - 1)
    resposta = cliente_http.patch(
        "/admin/auth/password",
        json={
            "current_password": SENHA_ATUAL,
            "new_password": curta,
            "confirm_password": curta,
        },
        headers=_auth(lojista),
    )
    assert resposta.status_code == 422, resposta.text


def test_confirmacao_diferente_e_recusada(cliente_http, lojista):
    resposta = cliente_http.patch(
        "/admin/auth/password",
        json={
            "current_password": SENHA_ATUAL,
            "new_password": SENHA_NOVA,
            "confirm_password": SENHA_NOVA + "x",
        },
        headers=_auth(lojista),
    )
    assert resposta.status_code == 400, resposta.text


def test_token_anterior_a_troca_para_de_valer(cliente_http, lojista):
    """O ponto inteiro do bloco: o token do ladrao morre junto."""
    token_antigo = _auth(lojista)

    antes = cliente_http.get("/admin/auth/me", headers=token_antigo)
    assert antes.status_code == 200, antes.text

    # `iat` tem resolucao de segundos: sem esperar, o token da linha de cima
    # nasceria no mesmo segundo da troca e o teste passaria pelo
    # arredondamento conservador, nao pela regra.
    time.sleep(1.1)

    trocar = cliente_http.patch(
        "/admin/auth/password",
        json={
            "current_password": SENHA_ATUAL,
            "new_password": SENHA_NOVA,
            "confirm_password": SENHA_NOVA,
        },
        headers=token_antigo,
    )
    assert trocar.status_code == 200, trocar.text

    depois = cliente_http.get("/admin/auth/me", headers=token_antigo)
    assert depois.status_code == 401, (depois.status_code, depois.text)
    assert "revogado" in depois.json()["detail"].lower()


def test_o_ticket_de_stream_tambem_e_revogado(cliente_http, db, lojista):
    """Um ticket vivo manteria o pedido chegando na tela de quem foi expulso."""
    ticket = AdminAuthService.create_stream_ticket(lojista).ticket

    time.sleep(1.1)
    lojista.password_changed_at = utcnow()
    db.add(lojista)
    db.flush()

    with pytest.raises(HTTPException) as erro:
        AdminAuthService(db).get_admin_from_stream_ticket(ticket)
    assert erro.value.status_code == 401


def test_token_emitido_depois_da_troca_continua_valendo(cliente_http, db, lojista):
    lojista.password_changed_at = utcnow() - timedelta(seconds=5)
    db.add(lojista)
    db.flush()

    resposta = cliente_http.get("/admin/auth/me", headers=_auth(lojista))
    assert resposta.status_code == 200, resposta.text


def test_lojista_sem_troca_de_senha_nao_e_afetado(cliente_http, lojista):
    """`password_changed_at` nulo = nada revogado, o estado das linhas de hoje."""
    assert lojista.password_changed_at is None
    resposta = cliente_http.get("/admin/auth/me", headers=_auth(lojista))
    assert resposta.status_code == 200, resposta.text


def test_um_lojista_nao_revoga_o_token_do_outro(cliente_http, db, lojista):
    outro = AdminUser(
        restaurant_id=lojista.restaurant_id,
        branch_id=lojista.branch_id,
        name="Outro",
        email=f"{uuid.uuid4().hex[:10]}@exemplo.com",
        password_hash=hash_password(SENHA_ATUAL),
        role="manager",
        is_active=True,
    )
    db.add(outro)
    db.flush()
    auth_do_outro = _auth(outro)

    time.sleep(1.1)
    cliente_http.patch(
        "/admin/auth/password",
        json={
            "current_password": SENHA_ATUAL,
            "new_password": SENHA_NOVA,
            "confirm_password": SENHA_NOVA,
        },
        headers=_auth(lojista),
    )

    resposta = cliente_http.get("/admin/auth/me", headers=auth_do_outro)
    assert resposta.status_code == 200, resposta.text
