"""Horario de funcionamento da filial.

O bug corrigido aqui: quando a hora atual nao caia em nenhuma faixa do dia,
`_select_business_hour_for_prep_time` devolvia a PRIMEIRA faixa cadastrada.
As 3h da manha o pedido passava com o tempo de preparo do almoco, e a loja
fechada aceitava pedido.

A regra agora: ou existe faixa que contem o agora, ou esta fechado.
"""

import unittest
import uuid
from datetime import datetime, time
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


def build_service(periods_by_weekday):
    service = BranchHoursService.__new__(BranchHoursService)
    service.branch_repository = SimpleNamespace(
        list_business_hours_by_weekday=lambda branch_id, weekday: periods_by_weekday.get(weekday, [])
    )
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
        # Dia inteiro aberto para nao depender da hora em que o teste roda.
        service = build_service({weekday: [period(time(0, 0), time(23, 59), 15, 25)] for weekday in range(7)})

        found = service.ensure_branch_is_open(BRANCH_ID)

        self.assertEqual(found.prep_time_max, 25)


if __name__ == "__main__":
    unittest.main()
