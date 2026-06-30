from sqlalchemy.orm import Session

from src.models.ai_feedback_model import AIFeedback
from src.schemas.ai_feedback_schema import AIFeedbackRequest


class AIFeedbackRepository:
    def __init__(self, db: Session):
        self.db = db

    def create(self, feedback: AIFeedbackRequest) -> None:
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
        self.db.commit()
