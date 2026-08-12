"""Consultas do agente de impressao: sinal de vida, impressoras e comandos.

Separado de `PrintingSectorRepository` porque sao coisas de ciclos de vida
diferentes: setor e cadastro do lojista, e o que esta aqui e estado de uma
maquina do balcao — nasce e morre com a instalacao dela.

Como em todo repositorio deste projeto: so consulta. Nao decide, nao levanta
erro de regra e nao commita.
"""

import uuid
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from src.models.print_agent_model import PrintAgent, PrintAgentCommand, PrintAgentPrinter


class PrintAgentRepository:
    def __init__(self, db: Session):
        self.db = db

    def get_agent(self, branch_id: uuid.UUID) -> PrintAgent | None:
        return self.db.scalar(select(PrintAgent).where(PrintAgent.branch_id == branch_id))

    def add_agent(self, agent: PrintAgent) -> PrintAgent:
        self.db.add(agent)
        self.db.flush()
        return agent

    def list_printers(self, branch_id: uuid.UUID) -> list[PrintAgentPrinter]:
        """As impressoras da maquina, a padrao primeiro.

        A ordem e a da tela: o seletor do painel abre com a impressora
        padrao no topo, que e a que recebe o que nao foi mapeado.
        """
        stmt = (
            select(PrintAgentPrinter)
            .where(PrintAgentPrinter.branch_id == branch_id)
            .order_by(PrintAgentPrinter.is_default.desc(), PrintAgentPrinter.name.asc())
        )
        return list(self.db.scalars(stmt).all())

    def delete_printers(self, branch_id: uuid.UUID) -> int:
        """Apaga a lista inteira da filial, para o service regravar.

        DELETE de verdade, e nao `is_active`, ao contrario do resto do
        projeto: aqui nao ha FK apontando para esta linha nem historico a
        preservar. A tabela e uma FOTO do que o Windows daquela maquina
        enxerga agora — impressora desinstalada tem que sumir do seletor,
        senao o lojista escolhe uma que nao existe mais.
        """
        result = self.db.execute(
            delete(PrintAgentPrinter).where(PrintAgentPrinter.branch_id == branch_id)
        )
        return result.rowcount or 0

    def add_printers(self, printers: list[PrintAgentPrinter]) -> list[PrintAgentPrinter]:
        self.db.add_all(printers)
        self.db.flush()
        return printers

    def add_command(self, command: PrintAgentCommand) -> PrintAgentCommand:
        self.db.add(command)
        self.db.flush()
        return command

    def list_commands_since(
        self,
        branch_id: uuid.UUID,
        since: datetime,
        limit: int,
    ) -> list[PrintAgentCommand]:
        """Comandos daquela filial depois do cursor do stream.

        `branch_id` e obrigatorio e nao aceita nulo, diferente das consultas
        de pedido: um comando de impressao e sempre de UMA maquina. Um
        agente sem filial (`branch_id` nulo no escopo) nao tem como saber
        quais comandos sao dele, e por isso o service o recusa antes de
        chegar aqui.
        """
        stmt = (
            select(PrintAgentCommand)
            .where(
                PrintAgentCommand.branch_id == branch_id,
                PrintAgentCommand.created_at > since,
            )
            .order_by(PrintAgentCommand.created_at.asc())
            .limit(limit)
        )
        return list(self.db.scalars(stmt).all())
