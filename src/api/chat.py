import uuid
from datetime import datetime, timedelta, timezone
from typing import TypedDict

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from src.ai.schemas.chat_response_schema import ChatResponse
from src.ai.services.chat_llm_service import ChatLLMService
from src.ai.services.retrieval_service import RetrievalService
from src.api.dependencies.database import get_db


router = APIRouter(prefix="/chat", tags=["chat"])

_MAX_SESSION_MESSAGES = 20
_SESSION_TTL = timedelta(hours=1)


class SessionMessage(TypedDict):
    role: str
    content: str


class SessionState(TypedDict):
    messages: list[SessionMessage]
    last_interaction: datetime


_SESSION_HISTORY: dict[str, SessionState] = {}


class ChatRequest(BaseModel):
    restaurant_id: uuid.UUID
    session_id: str = Field(min_length=1)
    message: str = Field(min_length=1)


@router.post("", response_model=ChatResponse)
def chat(
    request: ChatRequest,
    db: Session = Depends(get_db),
) -> ChatResponse:
    now = _utc_now()
    _cleanup_inactive_sessions(now)

    retrieved_products = RetrievalService(db).retrieve_products(
        restaurant_id=request.restaurant_id,
        question=request.message,
    )
    session = _SESSION_HISTORY.get(request.session_id)
    conversation = session["messages"][-_MAX_SESSION_MESSAGES:] if session else []

    response = ChatLLMService().invoke(
        restaurant_context=f"restaurant_id={request.restaurant_id}",
        conversation=conversation,
        retrieved_products=retrieved_products,
        user_message=request.message,
    )

    session = _SESSION_HISTORY.setdefault(
        request.session_id,
        {"messages": [], "last_interaction": now},
    )
    session["messages"].extend(
        [
            {"role": "user", "content": request.message},
            {"role": "assistant", "content": response.message},
        ]
    )
    session["messages"] = session["messages"][-_MAX_SESSION_MESSAGES:]
    session["last_interaction"] = now
    return response


def _cleanup_inactive_sessions(now: datetime) -> None:
    expired_session_ids = [
        session_id
        for session_id, session in _SESSION_HISTORY.items()
        if now - session["last_interaction"] > _SESSION_TTL
    ]
    for session_id in expired_session_ids:
        del _SESSION_HISTORY[session_id]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
