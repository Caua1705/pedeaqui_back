"""Quanto o atendimento por voz consumiu, por restaurante e por periodo.

Responde tres perguntas e nada mais: quantas sessoes, quantos tokens e qual a
duracao media. Nao tem tela, nao tem rota — e um comando que se roda quando a
fatura da OpenAI chega e alguem precisa saber de quem foi o gasto.

Os numeros saem de `ai_voice_sessions`, e quem os coloca la e o NAVEGADOR, no
`POST /voice/session/{id}/ended`. Duas consequencias que valem ler antes de
usar o resultado para cobrar alguem:

- **sessao que nao reportou aparece com zero token.** A contagem de sessoes e
  do servidor e esta sempre certa; os tokens dependem do cliente ter avisado.
  Por isso o relatorio mostra quantas sessoes tinham numero — e a diferenca
  entre as duas contagens e a margem de erro do periodo.
- **o dono da verdade e a fatura da OpenAI.** Isto aqui e o rateio dela.

Uso:

    python scripts/voice_usage_report.py
    python scripts/voice_usage_report.py --desde 2026-08-01 --ate 2026-08-15
    python scripts/voice_usage_report.py --restaurante 0f9c8d2e-....

No container:

    docker exec pedeaqui-api python scripts/voice_usage_report.py --desde 2026-08-01
"""

import argparse
import sys
import uuid
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from sqlalchemy import Row

from src.core.constants import PLATFORM_TIMEZONE
from src.db.session import SessionLocal
from src.repositories.voice_session_repository import VoiceSessionRepository


FUSO = ZoneInfo(PLATFORM_TIMEZONE)
DIAS_PADRAO = 30


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Sessoes, tokens e duracao media da voz, por restaurante."
    )
    parser.add_argument(
        "--desde",
        type=date.fromisoformat,
        help=f"Primeiro dia do periodo (AAAA-MM-DD). Padrao: {DIAS_PADRAO} dias atras.",
    )
    parser.add_argument(
        "--ate",
        type=date.fromisoformat,
        help="Ultimo dia do periodo, INCLUSIVE (AAAA-MM-DD). Padrao: hoje.",
    )
    parser.add_argument(
        "--restaurante",
        type=uuid.UUID,
        help="Limita a um restaurante. Sem isto, sai um bloco por restaurante.",
    )
    args = parser.parse_args()

    hoje = datetime.now(FUSO).date()
    ultimo_dia = args.ate or hoje
    primeiro_dia = args.desde or (ultimo_dia - timedelta(days=DIAS_PADRAO - 1))
    if primeiro_dia > ultimo_dia:
        print("O --desde e depois do --ate.")
        return 1

    with SessionLocal() as db:
        linhas = VoiceSessionRepository(db).uso_por_restaurante(
            desde=_inicio_do_dia(primeiro_dia),
            ate=_inicio_do_dia(ultimo_dia + timedelta(days=1)),
            restaurant_id=args.restaurante,
        )

    _imprimir(linhas, primeiro_dia, ultimo_dia)
    return 0


def _inicio_do_dia(dia: date) -> datetime:
    """Meia-noite no fuso da plataforma, e nao em UTC.

    O periodo que alguem pede e o periodo do calendario dele. Cortar em UTC
    jogaria as sessoes das 21h as 00h — que sao as do jantar, o horario de
    maior movimento — para o dia seguinte.
    """
    return datetime.combine(dia, time.min, tzinfo=FUSO)


def _imprimir(linhas: list[Row], primeiro_dia: date, ultimo_dia: date) -> None:
    print(f"Uso da voz de {primeiro_dia:%d/%m/%Y} a {ultimo_dia:%d/%m/%Y} ({PLATFORM_TIMEZONE})")
    print()

    if not linhas:
        print("Nenhuma sessao de voz no periodo.")
        return

    for linha in linhas:
        _imprimir_restaurante(linha)

    if len(linhas) > 1:
        _imprimir_total(linhas)


def _imprimir_restaurante(linha: Row) -> None:
    print(linha.restaurante)
    print(_campo("sessoes", str(linha.sessoes)))
    print(_campo("com numero reportado", f"{linha.com_duracao} de {linha.sessoes}"))
    print(_campo("duracao media", _duracao(linha.duracao_media_s)))
    print(_campo("tokens entrada (audio)", _numero(linha.entrada_audio)))
    print(_campo("tokens entrada (texto)", _numero(linha.entrada_texto)))
    print(_campo("tokens saida (audio)", _numero(linha.saida_audio)))
    print(_campo("tokens saida (texto)", _numero(linha.saida_texto)))
    print(_campo("TOTAL de tokens", _numero(linha.total)))
    # Fora do total de proposito: o cache e a fatia da ENTRADA que saiu mais
    # barata, e nao uma quinta parcela. Somado, seria contado duas vezes.
    print(_campo("dos quais vieram de cache", _numero(linha.cache)))
    print()


def _imprimir_total(linhas: list[Row]) -> None:
    """O somatorio de todos os restaurantes do periodo.

    A duracao media geral e recalculada a partir das sessoes que reportaram
    duracao, e nao pela media das medias — restaurante com 2 sessoes pesaria
    igual a um com 200.
    """
    sessoes = sum(linha.sessoes for linha in linhas)
    com_duracao = sum(linha.com_duracao for linha in linhas)
    segundos = sum(
        linha.duracao_media_s * linha.com_duracao
        for linha in linhas
        if linha.duracao_media_s is not None
    )
    media = (segundos / com_duracao) if com_duracao else None

    print(f"TODOS OS {len(linhas)} RESTAURANTES")
    print(_campo("sessoes", str(sessoes)))
    print(_campo("com numero reportado", f"{com_duracao} de {sessoes}"))
    print(_campo("duracao media", _duracao(media)))
    print(_campo("TOTAL de tokens", _numero(sum(linha.total for linha in linhas))))
    print(_campo("dos quais vieram de cache", _numero(sum(linha.cache for linha in linhas))))


def _campo(rotulo: str, valor: str) -> str:
    return f"  {rotulo} {'.' * (30 - len(rotulo))} {valor}"


def _numero(valor: int | None) -> str:
    """Milhar com ponto, que e como se le numero grande em portugues."""
    return f"{valor or 0:,}".replace(",", ".")


def _duracao(segundos: Decimal | float | None) -> str:
    if segundos is None:
        return "— (nenhuma sessao reportou)"
    return f"{float(segundos):.0f} s"


if __name__ == "__main__":
    raise SystemExit(main())
