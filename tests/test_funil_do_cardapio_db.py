"""O funil do cardápio contra um Postgres de verdade.

Quatro coisas que só o banco prova, e cada uma já tem um jeito conhecido de
dar errado em silêncio:

1. **O CHECK e a constante concordam** (armadilha 15). O teste de schema
   prova que o Pydantic aceita a lista; este prova que o banco aceita a mesma.
   Divergência não dá erro na tela — dá `IntegrityError` no INSERT de um tipo
   que o front já tinha aprendido a mandar.
2. **A filial tem que ser daquele restaurante.** A FK composta é o que
   dispensa um SELECT de conferência por requisição na rota mais quente da
   API; se ela não estiver lá, nada avisa — só nasce um evento apontando
   para a loja de outro restaurante.
3. **Sessão distinta é a unidade do funil.** `COUNT(DISTINCT session_id)` e
   `COUNT(*)` só divergem quando há dado dentro, que é exatamente o que um
   teste com dublê não pega.
4. **O expurgo apaga em lotes e para sozinho.** O laço só termina quando o
   lote volta vazio; escrito errado, ele roda para sempre ou apaga um lote só.
"""

import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from main import app
from src.api.dependencies.database import get_db

from src.core.constants import DEFAULT_TRAFFIC_SOURCE, MENU_EVENT_TYPES
from src.models.menu_event_model import MenuEvent
from src.repositories.menu_event_repository import MenuEventRepository
from src.schemas.menu_event_schema import MenuEventBatchRequest
from src.services.admin_report_service import AdminReportService
from src.services.menu_event_service import (
    MENU_EVENT_RETENTION_DAYS,
    MenuEventService,
    menu_event_retention_cutoff,
)
from src.utils.security import utcnow
from tests.fabricas_db import criar_filial, criar_pedido, criar_restaurante


pytestmark = pytest.mark.db


@pytest.fixture
def loja(db):
    restaurante = criar_restaurante(db)
    return restaurante, criar_filial(db, restaurante)


@pytest.fixture
def cliente_http(db):
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


def _evento(
    db,
    loja,
    event_type: str = "menu_view",
    session_id: str = "s1",
    source: str = DEFAULT_TRAFFIC_SOURCE,
    dias_atras: int = 0,
):
    restaurante, filial = loja
    evento = MenuEvent(
        restaurant_id=restaurante.id,
        branch_id=filial.id,
        session_id=session_id,
        event_type=event_type,
        source=source,
        occurred_at=utcnow() - timedelta(days=dias_atras),
    )
    db.add(evento)
    db.flush()
    return evento


class TestTipoDeEvento:
    def test_todo_tipo_da_constante_e_aceito_pelo_check(self, db, loja):
        """A outra metade da armadilha 15.

        O teste de schema prova que o Pydantic aceita a constante; este prova
        que o CHECK do banco aceita a mesma lista. Um tipo que exista só na
        constante passa a validação e morre no INSERT.
        """
        for tipo in MENU_EVENT_TYPES:
            evento = _evento(db, loja, event_type=tipo)

            assert evento.id is not None

    def test_tipo_fora_da_lista_e_recusado_pelo_banco(self, db, loja):
        with pytest.raises(IntegrityError):
            _evento(db, loja, event_type="scrolled")


class TestAmarraDeFilial:
    def test_filial_de_outro_restaurante_nao_grava(self, db, loja):
        """A FK composta `(restaurant_id, branch_id)`.

        É ela que permite à rota não conferir a filial com um SELECT por
        requisição. Sem ela o INSERT passaria, e o evento de uma loja
        apareceria no funil de outro restaurante — com 200 e sem log.
        """
        _, filial = loja
        outro_restaurante = criar_restaurante(db, nome="Outro Restaurante")

        evento = MenuEvent(
            restaurant_id=outro_restaurante.id,
            branch_id=filial.id,
            session_id="s1",
            event_type="menu_view",
            source=DEFAULT_TRAFFIC_SOURCE,
        )
        db.add(evento)

        with pytest.raises(IntegrityError):
            db.flush()


class TestGravacaoEmLote:
    def test_o_lote_inteiro_vira_uma_linha_por_evento(self, db, loja):
        restaurante, filial = loja
        payload = MenuEventBatchRequest(
            restaurant_id=restaurante.id,
            branch_id=filial.id,
            session_id="sessao-1",
            source="QR Mesa 04",
            events=[
                {"event_type": "menu_view"},
                {"event_type": "product_view"},
                {"event_type": "cart_add"},
            ],
        )

        resposta = MenuEventService(db).record_batch(payload)

        assert resposta.recorded == 3

    def test_a_origem_e_gravada_ja_normalizada(self, db, loja):
        """Sem isto, `QR Mesa 04` e `qr-mesa-04` viram duas linhas do
        relatório para a mesma mesa."""
        restaurante, filial = loja
        payload = MenuEventBatchRequest(
            restaurant_id=restaurante.id,
            branch_id=filial.id,
            session_id="sessao-1",
            source="QR Mesa 04",
            events=[{"event_type": "menu_view"}],
        )

        MenuEventService(db).record_batch(payload)

        gravado = db.query(MenuEvent).filter(MenuEvent.session_id == "sessao-1").one()
        assert gravado.source == "qr-mesa-04"

    def test_filial_inexistente_nao_derruba_a_rota(self, db, loja):
        """Telemetria não derruba cardápio.

        O erro esperado aqui é de integridade — front mandando id errado — e
        ele não pode virar 500 na tela de quem está escolhendo o jantar.
        """
        restaurante, _ = loja
        payload = MenuEventBatchRequest(
            restaurant_id=restaurante.id,
            branch_id=uuid.uuid4(),
            session_id="sessao-1",
            events=[{"event_type": "menu_view"}],
        )

        resposta = MenuEventService(db).record_batch(payload)

        assert resposta.recorded == 0


class TestContagemDeSessoes:
    def test_a_unidade_e_a_sessao_e_nao_o_evento(self, db, loja):
        """Três toques da mesma pessoa no mesmo degrau são UMA sessão.

        O front já deduplica, mas o `DISTINCT` é o que garante o número
        quando ele parar de deduplicar — e ele vai parar, no dia em que
        alguém reescrever a tela.
        """
        restaurante, filial = loja
        for _ in range(3):
            _evento(db, loja, event_type="product_view", session_id="mesma-sessao")
        _evento(db, loja, event_type="product_view", session_id="outra-sessao")

        contagem = MenuEventRepository(db).sessions_by_event_type(
            restaurante.id,
            utcnow() - timedelta(days=1),
            utcnow() + timedelta(days=1),
        )

        assert contagem["product_view"] == 2

    def test_o_recorte_de_filial_separa_as_lojas(self, db, loja):
        restaurante, _ = loja
        segunda_filial = criar_filial(db, restaurante, nome="Aldeota")
        _evento(db, loja, session_id="matriz-1")
        evento_da_outra = MenuEvent(
            restaurant_id=restaurante.id,
            branch_id=segunda_filial.id,
            session_id="aldeota-1",
            event_type="menu_view",
            source=DEFAULT_TRAFFIC_SOURCE,
        )
        db.add(evento_da_outra)
        db.flush()

        repositorio = MenuEventRepository(db)
        janela = (utcnow() - timedelta(days=1), utcnow() + timedelta(days=1))

        rede = repositorio.sessions_by_event_type(restaurante.id, *janela)
        so_aldeota = repositorio.sessions_by_event_type(
            restaurante.id, *janela, branch_id=segunda_filial.id
        )

        assert rede["menu_view"] == 2
        assert so_aldeota["menu_view"] == 1

    def test_a_divisao_por_origem_conta_so_a_visita(self, db, loja):
        """Somar os quatro degraus contaria a mesma sessão uma vez por degrau
        alcançado, e a origem que converte melhor apareceria com mais
        "sessões" — inflando o próprio denominador."""
        restaurante, _ = loja
        for tipo in MENU_EVENT_TYPES:
            _evento(db, loja, event_type=tipo, session_id="s1", source="panfleto")

        linhas = MenuEventRepository(db).sessions_by_source(
            restaurante.id,
            utcnow() - timedelta(days=1),
            utcnow() + timedelta(days=1),
        )

        assert dict(linhas) == {"panfleto": 1}


class TestOQuintoDegrau:
    def test_o_pedido_cancelado_conta_no_funil(self, db, loja):
        """O funil mede se a PESSOA terminou de pedir.

        A loja recusar meia hora depois é outro problema, com outra solução.
        Contá-lo como não-conversão faria o funil dizer "o checkout está
        perdendo gente" quando o que houve foi a loja recusando pedido — que
        é exatamente o colapso de diagnósticos que o funil existe para
        desfazer.
        """
        restaurante, filial = loja
        criar_pedido(db, restaurante, filial, status="completed")
        criar_pedido(db, restaurante, filial, status="cancelled")

        report = AdminReportService(db).funnel_report(
            restaurante.id, *periodo_de_relatorio_de_hoje()
        )

        assert report.orders_count == 2

    def test_o_resumo_continua_sem_contar_o_cancelado(self, db, loja):
        """A outra metade do teste acima: a ressalva do `orders_note` é
        verdade, e os dois relatórios discordam de propósito."""
        restaurante, filial = loja
        criar_pedido(db, restaurante, filial, status="completed")
        criar_pedido(db, restaurante, filial, status="cancelled")

        resumo = AdminReportService(db).sales_summary(
            restaurante.id, *periodo_de_relatorio_de_hoje()
        )

        assert resumo.orders_count == 1


class TestOrigemNoPedido:
    def test_pedido_sem_origem_nasce_direct(self, db, loja):
        """NOT NULL com default. O nulo dividiria o relatório entre "veio
        direto" e "não sabemos", e nenhum lojista quer essa distinção."""
        restaurante, filial = loja

        pedido = criar_pedido(db, restaurante, filial)
        db.refresh(pedido)

        assert pedido.source_snapshot == DEFAULT_TRAFFIC_SOURCE

    def test_o_relatorio_recorta_por_origem(self, db, loja):
        restaurante, filial = loja
        do_qr = criar_pedido(db, restaurante, filial, status="completed")
        do_qr.source_snapshot = "qr-mesa-04"
        criar_pedido(db, restaurante, filial, status="completed")
        db.flush()

        servico = AdminReportService(db)
        periodo = periodo_de_relatorio_de_hoje()

        tudo = servico.sales_summary(restaurante.id, *periodo)
        so_o_qr = servico.sales_summary(restaurante.id, *periodo, source="qr-mesa-04")

        assert tudo.orders_count == 2
        assert so_o_qr.orders_count == 1


class TestRetencao:
    def test_o_corte_fica_no_passado(self):
        agora = utcnow()

        assert menu_event_retention_cutoff(agora) < agora

    def test_o_corte_e_de_noventa_dias(self):
        """O número bate com `MAX_REPORT_DAYS` (92) de propósito: não pode
        existir tela capaz de pedir um recorte que o banco já apagou."""
        agora = utcnow()

        assert menu_event_retention_cutoff(agora) == agora - timedelta(
            days=MENU_EVENT_RETENTION_DAYS
        )

    def test_o_expurgo_apaga_o_vencido_e_deixa_o_resto(self, db, loja):
        _evento(db, loja, session_id="antigo", dias_atras=MENU_EVENT_RETENTION_DAYS + 1)
        _evento(db, loja, session_id="recente", dias_atras=1)

        apagados = MenuEventRepository(db).delete_occurred_before(
            menu_event_retention_cutoff(utcnow()), limit=1000
        )

        assert apagados == 1
        assert db.query(MenuEvent).one().session_id == "recente"

    def test_o_lote_limita_quantos_saem_por_vez(self, db, loja):
        """O expurgo apaga em lotes com commit entre um e outro; um DELETE
        único de centenas de milhares de linhas segura lock e infla a tabela
        pela duração inteira da transação."""
        for indice in range(5):
            _evento(
                db,
                loja,
                session_id=f"antigo-{indice}",
                dias_atras=MENU_EVENT_RETENTION_DAYS + 1,
            )
        cutoff = menu_event_retention_cutoff(utcnow())
        repositorio = MenuEventRepository(db)

        primeiro = repositorio.delete_occurred_before(cutoff, limit=2)
        segundo = repositorio.delete_occurred_before(cutoff, limit=2)
        terceiro = repositorio.delete_occurred_before(cutoff, limit=2)
        quarto = repositorio.delete_occurred_before(cutoff, limit=2)

        assert [primeiro, segundo, terceiro, quarto] == [2, 2, 1, 0]

    def test_o_laco_para_quando_o_lote_volta_vazio(self, db, loja):
        """O zero do fim é o que termina o laço do script. Sem ele o expurgo
        rodaria para sempre."""
        assert (
            MenuEventRepository(db).delete_occurred_before(
                menu_event_retention_cutoff(utcnow()), limit=10
            )
            == 0
        )


def periodo_de_relatorio_de_hoje():
    """As duas datas que cobrem o dia de hoje no fuso da operação.

    Os pedidos das fábricas nascem com `now()` do banco, e o recorte do
    relatório é lido em America/Fortaleza: uma janela de um dia para cada lado
    é o jeito de o teste não depender da hora em que ele roda.
    """
    hoje = utcnow().date()
    return hoje - timedelta(days=1), hoje + timedelta(days=1)


class TestARota:
    """A rota pública, de ponta a ponta.

    O que só o `TestClient` prova: o `202`, e que o corpo do lote atravessa
    schema, service e banco sem ninguém no meio exigir autenticação.
    """

    def test_lote_responde_202_e_grava(self, cliente_http, db, loja):
        restaurante, filial = loja

        resposta = cliente_http.post(
            "/menu-events",
            json={
                "restaurant_id": str(restaurante.id),
                "branch_id": str(filial.id),
                "session_id": "sessao-http",
                "source": "QR Mesa 04",
                "events": [
                    {"event_type": "menu_view"},
                    {"event_type": "checkout_start"},
                ],
            },
        )

        assert resposta.status_code == 202
        assert resposta.json() == {"recorded": 2}

    def test_a_rota_nao_pede_login(self, cliente_http, db, loja):
        """O cardápio é público e quem navega nele pode nem ter conta.

        Este teste é a prova de que ninguém acrescentou uma dependência de
        autenticação sem perceber — o que transformaria o funil em "só quem
        já tem conta", que é o oposto da pergunta.
        """
        restaurante, filial = loja

        resposta = cliente_http.post(
            "/menu-events",
            json={
                "restaurant_id": str(restaurante.id),
                "branch_id": str(filial.id),
                "session_id": "sem-login",
                "events": [{"event_type": "menu_view"}],
            },
        )

        assert resposta.status_code == 202

    def test_lote_acima_do_teto_e_recusado_com_422(self, cliente_http, db, loja):
        restaurante, filial = loja

        resposta = cliente_http.post(
            "/menu-events",
            json={
                "restaurant_id": str(restaurante.id),
                "branch_id": str(filial.id),
                "session_id": "lote-gigante",
                "events": [{"event_type": "cart_add"}] * 51,
            },
        )

        assert resposta.status_code == 422

    def test_filial_errada_responde_202_com_zero(self, cliente_http, db, loja):
        """Telemetria não derruba cardápio, e isso vale até a borda HTTP: o
        front que mandar um id errado recebe `202`, não `500` na tela de quem
        está escolhendo o jantar."""
        restaurante, _ = loja

        resposta = cliente_http.post(
            "/menu-events",
            json={
                "restaurant_id": str(restaurante.id),
                "branch_id": str(uuid.uuid4()),
                "session_id": "filial-errada",
                "events": [{"event_type": "menu_view"}],
            },
        )

        assert resposta.status_code == 202
        assert resposta.json() == {"recorded": 0}
