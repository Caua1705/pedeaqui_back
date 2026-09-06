"""As leituras de custo da PLATAFORMA — nao do lojista.

Duas rotas, dois fornecedores, o mesmo publico: `GET /internal/ai-usage` (a
fatura da OpenAI, rateada) e `GET /internal/whatsapp-usage` (quantos avisos cada
loja mandou pelo cartao da Meta). Publico igual, porta igual, chave igual — ver
`exigir_chave_da_plataforma`.

## Por que elas nao moram em `/admin/reports`

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
from src.schemas.whatsapp_usage_schema import WhatsAppUsageReportResponse
from src.services.ai_usage_service import AIUsageService
from src.services.whatsapp_usage_service import WhatsAppUsageService


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
    """Custo de IA por restaurante, no periodo.

    `text_*` e `voice_*` continuam separados, e desde 06/09/2026 a segunda
    metade e HISTORICA: o assistente de voz saiu do projeto e nenhuma linha
    nova nasce com `surface = 'voice'`. Janela que alcance agosto ou o comeco
    de setembro de 2026 ainda a traz preenchida, e e por isso que ela nao saiu
    da resposta — sem ela `calls` deixaria de fechar com `text_calls`.

    O periodo e de CALENDARIO, no fuso da plataforma, e `end_date` entra
    INTEIRO: quem pede "ate 31/08" quer o dia 31 todo. Cortar em UTC jogaria
    as conversas das 21h as 00h — o horario de maior movimento — para o dia
    seguinte.

    Ver `AIUsageService.custo_por_restaurante` para o que `calls_without_price`
    significa, e por que o total sem ele engana.
    """
    desde, ate = _janela(start_date, end_date)
    return AIUsageService(db).custo_por_restaurante(
        desde=desde, ate=ate, restaurant_id=restaurant_id
    )


@router.get(
    "/whatsapp-usage",
    response_model=WhatsAppUsageReportResponse,
    dependencies=[Depends(exigir_chave_da_plataforma)],
)
def whatsapp_usage_report(
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    restaurant_id: uuid.UUID | None = Query(default=None),
    db: Session = Depends(get_db),
) -> WhatsAppUsageReportResponse:
    """Quantos avisos de WhatsApp cada restaurante mandou, no periodo.

    Mesma porta e mesmo publico de `/ai-usage`, e por isso a MESMA chave: o
    cartao da Meta e o da OpenAI sao os dois da plataforma, cobrados por conta e
    nao por loja. Chave nova aqui seria segredo novo para o publico que ja
    existe — o contrario do que a armadilha 32 pede.

    **Sem dinheiro na resposta**, e a ausencia esta explicada em
    `src/schemas/whatsapp_usage_schema.py`: template de utilidade dentro da
    janela de 24h e gratuito, e nao esta gravado se a janela estava aberta no
    instante do envio. A contagem e exata; o preco seria inventado.
    """
    desde, ate = _janela(start_date, end_date)
    return WhatsAppUsageService(db).templates_por_restaurante(
        desde=desde, ate=ate, restaurant_id=restaurant_id
    )


def _janela(start_date: date | None, end_date: date | None) -> tuple[datetime, datetime]:
    """A janela `[desde, ate)` de fato consultada, a partir do que veio na URL.

    Uma funcao so para as duas rotas porque a regra e a mesma e ela tem tres
    detalhes que nao dao para lembrar de repetir: o periodo e de CALENDARIO no
    fuso da plataforma, `end_date` entra INTEIRO (quem pede "ate 31/08" quer o
    dia 31 todo) e o default sao os ultimos 30 dias.

    Cortar em UTC jogaria o movimento das 21h as 00h para o dia seguinte — o
    horario de pico do restaurante.
    """
    hoje = datetime.now(FUSO).date()
    ultimo_dia = end_date or hoje
    primeiro_dia = start_date or (ultimo_dia - timedelta(days=DIAS_PADRAO - 1))
    if primeiro_dia > ultimo_dia:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="start_date e depois de end_date",
        )

    return _inicio_do_dia(primeiro_dia), _inicio_do_dia(ultimo_dia + timedelta(days=1))


def _inicio_do_dia(dia: date) -> datetime:
    return datetime.combine(dia, time.min, tzinfo=FUSO)
