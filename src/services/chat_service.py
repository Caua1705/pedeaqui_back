import hashlib
import logging
import uuid
from datetime import datetime, timedelta, timezone
from time import perf_counter
from typing import TypedDict

from sqlalchemy.orm import Session

from src.ai.schemas.chat_response_schema import ChatResponse
from src.ai.services.chat_llm_service import ChatLLMService
from src.ai.services.retrieval_service import RetrievalService
from src.repositories.ai_feedback_repository import AIFeedbackRepository
from src.repositories.product_repository import ProductRepository
from src.schemas.ai_feedback_schema import AIFeedbackRequest, AIFeedbackResponse
from src.services.menu_service import MenuService


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


class ChatService:
    def __init__(self, db: Session):
        self.db = db
        self.retrieval_service = RetrievalService(db)
        self.product_repository = ProductRepository(db)

    def create_feedback(self, request: AIFeedbackRequest) -> AIFeedbackResponse:
        AIFeedbackRepository(self.db).create(request)
        return AIFeedbackResponse(success=True)

    def chat(
        self,
        restaurant_id: uuid.UUID,
        session_id: str,
        message: str,
    ) -> ChatResponse:
        started_at = perf_counter()
        validation_started_at = perf_counter()
        logger.info(
            "[AI /chat perf] validation_ms=%.2f",
            (perf_counter() - validation_started_at) * 1000,
        )
        # A mensagem do usuario e dado pessoal e nao vai para o log.
        # O digest permite correlacionar requisicoes sem expor o conteudo.
        logger.info(
            "[AI /chat] Nova requisicao | restaurant_id=%s | session_id=%s "
            "| message_chars=%d | message_digest=%s",
            restaurant_id,
            session_id,
            len(message),
            _message_digest(message),
        )

        try:
            now = _utc_now()
            _cleanup_inactive_sessions(now)
            conversation = _get_session_conversation(session_id)
            logger.info("[AI /chat cache] final_response_cache_hit=false")

            retrieved_products = self.retrieval_service.retrieve_products(
                restaurant_id=restaurant_id,
                question=message,
            )
            llm_response = self._invoke_llm(
                restaurant_id=restaurant_id,
                conversation=conversation,
                retrieved_products=retrieved_products,
                message=message,
            )
            selected_product_ids = self._validate_selected_product_ids(
                retrieved_products=retrieved_products,
                selected_product_ids=llm_response.selected_product_ids,
            )
            products = self._hydrate_products(restaurant_id, selected_product_ids)
            response = self._build_response(
                llm_response=llm_response,
                selected_product_ids=selected_product_ids,
                products=products,
            )

            _store_session_turn(session_id, message, response.message, now)
            logger.info(
                "[AI /chat] Retorno final | response_type=%s | products_count=%d",
                response.response_type,
                len(response.products),
            )
            return response
        except Exception:
            logger.exception(
                "[AI /chat] Erro no pipeline | restaurant_id=%s | session_id=%s",
                restaurant_id,
                session_id,
            )
            raise
        finally:
            logger.info(
                "[AI /chat perf] total_ms=%.2f",
                (perf_counter() - started_at) * 1000,
            )

    @staticmethod
    def _invoke_llm(
        restaurant_id: uuid.UUID,
        conversation: list[SessionMessage],
        retrieved_products: list[dict],
        message: str,
    ):
        llm_started_at = perf_counter()
        llm_response = ChatLLMService().invoke(
            restaurant_context=f"restaurant_id={restaurant_id}",
            conversation=conversation,
            retrieved_products=retrieved_products,
            user_message=message,
        )
        logger.info(
            "[AI /chat perf] llm_ms=%.2f",
            (perf_counter() - llm_started_at) * 1000,
        )
        logger.info(
            "[AI /chat] Structured Output | response_type=%s",
            llm_response.response_type,
        )
        return llm_response

    @staticmethod
    def _validate_selected_product_ids(
        retrieved_products: list[dict],
        selected_product_ids: list[uuid.UUID],
    ) -> list[uuid.UUID]:
        ids_validation_started_at = perf_counter()
        retrieved_product_ids = {
            uuid.UUID(str(product["id"])) for product in retrieved_products
        }
        valid_product_ids = list(
            dict.fromkeys(
                uuid.UUID(str(product_id))
                for product_id in selected_product_ids
                if uuid.UUID(str(product_id)) in retrieved_product_ids
            )
        )
        logger.info(
            "[AI /chat perf] selected_ids_validation_ms=%.2f",
            (perf_counter() - ids_validation_started_at) * 1000,
        )
        return valid_product_ids

    def _hydrate_products(
        self,
        restaurant_id: uuid.UUID,
        selected_product_ids: list[uuid.UUID],
    ):
        hydration_started_at = perf_counter()
        products_by_id = {
            uuid.UUID(str(product.id)): product
            for product in self.product_repository.list_active_by_ids(
                restaurant_id,
                selected_product_ids,
            )
        }
        logger.info(
            "[AI /chat perf] hydration_ms=%.2f",
            (perf_counter() - hydration_started_at) * 1000,
        )
        return [
            MenuService.product_response(products_by_id[product_id])
            for product_id in selected_product_ids
            if product_id in products_by_id
        ]

    @staticmethod
    def _build_response(
        llm_response,
        selected_product_ids: list[uuid.UUID],
        products: list,
    ) -> ChatResponse:
        response_build_started_at = perf_counter()
        response_type = llm_response.response_type
        if products:
            response_type = "products"
        elif response_type == "products":
            logger.warning(
                "[AI /chat] Nenhum produto valido para response_type=products "
                "| selected=%d | validados=%d",
                len(llm_response.selected_product_ids),
                len(selected_product_ids),
            )
            response_type = "text"

        response = ChatResponse(
            response_type=response_type,
            message=llm_response.message,
            products=products,
        )
        if response.products:
            logger.info(
                "[AI /chat] Produtos enviados ao frontend | quantidade=%d",
                len(response.products),
            )
        logger.info(
            "[AI /chat perf] response_build_ms=%.2f",
            (perf_counter() - response_build_started_at) * 1000,
        )
        return response


def _get_session_conversation(session_id: str) -> list[SessionMessage]:
    session = _SESSION_HISTORY.get(session_id)
    return session["messages"][-_MAX_SESSION_MESSAGES:] if session else []


def _cleanup_inactive_sessions(now: datetime) -> None:
    expired_session_ids = [
        session_id
        for session_id, session in _SESSION_HISTORY.items()
        if now - session["last_interaction"] > _SESSION_TTL
    ]
    for session_id in expired_session_ids:
        del _SESSION_HISTORY[session_id]


def _store_session_turn(
    session_id: str,
    user_message: str,
    assistant_message: str,
    now: datetime,
) -> None:
    session = _SESSION_HISTORY.setdefault(
        session_id,
        {"messages": [], "last_interaction": now},
    )
    session["messages"].extend(
        [
            {"role": "user", "content": user_message},
            {"role": "assistant", "content": assistant_message},
        ]
    )
    session["messages"] = session["messages"][-_MAX_SESSION_MESSAGES:]
    session["last_interaction"] = now


def _message_digest(message: str) -> str:
    return hashlib.sha256(message.encode("utf-8")).hexdigest()[:12]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
