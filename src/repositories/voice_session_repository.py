"""Consultas do livro-razao de sessoes de voz. So consulta; nao commita."""

import uuid
from datetime import datetime

from sqlalchemy import Row, func, select
from sqlalchemy.orm import Session

from src.models.ai_voice_session_model import AIVoiceSession
from src.models.restaurant_model import Restaurant
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

    def uso_por_restaurante(
        self,
        desde: datetime,
        ate: datetime,
        restaurant_id: uuid.UUID | None = None,
    ) -> list[Row]:
        """Uma linha por restaurante com sessao emitida na janela `[desde, ate)`.

        A janela corta por `issued_at` — quando a credencial foi EMITIDA. Uma
        sessao que comeca 23h58 e acaba 00h03 conta no dia em que comecou, que
        e o dia em que ela aparece na cota e o dia em que o cliente ligou.

        `COALESCE` em cada parcela porque contador nao reportado e NULL, e
        `NULL + numero` e NULL em SQL: sem ele, uma unica sessao sem numero
        zeraria o total do restaurante inteiro.

        `cached_tokens` sai em coluna separada e NAO entra no total: e a fatia
        da ENTRADA que veio do cache, entao soma-lo contaria esses tokens duas
        vezes.

        A media de duracao ignora as sessoes sem duracao reportada — `AVG` do
        Postgres pula NULL. E por isso a contagem dessas vem junto: media de
        3 sessoes num universo de 40 e um numero que precisa vir com o
        denominador ao lado.
        """
        entrada_audio = func.coalesce(AIVoiceSession.input_audio_tokens, 0)
        entrada_texto = func.coalesce(AIVoiceSession.input_text_tokens, 0)
        saida_audio = func.coalesce(AIVoiceSession.output_audio_tokens, 0)
        saida_texto = func.coalesce(AIVoiceSession.output_text_tokens, 0)

        stmt = (
            select(
                Restaurant.id.label("restaurant_id"),
                Restaurant.name.label("restaurante"),
                func.count().label("sessoes"),
                func.count(AIVoiceSession.duration_seconds).label("com_duracao"),
                func.avg(AIVoiceSession.duration_seconds).label("duracao_media_s"),
                func.sum(entrada_audio).label("entrada_audio"),
                func.sum(entrada_texto).label("entrada_texto"),
                func.sum(saida_audio).label("saida_audio"),
                func.sum(saida_texto).label("saida_texto"),
                func.sum(entrada_audio + entrada_texto + saida_audio + saida_texto).label("total"),
                func.sum(func.coalesce(AIVoiceSession.cached_tokens, 0)).label("cache"),
            )
            .join(Restaurant, Restaurant.id == AIVoiceSession.restaurant_id)
            .where(
                AIVoiceSession.issued_at >= desde,
                AIVoiceSession.issued_at < ate,
            )
            .group_by(Restaurant.id, Restaurant.name)
            .order_by(Restaurant.name)
        )
        if restaurant_id is not None:
            stmt = stmt.where(AIVoiceSession.restaurant_id == restaurant_id)

        return list(self.db.execute(stmt).all())

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
