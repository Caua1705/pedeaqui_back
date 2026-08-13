from sqlalchemy import select
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
