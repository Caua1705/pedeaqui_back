import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Integer, Text, TIMESTAMP
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from src.db.base import Base


class AIVoiceSession(Base):
    """Uma credencial de voz emitida. Ver a revisao 20260815_0021.

    `openai_call_id` e nulo ate o navegador reportar: ele so existe no
    cabecalho `Location` da resposta que a OpenAI da ao NAVEGADOR, e sem ele o
    servidor nao consegue desligar a sessao.
    """

    __tablename__ = "ai_voice_sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    restaurant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("restaurants.id"), nullable=False
    )
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customers.id")
    )
    openai_call_id: Mapped[str | None] = mapped_column(Text)
    issued_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    ended_reason: Mapped[str | None] = mapped_column(Text)

    # O consumo, reportado pelo navegador ao encerrar. Ver a revisao
    # 20260815_0023. NULO e "nao reportado", e nao zero: a sessao que encerra
    # sem mandar numero nenhum continua valendo.
    #
    # `cached_tokens` e SUBCONJUNTO da entrada, nao uma quinta parcela — o
    # total sao os quatro primeiros, e somar o cache junto o conta duas vezes.
    input_audio_tokens: Mapped[int | None] = mapped_column(Integer)
    input_text_tokens: Mapped[int | None] = mapped_column(Integer)
    output_audio_tokens: Mapped[int | None] = mapped_column(Integer)
    output_text_tokens: Mapped[int | None] = mapped_column(Integer)
    cached_tokens: Mapped[int | None] = mapped_column(Integer)
    duration_seconds: Mapped[int | None] = mapped_column(Integer)
