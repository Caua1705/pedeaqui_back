"""O agente de impressao: sinal de vida, impressoras da maquina e comandos.

Duas direcoes, e elas nao se confundem:

- **agente -> API**: `heartbeat` e `report_printers`. Sao fatos sobre a
  maquina do balcao. A filial vem do TOKEN, nunca do corpo — um agente que
  pudesse escolher a filial se anunciaria como outra loja e receberia os
  comandos dela.
- **painel -> agente**: `request_print_test`. Grava uma linha; quem entrega
  e o stream SSE que o agente ja escuta.

**Por que o comando nao ganhou um canal proprio.** A tentacao seria uma fila
em memoria, ou uma rota de polling separada. As duas quebram pelo mesmo
motivo da armadilha 20: com mais de um worker, o comando criado no worker A
nunca chegaria ao agente conectado no worker B, e um deploy no meio do
almoco perderia a fila. O stream ja resolve isso — cursor no banco, replay
por `Last-Event-ID`, reconexao automatica — e um segundo mecanismo seria um
segundo lugar para errar.

**O agente PRECISA estar preso a uma filial.** `scope.branch_id` nulo
significa "todas as filiais do restaurante" (`admin_scope.py`), e nao existe
"o heartbeat de todas as filiais": a maquina esta numa loja so. Um agente
nessa situacao e recusado com 400 e uma mensagem que diz o que fazer — e o
mesmo defeito que a revisao 0015 corrigiu no usuario do Junior.
"""

import logging
import uuid
from datetime import datetime

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from src.api.dependencies.admin_scope import AdminScope
from src.models.print_agent_model import PrintAgent, PrintAgentCommand, PrintAgentPrinter
from src.repositories.branch_repository import BranchRepository
from src.repositories.print_agent_repository import PrintAgentRepository
from src.repositories.printing_sector_repository import PrintingSectorRepository
from src.schemas.admin_printing_schema import (
    PrintAgentCommandType,
    PrintAgentHeartbeatRequest,
    PrintAgentPrinterResponse,
    PrintAgentPrintersRequest,
    PrintAgentPrintersResponse,
    PrintAgentStatusResponse,
    PrintTestRequest,
    PrintTestResponse,
)
from src.utils.security import utcnow


logger = logging.getLogger("uvicorn.error")

# Depois de quanto tempo sem sinal o painel mostra o agente como offline.
#
# O agente bate a cada HEARTBEAT_INTERVAL_SECONDS (30s, do lado dele). Tres
# batidas perdidas e o limite: menos que isso acenderia alarme falso a cada
# reconexao de stream, mais que isso deixaria a cozinha meia hora sem
# imprimir antes de alguem desconfiar.
ONLINE_WINDOW_SECONDS = 90

SEM_FILIAL = (
    "Este usuario nao esta preso a uma filial. O agente de impressao precisa "
    "de um usuario com branch_id preenchido: sem ele nao ha como saber de "
    "qual loja e esta maquina."
)


class PrintAgentService:
    def __init__(self, db: Session):
        self.db = db
        self.repository = PrintAgentRepository(db)
        self.branch_repository = BranchRepository(db)
        self.sector_repository = PrintingSectorRepository(db)

    # -- agente -> API ------------------------------------------------------

    def heartbeat(
        self,
        scope: AdminScope,
        payload: PrintAgentHeartbeatRequest,
    ) -> PrintAgentStatusResponse:
        """Registra que o agente daquela filial esta vivo, e em qual versao.

        Upsert de uma linha so por filial. A versao e regravada a cada
        batida de proposito: e assim que o painel percebe que a maquina foi
        atualizada, sem ninguem avisar.
        """
        branch_id = self._branch_of_agent(scope)
        agent = self.repository.get_agent(branch_id)
        now = utcnow()

        if agent is None:
            agent = PrintAgent(
                branch_id=branch_id,
                agent_version=payload.agent_version,
                last_seen_at=now,
            )
            self._commit(lambda: self.repository.add_agent(agent))
            logger.info(
                "[Impressao] agente visto pela primeira vez branch_id=%s versao=%s",
                branch_id,
                payload.agent_version,
            )
            return self._status(branch_id, agent)

        if agent.agent_version != payload.agent_version:
            logger.info(
                "[Impressao] agente mudou de versao branch_id=%s de=%s para=%s",
                branch_id,
                agent.agent_version,
                payload.agent_version,
            )
        agent.agent_version = payload.agent_version
        agent.last_seen_at = now
        self._commit()
        return self._status(branch_id, agent)

    def report_printers(
        self,
        scope: AdminScope,
        payload: PrintAgentPrintersRequest,
    ) -> PrintAgentPrintersResponse:
        """Substitui a lista de impressoras daquela maquina.

        Substitui, e nao acrescenta: a tabela e uma foto do que o Windows
        enxerga agora. Impressora desinstalada que continuasse no seletor do
        painel viraria uma escolha que nunca imprime.
        """
        branch_id = self._branch_of_agent(scope)
        now = utcnow()
        novas = [
            PrintAgentPrinter(
                branch_id=branch_id,
                name=printer.name,
                is_default=printer.is_default,
                reported_at=now,
            )
            for printer in payload.printers
        ]

        def replace() -> None:
            self.repository.delete_printers(branch_id)
            # Flush entre o DELETE e o INSERT: sem ele, o UNIQUE
            # (branch_id, name) bate contra as linhas que estao sendo
            # apagadas na mesma transacao.
            self.db.flush()
            self.repository.add_printers(novas)

        self._commit(replace)
        logger.info(
            "[Impressao] agente reportou %d impressora(s) branch_id=%s",
            len(novas),
            branch_id,
        )
        return self.list_printers(scope, branch_id)

    # -- painel -------------------------------------------------------------

    def get_status(self, scope: AdminScope, branch_id: uuid.UUID) -> PrintAgentStatusResponse:
        self._ensure_branch(scope, branch_id)
        return self._status(branch_id, self.repository.get_agent(branch_id))

    def list_printers(
        self,
        scope: AdminScope,
        branch_id: uuid.UUID,
    ) -> PrintAgentPrintersResponse:
        self._ensure_branch(scope, branch_id)
        return PrintAgentPrintersResponse(
            branch_id=branch_id,
            printers=[
                PrintAgentPrinterResponse.model_validate(printer)
                for printer in self.repository.list_printers(branch_id)
            ],
        )

    def request_print_test(
        self,
        scope: AdminScope,
        branch_id: uuid.UUID,
        payload: PrintTestRequest,
    ) -> PrintTestResponse:
        """Enfileira uma via de teste para a maquina daquela filial.

        Responde 202-em-espirito: o comando foi gravado, e nao "a bobina
        saiu". Por isso a resposta leva `agent_is_online` — sem ele o
        lojista aperta o botao, ve sucesso e fica olhando uma impressora que
        nao vai receber nada, porque o agente esta desligado desde ontem.
        """
        self._ensure_branch(scope, branch_id)
        sector = self._resolve_sector(scope, branch_id, payload.printing_sector_id)

        command = PrintAgentCommand(
            branch_id=branch_id,
            command_type=PrintAgentCommandType.PRINT_TEST.value,
            # A impressora do corpo vence a do setor: e o caso de conferir
            # uma maquina recem-instalada, antes de existir setor nenhum.
            printer_name=payload.printer_name or (sector.printer_name if sector else None),
            printing_sector_id=sector.id if sector else None,
            created_by_admin_user_id=scope.admin_user.id,
        )
        self._commit(lambda: self.repository.add_command(command))
        logger.info(
            "[Impressao] teste solicitado branch_id=%s impressora=%s por=%s",
            branch_id,
            command.printer_name,
            scope.admin_user.id,
        )
        return PrintTestResponse(
            command_id=command.id,
            branch_id=branch_id,
            created_at=command.created_at,
            agent_is_online=self._is_online(self.repository.get_agent(branch_id)),
        )

    # -- internos -----------------------------------------------------------

    def _branch_of_agent(self, scope: AdminScope) -> uuid.UUID:
        if scope.branch_id is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=SEM_FILIAL
            )
        return scope.branch_id

    def _ensure_branch(self, scope: AdminScope, branch_id: uuid.UUID) -> None:
        """404 para filial fora do escopo, como no resto de /admin.

        As duas conferencias, como em `AdminPrintingService._get_branch`: o
        escopo barra a filial que este lojista nao enxerga, e o repositorio
        barra a filial de outro restaurante.
        """
        scope.ensure_branch_allowed(branch_id)
        branch = self.branch_repository.get_active_by_id_and_restaurant(
            branch_id, scope.restaurant_id
        )
        if branch is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Filial nao encontrada"
            )

    def _resolve_sector(
        self,
        scope: AdminScope,
        branch_id: uuid.UUID,
        sector_id: uuid.UUID | None,
    ):
        if sector_id is None:
            return None
        sector = self.sector_repository.get(sector_id, scope.restaurant_id)
        if sector is None or sector.branch_id != branch_id:
            # Mesmo 404 para "nao existe" e "e de outra filial": um 403
            # confirmaria que aquele setor existe em algum lugar.
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Setor de impressao nao encontrado",
            )
        return sector

    def _status(
        self,
        branch_id: uuid.UUID,
        agent: PrintAgent | None,
    ) -> PrintAgentStatusResponse:
        if agent is None:
            # Filial que nunca teve agente. Nao e erro: e a loja que ainda
            # nao instalou, e a tela precisa poder dizer isso.
            return PrintAgentStatusResponse(branch_id=branch_id, is_online=False)

        return PrintAgentStatusResponse(
            branch_id=branch_id,
            agent_version=agent.agent_version,
            last_seen_at=agent.last_seen_at,
            seconds_since_last_seen=int(_seconds_since(agent.last_seen_at)),
            is_online=self._is_online(agent),
        )

    @staticmethod
    def _is_online(agent: PrintAgent | None) -> bool:
        if agent is None:
            return False
        return _seconds_since(agent.last_seen_at) <= ONLINE_WINDOW_SECONDS

    def _commit(self, before_commit=None) -> None:
        """Mesmo arranjo de `AdminPrintingService._commit`: a escrita entra no
        MESMO try do commit, porque uma falha ali precisa desfazer a sessao do
        mesmo jeito."""
        try:
            if before_commit is not None:
                before_commit()
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise


def _seconds_since(moment: datetime) -> float:
    """Segundos desde `moment`, tolerando coluna sem fuso.

    O Postgres devolve `timestamptz` com fuso, mas uma linha gravada por
    fora (script, correcao a mao) pode chegar ingenua — e subtrair ingenuo
    de consciente levanta TypeError, que aqui viraria 500 numa tela de
    diagnostico.
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=utcnow().tzinfo)
    return (utcnow() - moment).total_seconds()
