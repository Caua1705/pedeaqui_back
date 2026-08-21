from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from src.models.ai_feedback_model import AIFeedback
from src.schemas.ai_feedback_schema import AIFeedbackRequest


class AIFeedbackRepository:
    def __init__(self, db: Session):
        self.db = db

    def create(self, feedback: AIFeedbackRequest) -> None:
        """Grava o voto, ou troca o que ja existia para aquela mensagem.

        NAO commita, ao contrario de antes: quem commita e o service, sempre
        (regra de camadas do projeto). Enquanto o commit morava aqui, o
        service devolvia `success=True` sobre uma transacao que ele nao
        controlava — e um erro depois desta linha deixava o voto gravado com
        a resposta dizendo o contrario.
        """
        existing_feedback = self.db.scalar(
            select(AIFeedback).where(
                AIFeedback.session_id == feedback.session_id,
                AIFeedback.assistant_message == feedback.assistant_message,
            )
        )

        if existing_feedback:
            # Mesmo voto de novo: nada muda. Sair aqui evita um UPDATE que
            # so reescreveria o valor que ja esta la.
            if existing_feedback.feedback == feedback.feedback:
                return

            existing_feedback.feedback = feedback.feedback
            self.db.flush()
            return

        self.db.add(
            AIFeedback(
                restaurant_id=feedback.restaurant_id,
                session_id=feedback.session_id,
                user_message=feedback.user_message,
                assistant_message=feedback.assistant_message,
                response_type=feedback.response_type,
                selected_product_ids=feedback.selected_product_ids,
                feedback=feedback.feedback,
            )
        )
        self.db.flush()

    def delete_created_before(self, cutoff: datetime) -> int:
        """Apaga o feedback velho. Devolve quantos sairam.

        Corta por `created_at` e nao por vencimento porque a linha nao tem
        vencimento: ela vale enquanto servir de amostra de qualidade das
        respostas do Rapi. Quem sabe ate quando e
        `chat_service.feedback_retention_cutoff`, e e de la que o `cutoff`
        tem que vir — o repositorio so consulta.
        """
        resultado = self.db.execute(delete(AIFeedback).where(AIFeedback.created_at < cutoff))
        return resultado.rowcount or 0
