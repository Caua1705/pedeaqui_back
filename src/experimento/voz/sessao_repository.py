"""Consultas do livro-razao de sessoes de voz. So consulta; nao commita."""

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.models.ai_voice_session_model import AIVoiceSession


class VozSessaoRepository:
    def __init__(self, db: Session):
        self.db = db

    def registrar(
        self,
        restaurant_id: uuid.UUID,
        customer_id: uuid.UUID | None,
        expires_at: datetime,
    ) -> AIVoiceSession:
        sessao = AIVoiceSession(
            restaurant_id=restaurant_id,
            customer_id=customer_id,
            expires_at=expires_at,
        )
        self.db.add(sessao)
        self.db.flush()
        return sessao

    def get(self, sessao_id: uuid.UUID) -> AIVoiceSession | None:
        return self.db.get(AIVoiceSession, sessao_id)

    def listar_vencidas_em_aberto(self, agora: datetime, limite: int = 20) -> list[AIVoiceSession]:
        """Sessoes que passaram do teto e ninguem fechou.

        Sem `ended_at` e com `expires_at` no passado. E a consulta que o indice
        parcial `ix_ai_voice_sessions_abertas` serve.
        """
        stmt = (
            select(AIVoiceSession)
            .where(
                AIVoiceSession.ended_at.is_(None),
                AIVoiceSession.expires_at < agora,
            )
            .order_by(AIVoiceSession.expires_at)
            .limit(limite)
        )
        return list(self.db.scalars(stmt).all())
