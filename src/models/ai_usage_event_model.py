import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import ForeignKey, Integer, Numeric, Text, TIMESTAMP
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from src.db.base import Base


#: Os dois valores de `surface`. Nomes com constante porque eles aparecem no
#: CHECK do banco, na gravacao e na leitura — tres lugares para uma string
#: solta divergir.
SURFACE_TEXT = "text"
SURFACE_VOICE = "voice"
AI_SURFACES = (SURFACE_TEXT, SURFACE_VOICE)


class AIUsageEvent(Base):
    """Uma chamada de IA e o que ela custou. Ver a revisao 20260902_0044.

    Uma linha por turno do `/chat` e uma linha por SESSAO de voz — a voz nao
    reporta por turno, porque o audio vai do navegador direto para a OpenAI e
    o backend nunca ve os eventos da conversa.

    `cost_usd` NULO significa "modelo sem preco em `src/ai/custo.py`", e nao
    "de graca". Os tokens ficam gravados do mesmo jeito, entao a linha continua
    reprocessavel quando a tabela de precos for atualizada.

    `cached_input_tokens` e SUBCONJUNTO de `input_tokens`: somar os dois conta
    o cache duas vezes.
    """

    __tablename__ = "ai_usage_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    restaurant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("restaurants.id"), nullable=False
    )
    branch_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("branches.id")
    )
    surface: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)

    input_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    cached_input_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    output_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))

    # A sessao de voz de que esta linha fala. UNIQUE parcial no banco: o aviso
    # de fim da sessao pode chegar duas vezes (vai com `keepalive`, e o
    # navegador o reenvia ao fechar a aba), e sem a chave a segunda chegada
    # dobraria o custo daquela conversa.
    voice_session_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ai_voice_sessions.id")
    )

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
