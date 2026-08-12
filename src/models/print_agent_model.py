"""O agente de impressao visto de dentro da API.

Tres tabelas, e as tres existem para a tela de Impressao do painel poder
responder perguntas que hoje so o log da maquina do balcao responde: o
agente esta no ar? qual versao? quais impressoras aquela maquina tem? e
como mandar uma via de teste para la.

O agente continua sendo burro (ver o docstring de `print-agent/agent.py`):
nada aqui e regra de negocio dele. Sao fatos que ele reporta e comandos que
ele obedece.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Text, TIMESTAMP
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from src.db.base import Base


class PrintAgent(Base):
    """Ultimo sinal de vida do agente de uma filial.

    Uma linha por filial (`branch_id` UNIQUE): uma loja, uma maquina de
    balcao, um agente. Duas maquinas na mesma filial sobrescreveriam o
    `last_seen_at` uma da outra e o painel diria "online" com uma delas
    parada — por isso o UNIQUE, que faz esse caso aparecer em vez de mentir.
    """

    __tablename__ = "print_agents"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    branch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("branches.id"), nullable=False, unique=True
    )
    agent_version: Mapped[str | None] = mapped_column(Text)
    last_seen_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    created_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class PrintAgentPrinter(Base):
    """Uma impressora instalada na maquina do balcao, como o Windows a ve.

    Existe para o painel oferecer uma LISTA em vez de um campo de texto. O
    nome tem que bater byte a byte com o do Windows, e um espaco a mais
    digitado a mao faz a via nao sair — sem erro, porque quem descobre isso
    e a impressora que nao recebeu nada.
    """

    __tablename__ = "print_agent_printers"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    branch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("branches.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    reported_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)


class PrintAgentCommand(Base):
    """Uma ordem do painel para o agente.

    Nao tem `delivered_at` de proposito: a entrega e do cursor do stream, o
    mesmo que ja entrega os pedidos. Marcar entrega exigiria o agente
    escrever de volta — outra rota, para saber o que o `last_seen_at` do
    heartbeat ja aproxima.

    A repeticao (o replay de ate uma hora do stream) e tratada no agente,
    que lembra os comandos ja executados no mesmo arquivo em que lembra os
    pedidos ja impressos.
    """

    __tablename__ = "print_agent_commands"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    branch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("branches.id"), nullable=False
    )
    command_type: Mapped[str] = mapped_column(Text, nullable=False)
    printer_name: Mapped[str | None] = mapped_column(Text)
    printing_sector_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("printing_sectors.id")
    )
    created_by_admin_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("admin_users.id")
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
