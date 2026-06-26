from typing import Any

from langchain_openai import ChatOpenAI

from src.ai.prompts.chat_prompt import build_chat_prompt
from src.ai.schemas.chat_response_schema import ChatResponse
from src.core.config import settings


class ChatLLMService:
    """LCEL chat service for Rapi structured responses."""

    def __init__(self) -> None:
        self.llm = ChatOpenAI(
            api_key=settings.OPENAI_API_KEY,
            model=settings.MODEL_NAME,
            temperature=0,
        )

    def build_chain(self):
        """Build the Prompt -> ChatOpenAI structured output LCEL chain."""
        prompt = build_chat_prompt()
        structured_llm = self.llm.with_structured_output(ChatResponse)
        return prompt | structured_llm

    def invoke(
        self,
        restaurant_context: str,
        conversation: list[dict[str, str]],
        retrieved_products: list[dict[str, Any]],
        user_message: str,
    ) -> ChatResponse:
        """Invoke the LCEL chain and return a structured chat response."""
        chain = self.build_chain()
        return chain.invoke(
            {
                "restaurant_context": restaurant_context,
                "conversation": conversation,
                "retrieved_products": retrieved_products,
                "user_message": user_message,
            }
        )
