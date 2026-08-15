from langchain_core.prompts import ChatPromptTemplate

from src.ai.prompts.system_prompt import SYSTEM_PROMPT


def build_chat_prompt() -> ChatPromptTemplate:
    """Build the chat prompt used by the Rapi LCEL chain."""
    return ChatPromptTemplate.from_messages(
        [
            ("system", SYSTEM_PROMPT),
            (
                "human",
                """
Restaurante:
{restaurant_context}

Historico da sessao:
{conversation}

Produtos recuperados:
{retrieved_products}

Mensagem do usuario:
{user_message}
""",
            ),
        ]
    )
