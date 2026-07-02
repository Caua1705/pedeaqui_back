import re
import threading
import unicodedata
from copy import deepcopy
from dataclasses import dataclass
from time import monotonic
from typing import Any, Generic, TypeVar


T = TypeVar("T")
_EMBEDDING_TTL_SECONDS = 60 * 60
_RETRIEVAL_TTL_SECONDS = 20 * 60


@dataclass
class _CacheEntry(Generic[T]):
    value: T
    expires_at: float


class ChatCache:
    def __init__(
        self,
        embedding_ttl_seconds: int = _EMBEDDING_TTL_SECONDS,
        retrieval_ttl_seconds: int = _RETRIEVAL_TTL_SECONDS,
    ) -> None:
        self.embedding_ttl_seconds = embedding_ttl_seconds
        self.retrieval_ttl_seconds = retrieval_ttl_seconds
        self._embeddings: dict[str, _CacheEntry[list[float]]] = {}
        self._retrievals: dict[str, _CacheEntry[list[dict[str, Any]]]] = {}
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

    def get_embedding(self, key: str) -> list[float] | None:
        value = self._get(self._embeddings, key)
        return list(value) if value is not None else None

    def set_embedding(self, key: str, embedding: list[float]) -> None:
        self._set(
            self._embeddings,
            key,
            list(embedding),
            self.embedding_ttl_seconds,
        )

    def get_retrieval(self, key: str) -> list[dict[str, Any]] | None:
        return self._get(self._retrievals, key)

    def set_retrieval(self, key: str, products: list[dict[str, Any]]) -> None:
        self._set(
            self._retrievals,
            key,
            products,
            self.retrieval_ttl_seconds,
        )

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

    def _set(
        self,
        cache: dict[str, _CacheEntry[T]],
        key: str,
        value: T,
        ttl_seconds: int,
    ) -> None:
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
                expires_at=now + ttl_seconds,
            )


chat_cache = ChatCache()
