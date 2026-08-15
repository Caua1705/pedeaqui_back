import logging
import re
import threading
import unicodedata
from copy import deepcopy
from dataclasses import dataclass
from time import monotonic
from typing import Any, Generic, TypeVar

from src.core.config import settings


logger = logging.getLogger("uvicorn.error")

T = TypeVar("T")
_EMBEDDING_TTL_SECONDS = 60 * 60
_RETRIEVAL_TTL_SECONDS = 20 * 60


@dataclass
class _CacheEntry(Generic[T]):
    value: T
    expires_at: float


class MenuGeneration:
    """Contador por restaurante que invalida o cache de busca quando o indice muda.

    Mora no Redis, e nao em memoria, por um motivo simples: quem MUDA o indice
    e o worker de reindex, que e outro processo, em outro container. Um
    contador em memoria nunca veria o incremento dele, e o cache de busca
    continuaria servindo o cardapio velho pelos 20 minutos do TTL — que e
    exatamente o atraso que a varredura automatica existe para eliminar.

    Sem `REDIS_URL` o contador fica parado em zero e nada quebra: o cache volta
    a ser so-TTL, que e o comportamento que ja existia antes deste arquivo
    mudar. Degradar em silencio e aceitavel aqui porque o TTL continua sendo o
    teto do erro — no pior caso o Rapi demora 20 minutos para conhecer o
    produto novo, em vez de nunca.
    """

    def __init__(self, redis_client=None) -> None:
        self.redis = redis_client
        if self.redis is not None:
            return
        if not settings.REDIS_URL:
            return
        try:
            import redis

            self.redis = redis.Redis.from_url(
                settings.REDIS_URL,
                decode_responses=True,
                socket_timeout=1,
                socket_connect_timeout=1,
            )
        except Exception:
            logger.warning("[AI cache] menu_generation_redis_initialization_failed=true")

    def current(self, restaurant_id: object) -> int:
        if self.redis is None:
            return 0
        try:
            value = self.redis.get(self._key(restaurant_id))
        except Exception:
            logger.warning("[AI cache] menu_generation_read_failed=true")
            return 0
        if value is None:
            return 0
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    def bump(self, restaurant_id: object) -> None:
        """Chamado pelo reindex depois de gravar. Falha aqui nao pode derrubar o worker.

        O custo de um incremento perdido e um cache de busca servindo o
        cardapio anterior ate o TTL — mesmo estado de quando nao ha Redis.
        Bem menor que o de abortar a indexacao por causa do cache.
        """
        if self.redis is None:
            return
        try:
            self.redis.incr(self._key(restaurant_id))
        except Exception:
            logger.warning("[AI cache] menu_generation_bump_failed=true")

    @staticmethod
    def _key(restaurant_id: object) -> str:
        return f"ai:menu_generation:{restaurant_id}"


menu_generation = MenuGeneration()


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

    def embedding_key(self, restaurant_id: object, message: str) -> str:
        """Chave do vetor da PERGUNTA. De proposito sem a geracao do cardapio.

        O embedding de "tem pizza vegana?" e o mesmo antes e depois de o
        lojista mexer no menu — a pergunta nao mudou. Botar a geracao nesta
        chave jogaria fora, a cada reindex, justamente as chamadas que custam
        dinheiro.
        """
        return f"{restaurant_id}:{self.normalize_message(message)}"

    def retrieval_key(self, restaurant_id: object, message: str) -> str:
        """Chave dos produtos encontrados. Com a geracao, porque o resultado envelhece.

        O reindex incrementa a geracao daquele restaurante e todas as entradas
        anteriores deixam de ser alcancaveis — sem varrer, sem apagar, sem
        precisar saber quais perguntas estavam guardadas. As antigas caem
        sozinhas quando o TTL vence.
        """
        generation = menu_generation.current(restaurant_id)
        return f"{restaurant_id}:g{generation}:{self.normalize_message(message)}"

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
