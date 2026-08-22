"""A rota que recebe o funil do cardapio.

Uma rota, um lote, sem autenticacao — o cardapio e publico e quem navega nele
pode nem ter conta. Desenho completo em `docs/funil-e-origem.md`.

## Por que 202, e nao 200

Porque e a verdade sobre o que aconteceu. A pagina nao espera esta resposta:
a ultima leva de eventos sai por `navigator.sendBeacon` no fechamento da aba,
e o `sendBeacon` nem entrega o corpo da resposta a quem chamou. E o service
responde `recorded=0` em vez de erro quando o INSERT falha (telemetria nao
derruba cardapio), entao um 200 prometeria uma gravacao que a rota
deliberadamente nao garante.
"""

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.orm import Session

from src.api.dependencies.database import get_db
from src.api.rate_limit import MENU_EVENT_RATE_LIMIT, limiter
from src.schemas.menu_event_schema import (
    MenuEventBatchRequest,
    MenuEventBatchResponse,
)
from src.services.menu_event_service import MenuEventService


router = APIRouter(tags=["menu events"])


@router.post(
    "/menu-events",
    response_model=MenuEventBatchResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
@limiter.limit(MENU_EVENT_RATE_LIMIT)
def record_menu_events(
    request: Request,
    payload: MenuEventBatchRequest,
    db: Session = Depends(get_db),
) -> MenuEventBatchResponse:
    """Registra um lote de eventos do funil.

    O corpo leva `restaurant_id`, `branch_id`, `session_id` e `source` UMA
    vez, e a lista de eventos dentro. Ate 50 por lote; acima disso o corpo
    inteiro e recusado com 422, nunca truncado — um lote cortado pela metade
    entregaria um funil silenciosamente errado.

    **Nao ha campo de instante.** O horario e o do servidor, para o funil nao
    depender do relogio do celular de quem navega.

    **Nao ha campo de cliente.** Nem quando a pessoa esta logada: ligar sessao
    a pessoa transformaria a contagem em historico comportamental
    identificado, e nenhum relatorio precisa disso. Ver a secao 5 do
    documento.

    `source` desconhecido nao e erro: vira `direct`. Recusar aqui nao
    consertaria o QR impresso com defeito — so apagaria o dado que se quer
    coletar.
    """
    return MenuEventService(db).record_batch(payload)
