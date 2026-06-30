import uuid
from typing import Literal

from pydantic import BaseModel


class AIFeedbackRequest(BaseModel):
    restaurant_id: uuid.UUID
    session_id: str
    user_message: str
    assistant_message: str
    response_type: str
    selected_product_ids: list[str]
    feedback: Literal['like', 'dislike']


class AIFeedbackResponse(BaseModel):
    success: bool
