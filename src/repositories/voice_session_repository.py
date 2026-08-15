"""Consultas do livro-razao de sessoes de voz. So consulta; nao commita."""

import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.models.ai_voice_session_model import AIVoiceSession
from src.models.restaurant_setting_model import RestaurantSetting


class VoiceSessionRepository:
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

    def voz_habilitada(self, restaurant_id: uuid.UUID) -> bool:
        """A voz esta ligada NESTE restaurante?

        Le so a coluna, e nao a linha inteira de `restaurant_settings`: e uma
        pergunta de sim ou nao no caminho de uma requisicao paga.

        Restaurante sem linha de configuracao devolve `None` e conta como
        desligado. Ausencia de configuracao nunca pode significar "pode
        gastar".
        """
        stmt = select(RestaurantSetting.voice_enabled).where(
            RestaurantSetting.restaurant_id == restaurant_id
        )
        return bool(self.db.scalar(stmt))

    def contar_do_cliente_desde(self, customer_id: uuid.UUID, desde: datetime) -> int:
        stmt = (
            select(func.count())
            .select_from(AIVoiceSession)
            .where(
                AIVoiceSession.customer_id == customer_id,
                AIVoiceSession.issued_at >= desde,
            )
        )
        return self.db.scalar(stmt) or 0

    def contar_do_restaurante_desde(self, restaurant_id: uuid.UUID, desde: datetime) -> int:
        stmt = (
            select(func.count())
            .select_from(AIVoiceSession)
            .where(
                AIVoiceSession.restaurant_id == restaurant_id,
                AIVoiceSession.issued_at >= desde,
            )
        )
        return self.db.scalar(stmt) or 0

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
