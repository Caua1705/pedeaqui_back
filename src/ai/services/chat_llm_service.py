"""A cadeia do Rapi de texto, e o cliente HTTP que ela reusa.

O CLIENTE HTTP E UM SO POR MODELO NO PROCESSO, e isso e o que importa neste
arquivo. E o mesmo conserto que `embedding_service.py` ja fez para a busca,
pelo mesmo motivo e com a mesma forma — la esta a medicao original (mediana
de 654 ms para 340 ms, 314 ms de diferenca em 11 de 12 pares intercalados).

O caminho do LLM tinha ficado de fora daquele conserto: `ChatLLMService()` e
construido por REQUISICAO, e cada construcao trazia um `ChatOpenAI` novo, com
um cliente HTTP novo, com um pool de conexoes novo — ou seja, handshake TLS
com a OpenAI em toda pergunta do cliente.

Medido em 24/08/2026, em producao, sobre oito turnos quentes:

    llm_call_ms = 1023 ms fixos + 14,2 ms por token de saida   (R2 = 0,94)

Os 1023 ms sao o que se paga ANTES do primeiro token, e o handshake estava
dentro deles. Contra a ENTRADA a mesma regressao da R2 = 0,19 — o tamanho do
prompt nao explica a espera, o custo fixo e o comprimento da resposta
explicam.

A CHAVE DO CACHE E O NOME DO MODELO, e nao "nenhuma chave". `MODEL_NAME` sai
do ambiente justamente para poder mudar sem deploy; um cache sem chave
congelaria o primeiro modelo visto no processo e a variavel voltaria a ser
configuracao morta — que e o defeito que `test_chat_llm_model.py` existe para
impedir.

`lru_cache` e nao um objeto de modulo, pelo mesmo motivo de `get_engine` em
`src/db/session.py`: construido no import, ele congelaria `settings` no
instante em que qualquer modulo de `src` fosse importado.
"""

import logging
from functools import lru_cache
from time import perf_counter
from typing import Any

from langchain_openai import ChatOpenAI

from src.ai.prompts.chat_prompt import build_chat_prompt
from src.ai.prompts.system_prompt import SYSTEM_PROMPT
from src.ai.schemas.chat_response_schema import ChatLLMResponse
from src.core.config import settings

logger = logging.getLogger("uvicorn.error")


@lru_cache
def get_chat_client(model: str) -> ChatOpenAI:
    """O cliente daquele modelo no processo. Um so, criado na primeira pergunta."""
    return ChatOpenAI(
        api_key=settings.OPENAI_API_KEY,
        model=model,
        reasoning_effort="minimal",
        verbosity="low",
        max_completion_tokens=300,
        use_responses_api=True,
    )


class ChatLLMService:
    """LCEL chat service for Rapi structured responses."""

    def __init__(self) -> None:
        # O modelo sai do ambiente, no mesmo padrao de `EMBEDDING_MODEL` e
        # `VOICE_MODEL`: trocar de modelo — para testar um novo, ou para fugir
        # de uma indisponibilidade da OpenAI — nao pode exigir deploy. O
        # default de `MODEL_NAME` em `config.py` e o mesmo valor que estava
        # fixo aqui, entao nada muda para quem nao define a variavel.
        self.llm = get_chat_client(settings.MODEL_NAME)

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
        try:
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
        except Exception:
            logger.warning("[AI /chat usage] usage_log_falhou=true", exc_info=True)

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

        A MEDIDA E `str()`, E ISSO NAO E DETALHE. Este log ja derrubou o
        `/chat` em producao uma vez, com `json.dumps`: os produtos vem da
        busca com `id` em `UUID` (e `similarity_search` que devolve assim, e e
        por isso que `_validate_selected_product_ids` faz `uuid.UUID(str(...))`),
        e o `json.dumps` recusa `UUID` com `TypeError`. Um log de medicao
        levou o caminho quente junto.

        `str()` conserta as DUAS coisas de uma vez, e a segunda e a que
        importa mais: alem de nao ter como falhar sobre `UUID`, ela e a
        medida CERTA. O `ChatPromptTemplate` interpola `{retrieved_products}`
        e `{conversation}` com `str()`, entao o que entra no prompt e o `repr`
        da lista de dicionarios — nao JSON. O `json.dumps` media um texto que
        nunca foi enviado a OpenAI.
        """
        try:
            logger.info(
                "[AI /chat prompt] system_chars=%d | context_chars=%d "
                "| conversation_chars=%d | conversation_messages=%d "
                "| products_chars=%d | products_count=%d | user_chars=%d",
                len(SYSTEM_PROMPT),
                len(restaurant_context),
                len(str(conversation)),
                len(conversation),
                len(str(retrieved_products)),
                len(retrieved_products),
                len(user_message),
            )
        except Exception:
            logger.warning("[AI /chat prompt] prompt_size_log_falhou=true", exc_info=True)
