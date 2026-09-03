"""Horario de funcionamento da filial.

Existia so dentro da estimativa de entrega, e com um bug: quando a hora
atual nao caia em nenhuma faixa do dia, o codigo pegava a PRIMEIRA faixa
cadastrada. As 3h da manha, um pedido passava com o tempo de preparo do
almoco — e passava mesmo com a filial fechada.

Aqui a regra e a obvia: ou existe uma faixa que contem o agora, ou a filial
esta fechada. Nao ha faixa "mais parecida".

O caso que obriga a olhar dois dias: uma faixa 18:00–02:00 pertence ao
weekday em que ELA COMECA. A 1h da manha de terca, quem esta aberto e a
faixa da segunda. Por isso `find_current_period` consulta hoje e ontem.
"""

import uuid
from datetime import datetime, time as datetime_time
from zoneinfo import ZoneInfo

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from src.core.constants import PLATFORM_TIMEZONE
from src.models.branch_business_hour_model import BranchBusinessHour
from src.repositories.branch_repository import BranchRepository


BRANCH_TIMEZONE = ZoneInfo(PLATFORM_TIMEZONE)


class BranchHoursService:
    def __init__(self, db: Session):
        self.branch_repository = BranchRepository(db)
        # O RELOGIO, INJETAVEL — mesmo desenho de `CouponService.clock`.
        #
        # `find_current_period` sempre aceitou um `now`, mas
        # `ensure_branch_is_open` nao tinha como passar um: ele lia o relogio
        # da maquina. O sintoma nao e teorico. Um teste que declara a filial
        # "aberta o dia inteiro" como 00:00-23:59 e `_period_covers_same_day`
        # compara `current_time <= closes_at`: entre 23:59:01 e 23:59:59 a
        # faixa nao cobre nada e a filial fica FECHADA. Um minuto por dia, num
        # teste cujo comentario dizia, com todas as letras, existir "para nao
        # depender da hora em que o teste roda".
        self.clock = lambda: datetime.now(BRANCH_TIMEZONE)

    def find_current_period(
        self,
        branch_id: uuid.UUID,
        now: datetime | None = None,
    ) -> BranchBusinessHour | None:
        """A faixa de funcionamento que contem o momento atual, ou None."""
        moment = now or self.clock()
        current_time = moment.time()

        for period in self._open_periods(branch_id, moment.weekday()):
            if _period_covers_same_day(period, current_time):
                return period

        yesterday_weekday = (moment.weekday() - 1) % 7
        for period in self._open_periods(branch_id, yesterday_weekday):
            if _period_covers_after_midnight(period, current_time):
                return period

        return None

    def ensure_branch_is_open(
        self,
        branch_id: uuid.UUID,
        now: datetime | None = None,
    ) -> BranchBusinessHour:
        """Bloqueia pedido fora do horario da filial.

        Vale para retirada tambem, e ai esta metade do motivo desta funcao
        existir: pedido de retirada nao passa pela estimativa de entrega,
        entao nunca chegava perto de uma checagem de horario.

        `now` existe para quem JA tem um instante nas maos passa-lo adiante,
        em vez de ler o relogio uma segunda vez: duas leituras na mesma
        requisicao podem cair em lados diferentes da virada de faixa, e ai a
        resposta conta uma historia que nunca foi verdade num unico momento.
        """
        period = self.find_current_period(branch_id, now)
        if period is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="A loja está fechada neste horário",
            )
        return period

    def _open_periods(
        self,
        branch_id: uuid.UUID,
        weekday: int,
    ) -> list[BranchBusinessHour]:
        periods = self.branch_repository.list_business_hours_by_weekday(branch_id, weekday)
        # `is_closed` era ignorado: uma linha marcada como fechada mas com
        # horarios preenchidos (o jeito comum de registrar "domingo fechado")
        # abria a filial.
        return [
            period
            for period in periods
            if not period.is_closed
            and period.opens_at is not None
            and period.closes_at is not None
        ]


def _period_covers_same_day(period: BranchBusinessHour, current_time: datetime_time) -> bool:
    if period.opens_at <= period.closes_at:
        return period.opens_at <= current_time <= period.closes_at
    # Faixa que vira a noite (18:00–02:00): hoje ela cobre do abre ate a
    # meia-noite. O outro lado e tratado em _period_covers_after_midnight.
    return current_time >= period.opens_at


def _period_covers_after_midnight(period: BranchBusinessHour, current_time: datetime_time) -> bool:
    if period.opens_at <= period.closes_at:
        return False
    return current_time <= period.closes_at
