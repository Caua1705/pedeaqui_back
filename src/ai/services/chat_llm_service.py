import logging
from typing import Any

from langchain_openai import ChatOpenAI

from src.ai.prompts.chat_prompt import build_chat_prompt
from src.ai.schemas.chat_response_schema import ChatLLMResponse
from src.core.config import settings

logger = logging.getLogger("uvicorn.error")


class ChatLLMService:
    """LCEL chat service for Rapi structured responses."""

    def __init__(self) -> None:
        # O modelo sai do ambiente, no mesmo padrao de `EMBEDDING_MODEL` e
        # `VOICE_MODEL`: trocar de modelo — para testar um novo, ou para fugir
        # de uma indisponibilidade da OpenAI — nao pode exigir deploy. O
        # default de `MODEL_NAME` em `config.py` e o mesmo valor que estava
        # fixo aqui, entao nada muda para quem nao define a variavel.
        self.llm = ChatOpenAI(
            api_key=settings.OPENAI_API_KEY,
            model=settings.MODEL_NAME,
            reasoning_effort="minimal",
            verbosity="low",
            max_completion_tokens=300,
            use_responses_api=True,
        )

    def build_chain(self):
        """Build the Prompt -> ChatOpenAI structured output LCEL chain."""
        prompt = build_chat_prompt()
        structured_llm = self.llm.with_structured_output(ChatLLMResponse)
        return prompt | structured_llm

    def invoke(
        self,
        restaurant_context: str,
        conversation: list[dict[str, str]],
        retrieved_products: list[dict[str, Any]],
        user_message: str,
    ) -> ChatLLMResponse:
        """Invoke the LCEL chain and return a structured chat response."""
        chain = self.build_chain()
        logger.info("[AI LLM] Início da chamada ao LLM")
        response = chain.invoke(
            {
                "restaurant_context": restaurant_context,
                "conversation": conversation,
                "retrieved_products": retrieved_products,
                "user_message": user_message,
            }
        )
        logger.info("[AI LLM] Fim da chamada ao LLM")
        return response
