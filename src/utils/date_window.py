"""Data do lojista -> instante UTC, num lugar so.

As datas que chegam pela querystring do painel estao no fuso da OPERACAO
(`America/Fortaleza`), e nunca em UTC: "os pedidos de ontem", para quem esta
no balcao, sao os do dia dele. Sem a conversao, tres horas de pedidos caem no
dia errado do recorte — e caem em silencio, com 200 e uma lista plausivel.

Existe como modulo porque a regra passou a ter DOIS donos: os relatorios de
desempenho (`AdminReportService`) e os filtros de data da listagem de
clientes. Duas conversoes escritas separadas discordariam no dia em que
alguem mexesse numa — e o sintoma seria um pedido aparecendo num relatorio e
nao no outro.
"""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from src.core.constants import PLATFORM_TIMEZONE


OPERATION_TIMEZONE = ZoneInfo(PLATFORM_TIMEZONE)


def day_start(day: date) -> datetime:
    """Meia-noite daquele dia no fuso da operacao. Inclusivo."""
    return datetime.combine(day, time.min, tzinfo=OPERATION_TIMEZONE)


def day_end_exclusive(day: date) -> datetime:
    """Meia-noite do dia SEGUINTE, no fuso da operacao. Exclusivo.

    Exclusivo e nao "23:59:59" de proposito: com o fim fechado, o pedido
    gravado as 23:59:59.7 fica de fora do dia em que ele aconteceu.
    """
    return day_start(day + timedelta(days=1))


def period_bounds(start_date: date, end_date: date) -> tuple[datetime, datetime]:
    """O par pronto, para quem recebe as duas datas juntas."""
    return day_start(start_date), day_end_exclusive(end_date)
