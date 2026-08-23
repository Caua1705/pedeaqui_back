"""A senha temporária abre a troca de senha, e mais nada. Ponta a ponta.

Contra o app inteiro (TestClient) porque o que se prova é uma decisão de
**dependência de rota**: o bloqueio mora em `get_current_admin`, e testá-lo
chamando o service não exercitaria nem o token, nem a rota liberada, nem as
que não são.

Por que a checagem é do backend e não da tela: a senha temporária atravessa um
canal informal — WhatsApp, papel, voz no balcão. O que limita o prejuízo disso
é a troca obrigatória. Se ela morasse só no painel, quem interceptasse a senha
chamaria a API direto, e a lista de clientes com telefone e o faturamento
estariam a um `curl` da tela que estaria "obrigando" a troca.
"""

import time
import uuid

import pytest
from fastapi.testclient import TestClient

from main import app
from src.api.dependencies.database import get_db
from src.models.admin_user_model import AdminUser
from src.services.admin_auth_service import AdminAuthService
from src.utils.security import hash_password
from tests.fabricas_db import criar_filial, criar_restaurante


pytestmark = pytest.mark.db


SENHA_TEMPORARIA = "SENHATEMPORARIA12345"
SENHA_ESCOLHIDA = "a-minha-senha-de-verdade"


@pytest.fixture
def cliente_http(db):
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as cliente:
        yield cliente
    app.dependency_overrides.clear()


@pytest.fixture
def loja(db):
    restaurante = criar_restaurante(db)
    filial = criar_filial(db, restaurante)
    return {"restaurante": restaurante, "filial": filial}


def criar_dono(db, loja, *, must_change_password: bool) -> AdminUser:
    usuario = AdminUser(
        restaurant_id=loja["restaurante"].id,
        branch_id=loja["filial"].id,
        name="Dono",
        email=f"{uuid.uuid4().hex[:12]}@exemplo.com",
        password_hash=hash_password(SENHA_TEMPORARIA),
        role="owner",
        is_active=True,
        must_change_password=must_change_password,
    )
    db.add(usuario)
    db.flush()
    return usuario


def como(usuario: AdminUser) -> dict:
    return {"Authorization": f"Bearer {AdminAuthService.create_access_token(usuario)}"}


def test_o_login_ja_avisa_que_a_senha_e_temporaria(cliente_http, db, loja):
    """O sinal que o painel obedece sai no login, nao num 403 depois."""
    usuario = criar_dono(db, loja, must_change_password=True)

    resposta = cliente_http.post(
        "/admin/auth/login",
        json={"email": usuario.email, "password": SENHA_TEMPORARIA},
    )

    assert resposta.status_code == 200, resposta.text[:300]
    assert resposta.json()["admin_user"]["must_change_password"] is True


def test_o_me_continua_abrindo(cliente_http, db, loja):
    """Sem ele o painel nao tem como saber para onde mandar a pessoa."""
    usuario = criar_dono(db, loja, must_change_password=True)

    resposta = cliente_http.get("/admin/auth/me", headers=como(usuario))

    assert resposta.status_code == 200, resposta.text[:300]
    assert resposta.json()["must_change_password"] is True


def test_as_outras_rotas_do_painel_respondem_403(cliente_http, db, loja):
    usuario = criar_dono(db, loja, must_change_password=True)

    for caminho in ("/admin/orders", "/admin/products", "/admin/customers", "/admin/users"):
        resposta = cliente_http.get(caminho, headers=como(usuario))
        assert resposta.status_code == 403, (caminho, resposta.status_code, resposta.text[:200])
        assert "Troque a senha" in resposta.json()["detail"]


def test_403_e_nao_401(cliente_http, db, loja):
    """O token e valido e a identidade e conhecida.

    401 mandaria o painel para a tela de login, que e onde a pessoa nao resolve
    nada — ela ja entrou.
    """
    usuario = criar_dono(db, loja, must_change_password=True)

    resposta = cliente_http.get("/admin/orders", headers=como(usuario))

    assert resposta.status_code == 403


def test_o_ticket_do_stream_tambem_e_barrado(cliente_http, db, loja):
    """A rota que emite o ticket passa por `get_current_admin`.

    E o que fecha o SSE de graca: sem ticket nao ha stream, e o stream e a
    unica rota /admin que nao autentica por Bearer.
    """
    usuario = criar_dono(db, loja, must_change_password=True)

    resposta = cliente_http.post("/admin/orders/stream-ticket", headers=como(usuario))

    assert resposta.status_code == 403, resposta.text[:300]


def test_trocar_a_senha_destranca_o_painel(cliente_http, db, loja):
    usuario = criar_dono(db, loja, must_change_password=True)

    troca = cliente_http.patch(
        "/admin/auth/password",
        json={
            "current_password": SENHA_TEMPORARIA,
            "new_password": SENHA_ESCOLHIDA,
            "confirm_password": SENHA_ESCOLHIDA,
        },
        headers=como(usuario),
    )
    assert troca.status_code == 200, troca.text[:300]

    # O token anterior morreu junto (`password_changed_at`), entao o painel
    # refaz o login — que e o que ele faz de verdade depois da troca.
    #
    # O segundo de espera nao e folclore: `token_was_issued_before_password_change`
    # trata como ANTERIOR o token emitido no mesmo segundo da troca, porque o
    # `iat` do JWT tem resolucao de segundos e o erro para esse lado custa um
    # login a mais em vez de deixar aberta a sessao do ladrao. Sem a espera, o
    # teste mediria essa regra em vez de medir a troca de senha.
    time.sleep(1.1)
    login = cliente_http.post(
        "/admin/auth/login",
        json={"email": usuario.email, "password": SENHA_ESCOLHIDA},
    )
    assert login.status_code == 200, login.text[:300]
    assert login.json()["admin_user"]["must_change_password"] is False

    novo_token = {"Authorization": f"Bearer {login.json()['access_token']}"}
    assert cliente_http.get("/admin/orders", headers=novo_token).status_code == 200


def test_quem_nao_tem_pendencia_nao_e_incomodado(cliente_http, db, loja):
    """Todo lojista existente esta assim, e o deploy nao pode move-los."""
    usuario = criar_dono(db, loja, must_change_password=False)

    resposta = cliente_http.get("/admin/orders", headers=como(usuario))

    assert resposta.status_code == 200, resposta.text[:300]


def test_a_troca_pelo_proprio_lojista_sem_pendencia_nao_cria_uma(cliente_http, db, loja):
    usuario = criar_dono(db, loja, must_change_password=False)

    troca = cliente_http.patch(
        "/admin/auth/password",
        json={
            "current_password": SENHA_TEMPORARIA,
            "new_password": SENHA_ESCOLHIDA,
            "confirm_password": SENHA_ESCOLHIDA,
        },
        headers=como(usuario),
    )

    assert troca.status_code == 200, troca.text[:300]
    login = cliente_http.post(
        "/admin/auth/login",
        json={"email": usuario.email, "password": SENHA_ESCOLHIDA},
    )
    assert login.json()["admin_user"]["must_change_password"] is False
