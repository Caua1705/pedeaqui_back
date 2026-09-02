"""Horario de funcionamento da filial.

O bug corrigido aqui: quando a hora atual nao caia em nenhuma faixa do dia,
`_select_business_hour_for_prep_time` devolvia a PRIMEIRA faixa cadastrada.
As 3h da manha o pedido passava com o tempo de preparo do almoco, e a loja
fechada aceitava pedido.

A regra agora: ou existe faixa que contem o agora, ou esta fechado.
"""

import unittest
import uuid
from datetime import datetime, time, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from fastapi import HTTPException

from src.services.branch_hours_service import BranchHoursService
from tests import fabricas


TIMEZONE = ZoneInfo("America/Fortaleza")
BRANCH_ID = uuid.uuid4()

# 2026-07-27 e uma segunda-feira; 2026-07-28, terca.
MONDAY = 0
TUESDAY = 1


def period(opens_at, closes_at, prep_time_min=20, prep_time_max=30, is_closed=False):
    return fabricas.horario(
        opens_at=opens_at,
        closes_at=closes_at,
        prep_time_min=prep_time_min,
        prep_time_max=prep_time_max,
        is_closed=is_closed,
    )


def build_service(periods_by_weekday, agora=None):
    service = BranchHoursService.__new__(BranchHoursService)
    service.branch_repository = SimpleNamespace(
        list_business_hours_by_weekday=lambda branch_id, weekday: periods_by_weekday.get(weekday, [])
    )
    # O relogio injetado. Sem ele, `ensure_branch_is_open` le o da maquina —
    # ver `test_open_branch_returns_the_current_period`.
    service.clock = lambda: agora or moment(27, 12)
    return service


def moment(day, hour, minute=0):
    return datetime(2026, 7, day, hour, minute, tzinfo=TIMEZONE)


class CurrentPeriodTests(unittest.TestCase):
    def test_lunch_period_is_found_at_lunch_time(self):
        service = build_service({MONDAY: [period(time(11, 0), time(15, 0), 20, 30)]})

        found = service.find_current_period(BRANCH_ID, moment(27, 12))

        self.assertEqual(found.prep_time_min, 20)

    def test_three_in_the_morning_is_closed_and_does_not_fall_back_to_lunch(self):
        service = build_service({
            MONDAY: [
                period(time(11, 0), time(15, 0), 20, 30),
                period(time(18, 0), time(23, 0), 40, 60),
            ],
        })

        self.assertIsNone(service.find_current_period(BRANCH_ID, moment(27, 3)))

    def test_the_period_that_contains_now_wins_over_the_first_of_the_day(self):
        service = build_service({
            MONDAY: [
                period(time(11, 0), time(15, 0), 20, 30),
                period(time(18, 0), time(23, 0), 40, 60),
            ],
        })

        found = service.find_current_period(BRANCH_ID, moment(27, 19))

        self.assertEqual(found.prep_time_min, 40)

    def test_period_marked_as_closed_does_not_open_the_branch(self):
        # "Domingo fechado" costuma ser gravado com os horarios preenchidos e
        # is_closed=true. A flag era ignorada.
        service = build_service({MONDAY: [period(time(0, 0), time(23, 59), is_closed=True)]})

        self.assertIsNone(service.find_current_period(BRANCH_ID, moment(27, 12)))

    def test_period_without_hours_is_ignored(self):
        service = build_service({MONDAY: [period(None, None)]})

        self.assertIsNone(service.find_current_period(BRANCH_ID, moment(27, 12)))


class OvernightPeriodTests(unittest.TestCase):
    """Faixa 18:00–02:00 pertence ao dia em que ela COMECA."""

    def test_before_midnight_the_period_of_today_is_used(self):
        service = build_service({MONDAY: [period(time(18, 0), time(2, 0), 40, 60)]})

        found = service.find_current_period(BRANCH_ID, moment(27, 23))

        self.assertEqual(found.prep_time_min, 40)

    def test_after_midnight_the_period_of_yesterday_is_used(self):
        # 1h da manha de terca: quem esta aberto e a faixa da segunda. Sem
        # olhar o dia anterior, a lanchonete noturna recusaria o pedido.
        service = build_service({MONDAY: [period(time(18, 0), time(2, 0), 40, 60)]})

        found = service.find_current_period(BRANCH_ID, moment(28, 1))

        self.assertEqual(found.prep_time_min, 40)

    def test_after_the_overnight_period_closes_it_is_closed_again(self):
        service = build_service({MONDAY: [period(time(18, 0), time(2, 0), 40, 60)]})

        self.assertIsNone(service.find_current_period(BRANCH_ID, moment(28, 3)))

    def test_yesterdays_daytime_period_never_leaks_into_today(self):
        service = build_service({MONDAY: [period(time(11, 0), time(15, 0))]})

        self.assertIsNone(service.find_current_period(BRANCH_ID, moment(28, 12)))


class EnsureOpenTests(unittest.TestCase):
    def test_closed_branch_refuses_the_order(self):
        service = build_service({})

        with self.assertRaises(HTTPException) as raised:
            service.ensure_branch_is_open(BRANCH_ID)

        self.assertEqual(raised.exception.status_code, 400)

    def test_open_branch_returns_the_current_period(self):
        """O QUE ESTE TESTE JA ESCONDEU, e por que o relogio agora e injetado.

        Ele dizia "dia inteiro aberto para nao depender da hora em que o teste
        roda", e a faixa 00:00-23:59 NAO e o dia inteiro:
        `_period_covers_same_day` compara `current_time <= closes_at`, e
        `23:59:30 > 23:59:00`. Entre 23:59:01 e 23:59:59 a filial ficava
        fechada e este teste falhava — 59 segundos por dia, com um vermelho
        que nao aponta para a hora em lugar nenhum.

        Achado rodando a suite inteira com o relogio congelado em instantes
        adversariais: foi o UNICO teste que falhou as 23:59:30 e passou as
        12:00.

        O conserto e o instante injetado — meio-dia, longe de qualquer borda.
        Esticar a faixa para `23:59:59.999999` trocaria uma borda por outra
        menor e deixaria o teste dependendo do relogio para nada. O buraco em
        si nao e defeito e nao foi mexido: ele e o comportamento correto de
        `_period_covers_same_day`, e esta afirmado no teste seguinte.
        """
        service = build_service(
            {weekday: [period(time(0, 0), time(23, 59), 15, 25)] for weekday in range(7)}
        )

        found = service.ensure_branch_is_open(BRANCH_ID)

        self.assertEqual(found.prep_time_max, 25)

    def test_a_faixa_ate_23_59_nao_cobre_23_59_30(self):
        """O buraco, afirmado — para ninguem "consertar" o teste de cima errado.

        `closes_at` e um `time` com resolucao de minuto no cadastro, e a
        comparacao e inclusiva nos dois lados. Uma loja que fecha 23:59 esta
        fechada as 23:59:30, e isso e o certo: o lojista disse ate 23:59.

        NAO ha, hoje, jeito limpo de cadastrar "24 horas". 00:00-00:00 nao
        serve: `opens_at <= closes_at` e verdadeiro, entao cai no ramo do
        mesmo dia e cobre exatamente a meia-noite — conferido, e o contrario
        do que a intuicao diz. O que cobriria e `closes_at` com segundos
        (`23:59:59`), e nem o painel nem `BusinessHourInput` pedem segundos.

        Nada disso foi mexido: e um buraco de UM MINUTO por dia numa loja que
        se declara aberta ate 23:59, e o custo de mexer no formato do cadastro
        e maior que o do buraco. O que este teste garante e que ele nao volte
        a ser DESCOBERTO por acidente, num vermelho de madrugada.
        """
        service = build_service(
            {weekday: [period(time(0, 0), time(23, 59), 15, 25)] for weekday in range(7)},
            agora=moment(27, 23, 59) + timedelta(seconds=30),
        )

        with self.assertRaises(HTTPException):
            service.ensure_branch_is_open(BRANCH_ID)


if __name__ == "__main__":
    unittest.main()
