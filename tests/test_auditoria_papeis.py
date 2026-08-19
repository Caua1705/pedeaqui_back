"""O que cada papel de fato alcanca, medido contra o Postgres.

Este arquivo nasceu como AUDITORIA: ele existia para produzir evidencia de
que `role` nao entrava em decisao de autorizacao nenhuma, e os quatro
primeiros testes afirmavam **200** — atendente editando preco, lendo a base
de clientes com telefone, lendo faturamento e comissao, e mudando o pedido
minimo do restaurante.

A pergunta que ele responde e a do `config.ini` do agente de impressao, que
guarda e-mail e senha em texto puro na maquina do balcao de um restaurante
de verdade: se alguem ler aquele arquivo, o que ganha?

**Agora ele e o teste de regressao da resposta.** Os mesmos quatro casos
afirmam 403, e o resto do arquivo trava as duas metades que faltam:

- o que o atendente CONTINUA podendo (senao ele usa a conta do dono, e ai
  nada disto vale nada);
- o que o papel de maquina `print_agent` alcanca, que e a lista curta que a
  frente inteira existe para produzir.
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

# Derivado da data de execução, nunca literal — ver `fab.periodo_de_relatorio`.
# A versão fixa deste arquivo envelheceu e fez o relatório voltar vazio.
PERIODO = fab.periodo_de_relatorio()


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
    pedido = fab.criar_pedido(
        db, restaurante, matriz, cliente, status="completed", total=Decimal("100.00")
    )
    # Faturamento da OUTRA filial, com status que entra no relatorio.
    fab.criar_pedido(
        db, restaurante, outra, cliente, status="completed", total=Decimal("777.00")
    )
    db.add(RestaurantSetting(restaurant_id=restaurante.id, min_order_value=Decimal("0.00")))
    db.flush()

    usuarios = {
        papel: _admin(db, restaurante, matriz, papel)
        for papel in ("owner", "manager", "attendant", "print_agent")
    }
    db.expire_all()
    return {
        "restaurante": restaurante,
        "matriz": matriz,
        "outra": outra,
        "produto": produto,
        "pedido": pedido,
        "cliente": cliente,
        "auth": {
            papel: {
                "Authorization": f"Bearer {AdminAuthService.create_access_token(usuario)}"
            }
            for papel, usuario in usuarios.items()
        },
    }


def como(loja, papel: str) -> dict:
    return loja["auth"][papel]


# ---------------------------------------------------------------------------
# Os quatro casos da auditoria original. Afirmavam 200; afirmam 403.
# ---------------------------------------------------------------------------


def test_attendant_nao_edita_mais_preco_de_produto(cliente_http, loja):
    r = cliente_http.patch(
        f"/admin/products/{loja['produto'].id}",
        json={"price": "0.01"},
        headers=como(loja, "attendant"),
    )
    assert r.status_code == 403, (r.status_code, r.text[:300])


def test_attendant_nao_le_mais_a_lista_de_clientes(cliente_http, loja):
    """A rota que mais pesa numa senha vazada: nome + telefone de toda a base."""
    r = cliente_http.get("/admin/customers", headers=como(loja, "attendant"))
    assert r.status_code == 403, (r.status_code, r.text[:300])


def test_attendant_nao_le_mais_os_relatorios_financeiros(cliente_http, loja):
    for caminho in (
        f"/admin/reports/summary?{PERIODO}",
        f"/admin/reports/commission?{PERIODO}",
        f"/admin/reports/sales-by-day?{PERIODO}",
        f"/admin/reports/products?{PERIODO}",
    ):
        r = cliente_http.get(caminho, headers=como(loja, "attendant"))
        assert r.status_code == 403, (caminho, r.status_code, r.text[:300])


def test_attendant_nao_muda_mais_as_configuracoes_do_restaurante(cliente_http, loja):
    r = cliente_http.patch(
        "/admin/settings",
        json={"min_order_value": "999.00"},
        headers=como(loja, "attendant"),
    )
    assert r.status_code == 403, (r.status_code, r.text[:300])


# ---------------------------------------------------------------------------
# O que o atendente CONTINUA podendo.
#
# Esta metade e tao importante quanto a de cima. Um atendente que nao consegue
# marcar "acabou a picanha" as 20h liga para o dono, e a saida pratica e ele
# usar a conta do dono — que devolve todas as permissoes de uma vez e desfaz
# a frente inteira sem nenhum teste ficar vermelho.
# ---------------------------------------------------------------------------


def test_attendant_ainda_marca_produto_como_esgotado(cliente_http, loja):
    r = cliente_http.patch(
        f"/admin/products/{loja['produto'].id}/availability",
        json={"is_available": False},
        headers=como(loja, "attendant"),
    )
    assert r.status_code == 200, (r.status_code, r.text[:300])


def test_attendant_ainda_abre_e_fecha_a_loja(cliente_http, loja):
    """A rota mudou de alvo na revisao 20260818_0025 — o papel nao mudou.

    Era `/admin/settings/store-status` e fechava o restaurante inteiro; hoje
    fecha UMA filial. Continua sendo do atendente pelo mesmo motivo: "vamos
    parar de aceitar pedido" as 21h e decisao de quem esta no balcao.
    """
    r = cliente_http.patch(
        f"/admin/branches/{loja['matriz'].id}/store-status",
        json={"is_open": False},
        headers=como(loja, "attendant"),
    )
    assert r.status_code == 200, (r.status_code, r.text[:300])


def test_attendant_ainda_ve_os_pedidos_e_o_cardapio(cliente_http, loja):
    """Sem estas duas, nao ha tela de balcao: e nelas que ele trabalha."""
    for caminho in ("/admin/orders", "/admin/products"):
        r = cliente_http.get(caminho, headers=como(loja, "attendant"))
        assert r.status_code == 200, (caminho, r.status_code, r.text[:300])


def test_attendant_nao_cancela_mas_pode_recusar(cliente_http, loja):
    """Cancelar pedido pago nao estorna nada e o log e o unico rastro.

    Quem esta no balcao e precisa nao atender um pedido tem `rejected` na
    maquina de estados — essa continua sendo dele.
    """
    r = cliente_http.patch(
        f"/admin/orders/{loja['pedido'].id}/cancel",
        json={"reason": "cliente desistiu"},
        headers=como(loja, "attendant"),
    )
    assert r.status_code == 403, (r.status_code, r.text[:300])


# ---------------------------------------------------------------------------
# O gerente: tudo do cardapio menos o dinheiro.
# ---------------------------------------------------------------------------


def test_manager_edita_produto_mas_nao_o_preco(cliente_http, loja):
    """A unica linha que separa gerente de dono no cardapio.

    Os dois casos na MESMA rota, de proposito: e o corpo que decide, nao o
    caminho, e nenhuma dependencia de rota conseguiria expressar isso.
    """
    sem_preco = cliente_http.patch(
        f"/admin/products/{loja['produto'].id}",
        json={"name": "Picanha na Chapa"},
        headers=como(loja, "manager"),
    )
    assert sem_preco.status_code == 200, (sem_preco.status_code, sem_preco.text[:300])

    com_preco = cliente_http.patch(
        f"/admin/products/{loja['produto'].id}",
        json={"price": "0.01"},
        headers=como(loja, "manager"),
    )
    assert com_preco.status_code == 403, (com_preco.status_code, com_preco.text[:300])


def test_manager_nao_cria_cupom(cliente_http, loja):
    """Desconto ilimitado pela porta ao lado do preco.

    Cupom nao estava na matriz da proposta. Deixa-lo de fora faria a regra do
    preco valer so no cardapio: um cupom de 99% custa o mesmo e nao passa por
    `products`.
    """
    r = cliente_http.post(
        "/admin/coupons",
        json={
            "code": "TESTE99",
            "discount_type": "percentage",
            "discount_value": "99.00",
        },
        headers=como(loja, "manager"),
    )
    assert r.status_code == 403, (r.status_code, r.text[:300])


def test_manager_nao_le_faturamento_mas_le_o_operacional(cliente_http, loja):
    financeiro = cliente_http.get(
        f"/admin/reports/summary?{PERIODO}", headers=como(loja, "manager")
    )
    assert financeiro.status_code == 403, (financeiro.status_code, financeiro.text[:300])

    operacional = cliente_http.get(
        f"/admin/reports/products?{PERIODO}", headers=como(loja, "manager")
    )
    assert operacional.status_code == 200, (operacional.status_code, operacional.text[:300])


# ---------------------------------------------------------------------------
# O papel de maquina. E este bloco que mede o tamanho do estrago do config.ini.
# ---------------------------------------------------------------------------


def test_print_agent_alcanca_as_vias_de_um_pedido(cliente_http, loja):
    """A rota que faz a comanda existir. Sem ela o agente nao serve para nada."""
    r = cliente_http.get(
        f"/admin/orders/{loja['pedido'].id}/print-jobs", headers=como(loja, "print_agent")
    )
    assert r.status_code == 200, (r.status_code, r.text[:300])


def test_print_agent_abre_o_stream_e_bate_o_heartbeat(cliente_http, loja):
    ticket = cliente_http.post(
        "/admin/orders/stream-ticket", headers=como(loja, "print_agent")
    )
    assert ticket.status_code == 200, (ticket.status_code, ticket.text[:300])

    batida = cliente_http.post(
        "/admin/print-agent/heartbeat",
        json={"agent_version": "1.0.0"},
        headers=como(loja, "print_agent"),
    )
    assert batida.status_code == 200, (batida.status_code, batida.text[:300])


def test_print_agent_nao_alcanca_nada_do_painel(cliente_http, loja):
    """A senha em texto puro no balcao passa a comprar SO as comandas.

    Antes desta frente, cada uma destas linhas respondia 200 — inclusive o
    relatorio de comissao, que traz o percentual negociado por contrato.
    """
    negadas = (
        ("GET", "/admin/customers"),
        ("GET", f"/admin/reports/summary?{PERIODO}"),
        ("GET", f"/admin/reports/commission?{PERIODO}"),
        ("GET", "/admin/orders"),
        ("GET", "/admin/products"),
        ("GET", "/admin/settings"),
    )
    for metodo, caminho in negadas:
        r = cliente_http.request(metodo, caminho, headers=como(loja, "print_agent"))
        assert r.status_code == 403, (caminho, r.status_code, r.text[:300])


def test_print_agent_nao_muda_status_de_pedido(cliente_http, loja):
    """Ele reage a mudanca de status; nao a provoca.

    Uma credencial de maquina que aceita pedido aceitaria tambem o pedido que
    o lojista ainda estava olhando na tela.
    """
    r = cliente_http.patch(
        f"/admin/orders/{loja['pedido'].id}/status",
        json={"status": "accepted"},
        headers=como(loja, "print_agent"),
    )
    assert r.status_code == 403, (r.status_code, r.text[:300])


def test_pessoas_nao_batem_heartbeat_no_lugar_do_agente(cliente_http, loja):
    """A porta do agente nao se abre do lado de dentro.

    Nao ha pessoa com motivo para dizer "a maquina esta viva": o painel
    mostraria um agente no ar que nao existe, e a tela de "por que nao esta
    imprimindo" passaria a mentir.
    """
    r = cliente_http.post(
        "/admin/print-agent/heartbeat",
        json={"agent_version": "1.0.0"},
        headers=como(loja, "owner"),
    )
    assert r.status_code == 403, (r.status_code, r.text[:300])


# ---------------------------------------------------------------------------
# O dono continua alcancando tudo, e o relatorio continua sem recorte de filial
# ---------------------------------------------------------------------------


def test_relatorio_do_dono_ignora_a_filial_e_soma_o_restaurante_inteiro(cliente_http, loja):
    """O relatorio NAO tem recorte de filial — e decisao registrada.

    O cabecalho de `src/api/endpoints/admin_reports.py` diz que as rotas de
    relatorio usam `get_current_admin` e nao `get_admin_scope` de proposito:
    "o relatorio e do RESTAURANTE, nao da filial". O pedido de 877,00
    (100 + 777) prova que continua assim.

    **E e por causa disto que o papel importa tanto ali.** Enquanto nao ha
    recorte de filial, "ler relatorio" e ler o restaurante inteiro, e nao a
    loja em que a pessoa trabalha.
    """
    r = cliente_http.get(f"/admin/reports/summary?{PERIODO}", headers=como(loja, "owner"))
    assert r.status_code == 200, r.text[:300]
    assert "877" in r.text, (
        "O relatorio deixou de somar o restaurante inteiro — o comentario de "
        "admin_reports.py precisa ser revisto: " + r.text[:400]
    )
