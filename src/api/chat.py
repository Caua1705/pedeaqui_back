import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from src.ai.schemas.chat_response_schema import ChatResponse
from src.api.dependencies.database import get_db
from src.schemas.ai_feedback_schema import AIFeedbackRequest, AIFeedbackResponse
from src.services.chat_service import ChatService


router = APIRouter(prefix="/chat", tags=["chat"])


class ChatRequest(BaseModel):
    restaurant_id: uuid.UUID
    session_id: str = Field(min_length=1)
    message: str = Field(min_length=1)


@router.post("/feedback", response_model=AIFeedbackResponse)
def create_feedback(
    request: AIFeedbackRequest,
    db: Session = Depends(get_db),
) -> AIFeedbackResponse:
    return ChatService(db).create_feedback(request)


@router.post("", response_model=ChatResponse)
def chat(
    request: ChatRequest,
    db: Session = Depends(get_db),
) -> ChatResponse:
    return ChatService(db).chat(
        restaurant_id=request.restaurant_id,
        session_id=request.session_id,
        message=request.message,
    )
