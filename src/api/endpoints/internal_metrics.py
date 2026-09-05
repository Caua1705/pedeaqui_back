"""A leitura de custo de IA, para quem opera a PLATAFORMA — nao para o lojista.

## Por que ela nao mora em `/admin/reports`

Porque o publico e outro. Quanto o assistente custa a plataforma responde "a
comissao paga a conta?", e essa e a nossa margem: publicar isso no painel a
poria na mesa de negociacao do lojista, com a agravante de que ele veria o
numero e nos veriamos o numero dele ao mesmo tempo. E o raciocinio da
armadilha 17, que mantem `platform_commission_percent` fora de todo schema do
painel — la o risco e o lojista EDITAR quanto paga; aqui e ele saber a margem
antes de sentar para negociar.

Por isso tambem `include_in_schema=False`: o painel consome o `/openapi.json`
(armadilha 16), e rota que nao e do painel nao entra no contrato dele.
`tests/test_custo_de_ia.py` trava as duas coisas — a ausencia no documento e a
recusa sem chave.

## Segredo NOVO, e nao um dos que ja existem

Armadilha 32: segredo novo por publico novo. Houve a tentacao concreta de
reaproveitar a `INTERNAL_API_KEY` — a X-API-Key que as rotas /admin usavam
antes do JWT de lojista —, e ela seria o pior caso possivel: uma chave que
"nao e usada por rota nenhuma" voltando a valer alguma coisa em silencio,
depois de anos parada num `.env` que ninguem revisa. Aquela variavel foi
removida em 05/09/2026, e esta e propria desta rota.

`PLATFORM_METRICS_KEY` e OPCIONAL: sem ela a rota responde 503 e o resto da
API sobe igual. Obrigatoria, ela derrubaria o boot de todo mundo por causa de
um relatorio que so uma pessoa le.

## `compare_digest`, e nao `!=`

Armadilha 18. `!=` aborta no primeiro byte divergente e permite recuperar a
chave byte a byte pelo tempo de resposta.
"""

import hmac
import uuid
from datetime import date, datetime, time, timedelta

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy.orm import Session
from zoneinfo import ZoneInfo

from src.api.dependencies.database import get_db
from src.core.config import settings
from src.core.constants import PLATFORM_TIMEZONE
from src.schemas.ai_usage_schema import AIUsageReportResponse
from src.services.ai_usage_service import AIUsageService


router = APIRouter(prefix="/internal", tags=["internal"], include_in_schema=False)

FUSO = ZoneInfo(PLATFORM_TIMEZONE)

#: Quantos dias o relatorio cobre quando ninguem escolhe o periodo. Um mes e o
#: ciclo da fatura da OpenAI, que e com quem este numero e conferido.
DIAS_PADRAO = 30


def exigir_chave_da_plataforma(
    x_internal_key: str | None = Header(default=None),
) -> None:
    """A porta desta rota. 503 sem chave configurada, 401 com chave errada.

    503 e nao 401 quando a variavel esta vazia porque as duas situacoes pedem
    coisas diferentes de quem chama: com chave errada, corrija a chave; sem
    variavel no servidor, ninguem consegue entrar de jeito nenhum e o conserto
    e no `.env`. Um 401 nos dois casos mandaria procurar a chave certa para
    sempre.
    """
    configurada = (settings.PLATFORM_METRICS_KEY or "").strip()
    if not configurada:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="PLATFORM_METRICS_KEY nao configurada neste servidor.",
        )

    if not hmac.compare_digest(x_internal_key or "", configurada):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Chave invalida"
        )


@router.get(
    "/ai-usage",
    response_model=AIUsageReportResponse,
    dependencies=[Depends(exigir_chave_da_plataforma)],
)
def ai_usage_report(
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    restaurant_id: uuid.UUID | None = Query(default=None),
    db: Session = Depends(get_db),
) -> AIUsageReportResponse:
    """Custo de IA por restaurante, no periodo. Voz e texto separados.

    O periodo e de CALENDARIO, no fuso da plataforma, e `end_date` entra
    INTEIRO: quem pede "ate 31/08" quer o dia 31 todo. Cortar em UTC jogaria
    as conversas das 21h as 00h — o horario de maior movimento — para o dia
    seguinte, que e o mesmo cuidado de `scripts/voice_usage_report.py`.

    Ver `AIUsageService.custo_por_restaurante` para o que `calls_without_price`
    significa, e por que o total sem ele engana.
    """
    hoje = datetime.now(FUSO).date()
    ultimo_dia = end_date or hoje
    primeiro_dia = start_date or (ultimo_dia - timedelta(days=DIAS_PADRAO - 1))
    if primeiro_dia > ultimo_dia:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="start_date e depois de end_date",
        )

    return AIUsageService(db).custo_por_restaurante(
        desde=_inicio_do_dia(primeiro_dia),
        ate=_inicio_do_dia(ultimo_dia + timedelta(days=1)),
        restaurant_id=restaurant_id,
    )


def _inicio_do_dia(dia: date) -> datetime:
    return datetime.combine(dia, time.min, tzinfo=FUSO)
