import logging
import uuid
from datetime import datetime, timedelta, timezone
from time import perf_counter
from typing import TypedDict

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from src.ai.schemas.chat_response_schema import ChatResponse
from src.ai.services.chat_llm_service import ChatLLMService
from src.ai.services.retrieval_service import RetrievalService
from src.api.dependencies.database import get_db
from src.repositories.product_repository import ProductRepository
from src.services.menu_service import MenuService


router = APIRouter(prefix="/chat", tags=["chat"])
logger = logging.getLogger("uvicorn.error")

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
    started_at = perf_counter()
    logger.info(
        "[AI /chat] Nova requisição | restaurant_id=%s | session_id=%s | mensagem=%r",
        request.restaurant_id,
        request.session_id,
        request.message,
    )

    try:
        now = _utc_now()
        _cleanup_inactive_sessions(now)

        retrieved_products = RetrievalService(db).retrieve_products(
            restaurant_id=request.restaurant_id,
            question=request.message,
        )
        session = _SESSION_HISTORY.get(request.session_id)
        conversation = session["messages"][-_MAX_SESSION_MESSAGES:] if session else []

        llm_response = ChatLLMService().invoke(
            restaurant_context=f"restaurant_id={request.restaurant_id}",
            conversation=conversation,
            retrieved_products=retrieved_products,
            user_message=request.message,
        )
        logger.info(
            "[AI /chat] Structured Output | response_type=%s",
            llm_response.response_type,
        )

        retrieved_product_ids = {product["id"] for product in retrieved_products}
        selected_product_ids = list(
            dict.fromkeys(
                product_id
                for product_id in llm_response.selected_product_ids
                if product_id in retrieved_product_ids
            )
        )
        products_by_id = {
            product.id: product
            for product in ProductRepository(db).list_active_by_ids(
                request.restaurant_id,
                selected_product_ids,
            )
        }
        products = [
            MenuService.product_response(products_by_id[product_id])
            for product_id in selected_product_ids
            if product_id in products_by_id
        ]
        response = ChatResponse(
            response_type=llm_response.response_type,
            message=llm_response.message,
            products=products,
        )
        if response.products:
            logger.info(
                "[AI /chat] Produtos enviados ao frontend | quantidade=%d",
                len(response.products),
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
    except Exception:
        logger.exception(
            "[AI /chat] Erro no pipeline | restaurant_id=%s | session_id=%s",
            request.restaurant_id,
            request.session_id,
        )
        raise
    finally:
        logger.info(
            "[AI /chat] Requisição finalizada | tempo_total=%.3fs",
            perf_counter() - started_at,
        )


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
