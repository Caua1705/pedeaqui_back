import json
import logging
from time import perf_counter
from typing import Any

from langchain_openai import ChatOpenAI

from src.ai.prompts.chat_prompt import build_chat_prompt
from src.ai.prompts.system_prompt import SYSTEM_PROMPT
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
        """Build the Prompt -> ChatOpenAI structured output LCEL chain.

        `include_raw=True` desde a medicao de 24/08/2026, e o motivo e o
        USAGE. Sem ele, `with_structured_output` devolve so o objeto ja
        validado e **joga fora a `AIMessage` crua** — que e onde vive
        `usage_metadata`. O resultado pratico era que o unico numero capaz de
        dizer quanto do prompt a gente esta pagando, e quantos tokens de
        RACIOCINIO o modelo gastou antes da primeira palavra, nao existia em
        lugar nenhum: nem na resposta, nem no log.

        O preco de ligar isso e que a saida da cadeia deixa de ser o objeto e
        passa a ser `{"raw", "parsed", "parsing_error"}` — e, principalmente,
        que **erro de parse deixa de ser excecao e vira campo**. `invoke`
        desfaz as duas coisas: desembrulha o `parsed` e levanta o
        `parsing_error`, para quem chama continuar vendo exatamente o que via.
        """
        prompt = build_chat_prompt()
        structured_llm = self.llm.with_structured_output(
            ChatLLMResponse, include_raw=True
        )
        return prompt | structured_llm

    def invoke(
        self,
        restaurant_context: str,
        conversation: list[dict[str, str]],
        retrieved_products: list[dict[str, Any]],
        user_message: str,
    ) -> ChatLLMResponse:
        """Invoke the LCEL chain and return a structured chat response."""
        chain_started_at = perf_counter()
        chain = self.build_chain()
        logger.info(
            "[AI /chat perf] llm_chain_build_ms=%.2f",
            (perf_counter() - chain_started_at) * 1000,
        )

        self._log_prompt_size(
            restaurant_context, conversation, retrieved_products, user_message
        )

        logger.info("[AI LLM] Início da chamada ao LLM")
        call_started_at = perf_counter()
        result = chain.invoke(
            {
                "restaurant_context": restaurant_context,
                "conversation": conversation,
                "retrieved_products": retrieved_products,
                "user_message": user_message,
            }
        )
        logger.info(
            "[AI /chat perf] llm_call_ms=%.2f",
            (perf_counter() - call_started_at) * 1000,
        )
        logger.info("[AI LLM] Fim da chamada ao LLM")

        # O usage e logado ANTES de qualquer checagem de parse: uma resposta
        # que nao validou custou os mesmos tokens de uma que validou, e e
        # justamente nela que saber quantos foram explica o porque.
        self._log_usage(result.get("raw"))
        return self._unwrap(result)

    @staticmethod
    def _unwrap(result: dict[str, Any]) -> ChatLLMResponse:
        """O objeto validado, com o erro de parse voltando a ser excecao.

        `include_raw=True` engole a falha de validacao e a entrega em
        `parsing_error`. Devolver isso adiante faria uma resposta invalida
        seguir como se fosse boa; levantar aqui mantem o comportamento de
        antes da instrumentacao, em que o `_answer` do `ChatService` via a
        excecao e o `chat()` a registrava com stack trace.
        """
        parsing_error = result.get("parsing_error")
        if parsing_error is not None:
            raise parsing_error

        parsed = result.get("parsed")
        if parsed is None:
            raise ValueError("O modelo nao devolveu uma resposta estruturada.")
        return parsed

    def _log_usage(self, raw: Any) -> None:
        """Os tokens que a OpenAI diz ter cobrado — nao os que a gente estima.

        `reasoning_tokens` e o campo que justifica esta funcao existir. Ele
        conta tokens que o modelo GERA antes de escrever a primeira palavra da
        resposta, e gerar e serial: cada um deles e tempo de espera do
        cliente, mesmo com `reasoning_effort="minimal"`.

        `cached_input_tokens` diz quanto do prompt a OpenAI serviu do cache
        dela. Prompt que comeca sempre igual (o system) tende a cachear; o que
        muda no comeco, nao. E o unico jeito de saber se vale reordenar as
        secoes do prompt.

        Falha aqui nao pode derrubar a resposta: o usage e observabilidade, e
        o cliente ja tem o texto dele. Um formato que mude do lado da OpenAI
        vira uma linha dizendo que nao veio, nao um 500.
        """
        usage = getattr(raw, "usage_metadata", None)
        if not usage:
            logger.info(
                "[AI /chat usage] usage_indisponivel=true | model=%s",
                self.llm.model_name,
            )
            return

        input_details = usage.get("input_token_details") or {}
        output_details = usage.get("output_token_details") or {}
        logger.info(
            "[AI /chat usage] model=%s | input_tokens=%s | cached_input_tokens=%s "
            "| output_tokens=%s | reasoning_tokens=%s | total_tokens=%s",
            self.llm.model_name,
            usage.get("input_tokens"),
            input_details.get("cache_read", 0),
            usage.get("output_tokens"),
            output_details.get("reasoning", 0),
            usage.get("total_tokens"),
        )

    @staticmethod
    def _log_prompt_size(
        restaurant_context: str,
        conversation: list[dict[str, str]],
        retrieved_products: list[dict[str, Any]],
        user_message: str,
    ) -> None:
        """De onde vem o tamanho do prompt, em caracteres, secao por secao.

        O usage diz QUANTO o prompt custou; isto diz QUAL PEDACO custou. Sao
        perguntas diferentes e a segunda e a acionavel: `system_chars` e fixo
        e so muda editando o prompt, `products_chars` muda mexendo no `top_k`
        ou no que `_format_retrieved_product` guarda, e `conversation_chars`
        cresce sozinho ate o teto de `_MAX_SESSION_MESSAGES` — e e o unico que
        faz o mesmo cliente ficar mais lento quanto mais ele conversa.

        Caracteres e nao tokens de proposito: contar token exigiria carregar o
        tiktoken so para o log, e a proporcao entre as secoes — que e o que se
        quer aqui — e a mesma nas duas unidades.
        """
        conversation_chars = sum(
            len(str(message.get("content", ""))) for message in conversation
        )
        products_chars = len(json.dumps(retrieved_products, ensure_ascii=False))
        logger.info(
            "[AI /chat prompt] system_chars=%d | context_chars=%d "
            "| conversation_chars=%d | conversation_messages=%d "
            "| products_chars=%d | products_count=%d | user_chars=%d",
            len(SYSTEM_PROMPT),
            len(restaurant_context),
            conversation_chars,
            len(conversation),
            products_chars,
            len(retrieved_products),
            len(user_message),
        )
