"""A operação por filial pela HTTP, ponta a ponta.

Os testes unitários provam cada peça isolada. Este prova a única coisa que
elas não conseguem provar sozinhas: **que as quatro superfícies concordam.**

Fechar a filial do Centro tem que produzir, no mesmo minuto:

    PATCH /admin/branches/{centro}/store-status   →  a chave grava
    POST  .../branches/availability               →  Centro fechada, Aldeota aberta
    GET   .../menu?branch_id={centro}             →  is_open false
    POST  .../orders  (para o Centro)             →  400

Enquanto `is_open` foi do restaurante, essas quatro respostas eram a MESMA
resposta e não havia como discordarem. Agora podem — e é exatamente essa a
regressão que este arquivo existe para pegar, porque nenhuma das quatro
falharia sozinha.
"""

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from main import app
from src.api.dependencies.database import get_db
from src.models.admin_user_model import AdminUser
from src.models.branch_business_hour_model import BranchBusinessHour
from src.models.branch_payment_method_model import BranchPaymentMethod
from src.services.admin_auth_service import AdminAuthService
from tests import fabricas_db as fab


pytestmark = pytest.mark.db


@pytest.fixture
def cliente_http(db: Session):
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


def _abre_a_semana_inteira(db: Session, filial) -> None:
    """Sete dias 00:00–23:59. Sem isso, `ensure_branch_is_open` recusa tudo.

    O ponto do arquivo é a PAUSA MANUAL, e ela só se distingue da agenda com
    a agenda fora do caminho (armadilha 3).
    """
    for weekday in range(7):
        db.add(BranchBusinessHour(
            branch_id=filial.id,
            weekday=weekday,
            opens_at="00:00",
            closes_at="23:59",
            prep_time_min=20,
            prep_time_max=30,
            is_closed=False,
            sort_order=0,
        ))
    db.flush()


@pytest.fixture
def rede(db: Session):
    """Um restaurante, duas filiais, tudo aberto e vendendo."""
    restaurante = fab.criar_restaurante(db, "Junior da Picanha")
    centro = fab.criar_filial(db, restaurante, "Centro")
    aldeota = fab.criar_filial(db, restaurante, "Aldeota")
    centro.is_main = True
    fab.criar_configuracoes(db, restaurante)

    # UM cardapio por loja, desde a revisao 20260820_0026. As duas vendem a
    # mesma picanha, e as duas linhas compartilham `catalog_key` — e assim que
    # a rede fica com dois produtos independentes que o relatorio ainda sabe
    # somar.
    produtos = {}
    for filial in (centro, aldeota):
        categoria = fab.criar_categoria(db, restaurante, filial=filial)
        produtos[filial.name] = fab.criar_produto(
            db,
            restaurante,
            categoria,
            preco=Decimal("50.00"),
            catalog_key="picanha",
        )

    for filial in (centro, aldeota):
        _abre_a_semana_inteira(db, filial)
        db.add(BranchPaymentMethod(
            branch_id=filial.id,
            payment_flow="delivery",
            method_type="cash",
            label="Dinheiro",
            enabled=True,
            requires_gateway=False,
            sort_order=0,
        ))

    dono = AdminUser(
        restaurant_id=restaurante.id,
        branch_id=None,
        name="Dona da rede",
        email=f"dono-{restaurante.slug}@exemplo.com",
        password_hash="$2b$12$" + "x" * 53,
        role="owner",
        is_active=True,
    )
    db.add(dono)
    db.flush()
    db.expire_all()

    return {
        "restaurante": restaurante,
        "centro": centro,
        "aldeota": aldeota,
        "produtos": produtos,
        "auth": {"Authorization": f"Bearer {AdminAuthService.create_access_token(dono)}"},
    }


def _fechar(cliente_http, rede, filial) -> None:
    resposta = cliente_http.patch(
        f"/admin/branches/{filial.id}/store-status",
        json={"is_open": False},
        headers=rede["auth"],
    )
    assert resposta.status_code == 200, resposta.text[:300]


def _pedido(rede, filial) -> dict:
    """Um pedido com o produto DAQUELA loja.

    Mandar o produto da outra e 400 desde a revisao 20260820_0026, e ha um
    teste so para isso — `test_produto_de_uma_filial_nao_fecha_pedido_na_outra`.
    """
    return {
        "branch_id": str(filial.id),
        "order_type": "pickup",
        "payment_method": "cash",
        "customer": {"name": "Ana", "phone": "85999999999"},
        "items": [{"product_id": str(rede["produtos"][filial.name].id), "quantity": 1}],
    }


def test_a_pausa_de_uma_filial_nao_fecha_a_outra_em_lugar_nenhum(cliente_http, rede):
    """As quatro superfícies, no mesmo minuto, sobre as mesmas duas filiais."""
    slug = rede["restaurante"].slug
    _fechar(cliente_http, rede, rede["centro"])

    disponibilidade = cliente_http.post(f"/restaurants/{slug}/branches/availability", json={})
    assert disponibilidade.status_code == 200, disponibilidade.text[:300]
    por_nome = {item["name"]: item for item in disponibilidade.json()["branches"]}
    assert por_nome["Centro"]["is_open_now"] is False
    assert por_nome["Centro"]["closed_reason"] == "branch_paused"
    assert por_nome["Aldeota"]["is_open_now"] is True
    assert por_nome["Aldeota"]["closed_reason"] is None

    fechada = cliente_http.get(f"/restaurants/{slug}/menu?branch_id={rede['centro'].id}")
    aberta = cliente_http.get(f"/restaurants/{slug}/menu?branch_id={rede['aldeota'].id}")
    assert fechada.json()["settings"]["is_open"] is False
    assert aberta.json()["settings"]["is_open"] is True

    recusado = cliente_http.post(f"/restaurants/{slug}/orders", json=_pedido(rede, rede["centro"]))
    aceito = cliente_http.post(f"/restaurants/{slug}/orders", json=_pedido(rede, rede["aldeota"]))
    assert recusado.status_code == 400, recusado.text[:300]
    assert aceito.status_code == 200, aceito.text[:300]


def test_a_agenda_aberta_convive_com_a_pausa_e_a_resposta_diz_qual_e_qual(cliente_http, rede):
    """`current_period` preenchido com `is_open_now` falso não é contradição.

    A agenda está em ordem — quem fechou foi o balcão. É o que permite a tela
    escrever "fechada no momento" em vez de "abre às 18:00", que seria mentira.
    """
    slug = rede["restaurante"].slug
    _fechar(cliente_http, rede, rede["centro"])

    resposta = cliente_http.post(f"/restaurants/{slug}/branches/availability", json={})
    centro = {item["name"]: item for item in resposta.json()["branches"]}["Centro"]

    assert centro["is_open_now"] is False
    assert centro["current_period"] is not None


def test_a_sobrescrita_comercial_vale_so_na_filial_que_a_recebeu(cliente_http, rede):
    """O outro regime: NULL herda, valor vence — e nenhum dos dois vaza."""
    slug = rede["restaurante"].slug

    cliente_http.patch(
        "/admin/settings",
        json={"min_order_value": "20.00"},
        headers=rede["auth"],
    )
    ajustada = cliente_http.patch(
        f"/admin/branches/{rede['centro'].id}/settings",
        json={"min_order_value": "45.00"},
        headers=rede["auth"],
    )
    assert ajustada.status_code == 200, ajustada.text[:300]

    centro = cliente_http.get(f"/restaurants/{slug}/menu?branch_id={rede['centro'].id}")
    aldeota = cliente_http.get(f"/restaurants/{slug}/menu?branch_id={rede['aldeota'].id}")
    assert centro.json()["settings"]["min_order_value"] == 45.0
    assert aldeota.json()["settings"]["min_order_value"] == 20.0

    # O pedido de 50,00 passa nas duas; o mínimo do Centro é o que morde.
    caro = cliente_http.post(f"/restaurants/{slug}/orders", json=_pedido(rede, rede["centro"]))
    assert caro.status_code == 200, caro.text[:300]


def test_voltar_a_herdar_e_uma_chamada_com_null(cliente_http, rede):
    """O terceiro estado do PATCH parcial, pela HTTP.

    Sem ele a filial ficaria com a cópia congelada para sempre, e mudar o
    padrão do restaurante não chegaria mais nela.
    """
    slug = rede["restaurante"].slug
    cliente_http.patch("/admin/settings", json={"min_order_value": "20.00"}, headers=rede["auth"])
    cliente_http.patch(
        f"/admin/branches/{rede['centro'].id}/settings",
        json={"min_order_value": "45.00"},
        headers=rede["auth"],
    )

    voltou = cliente_http.patch(
        f"/admin/branches/{rede['centro'].id}/settings",
        json={"min_order_value": None},
        headers=rede["auth"],
    )
    assert voltou.status_code == 200, voltou.text[:300]
    assert voltou.json()["overrides"]["min_order_value"] is None
    assert voltou.json()["effective"]["min_order_value"] == 20.0

    centro = cliente_http.get(f"/restaurants/{slug}/menu?branch_id={rede['centro'].id}")
    assert centro.json()["settings"]["min_order_value"] == 20.0


def test_desligar_a_entrega_de_uma_filial_nao_desliga_a_da_outra(cliente_http, rede):
    slug = rede["restaurante"].slug
    resposta = cliente_http.patch(
        f"/admin/branches/{rede['centro'].id}/order-types",
        json={"accepts_delivery": False},
        headers=rede["auth"],
    )
    assert resposta.status_code == 200, resposta.text[:300]

    centro = cliente_http.get(f"/restaurants/{slug}/menu?branch_id={rede['centro'].id}")
    aldeota = cliente_http.get(f"/restaurants/{slug}/menu?branch_id={rede['aldeota'].id}")
    assert centro.json()["settings"]["accepts_delivery"] is False
    assert aldeota.json()["settings"]["accepts_delivery"] is True
    # Retirada não foi tocada: o PATCH é parcial.
    assert centro.json()["settings"]["accepts_pickup"] is True


def test_o_menu_sem_branch_id_fala_da_filial_padrao(cliente_http, rede):
    """E diz de qual, para o app não ter que adivinhar."""
    slug = rede["restaurante"].slug
    _fechar(cliente_http, rede, rede["centro"])

    menu = cliente_http.get(f"/restaurants/{slug}/menu").json()

    # A Centro e a principal, entao e ela a padrao — e ela esta pausada.
    assert menu["settings_branch_id"] == str(rede["centro"].id)
    assert menu["settings"]["is_open"] is False


def test_o_menu_com_filial_de_outro_restaurante_e_404(cliente_http, db, rede):
    vizinho = fab.criar_restaurante(db, "Vizinho")
    filial_alheia = fab.criar_filial(db, vizinho, "Unica")

    resposta = cliente_http.get(
        f"/restaurants/{rede['restaurante'].slug}/menu?branch_id={filial_alheia.id}"
    )

    assert resposta.status_code == 404


def test_a_tela_de_operacao_do_painel_sai_em_uma_chamada(cliente_http, rede):
    _fechar(cliente_http, rede, rede["centro"])

    resposta = cliente_http.get("/admin/branches/operation", headers=rede["auth"])

    assert resposta.status_code == 200, resposta.text[:300]
    por_nome = {linha["branch_name"]: linha for linha in resposta.json()}
    assert por_nome["Centro"]["is_open"] is False
    assert por_nome["Centro"]["is_open_now"] is False
    assert por_nome["Aldeota"]["is_open"] is True
    assert por_nome["Aldeota"]["is_open_now"] is True
