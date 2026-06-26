import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from src.ai.schemas.chat_response_schema import ChatResponse
from src.ai.services.chat_llm_service import ChatLLMService
from src.ai.services.retrieval_service import RetrievalService
from src.api.dependencies.database import get_db


router = APIRouter(prefix="/chat", tags=["chat"])

_SESSION_HISTORY: dict[str, list[dict[str, str]]] = {}


class ChatRequest(BaseModel):
    restaurant_id: uuid.UUID
    session_id: str = Field(min_length=1)
    message: str = Field(min_length=1)


@router.post("", response_model=ChatResponse)
def chat(
    request: ChatRequest,
    db: Session = Depends(get_db),
) -> ChatResponse:
    retrieved_products = RetrievalService(db).retrieve_products(
        restaurant_id=request.restaurant_id,
        question=request.message,
    )
    conversation = _SESSION_HISTORY.get(request.session_id, [])

    response = ChatLLMService().invoke(
        restaurant_context=f"restaurant_id={request.restaurant_id}",
        conversation=conversation,
        retrieved_products=retrieved_products,
        user_message=request.message,
    )

    _SESSION_HISTORY.setdefault(request.session_id, []).extend(
        [
            {"role": "user", "content": request.message},
            {"role": "assistant", "content": response.message},
        ]
    )
    return response
