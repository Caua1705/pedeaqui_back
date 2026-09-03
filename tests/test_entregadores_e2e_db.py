"""A frente inteira, ponta a ponta, contra o Postgres e pelo HTTP.

O dono cadastra o motoboy e gera o acesso; o atendente atribui; o motoboy
abre o link com o codigo, ve o pedido, sai, entrega; o historico soma. E o
teste que responde "e se a dependencia, o service e o SQL nao concordarem".

Tambem e onde se prova o que a suite rapida nao consegue: que o link
regenerado morre NA HORA para a requisicao seguinte, e que o Bearer do
lojista nao abre rota nenhuma de `/courier` — nem o par do entregador abre
rota nenhuma de `/admin`.
"""

import uuid
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


def _admin(db: Session, restaurante, role: str = "owner") -> AdminUser:
    admin = AdminUser(
        restaurant_id=restaurante.id,
        name=f"Dono {role}",
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
    restaurante = fab.criar_restaurante(db)
    filial = fab.criar_filial(db, restaurante)
    filial.courier_fee_base = Decimal("6.00")
    filial.courier_fee_per_km = Decimal("1.00")
    cliente = fab.criar_cliente(db)
    pedido = fab.criar_pedido(db, restaurante, filial, cliente, status="ready")
    pedido.delivery_distance_km = Decimal("2.50")
    pedido.address_street = "Rua das Flores"
    pedido.address_number = "200"
    db.flush()
    admin = _admin(db, restaurante)
    token = AdminAuthService.create_access_token(admin)
    db.expire_all()
    return {
        "restaurante": restaurante,
        "filial": filial,
        "pedido": pedido,
        "auth": {"Authorization": f"Bearer {token}"},
    }


def _cadastrar_e_gerar(cliente_http, loja) -> dict:
    criado = cliente_http.post(
        "/admin/couriers",
        json={"branch_id": str(loja["filial"].id), "name": "Zé", "phone": "(85) 99999-0001"},
        headers=loja["auth"],
    )
    assert criado.status_code == 201, criado.text
    acesso = cliente_http.post(f"/admin/couriers/{criado.json()['id']}/access", headers=loja["auth"])
    assert acesso.status_code == 200, acesso.text
    return {"courier": criado.json(), **acesso.json()}


def test_do_cadastro_a_entrega(cliente_http, loja):
    ze = _cadastrar_e_gerar(cliente_http, loja)
    link, codigo = ze["link_token"], ze["access_code"]
    cabecalho = {"X-Courier-Code": codigo}

    # O painel atribui.
    atribuido = cliente_http.post(
        f"/admin/couriers/{ze['courier']['id']}/assignments",
        json={"order_ids": [str(loja["pedido"].id)]},
        headers=loja["auth"],
    )
    assert atribuido.status_code == 200, atribuido.text
    assert atribuido.json()["items"][0]["ok"] is True
    # 6 + 2.5 x 1 = 8.50
    assert atribuido.json()["items"][0]["assignment"]["courier_fee_snapshot"] == 8.5

    # O motoboy abre o link.
    me = cliente_http.get(f"/courier/{link}/me", headers=cabecalho)
    assert me.status_code == 200, me.text
    assert me.json()["name"] == "Zé"

    lista = cliente_http.get(f"/courier/{link}/orders", headers=cabecalho)
    assert lista.status_code == 200, lista.text
    assert [p["order_id"] for p in lista.json()] == [str(loja["pedido"].id)]
    assert lista.json()[0]["amount_to_collect"] == 50.0
    assert lista.json()[0]["can_leave"] is True

    saiu = cliente_http.post(
        f"/courier/{link}/orders/out-for-delivery",
        json={"order_ids": [str(loja["pedido"].id)]},
        headers=cabecalho,
    )
    assert saiu.status_code == 200, saiu.text
    assert saiu.json()["items"][0]["ok"] is True
    assert saiu.json()["items"][0]["order"]["status"] == "out_for_delivery"

    entregou = cliente_http.post(
        f"/courier/{link}/orders/{loja['pedido'].id}/delivered", headers=cabecalho
    )
    assert entregou.status_code == 200, entregou.text
    assert entregou.json()["status"] == "completed"

    # O painel ve o historico do pedido assinado pelo entregador.
    detalhe = cliente_http.get(f"/admin/orders/{loja['pedido'].id}", headers=loja["auth"])
    autores = [h["changed_by"] for h in detalhe.json()["status_history"]]
    assert autores[-2:] == ["entregador:Zé", "entregador:Zé"]

    # E o historico dele soma a taxa.
    historico = cliente_http.get(f"/courier/{link}/history", headers=cabecalho)
    assert historico.status_code == 200, historico.text
    assert historico.json()["deliveries_count"] == 1
    assert historico.json()["fee_total"] == 8.5

    # Entregue sumiu da lista.
    assert cliente_http.get(f"/courier/{link}/orders", headers=cabecalho).json() == []


def test_o_codigo_e_o_link_sao_o_par(cliente_http, loja):
    ze = _cadastrar_e_gerar(cliente_http, loja)
    link = ze["link_token"]

    sem_codigo = cliente_http.get(f"/courier/{link}/orders")
    codigo_errado = cliente_http.get(f"/courier/{link}/orders", headers={"X-Courier-Code": "000000"})
    link_errado = cliente_http.get("/courier/nao-existe/orders", headers={"X-Courier-Code": ze["access_code"]})

    assert sem_codigo.status_code == 401
    assert codigo_errado.status_code == 401
    assert link_errado.status_code == 404


def test_cinco_codigos_errados_travam_o_cadastro(cliente_http, loja):
    """A trava por falhas, pelo HTTP — e é aqui que se prova o que a suíte
    rápida não consegue: a contagem roda na DEPENDÊNCIA, e a requisição
    termina em 401. Sem o commit próprio, `get_db` fecharia a sessão sem
    gravar e a quinta tentativa seria sempre a primeira.

    Durante a trava, **nem o código certo abre** — que é o ponto inteiro
    dela: deixar a tentativa certa passar seria devolver justamente a
    resposta que a força bruta procura.
    """
    ze = _cadastrar_e_gerar(cliente_http, loja)
    link, codigo = ze["link_token"], ze["access_code"]

    for _ in range(5):
        errado = cliente_http.get(f"/courier/{link}/me", headers={"X-Courier-Code": "000000"})
    assert errado.status_code == 429, errado.text
    assert "acesso novo ao restaurante" in errado.json()["detail"]

    travado = cliente_http.get(f"/courier/{link}/me", headers={"X-Courier-Code": codigo})
    assert travado.status_code == 429, travado.text

    # E o painel vê o motivo: é o que o dono tem quando o motoboy liga.
    listado = cliente_http.get(f"/admin/couriers/{ze['courier']['id']}", headers=loja["auth"])
    assert listado.json()["access_blocked_until"] is not None


def test_regenerar_o_acesso_destrava_o_motoboy(cliente_http, loja):
    """A saída que não passa pelo app. O motoboy travado só recebe 429 na
    tela dele; quem destrava é a loja, gerando outro par."""
    ze = _cadastrar_e_gerar(cliente_http, loja)
    for _ in range(5):
        cliente_http.get(f"/courier/{ze['link_token']}/me", headers={"X-Courier-Code": "000000"})

    novo = cliente_http.post(
        f"/admin/couriers/{ze['courier']['id']}/access", headers=loja["auth"]
    ).json()

    aberto = cliente_http.get(
        f"/courier/{novo['link_token']}/me", headers={"X-Courier-Code": novo["access_code"]}
    )
    assert aberto.status_code == 200, aberto.text
    listado = cliente_http.get(f"/admin/couriers/{ze['courier']['id']}", headers=loja["auth"])
    assert listado.json()["access_blocked_until"] is None


def test_o_acerto_zera_a_contagem_de_falhas(cliente_http, loja):
    """Quatro erros e um acerto não deixam resíduo: o quinto erro de amanhã
    não pode ser a quinta falha de hoje."""
    ze = _cadastrar_e_gerar(cliente_http, loja)
    link, codigo = ze["link_token"], ze["access_code"]
    for _ in range(4):
        cliente_http.get(f"/courier/{link}/me", headers={"X-Courier-Code": "000000"})

    assert cliente_http.get(f"/courier/{link}/me", headers={"X-Courier-Code": codigo}).status_code == 200

    for _ in range(4):
        errado = cliente_http.get(f"/courier/{link}/me", headers={"X-Courier-Code": "000000"})
    assert errado.status_code == 401, errado.text


def test_regenerar_mata_o_link_antigo_na_hora(cliente_http, loja):
    ze = _cadastrar_e_gerar(cliente_http, loja)
    antigo = {"link": ze["link_token"], "codigo": ze["access_code"]}
    assert cliente_http.get(
        f"/courier/{antigo['link']}/me", headers={"X-Courier-Code": antigo["codigo"]}
    ).status_code == 200

    novo = cliente_http.post(f"/admin/couriers/{ze['courier']['id']}/access", headers=loja["auth"]).json()

    assert cliente_http.get(
        f"/courier/{antigo['link']}/me", headers={"X-Courier-Code": antigo["codigo"]}
    ).status_code == 404
    assert cliente_http.get(
        f"/courier/{novo['link_token']}/me", headers={"X-Courier-Code": novo["access_code"]}
    ).status_code == 200


def test_desativar_fecha_o_link_e_devolve_o_pedido(cliente_http, loja):
    ze = _cadastrar_e_gerar(cliente_http, loja)
    cabecalho = {"X-Courier-Code": ze["access_code"]}
    cliente_http.post(
        f"/admin/couriers/{ze['courier']['id']}/assignments",
        json={"order_ids": [str(loja["pedido"].id)]},
        headers=loja["auth"],
    )

    desativado = cliente_http.patch(
        f"/admin/couriers/{ze['courier']['id']}", json={"is_active": False}, headers=loja["auth"]
    )

    assert desativado.status_code == 200
    assert cliente_http.get(f"/courier/{ze['link_token']}/me", headers=cabecalho).status_code == 404
    quem = cliente_http.get(f"/admin/orders/{loja['pedido'].id}/courier", headers=loja["auth"]).json()
    assert quem["assignment"] is None


def test_as_duas_credenciais_nao_se_cruzam(cliente_http, loja):
    """Bearer de lojista nao abre `/courier`; o par do entregador nao abre
    `/admin`. E o que faz o entregador nao ser um papel do painel."""
    ze = _cadastrar_e_gerar(cliente_http, loja)

    com_bearer = cliente_http.get(f"/courier/{ze['link_token']}/orders", headers=loja["auth"])
    com_o_par = cliente_http.get(
        "/admin/orders",
        headers={"X-Courier-Code": ze["access_code"]},
    )
    com_o_link_como_bearer = cliente_http.get(
        "/admin/orders", headers={"Authorization": f"Bearer {ze['link_token']}"}
    )

    assert com_bearer.status_code == 401
    assert com_o_par.status_code == 401
    assert com_o_link_como_bearer.status_code == 401
