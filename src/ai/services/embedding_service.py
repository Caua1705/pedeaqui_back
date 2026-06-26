from langchain_openai import OpenAIEmbeddings

from src.core.config import settings


class EmbeddingService:
    """Generate text embeddings through OpenAI."""

    def __init__(self) -> None:
        self.embeddings = OpenAIEmbeddings(
            api_key=settings.OPENAI_API_KEY,
            model=settings.EMBEDDING_MODEL,
        )

    def generate_embedding(self, text: str) -> list[float]:
        """Generate an embedding for a single text input."""
        return self.embeddings.embed_query(text)
