"""Gravacao do funil do cardapio, e o prazo em que ele vence.

Desenho completo em `docs/funil-e-origem.md`. As duas regras que este arquivo
existe para cumprir:

**A telemetria nunca derruba o cardapio.** Se o INSERT falhar, a rota responde
como se tivesse gravado e o erro vai para o log. Uma contagem perdida custa um
ponto num grafico; um 500 aqui custa a tela do cliente que ia pedir comida.

**O instante e do servidor.** Nenhum campo de tempo vem do corpo. Relogio de
celular erra, as vezes por meses, e um evento datado em 2027 nao apareceria em
erro nenhum — sumiria da janela do relatorio e o numero ficaria errado para
sempre.
"""

import logging
from datetime import datetime, timedelta

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from src.repositories.menu_event_repository import MenuEventRepository
from src.schemas.menu_event_schema import (
    MenuEventBatchRequest,
    MenuEventBatchResponse,
)
from src.utils.security import utcnow


logger = logging.getLogger("uvicorn.error")


# Por quantos dias o evento de funil fica no banco.
MENU_EVENT_RETENTION_DAYS = 90

# Quantas linhas o expurgo apaga por transacao. Ver
# `MenuEventRepository.delete_occurred_before`.
MENU_EVENT_PURGE_BATCH = 10_000


def menu_event_retention_cutoff(now: datetime) -> datetime:
    """Antes deste instante, o evento de funil tem que sair.

    **ISTO NAO E FAXINA DE DISCO. E o mecanismo de exclusao desta tabela.**

    `menu_events` nao tem `customer_id` — de proposito, ver o docstring do
    modelo — entao a exclusao de conta (`customer_anonymization_service`) nao
    alcanca linha nenhuma daqui, hoje ou nunca. E exatamente a situacao do
    `ai_feedback` (`chat_service.feedback_retention_cutoff`) e do comentario
    de avaliacao (`order_review_service.review_retention_cutoff`), e nas tres
    a resposta e a mesma: o prazo curto E a defesa.

    Quem for esticar este numero "porque disco e barato" esta trocando o
    mecanismo de exclusao por espaco em disco, e o motivo esta escrito aqui
    para essa troca ser deliberada.

    POR QUE 90 DIAS. Sao tres coisas ao mesmo tempo:

    - cobrem um trimestre inteiro, que e o horizonte em que o lojista decide
      onde gastar em divulgacao;
    - batem com `MAX_REPORT_DAYS` (92) — o teto do periodo que os relatorios
      aceitam. Nao ha tela capaz de pedir um recorte que o banco ja apagou, e
      essa coincidencia nao e coincidencia: mexer num sem o outro cria
      exatamente esse buraco;
    - sao curtos o bastante para o rastro nunca virar arquivo permanente de
      comportamento de gente.

    E MAIS CURTO que os 365 dias do comentario de avaliacao porque la existe
    leitor de verdade — o lojista le "o que reclamaram no ano passado". Aqui o
    leitor e um grafico de tendencia, e tendencia de mais de um trimestre
    ninguem consulta. Se um dia consultar, o caminho e o agregado diario (que
    nao tem pessoa dentro e pode ficar para sempre), e nao esticar este prazo
    — mas ele precisa existir ANTES do primeiro expurgo, senao o historico
    nao volta.
    """
    return now - timedelta(days=MENU_EVENT_RETENTION_DAYS)


class MenuEventService:
    def __init__(self, db: Session):
        self.db = db
        self.menu_event_repository = MenuEventRepository(db)

    def record_batch(self, payload: MenuEventBatchRequest) -> MenuEventBatchResponse:
        """Grava o lote e responde quantos entraram.

        Nao valida restaurante nem filial com consulta: a FK simples e a
        composta `(restaurant_id, branch_id)` ja recusam no banco o que nao
        existe e o que nao combina, sem custar um round-trip por requisicao
        na rota mais movimentada da API.

        E por isso o `except` e largo. O erro esperado aqui e o de
        integridade — front mandando um id de filial errado —, e ele nao pode
        virar 500 na tela de quem esta escolhendo o jantar.
        """
        rows = self._to_rows(payload)
        try:
            recorded = self.menu_event_repository.insert_batch(rows)
            self.db.commit()
        except SQLAlchemyError:
            self.db.rollback()
            # `exc_info` sem os dados do corpo: o que interessa e a filial e o
            # tipo do erro, e `session_id` nao entra em log (nem identifica
            # ninguem, nem serve para depurar).
            logger.warning(
                "[Funil] lote descartado restaurant_id=%s branch_id=%s eventos=%s",
                payload.restaurant_id,
                payload.branch_id,
                len(rows),
                exc_info=True,
            )
            return MenuEventBatchResponse(recorded=0)

        return MenuEventBatchResponse(recorded=recorded)

    @staticmethod
    def _to_rows(payload: MenuEventBatchRequest) -> list[dict]:
        """O envelope aberto em uma linha por evento.

        O instante e o MESMO para o lote inteiro — o da requisicao — e nao o
        de cada toque. O front acumula por ate dez segundos, entao a
        diferenca e de segundos, e o funil conta sessoes por dia: nenhuma
        pergunta que ele responde muda com isso.
        """
        occurred_at = utcnow()
        return [
            {
                "restaurant_id": payload.restaurant_id,
                "branch_id": payload.branch_id,
                "session_id": payload.session_id,
                "event_type": event.event_type,
                "source": payload.source,
                "product_id": event.product_id,
                "occurred_at": occurred_at,
            }
            for event in payload.events
        ]
