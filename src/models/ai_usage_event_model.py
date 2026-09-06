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
#:
#: `voice` E HISTORICO desde 06/09/2026, e por isso ele continua aqui. O
#: assistente de voz saiu do projeto e nenhuma linha nova nasce com esse
#: valor — mas as que ja existem sao DINHEIRO ja gasto, e o CHECK do banco
#: continua aceitando os dois. Tirar a constante deixaria
#: `scripts/espelhos_de_enum.py` vermelho contra um CHECK que ninguem vai
#: reescrever (recriar a CHECK sem `voice` FALHARIA sobre as linhas gravadas),
#: e apagaria do relatorio a unica coisa que separa o custo da conversa falada
#: do custo do `/chat` nos meses em que os dois existiram.
SURFACE_TEXT = "text"
SURFACE_VOICE = "voice"
AI_SURFACES = (SURFACE_TEXT, SURFACE_VOICE)


class AIUsageEvent(Base):
    """Uma chamada de IA e o que ela custou. Ver a revisao 20260902_0044.

    Uma linha por turno do `/chat`. Houve tambem uma linha por SESSAO de voz
    ate 06/09/2026, quando o assistente falado saiu do projeto: essas linhas
    continuam na tabela, com `surface = 'voice'`, e continuam somando no
    relatorio de custo. O que nao existe mais e quem escreve novas.

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

    # `voice_session_id` NAO e mapeada desde 06/09/2026. A coluna continua no
    # banco, junto da CHECK `(surface = 'voice') = (voice_session_id IS NOT
    # NULL)`, ate a revisao preparada `20260906_0060` ser aplicada — e as duas
    # coisas convivem porque a linha de TEXTO que o ORM grava deixa a coluna
    # nula, que e o lado verdadeiro da CHECK para ela. Ver `docs/custo-de-ia.md`.

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
