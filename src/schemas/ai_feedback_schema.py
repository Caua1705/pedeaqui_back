import uuid
from typing import Literal

from pydantic import BaseModel, Field

from src.schemas.chat_limits import MAX_CHAT_MESSAGE_LENGTH, MAX_SESSION_ID_LENGTH


MAX_ASSISTANT_MESSAGE_LENGTH = 8000
MAX_SELECTED_PRODUCT_IDS = 50


class AIFeedbackRequest(BaseModel):
    restaurant_id: uuid.UUID
    session_id: str = Field(max_length=MAX_SESSION_ID_LENGTH)
    user_message: str = Field(max_length=MAX_CHAT_MESSAGE_LENGTH)
    assistant_message: str = Field(max_length=MAX_ASSISTANT_MESSAGE_LENGTH)
    response_type: str = Field(max_length=30)
    selected_product_ids: list[uuid.UUID] = Field(max_length=MAX_SELECTED_PRODUCT_IDS)
    feedback: Literal['like', 'dislike']


class AIFeedbackResponse(BaseModel):
    success: bool
