"""Embedding da pergunta do cliente, para a busca vetorial.

O CLIENTE HTTP E UM SO NO PROCESSO, e isso e o que importa neste arquivo.

`RetrievalService.__init__` constroi um `EmbeddingService` por requisicao, e
enquanto cada um trazia um `OpenAIEmbeddings` proprio, cada requisicao abria
conexao nova com a OpenAI — handshake TLS incluso.

Medido em 15/08/2026, A/B intercalado de 12 pares (o intercalado importa: a
variacao da rede sozinha ia de 300 a 2000 ms, maior que o efeito procurado):

    objeto novo por requisicao   mediana 654 ms
    cliente compartilhado        mediana 340 ms
                                 -------------
    diferenca                            314 ms, em 11 dos 12 pares

Trezentos milissegundos por busca somem dentro dos segundos que a chamada ao
modelo custa, e e por isso que o ganho nao aparece no relogio de quem usa o
`/chat`. Ele aparece na conta: a busca roda em todo turno.

`lru_cache` e nao um objeto de modulo, pelo mesmo motivo de `get_engine` em
`src/db/session.py`: construido no import, ele congelaria `settings` no
instante em que qualquer modulo de `src` fosse importado — e a suite de teste
precisa poder apontar para outra configuracao depois disso.
"""

from functools import lru_cache

from langchain_openai import OpenAIEmbeddings

from src.core.config import settings


@lru_cache
def get_embeddings_client() -> OpenAIEmbeddings:
    """O cliente do processo. Um so, criado na primeira busca."""
    return OpenAIEmbeddings(
        api_key=settings.OPENAI_API_KEY,
        model=settings.EMBEDDING_MODEL,
    )


class EmbeddingService:
    """Generate text embeddings through OpenAI."""

    def __init__(self) -> None:
        self.embeddings = get_embeddings_client()

    def generate_embedding(self, text: str) -> list[float]:
        """Generate an embedding for a single text input."""
        return self.embeddings.embed_query(text)
