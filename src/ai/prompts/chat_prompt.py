from langchain_core.prompts import ChatPromptTemplate

from src.ai.prompts.system_prompt import SYSTEM_PROMPT


def build_chat_prompt() -> ChatPromptTemplate:
    """Build the chat prompt used by the Rapi LCEL chain.

    `Loja` e uma secao PROPRIA, e nao mais uma linha do bloco `Restaurante`.
    Junto, "Aberta agora: nao" viraria mais uma linha sobre a marca, e a secao
    ESTADO DA LOJA do prompt — que fala em "o bloco Loja" — nao teria a que
    apontar.

    A separacao tem uma segunda razao aqui: os dois blocos tem CICLOS DE VIDA
    diferentes. `restaurant_context` e cadastro e muda quando o lojista edita
    o painel; `branch_state` muda sozinho quando o relogio vira ou quando
    alguem no balcao aperta um botao. Num bloco so, nada no prompt diria qual
    das duas coisas o modelo esta lendo.
    """
    return ChatPromptTemplate.from_messages(
        [
            ("system", SYSTEM_PROMPT),
            (
                "human",
                """
Restaurante:
{restaurant_context}

Loja:
{branch_state}

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
