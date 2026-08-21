"""A classificacao RFV e os cinco filtros, pela HTTP, contra o Postgres.

**Este arquivo passou a ser o dono da REGRA em 21/08/2026.** Antes havia
`tests/test_customer_segment.py`, quinze testes rapidos sobre uma funcao pura
em Python — a funcao foi apagada quando a formula desceu para o SQL, e os
testes vieram para ca. O motivo esta no cabecalho de
`src/services/customer_segment.py`; o custo, que e coverage saindo do lado
barato da suite, foi aceito de olhos abertos.

Para a perda doer o menos possivel, a escada e provada por uma TABELA DE
CASOS: cada caso e um telefone com um historico proprio, tudo entra numa
consulta so, e a comparacao e de dicionario. Quando um limiar mudar, a falha
diz **qual caso virou de rotulo**, e nao apenas que um teste ficou vermelho.

O que so o banco decide, e por isso mora aqui:

1. **A escada em SQL.** O `CASE`, os grampos, a divisao que nao pode estourar
   e o piso inteiro dos dias.
2. **O `FILTER` do SQL.** `billable_orders_count` e `total_spent` tem que
   contar o mesmo conjunto. Se divergirem, o ticket medio sai um pouco menor
   e nada acusa — nao ha excecao, nao ha log, so um numero errado ao lado de
   um total certo.
3. **O recorte manda na classificacao.** O mesmo cliente pode ser `fiel` no
   restaurante e `perdido` na filial, e as duas leituras estao certas.
4. **Os filtros valem antes do `LIMIT`**, e o `total` conta o que sobrou
   depois deles.
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


def _corpo(resposta) -> dict:
    assert resposta.status_code == 200, (resposta.status_code, resposta.text[:300])
    return resposta.json()


def _linha(resposta, telefone: str = TELEFONE) -> dict:
    linhas = [item for item in _corpo(resposta)["items"] if item["customer_phone"] == telefone]
    assert len(linhas) == 1, _corpo(resposta)["items"]
    return linhas[0]


# Datas RELATIVAS ao relogio, e nao literais: a classificacao compara o
# `created_at` gravado com o `utcnow()` da requisicao. Data fixa aqui
# envelheceria de rotulo em rotulo sem ninguem mexer no codigo.
AGORA = datetime.now(timezone.utc)


def _ha(dias: float) -> datetime:
    return AGORA - timedelta(days=dias)


# ---------------------------------------------------------------------------
# A tabela de casos
# ---------------------------------------------------------------------------
#
# Cada caso: (nome legivel, dias atras de CADA pedido, rotulo esperado).
#
# Os numeros saem dos defaults de `Settings`: cadencia grampeada entre 7 e 60
# dias, 30 para quem tem um pedido so, "em risco" no dobro, "perdido" no
# quadruplo, "fiel" a partir do terceiro pedido, "novo" enquanto o PRIMEIRO
# pedido couber em 30 dias.
CASOS = [
    # -- a cadencia e do cliente: o mesmo silencio, rotulos diferentes -------
    ("semanal em dia (10 dias)", [10 + 7 * i for i in range(12)], "fiel"),
    ("semanal atrasado (16 dias)", [16 + 7 * i for i in range(12)], "em_risco"),
    ("semanal sumido (30 dias)", [30 + 7 * i for i in range(12)], "perdido"),
    ("mensal com os mesmos 16 dias", [16 + 30 * i for i in range(6)], "fiel"),
    ("mensal atrasado (70 dias)", [70 + 30 * i for i in range(6)], "em_risco"),
    ("mensal sumido (130 dias)", [130 + 30 * i for i in range(6)], "perdido"),
    # -- os dois grampos ----------------------------------------------------
    # Sem o PISO a cadencia seria ~0 e qualquer silencio passaria do
    # quadruplo dela: o cliente sairia "perdido" no dia seguinte.
    ("dois pedidos no mesmo almoco", [5, 5], "novo"),
    # Sem o TETO a cadencia seria de 240 dias e "em risco" chegaria no dia
    # 480 — ou seja, nunca.
    ("dois pedidos com oito meses entre eles", [160, 400], "em_risco"),
    # -- quem nao tem intervalo para medir ----------------------------------
    ("um pedido ontem", [1], "novo"),
    ("um pedido ha 100 dias", [100], "em_risco"),
    ("um pedido ha 200 dias", [200], "perdido"),
    # -- a ordem da escada, e o buraco que "ocasional" fecha ----------------
    # Se a contagem fosse conferida antes da recencia, este sairia "fiel" — e
    # e exatamente ele que a reativacao precisa achar.
    ("doze pedidos, sumido ha seis meses", [180 + 7 * i for i in range(12)], "perdido"),
    # Pediu semana passada, entao esta em dia com o proprio ritmo; mas o
    # relacionamento tem dez meses, e "novo" faria a tela mentir.
    ("relacionamento de dez meses, pedido recente", [7, 300], "ocasional"),
    ("dois pedidos, relacionamento de dez dias", [2, 10], "novo"),
    ("o terceiro pedido vira padrao", [5, 20, 40], "fiel"),
    # -- o piso inteiro dos dias, que decidiu o desenho inteiro -------------
    #
    # Cadencia 7, limiar de "em risco" em 14. Este cliente esta a 14 dias e
    # 23 horas: `floor` da 14, e 14 nao e maior que 14 — ele continua fiel.
    # Com dias FRACIONARIOS (14,96) ele viraria "em risco", e essa e a
    # divergencia de ate 24h que fez a versao Python ser apagada em vez de
    # mantida ao lado desta. Se alguem trocar o `floor` por uma divisao crua,
    # este caso — e so ele — fica vermelho.
    ("quatorze dias e 23 horas", [14.958 + 7 * i for i in range(12)], "fiel"),
]


def test_a_escada_de_classificacao(cliente_http, db):
    """Todos os casos numa consulta so, comparados por dicionario.

    A falha diz qual caso virou de rotulo: o pytest mostra as chaves que
    diferem, e cada chave e a frase que descreve o cliente.
    """
    restaurante = fab.criar_restaurante(db)
    filial = fab.criar_filial(db, restaurante)

    telefones = {}
    for indice, (nome, dias_dos_pedidos, _) in enumerate(CASOS):
        telefone = f"8590000{indice:04d}"
        telefones[telefone] = nome
        for dias in dias_dos_pedidos:
            fab.criar_pedido(
                db,
                restaurante,
                filial,
                status="completed",
                created_at=_ha(dias),
                customer_phone_snapshot=telefone,
            )
    dono = _admin(db, restaurante, None)
    db.flush()

    corpo = _corpo(cliente_http.get("/admin/customers?limit=100", headers=_como(dono)))
    recebido = {
        telefones[item["customer_phone"]]: item["segment"]
        for item in corpo["items"]
        if item["customer_phone"] in telefones
    }

    assert recebido == {nome: esperado for nome, _, esperado in CASOS}


def test_os_dias_na_tela_saem_da_mesma_expressao_do_rotulo(cliente_http, db):
    """`days_since_last_order` e o `CASE` do segmento leem o MESMO numero.

    E o que impede a tela de mostrar "ha 14 dias" ao lado de uma etiqueta
    calculada sobre 14,96 — o numero e a etiqueta nao conseguem discordar.
    """
    restaurante = fab.criar_restaurante(db)
    filial = fab.criar_filial(db, restaurante)
    for dias in [14.958 + 7 * i for i in range(12)]:
        fab.criar_pedido(
            db,
            restaurante,
            filial,
            status="completed",
            created_at=_ha(dias),
            customer_phone_snapshot=TELEFONE,
        )
    dono = _admin(db, restaurante, None)
    db.flush()

    linha = _linha(cliente_http.get("/admin/customers", headers=_como(dono)))

    assert linha["days_since_last_order"] == 14
    assert linha["segment"] == "fiel"
    # E a cadencia que produziu o limiar sai junto, para a tela conseguir
    # explicar o rotulo: 14 dias sem pedir contra um ritmo de 7.
    assert linha["cadence_days"] == 7.0


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


def test_cliente_que_so_cancelou_nao_derruba_a_consulta(cliente_http, db):
    """Divisao por zero e estado NORMAL: quem pediu e cancelou tudo.

    No SQL isso passa por `nullif` + `coalesce`; sem eles a listagem inteira
    do restaurante viraria 500 por causa de uma linha.
    """
    restaurante = fab.criar_restaurante(db)
    filial = fab.criar_filial(db, restaurante)
    for dias in (10, 3):
        fab.criar_pedido(
            db,
            restaurante,
            filial,
            status="cancelled",
            created_at=_ha(dias),
            customer_phone_snapshot=TELEFONE,
        )
    dono = _admin(db, restaurante, None)
    db.flush()

    linha = _linha(cliente_http.get("/admin/customers", headers=_como(dono)))

    assert linha["billable_orders_count"] == 0
    assert linha["average_ticket"] == 0.0
    assert linha["total_spent"] == 0.0


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

    for semana in range(12):
        fab.criar_pedido(
            db,
            restaurante,
            centro,
            status="completed",
            created_at=_ha(3 + semana * 7),
            customer_phone_snapshot=TELEFONE,
        )
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
        cliente_http.get(f"/admin/customers?branch_id={aldeota.id}", headers=_como(dono))
    )
    # O gerente preso a filial nem precisa pedir o recorte: ele ja vem.
    do_gerente = _linha(cliente_http.get("/admin/customers", headers=_como(gerente_da_aldeota)))

    assert do_restaurante["segment"] == "fiel"
    assert do_restaurante["orders_count"] == 13

    assert da_aldeota["segment"] == "perdido"
    assert da_aldeota["orders_count"] == 1

    assert do_gerente == da_aldeota


# ---------------------------------------------------------------------------
# Os cinco filtros
# ---------------------------------------------------------------------------


def _restaurante_com_uma_base(db):
    """Oito clientes: cinco fieis (semanais) e tres perdidos.

    Os tres perdidos gastam mais, para o filtro de ticket ter o que separar
    sem depender do filtro de segmento.
    """
    restaurante = fab.criar_restaurante(db)
    filial = fab.criar_filial(db, restaurante)

    for indice in range(5):
        for semana in range(12):
            fab.criar_pedido(
                db,
                restaurante,
                filial,
                status="completed",
                total=Decimal("20.00"),
                created_at=_ha(3 + semana * 7),
                customer_phone_snapshot=f"8591000{indice:04d}",
            )
    for indice in range(3):
        fab.criar_pedido(
            db,
            restaurante,
            filial,
            status="completed",
            total=Decimal("200.00"),
            created_at=_ha(300),
            customer_phone_snapshot=f"8592000{indice:04d}",
        )
    dono = _admin(db, restaurante, None)
    db.flush()
    return dono


def test_o_filtro_de_segmento_vale_antes_do_limit(cliente_http, db):
    """Tres perdidos, pagina de dois: o `total` tem que dizer TRES.

    E a prova de que o filtro esta no SQL. Filtrando a pagina ja paginada, o
    total continuaria contando os oito clientes e a tela mostraria "8
    clientes" sobre uma lista de perdidos.
    """
    dono = _restaurante_com_uma_base(db)

    corpo = _corpo(
        cliente_http.get("/admin/customers?segment=perdido&limit=2", headers=_como(dono))
    )

    assert corpo["total"] == 3
    assert len(corpo["items"]) == 2
    assert {item["segment"] for item in corpo["items"]} == {"perdido"}


def test_o_filtro_de_segmento_aceita_so_os_cinco_codigos(cliente_http, db):
    dono = _restaurante_com_uma_base(db)

    resposta = cliente_http.get("/admin/customers?segment=sumido", headers=_como(dono))

    assert resposta.status_code == 422, resposta.text[:200]


def test_o_filtro_de_ticket_recorta_pelos_dois_lados(cliente_http, db):
    dono = _restaurante_com_uma_base(db)

    caros = _corpo(cliente_http.get("/admin/customers?min_ticket=100", headers=_como(dono)))
    baratos = _corpo(cliente_http.get("/admin/customers?max_ticket=100", headers=_como(dono)))

    assert caros["total"] == 3
    assert {item["average_ticket"] for item in caros["items"]} == {200.00}
    assert baratos["total"] == 5
    assert {item["average_ticket"] for item in baratos["items"]} == {20.00}


def test_o_filtro_de_data_recorta_pelo_ultimo_pedido(cliente_http, db):
    """Os semanais pediram ha tres dias; os perdidos, ha trezentos.

    O corte vai na semana passada, entre os dois grupos — e nao "ontem", que
    e depois dos dois e deixaria os oito do lado de ca.
    """
    dono = _restaurante_com_uma_base(db)
    semana_passada = (AGORA - timedelta(days=7)).date()

    recentes = _corpo(
        cliente_http.get(
            f"/admin/customers?last_order_from={semana_passada}", headers=_como(dono)
        )
    )
    antigos = _corpo(
        cliente_http.get(
            f"/admin/customers?last_order_to={semana_passada}", headers=_como(dono)
        )
    )

    assert recentes["total"] == 5
    assert antigos["total"] == 3


def test_o_dia_do_last_order_to_entra_inteiro(cliente_http, db):
    """Fim INCLUSIVO: o cliente que pediu hoje as 23h nao pode cair fora de
    um recorte que termina hoje. O service manda a meia-noite do dia
    SEGUINTE para o SQL, e e isso que este teste trava."""
    restaurante = fab.criar_restaurante(db)
    filial = fab.criar_filial(db, restaurante)
    fab.criar_pedido(
        db,
        restaurante,
        filial,
        status="completed",
        created_at=AGORA,
        customer_phone_snapshot=TELEFONE,
    )
    dono = _admin(db, restaurante, None)
    db.flush()

    # O "hoje" do lojista, no fuso da operacao.
    hoje = AGORA.astimezone(timezone(timedelta(hours=-3))).date()
    corpo = _corpo(
        cliente_http.get(f"/admin/customers?last_order_to={hoje}", headers=_como(dono))
    )

    assert [item["customer_phone"] for item in corpo["items"]] == [TELEFONE]


def test_periodo_invertido_responde_400(cliente_http, db):
    dono = _restaurante_com_uma_base(db)

    resposta = cliente_http.get(
        "/admin/customers?last_order_from=2026-08-31&last_order_to=2026-08-01",
        headers=_como(dono),
    )

    assert resposta.status_code == 400, resposta.text[:200]


def test_os_filtros_se_combinam(cliente_http, db):
    """Segmento e ticket ao mesmo tempo: nenhum dos oito e perdido E barato."""
    dono = _restaurante_com_uma_base(db)

    corpo = _corpo(
        cliente_http.get(
            "/admin/customers?segment=perdido&max_ticket=50", headers=_como(dono)
        )
    )

    assert corpo["total"] == 0
    assert corpo["items"] == []
