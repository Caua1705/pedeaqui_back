import uuid

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from src.ai.schemas.chat_response_schema import ChatResponse
from src.api.dependencies.database import get_db
from src.api.rate_limit import CHAT_FEEDBACK_RATE_LIMIT, CHAT_RATE_LIMIT, limiter
from src.schemas.ai_feedback_schema import AIFeedbackRequest, AIFeedbackResponse
from src.schemas.chat_limits import MAX_CHAT_MESSAGE_LENGTH, MAX_SESSION_ID_LENGTH
from src.services.chat_service import ChatService


router = APIRouter(prefix="/chat", tags=["chat"])


class ChatRequest(BaseModel):
    restaurant_id: uuid.UUID
    session_id: str = Field(min_length=1, max_length=MAX_SESSION_ID_LENGTH)
    message: str = Field(min_length=1, max_length=MAX_CHAT_MESSAGE_LENGTH)


@router.post("/feedback", response_model=AIFeedbackResponse)
@limiter.limit(CHAT_FEEDBACK_RATE_LIMIT)
def create_feedback(
    request: Request,
    payload: AIFeedbackRequest,
    db: Session = Depends(get_db),
) -> AIFeedbackResponse:
    return ChatService(db).create_feedback(payload)


@router.post("", response_model=ChatResponse)
@limiter.limit(CHAT_RATE_LIMIT)
def chat(
    request: Request,
    payload: ChatRequest,
    db: Session = Depends(get_db),
) -> ChatResponse:
    return ChatService(db).chat(
        restaurant_id=payload.restaurant_id,
        session_id=payload.session_id,
        message=payload.message,
    )
