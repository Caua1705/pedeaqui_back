"""Consultas e escrita do livro-razao de custo de IA. Nao commita.

Quem commita e o service, como em todo o resto do repositorio.
"""

import uuid
from datetime import datetime

from sqlalchemy import Row, func, select
from sqlalchemy.orm import Session

from src.models.ai_usage_event_model import AIUsageEvent, SURFACE_TEXT, SURFACE_VOICE
from src.models.restaurant_model import Restaurant


class AIUsageRepository:
    def __init__(self, db: Session):
        self.db = db

    def add(self, evento: AIUsageEvent) -> AIUsageEvent:
        self.db.add(evento)
        self.db.flush()
        return evento

    def get_by_voice_session(self, voice_session_id: uuid.UUID) -> AIUsageEvent | None:
        """A linha ja gravada para aquela sessao de voz, se houver.

        E o que torna a gravacao da voz idempotente: o aviso de fim vai com
        `keepalive` e pode chegar duas vezes, e a segunda chegada tem que
        CORRIGIR a primeira em vez de dobrar o custo da conversa.
        """
        stmt = select(AIUsageEvent).where(
            AIUsageEvent.voice_session_id == voice_session_id
        )
        return self.db.scalars(stmt).one_or_none()

    def custo_por_restaurante(
        self,
        desde: datetime,
        ate: datetime,
        restaurant_id: uuid.UUID | None = None,
    ) -> list[Row]:
        """Uma linha por restaurante com chamada na janela `[desde, ate)`.

        `sem_preco` e a coluna que impede a leitura ingenua do total. Ela conta
        as chamadas cujo modelo nao estava em `src/ai/custo.py` no dia — elas
        somam tokens e nao somam dinheiro, entao um total alto com `sem_preco`
        alto significa "esta faltando preco na tabela", e nao "custou pouco".

        `COALESCE` no custo porque `SUM` de coluna com NULL ignora a linha,
        mas `SUM` de NENHUMA linha devolve NULL — e um restaurante em que
        todas as chamadas ficaram sem preco sairia com total nulo em vez de
        zero, que e mais dificil de ler do lado de fora.
        """
        e_texto = AIUsageEvent.surface == SURFACE_TEXT
        e_voz = AIUsageEvent.surface == SURFACE_VOICE

        stmt = (
            select(
                Restaurant.id.label("restaurant_id"),
                Restaurant.name.label("restaurante"),
                func.count().label("chamadas"),
                func.count().filter(e_texto).label("chamadas_texto"),
                func.count().filter(e_voz).label("chamadas_voz"),
                func.coalesce(func.sum(AIUsageEvent.cost_usd), 0).label("custo_usd"),
                func.coalesce(
                    func.sum(AIUsageEvent.cost_usd).filter(e_texto), 0
                ).label("custo_texto_usd"),
                func.coalesce(
                    func.sum(AIUsageEvent.cost_usd).filter(e_voz), 0
                ).label("custo_voz_usd"),
                func.sum(AIUsageEvent.input_tokens).label("tokens_entrada"),
                func.sum(AIUsageEvent.output_tokens).label("tokens_saida"),
                func.count().filter(AIUsageEvent.cost_usd.is_(None)).label("sem_preco"),
            )
            .join(Restaurant, Restaurant.id == AIUsageEvent.restaurant_id)
            .where(
                AIUsageEvent.created_at >= desde,
                AIUsageEvent.created_at < ate,
            )
            .group_by(Restaurant.id, Restaurant.name)
            .order_by(Restaurant.name)
        )
        if restaurant_id is not None:
            stmt = stmt.where(AIUsageEvent.restaurant_id == restaurant_id)

        return list(self.db.execute(stmt).all())
