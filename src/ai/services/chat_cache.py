import re
import threading
import unicodedata
from copy import deepcopy
from dataclasses import dataclass
from time import monotonic
from typing import Generic, TypeVar

from src.ai.schemas.chat_response_schema import ChatResponse


T = TypeVar("T")
_CACHE_TTL_SECONDS = 300
_COMMON_MESSAGES = {
    "me recomenda um prato",
    "quero gastar ate r$ 50",
    "pedido para 2 pessoas",
    "nao estou com muita fome",
}


@dataclass
class _CacheEntry(Generic[T]):
    value: T
    expires_at: float


class ChatCache:
    def __init__(self, ttl_seconds: int = _CACHE_TTL_SECONDS) -> None:
        self.ttl_seconds = ttl_seconds
        self._embeddings: dict[str, _CacheEntry[list[float]]] = {}
        self._responses: dict[str, _CacheEntry[ChatResponse]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def normalize_message(message: str) -> str:
        normalized = unicodedata.normalize("NFKD", message.strip().lower())
        normalized = "".join(
            character
            for character in normalized
            if not unicodedata.combining(character)
        )
        return re.sub(r"\s+", " ", normalized)

    def key(self, restaurant_id: object, message: str) -> str:
        return f"{restaurant_id}:{self.normalize_message(message)}"

    def is_cacheable(self, message: str) -> bool:
        return self.normalize_message(message) in _COMMON_MESSAGES

    def get_embedding(self, key: str) -> list[float] | None:
        value = self._get(self._embeddings, key)
        return list(value) if value is not None else None

    def set_embedding(self, key: str, embedding: list[float]) -> None:
        self._set(self._embeddings, key, list(embedding))

    def get_response(self, key: str) -> ChatResponse | None:
        value = self._get(self._responses, key)
        return value.model_copy(deep=True) if value is not None else None

    def set_response(self, key: str, response: ChatResponse) -> None:
        self._set(self._responses, key, response.model_copy(deep=True))

    def _get(self, cache: dict[str, _CacheEntry[T]], key: str) -> T | None:
        now = monotonic()
        with self._lock:
            entry = cache.get(key)
            if entry is None:
                return None
            if entry.expires_at <= now:
                del cache[key]
                return None
            return deepcopy(entry.value)

    def _set(self, cache: dict[str, _CacheEntry[T]], key: str, value: T) -> None:
        now = monotonic()
        with self._lock:
            expired_keys = [
                cached_key
                for cached_key, entry in cache.items()
                if entry.expires_at <= now
            ]
            for expired_key in expired_keys:
                del cache[expired_key]
            cache[key] = _CacheEntry(
                value=deepcopy(value),
                expires_at=now + self.ttl_seconds,
            )


chat_cache = ChatCache()
