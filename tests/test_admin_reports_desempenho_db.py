"""As quatro consultas de Desempenho de 05/09/2026, contra o Postgres.

`tests/test_admin_reports.py` prova a montagem da resposta com um dublê de
repositório. **Este arquivo prova o SQL**, e ele é onde mora o risco de
verdade das quatro — nenhuma das cinco coisas abaixo tem como ser exercitada
sem banco, e todas erram em silêncio:

1. **a hora LOCAL.** `timezone('America/Fortaleza', created_at)` sobre um
   `timestamptz`. Errar devolve o pico da noite de madrugada, e o total
   continua certo;
2. **`weekday` 0 = segunda.** O `dow` do Postgres é 0 = DOMINGO e o `isodow`
   é 1 = segunda. É a armadilha 1 do lado do SQL: o número sai consistente e
   a tela mostra o dia errado;
3. **"primeiro pedido da vida"** — a subconsulta que separa novo de
   recorrente. Ela ignora o recorte de filial e o de data de propósito, e as
   duas metades só se veem com mais de um pedido no banco;
4. **`percentile_cont`** sobre uma duração calculada de `EXTRACT(EPOCH ...)`,
   com pedido que não passou por todos os estágios;
5. **o `LEFT JOIN` do histórico**, que é o que mantém no denominador o pedido
   sem carimbo nenhum.

O fuso da operação é UTC-3, e as fábricas gravam `created_at` em UTC: um
pedido às **01:00 UTC do dia 2** é **22:00 do dia 1** em Fortaleza. É esse par
que os testes de hora e de dia da semana usam, e ele é o caso que um
`EXTRACT` sem `timezone()` erra.
"""

import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from src.models.cashback_rule_model import CashbackRule
from src.models.cashback_transaction_model import CashbackTransaction
from src.models.order_status_history_model import OrderStatusHistory
from src.repositories.admin_report_repository import AdminReportRepository
from src.services.admin_report_service import AdminReportService
from tests.fabricas_db import criar_cliente, criar_filial, criar_pedido, criar_restaurante


pytestmark = pytest.mark.db


# Uma janela larga e fixa: nenhum teste daqui depende da data de execucao,
# porque todos escrevem `created_at` a mao.
INICIO = date(2026, 7, 1)
FIM = date(2026, 7, 31)


def utc(ano, mes, dia, hora=12, minuto=0):
    return datetime(ano, mes, dia, hora, minuto, tzinfo=timezone.utc)


def limites(inicio=INICIO, fim=FIM):
    """Os mesmos instantes que o service passaria ao repositorio."""
    return AdminReportService._period_bounds(inicio, fim)


def faturado(db, restaurante, filial, **campos):
    """Um pedido que CONTA como venda, pelo mesmo predicado do extrato."""
    campos.setdefault("status", "completed")
    campos.setdefault("payment_status", "paid")
    return criar_pedido(db, restaurante, filial, **campos)


def carimbar(db, pedido, status, quando):
    db.add(OrderStatusHistory(order_id=pedido.id, status=status, created_at=quando))
    db.flush()


@pytest.fixture
def loja(db):
    restaurante = criar_restaurante(db)
    filial = criar_filial(db, restaurante)
    return restaurante, filial


@pytest.fixture
def repositorio(db):
    return AdminReportRepository(db)


# ---------------------------------------------------------------------------
# 1. A hora local
# ---------------------------------------------------------------------------


class TestAHoraLocal:
    def test_pedido_das_22h_de_fortaleza_e_hora_22_e_nao_1(self, db, loja, repositorio):
        """O caso que um `EXTRACT(hour)` sem `timezone()` erra.

        01:00 UTC do dia 2 sao 22:00 do dia 1 em Fortaleza. Sem a conversao a
        linha cai na hora 1, e o lojista ve movimento de madrugada numa loja
        que fecha meia-noite."""
        restaurante, filial = loja
        faturado(db, restaurante, filial, created_at=utc(2026, 7, 2, 1, 0))

        linhas = repositorio.sales_by_hour(restaurante.id, *limites())

        assert linhas == [(22, 1, Decimal("50.00"))]

    def test_duas_horas_diferentes_saem_em_duas_linhas(self, db, loja, repositorio):
        restaurante, filial = loja
        faturado(db, restaurante, filial, created_at=utc(2026, 7, 10, 14, 0))
        faturado(db, restaurante, filial, created_at=utc(2026, 7, 11, 14, 30))
        faturado(db, restaurante, filial, created_at=utc(2026, 7, 12, 22, 0))

        linhas = repositorio.sales_by_hour(restaurante.id, *limites())

        # 14h UTC = 11h local; 22h UTC = 19h local.
        assert [(hora, pedidos) for hora, pedidos, _ in linhas] == [(11, 2), (19, 1)]

    def test_pedido_cancelado_nao_entra(self, db, loja, repositorio):
        restaurante, filial = loja
        criar_pedido(
            db, restaurante, filial, status="cancelled",
            payment_status="on_delivery", created_at=utc(2026, 7, 10, 14, 0),
        )

        assert repositorio.sales_by_hour(restaurante.id, *limites()) == []


class TestOWeekdayDoSQL:
    def test_weekday_e_0_para_SEGUNDA(self, db, loja, repositorio):
        """A armadilha 1 do lado do SQL.

        06/07/2026 e uma SEGUNDA. Com o `dow` do Postgres (0 = domingo) esta
        linha sairia como 1, o painel desenharia a coluna da terca, e nada
        falharia em lugar nenhum."""
        restaurante, filial = loja
        faturado(db, restaurante, filial, created_at=utc(2026, 7, 6, 15, 0))

        linhas = repositorio.sales_by_weekday_hour(restaurante.id, *limites())

        assert [(dia, hora) for dia, hora, _, _ in linhas] == [(0, 12)]

    def test_weekday_e_6_para_DOMINGO(self, db, loja, repositorio):
        restaurante, filial = loja
        # 05/07/2026 e domingo.
        faturado(db, restaurante, filial, created_at=utc(2026, 7, 5, 15, 0))

        linhas = repositorio.sales_by_weekday_hour(restaurante.id, *limites())

        assert linhas[0][0] == 6

    def test_o_dia_da_semana_tambem_e_o_LOCAL(self, db, loja, repositorio):
        """01:00 UTC de segunda ainda e DOMINGO em Fortaleza.

        E o mesmo par do teste da hora, e aqui ele muda o DIA — o erro mais
        caro dos dois, porque desloca a coluna inteira do mapa."""
        restaurante, filial = loja
        faturado(db, restaurante, filial, created_at=utc(2026, 7, 6, 1, 0))

        linhas = repositorio.sales_by_weekday_hour(restaurante.id, *limites())

        assert [(dia, hora) for dia, hora, _, _ in linhas] == [(6, 22)]


# ---------------------------------------------------------------------------
# 2. Bairro
# ---------------------------------------------------------------------------


class TestOBairro:
    def test_agrupa_por_bairro_e_soma_o_faturamento(self, db, loja, repositorio):
        restaurante, filial = loja
        for total in ("30.00", "70.00"):
            pedido = faturado(db, restaurante, filial, total=Decimal(total))
            pedido.address_neighborhood = "Aldeota"
            pedido.address_city = "Fortaleza"
        db.flush()

        linhas = repositorio.sales_by_neighborhood(restaurante.id, *limites_de_hoje())

        assert linhas == [("Aldeota", "Fortaleza", 2, Decimal("100.00"))]

    def test_a_retirada_fica_de_fora(self, db, loja, repositorio):
        """Retirada nao tem bairro, e um balde "sem bairro" faria a maior
        regiao da tela ser o balcao."""
        restaurante, filial = loja
        pedido = faturado(db, restaurante, filial, order_type="pickup")
        pedido.address_neighborhood = None
        db.flush()

        assert repositorio.sales_by_neighborhood(restaurante.id, *limites_de_hoje()) == []
        assert repositorio.count_non_delivery_orders(restaurante.id, *limites_de_hoje()) == 1

    def test_mesmo_bairro_em_cidades_diferentes_sao_duas_linhas(self, db, loja, repositorio):
        """"Centro" de Fortaleza e "Centro" de Maracanau sao dois lugares, e
        soma-los faria a tela propor estender a area para o bairro errado."""
        restaurante, filial = loja
        for cidade in ("Fortaleza", "Maracanau"):
            pedido = faturado(db, restaurante, filial)
            pedido.address_neighborhood = "Centro"
            pedido.address_city = cidade
        db.flush()

        linhas = repositorio.sales_by_neighborhood(restaurante.id, *limites_de_hoje())

        assert len(linhas) == 2
        assert {linha[1] for linha in linhas} == {"Fortaleza", "Maracanau"}

    def test_entrega_sem_bairro_registrado_vira_linha_nula(self, db, loja, repositorio):
        restaurante, filial = loja
        pedido = faturado(db, restaurante, filial, order_type="delivery")
        pedido.address_neighborhood = None
        pedido.address_city = None
        db.flush()

        linhas = repositorio.sales_by_neighborhood(restaurante.id, *limites_de_hoje())

        assert linhas == [(None, None, 1, Decimal("50.00"))]


def limites_de_hoje():
    """Uma janela que contem `now()`, para os pedidos sem `created_at` a mao."""
    hoje = date.today()
    return AdminReportService._period_bounds(hoje - timedelta(days=1), hoje + timedelta(days=1))


# ---------------------------------------------------------------------------
# 3. Novo x recorrente
# ---------------------------------------------------------------------------


class TestNovoERecorrente:
    def test_quem_estreou_no_periodo_e_novo(self, db, loja, repositorio):
        restaurante, filial = loja
        faturado(
            db, restaurante, filial,
            customer_phone_snapshot="85911111111", created_at=utc(2026, 7, 10),
        )

        totais = repositorio.customers_by_recency(restaurante.id, *limites())

        assert totais["new_customers_count"] == 1
        assert totais["returning_customers_count"] == 0
        assert totais["customers_count"] == 1

    def test_quem_ja_pedia_ANTES_do_periodo_e_recorrente(self, db, loja, repositorio):
        """A subconsulta varre a vida inteira do cliente, e nao o recorte.

        Limitada ao periodo, TODO cliente pareceria novo — que e o mesmo que
        nao ter a pergunta."""
        restaurante, filial = loja
        faturado(
            db, restaurante, filial,
            customer_phone_snapshot="85922222222", created_at=utc(2026, 5, 10),
        )
        faturado(
            db, restaurante, filial,
            customer_phone_snapshot="85922222222", created_at=utc(2026, 7, 10),
        )

        totais = repositorio.customers_by_recency(restaurante.id, *limites())

        assert totais["new_customers_count"] == 0
        assert totais["returning_customers_count"] == 1

    def test_a_receita_sai_separada_por_lado(self, db, loja, repositorio):
        restaurante, filial = loja
        faturado(
            db, restaurante, filial, total=Decimal("100.00"),
            customer_phone_snapshot="85911111111", created_at=utc(2026, 7, 10),
        )
        faturado(
            db, restaurante, filial, total=Decimal("40.00"),
            customer_phone_snapshot="85922222222", created_at=utc(2026, 5, 10),
        )
        faturado(
            db, restaurante, filial, total=Decimal("60.00"),
            customer_phone_snapshot="85922222222", created_at=utc(2026, 7, 12),
        )

        totais = repositorio.customers_by_recency(restaurante.id, *limites())

        assert totais["new_revenue_total"] == Decimal("100.00")
        assert totais["returning_revenue_total"] == Decimal("60.00")

    def test_dois_pedidos_do_mesmo_telefone_contam_UM_cliente(self, db, loja, repositorio):
        restaurante, filial = loja
        for dia in (10, 12, 14):
            faturado(
                db, restaurante, filial,
                customer_phone_snapshot="85911111111", created_at=utc(2026, 7, dia),
            )

        totais = repositorio.customers_by_recency(restaurante.id, *limites())

        assert totais["customers_count"] == 1

    def test_com_branch_id_a_estreia_continua_sendo_a_do_RESTAURANTE(
        self, db, loja, repositorio
    ):
        """A decisao que a descricao da rota promete, provada.

        O cliente estreou no Centro em maio e pediu na Aldeota em julho. Com
        a estreia lida POR FILIAL ele nasceria "novo" na Aldeota, e a soma
        das duas lojas teria mais clientes novos que o restaurante inteiro."""
        restaurante, centro = loja
        aldeota = criar_filial(db, restaurante, nome="Aldeota")
        faturado(
            db, restaurante, centro,
            customer_phone_snapshot="85933333333", created_at=utc(2026, 5, 10),
        )
        faturado(
            db, restaurante, aldeota,
            customer_phone_snapshot="85933333333", created_at=utc(2026, 7, 10),
        )

        totais = repositorio.customers_by_recency(
            restaurante.id, *limites(), branch_id=aldeota.id
        )

        assert totais["new_customers_count"] == 0
        assert totais["returning_customers_count"] == 1

    def test_o_telefone_nao_atravessa_restaurante(self, db, repositorio):
        """Quem pede em duas marcas e novo nas duas: o agregado do "primeiro
        pedido" e feito DENTRO de `restaurant_id`."""
        primeiro = criar_restaurante(db, nome="Primeiro")
        filial_a = criar_filial(db, primeiro)
        segundo = criar_restaurante(db, nome="Segundo")
        filial_b = criar_filial(db, segundo)

        faturado(
            db, primeiro, filial_a,
            customer_phone_snapshot="85944444444", created_at=utc(2026, 5, 10),
        )
        faturado(
            db, segundo, filial_b,
            customer_phone_snapshot="85944444444", created_at=utc(2026, 7, 10),
        )

        totais = repositorio.customers_by_recency(segundo.id, *limites())

        assert totais["new_customers_count"] == 1

    def test_pedido_cancelado_nao_conta_como_estreia(self, db, loja, repositorio):
        """A estreia usa o mesmo predicado de "virou venda". Um pedido
        cancelado em maio nao tira do cliente a condicao de novo em julho."""
        restaurante, filial = loja
        criar_pedido(
            db, restaurante, filial, status="cancelled", payment_status="on_delivery",
            customer_phone_snapshot="85955555555", created_at=utc(2026, 5, 10),
        )
        faturado(
            db, restaurante, filial,
            customer_phone_snapshot="85955555555", created_at=utc(2026, 7, 10),
        )

        totais = repositorio.customers_by_recency(restaurante.id, *limites())

        assert totais["new_customers_count"] == 1


# ---------------------------------------------------------------------------
# 4. O cashback do periodo
# ---------------------------------------------------------------------------


class TestOCashbackDoPeriodo:
    def test_o_resgatado_sai_dos_pedidos_faturados(self, db, loja, repositorio):
        restaurante, filial = loja
        pedido = faturado(db, restaurante, filial, created_at=utc(2026, 7, 10))
        pedido.cashback_redeemed_amount = Decimal("12.00")
        faturado(db, restaurante, filial, created_at=utc(2026, 7, 11))
        db.flush()

        totais = repositorio.cashback_redeemed_totals(restaurante.id, *limites())

        assert totais["redeemed_total"] == Decimal("12.00")
        assert totais["orders_with_redeem_count"] == 1

    def test_o_gerado_soma_as_linhas_earned_de_QUALQUER_status(self, db, loja, repositorio):
        """Filtrar por `available` faria o numero de um mes encolher sozinho
        conforme os clientes gastassem o saldo."""
        restaurante, filial = loja
        cliente = criar_cliente(db)
        for status, valor in (("available", "10.00"), ("used", "5.00")):
            db.add(
                CashbackTransaction(
                    customer_id=cliente.id,
                    restaurant_id=restaurante.id,
                    type="earned",
                    amount=Decimal(valor),
                    status=status,
                    created_at=utc(2026, 7, 10),
                )
            )
        db.flush()

        assert repositorio.cashback_earned_total(restaurante.id, *limites()) == Decimal("15.00")

    def test_o_resgate_negativo_do_razao_NAO_entra_no_gerado(self, db, loja, repositorio):
        restaurante, filial = loja
        cliente = criar_cliente(db)
        db.add(
            CashbackTransaction(
                customer_id=cliente.id, restaurant_id=restaurante.id,
                type="redeemed", amount=Decimal("-8.00"), status="used",
                created_at=utc(2026, 7, 10),
            )
        )
        db.flush()

        assert repositorio.cashback_earned_total(restaurante.id, *limites()) == 0

    def test_com_branch_id_o_credito_e_atribuido_pelo_PEDIDO(self, db, loja, repositorio):
        """`cashback_transactions` nao tem filial: o vinculo e o pedido que
        gerou o credito."""
        restaurante, centro = loja
        aldeota = criar_filial(db, restaurante, nome="Aldeota")
        cliente = criar_cliente(db)
        do_centro = faturado(db, restaurante, centro, created_at=utc(2026, 7, 10))
        da_aldeota = faturado(db, restaurante, aldeota, created_at=utc(2026, 7, 10))
        for pedido, valor in ((do_centro, "10.00"), (da_aldeota, "3.00")):
            db.add(
                CashbackTransaction(
                    customer_id=cliente.id, restaurant_id=restaurante.id,
                    order_id=pedido.id, type="earned", amount=Decimal(valor),
                    status="available", created_at=utc(2026, 7, 10),
                )
            )
        db.flush()

        total = repositorio.cashback_earned_total(
            restaurante.id, *limites(), branch_id=centro.id
        )

        assert total == Decimal("10.00")

    def test_credito_SEM_pedido_some_no_recorte_de_filial(self, db, loja, repositorio):
        """A consequencia declarada na descricao da rota: ajuste manual nao
        tem como ser atribuido a loja nenhuma. Sem recorte ele entra."""
        restaurante, filial = loja
        cliente = criar_cliente(db)
        db.add(
            CashbackTransaction(
                customer_id=cliente.id, restaurant_id=restaurante.id, order_id=None,
                type="earned", amount=Decimal("20.00"), status="available",
                created_at=utc(2026, 7, 10),
            )
        )
        db.flush()

        assert repositorio.cashback_earned_total(restaurante.id, *limites()) == Decimal("20.00")
        assert repositorio.cashback_earned_total(
            restaurante.id, *limites(), branch_id=filial.id
        ) == 0


class TestConfiguredContraOBanco:
    def test_regra_ligada_do_restaurante_deixa_configured_verdadeiro(self, db, loja):
        restaurante, _ = loja
        db.add(
            CashbackRule(
                restaurant_id=restaurante.id, branch_id=None, enabled=True,
                default_percent=Decimal("5.00"), min_redeem_balance=Decimal("0"),
                expiry_days=60,
            )
        )
        db.flush()

        relatorio = AdminReportService(db).customers_report(restaurante.id, INICIO, FIM)

        assert relatorio.cashback.configured is True

    def test_sem_regra_nenhuma_configured_e_falso(self, db, loja):
        restaurante, _ = loja

        relatorio = AdminReportService(db).customers_report(restaurante.id, INICIO, FIM)

        assert relatorio.cashback.configured is False


# ---------------------------------------------------------------------------
# 5. Os tempos entre os carimbos
# ---------------------------------------------------------------------------


class TestOsTemposDaOperacao:
    def test_aceite_e_preparo_saem_em_minutos(self, db, loja, repositorio):
        restaurante, filial = loja
        pedido = faturado(db, restaurante, filial, created_at=utc(2026, 7, 10, 12, 0))
        carimbar(db, pedido, "accepted", utc(2026, 7, 10, 12, 3))
        carimbar(db, pedido, "ready", utc(2026, 7, 10, 12, 23))

        medidas = repositorio.operation_durations(restaurante.id, *limites())

        assert medidas["orders_count"] == 1
        assert medidas["accept"]["median"] == Decimal("3")
        assert medidas["accept"]["orders_count"] == 1
        assert medidas["prep"]["median"] == Decimal("20")

    def test_o_primeiro_carimbo_e_que_vale(self, db, loja, repositorio):
        """Um pedido que volta a `preparing` depois de `ready` nao reabre o
        relogio: a promessa foi cumprida na primeira vez que a loja disse
        "pronto"."""
        restaurante, filial = loja
        pedido = faturado(db, restaurante, filial, created_at=utc(2026, 7, 10, 12, 0))
        carimbar(db, pedido, "accepted", utc(2026, 7, 10, 12, 2))
        carimbar(db, pedido, "ready", utc(2026, 7, 10, 12, 12))
        carimbar(db, pedido, "ready", utc(2026, 7, 10, 13, 30))

        medidas = repositorio.operation_durations(restaurante.id, *limites())

        assert medidas["prep"]["median"] == Decimal("10")

    def test_pedido_sem_carimbo_nenhum_fica_no_denominador(self, db, loja, repositorio):
        """`LEFT JOIN` e nao `JOIN`. Com o `JOIN` ele sumiria e a tela diria
        que o periodo teve menos pedidos do que teve."""
        restaurante, filial = loja
        faturado(db, restaurante, filial, created_at=utc(2026, 7, 10))

        medidas = repositorio.operation_durations(restaurante.id, *limites())

        assert medidas["orders_count"] == 1
        assert medidas["accept"]["orders_count"] == 0
        assert medidas["accept"]["median"] is None

    def test_a_entrega_so_conta_em_pedido_de_ENTREGA(self, db, loja, repositorio):
        restaurante, filial = loja
        retirada = faturado(
            db, restaurante, filial, order_type="pickup", created_at=utc(2026, 7, 10, 12, 0)
        )
        carimbar(db, retirada, "out_for_delivery", utc(2026, 7, 10, 12, 30))
        carimbar(db, retirada, "completed", utc(2026, 7, 10, 12, 50))

        medidas = repositorio.operation_durations(restaurante.id, *limites())

        assert medidas["delivery"]["orders_count"] == 0
        assert medidas["delivery"]["median"] is None

    def test_a_mediana_nao_e_puxada_pelo_pedido_esquecido(self, db, loja, repositorio):
        """O motivo de mediana e p90 virem antes da media na tela."""
        restaurante, filial = loja
        for minutos in (10, 10, 10, 180):
            pedido = faturado(db, restaurante, filial, created_at=utc(2026, 7, 10, 12, 0))
            carimbar(db, pedido, "accepted", utc(2026, 7, 10, 12, 0))
            carimbar(
                db, pedido, "ready",
                utc(2026, 7, 10, 12, 0) + timedelta(minutes=minutos),
            )

        medidas = repositorio.operation_durations(restaurante.id, *limites())

        assert medidas["prep"]["median"] == Decimal("10")
        assert medidas["prep"]["average"] > Decimal("50")

    def test_atrasado_e_contra_o_prazo_DO_PEDIDO(self, db, loja, repositorio):
        """Congelado na criacao — o cliente leu aquele prazo na vitrine."""
        restaurante, filial = loja
        pedido = faturado(db, restaurante, filial, created_at=utc(2026, 7, 10, 12, 0))
        pedido.delivery_prep_time_max = 15
        db.flush()
        carimbar(db, pedido, "accepted", utc(2026, 7, 10, 12, 0))
        carimbar(db, pedido, "ready", utc(2026, 7, 10, 12, 40))

        medidas = repositorio.operation_durations(restaurante.id, *limites())

        assert medidas["late_orders_count"] == 1
        assert medidas["late_orders_base_count"] == 1

    def test_dentro_do_prazo_nao_conta_como_atraso(self, db, loja, repositorio):
        restaurante, filial = loja
        pedido = faturado(db, restaurante, filial, created_at=utc(2026, 7, 10, 12, 0))
        pedido.delivery_prep_time_max = 40
        db.flush()
        carimbar(db, pedido, "accepted", utc(2026, 7, 10, 12, 0))
        carimbar(db, pedido, "ready", utc(2026, 7, 10, 12, 15))

        medidas = repositorio.operation_durations(restaurante.id, *limites())

        assert medidas["late_orders_count"] == 0
        assert medidas["late_orders_base_count"] == 1

    def test_pedido_SEM_prazo_prometido_fica_fora_do_denominador(
        self, db, loja, repositorio
    ):
        """Nao pode ser julgado atrasado, e conta-lo embaixo faria a tela
        subestimar o atraso."""
        restaurante, filial = loja
        pedido = faturado(db, restaurante, filial, created_at=utc(2026, 7, 10, 12, 0))
        pedido.delivery_prep_time_max = None
        db.flush()
        carimbar(db, pedido, "accepted", utc(2026, 7, 10, 12, 0))
        carimbar(db, pedido, "ready", utc(2026, 7, 10, 15, 0))

        medidas = repositorio.operation_durations(restaurante.id, *limites())

        assert medidas["prep"]["orders_count"] == 1
        assert medidas["late_orders_base_count"] == 0
        assert medidas["late_orders_count"] == 0


# ---------------------------------------------------------------------------
# O recorte de filial, nas quatro
# ---------------------------------------------------------------------------


class TestOIsolamentoDasQuatro:
    """Cada consulta so enxerga o restaurante pedido e, com recorte, so a
    filial pedida. E a leitura que custa mais caro errar no sistema inteiro."""

    def test_nenhuma_enxerga_o_restaurante_vizinho(self, db, repositorio):
        meu = criar_restaurante(db, nome="Meu")
        minha_filial = criar_filial(db, meu)
        alheio = criar_restaurante(db, nome="Alheio")
        filial_alheia = criar_filial(db, alheio)

        pedido = faturado(db, alheio, filial_alheia, created_at=utc(2026, 7, 10, 15, 0))
        pedido.address_neighborhood = "Aldeota"
        db.flush()
        carimbar(db, pedido, "accepted", utc(2026, 7, 10, 15, 5))

        assert repositorio.sales_by_hour(meu.id, *limites()) == []
        assert repositorio.sales_by_weekday_hour(meu.id, *limites()) == []
        assert repositorio.sales_by_neighborhood(meu.id, *limites()) == []
        assert repositorio.customers_by_recency(meu.id, *limites())["customers_count"] == 0
        assert repositorio.operation_durations(meu.id, *limites())["orders_count"] == 0
        assert minha_filial.id is not None

    def test_o_recorte_de_filial_restringe_as_quatro(self, db, loja, repositorio):
        restaurante, centro = loja
        aldeota = criar_filial(db, restaurante, nome="Aldeota")
        pedido = faturado(db, restaurante, aldeota, created_at=utc(2026, 7, 10, 15, 0))
        pedido.address_neighborhood = "Aldeota"
        db.flush()
        carimbar(db, pedido, "accepted", utc(2026, 7, 10, 15, 5))

        do_centro = {"branch_id": centro.id}
        assert repositorio.sales_by_hour(restaurante.id, *limites(), **do_centro) == []
        assert repositorio.sales_by_weekday_hour(restaurante.id, *limites(), **do_centro) == []
        assert repositorio.sales_by_neighborhood(restaurante.id, *limites(), **do_centro) == []
        assert (
            repositorio.customers_by_recency(restaurante.id, *limites(), **do_centro)[
                "customers_count"
            ]
            == 0
        )
        assert (
            repositorio.operation_durations(restaurante.id, *limites(), **do_centro)[
                "orders_count"
            ]
            == 0
        )

    def test_as_quatro_nao_escrevem_nada(self, db, loja, repositorio):
        """Relatorio le. Se alguem acrescentar uma escrita, esta linha fica
        vermelha antes de a rota rodar em producao."""
        restaurante, _ = loja
        db.flush()

        repositorio.sales_by_hour(restaurante.id, *limites())
        repositorio.sales_by_weekday_hour(restaurante.id, *limites())
        repositorio.sales_by_neighborhood(restaurante.id, *limites())
        repositorio.customers_by_recency(restaurante.id, *limites())
        repositorio.cashback_redeemed_totals(restaurante.id, *limites())
        repositorio.cashback_earned_total(restaurante.id, *limites())
        repositorio.operation_durations(restaurante.id, *limites())

        assert not db.new and not db.dirty and not db.deleted


class TestAsRespostasCompletas:
    """As quatro montadas de ponta a ponta, contra o banco.

    Os testes de `test_admin_reports.py` provam a montagem com dublê; estes
    provam que o dublê descreve o que o SQL de fato devolve — a costura entre
    as duas camadas, que e onde um nome de chave trocado passaria despercebido
    nos dois lados.
    """

    def test_sales_by_hour_de_ponta_a_ponta(self, db, loja):
        restaurante, filial = loja
        faturado(db, restaurante, filial, created_at=utc(2026, 7, 10, 22, 0))

        relatorio = AdminReportService(db).sales_by_hour(restaurante.id, INICIO, FIM)

        assert len(relatorio.hours) == 24
        assert relatorio.hours[19].orders_count == 1
        assert relatorio.revenue_total == Decimal("50.00")

    def test_neighborhoods_de_ponta_a_ponta(self, db, loja):
        restaurante, filial = loja
        pedido = faturado(db, restaurante, filial, created_at=utc(2026, 7, 10))
        pedido.address_neighborhood = "Aldeota"
        pedido.address_city = "Fortaleza"
        db.flush()

        relatorio = AdminReportService(db).neighborhoods_report(restaurante.id, INICIO, FIM)

        assert relatorio.neighborhoods[0].neighborhood == "Aldeota"
        assert relatorio.neighborhoods[0].average_ticket == Decimal("50.00")
        assert relatorio.neighborhoods[0].revenue_share_percent == Decimal("100.00")

    def test_customers_de_ponta_a_ponta(self, db, loja):
        restaurante, filial = loja
        faturado(
            db, restaurante, filial,
            customer_phone_snapshot="85911111111", created_at=utc(2026, 7, 10),
        )

        relatorio = AdminReportService(db).customers_report(restaurante.id, INICIO, FIM)

        assert relatorio.customers_count.current == Decimal("1")
        assert relatorio.new_customers_count.current == Decimal("1")
        assert relatorio.cashback.earned_total.current == Decimal("0.00")

    def test_operations_de_ponta_a_ponta(self, db, loja):
        restaurante, filial = loja
        pedido = faturado(db, restaurante, filial, created_at=utc(2026, 7, 10, 12, 0))
        carimbar(db, pedido, "accepted", utc(2026, 7, 10, 12, 5))

        relatorio = AdminReportService(db).operations_report(restaurante.id, INICIO, FIM)

        assert relatorio.orders_count == 1
        assert relatorio.accept_minutes.median == Decimal("5.0")
        assert relatorio.prep_minutes.median is None
        assert relatorio.late_orders_percent is None
        assert uuid.UUID(str(relatorio.restaurant_id)) == restaurante.id
