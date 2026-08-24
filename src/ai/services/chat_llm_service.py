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
from uuid import UUID

from langchain_core.utils.json import parse_partial_json
from langchain_openai import ChatOpenAI
from pydantic import ValidationError

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
        max_completion_tokens=settings.AI_MAX_COMPLETION_TOKENS,
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
        branch_state: str,
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
            restaurant_context,
            branch_state,
            conversation,
            retrieved_products,
            user_message,
        )

        logger.info("[AI LLM] Início da chamada ao LLM")
        call_started_at = perf_counter()
        try:
            result = chain.invoke(
                {
                    "restaurant_context": restaurant_context,
                    "branch_state": branch_state,
                    "conversation": conversation,
                    "retrieved_products": retrieved_products,
                    "user_message": user_message,
                }
            )
        except ValidationError as erro:
            # Resposta cortada no teto. Nao ha `result` nenhum aqui — nem
            # `raw`, nem usage: a excecao sobe de dentro do SDK da OpenAI e
            # leva a resposta crua junto. Por isso o cronometro e fechado a
            # mao antes do resgate, senao um turno cortado sairia do log sem
            # `llm_call_ms` e sumiria da medicao.
            logger.info(
                "[AI /chat perf] llm_call_ms=%.2f",
                (perf_counter() - call_started_at) * 1000,
            )
            resgatada = self._resgatar_resposta_cortada(erro)
            if resgatada is None:
                raise
            return resgatada
        logger.info(
            "[AI /chat perf] llm_call_ms=%.2f",
            (perf_counter() - call_started_at) * 1000,
        )
        logger.info("[AI LLM] Fim da chamada ao LLM")

        # O usage e logado ANTES de qualquer checagem de parse: uma resposta
        # que nao validou custou os mesmos tokens de uma que validou, e e
        # justamente nela que saber quantos foram explica o porque.
        self._log_usage(result.get("raw"))
        self._avisar_se_bateu_no_teto(result.get("raw"))
        return self._unwrap(result)

    def _resgatar_resposta_cortada(self, erro: ValidationError) -> ChatLLMResponse | None:
        """O texto que sobrou de um JSON cortado no meio, ou None.

        O QUE ISTO IMPEDE. Em 24/08/2026, no build `1ca8708`, o `/chat` caiu
        em producao com `Invalid JSON: EOF while parsing a string at line 1
        column 629`: a resposta bateu no `max_completion_tokens` no meio da
        lista de `selected_product_ids`, o JSON chegou cortado e o cliente viu
        erro na tela. O modelo tinha escrito a resposta INTEIRA em texto — so
        a lista de ids ficou pela metade — e nos jogamos tudo fora.

        Perder os cartoes e um arranhao; perder o turno e o cliente indo
        embora. Entao o turno degrada: o texto vai, os cartoes vao se ainda
        derem, e nada estoura.

        POR QUE ISTO NAO MORA NO `_unwrap`. Era la que parecia o lugar, porque
        e la que `parsing_error` e tratado. Mas neste modo de falha
        `parsing_error` **nunca e preenchido**: a `ValidationError` e levantada
        DENTRO do SDK da OpenAI, em `parse_response`, antes de o
        `include_raw=True` do langchain ter o que embrulhar. Ela sobe pelo
        `chain.invoke` inteiro. Por isso o resgate e um `except` em volta da
        chamada, e nao um `if` no retorno dela — medido em 24/08/2026 contra a
        API de verdade, forcando o corte com o teto em 140.

        O JSON parcial vem DA PROPRIA EXCECAO: `errors()[0]["input"]` e a
        string que o Pydantic tentou validar. E o unico lugar onde ela existe,
        porque a resposta crua morreu junto com a excecao.

        OS IDS BEM FORMADOS SAO PRESERVADOS, e o cortado nao. Um uuid pela
        metade nao vira `UUID` e `ChatLLMResponse` recusaria o objeto inteiro —
        o resgate falharia tentando resgatar. Os completos sao legitimos: o
        modelo os escolheu e eles ainda passam por
        `_validate_selected_product_ids` do lado de fora, que continua sendo a
        unica coisa entre uma alucinacao e a tela.

        `response_type` sai como "text" e nao como "products" mesmo havendo
        ids, porque quem decide isso e `ChatService._response_type`, olhando o
        que SOBROU da validacao. Dizer "products" aqui seria opinar sobre uma
        lista que ainda nao foi conferida.

        Devolve None quando nao ha o que resgatar — JSON cortado antes mesmo
        da `message`, ou erro que nao e de JSON invalido. Ai o chamador levanta
        a excecao original, que e o comportamento de antes: uma falha que o
        resgate nao cobre nao pode virar resposta vazia com cara de sucesso.
        """
        detalhes = erro.errors()
        if not detalhes or detalhes[0].get("type") != "json_invalid":
            return None

        parcial = detalhes[0].get("input")
        if not isinstance(parcial, str) or not parcial:
            return None

        try:
            recuperado = parse_partial_json(parcial)
        except Exception:
            return None

        if not isinstance(recuperado, dict):
            return None

        mensagem = recuperado.get("message")
        if not isinstance(mensagem, str) or not mensagem.strip():
            return None

        ids = []
        for bruto in recuperado.get("selected_product_ids") or []:
            try:
                ids.append(UUID(str(bruto)))
            except (ValueError, AttributeError, TypeError):
                # O ultimo id da lista e o que costuma vir cortado. Ele para
                # aqui em silencio de proposito: nao e o modelo desobedecendo
                # o formato, e a frase acabando no meio.
                continue

        logger.warning(
            "[AI /chat] RESPOSTA CORTADA E RESGATADA | json_chars=%d "
            "| message_chars=%d | ids_recuperados=%d | teto=%d | model=%s. "
            "O turno foi entregue sem estourar; suba AI_MAX_COMPLETION_TOKENS "
            "se isto se repetir.",
            len(parcial),
            len(mensagem),
            len(ids),
            settings.AI_MAX_COMPLETION_TOKENS,
            self.llm.model_name,
        )
        return ChatLLMResponse(
            message=mensagem,
            response_type="text",
            selected_product_ids=ids,
        )

    def _avisar_se_bateu_no_teto(self, raw: Any) -> None:
        """Uma linha alta quando a resposta encostou no `max_completion_tokens`.

        ESTA LINHA FALTAVA NO DIA DO INCIDENTE. Bater no teto nao tinha
        sintoma proprio: aparecia como `ValidationError` de JSON invalido, que
        parece defeito do modelo ou do schema, e nao "o orcamento acabou". A
        primeira hipotese levantada foi a errada por isso.

        A comparacao e com `output_tokens`, e nao com o tamanho da `message`,
        e e essa a licao do incidente: **o teto e cobrado sobre o que o modelo
        GERA, e o texto que o cliente le e menos da metade disso.** Medido em
        24/08/2026 contra a API: `output_tokens=129` para uma `message` de 41
        tokens, porque os dois uuids de `selected_product_ids` custaram 52 e o
        andaime do structured output, 25. **O uuid custa 26 tokens.**

        Nao e raciocinio — foi a primeira hipotese e ela nao se sustentou:
        `reasoning` veio 0 em todas as chamadas medidas, e a soma fecha sem
        ele. Ver a conta inteira em `Settings.AI_MAX_COMPLETION_TOKENS`.

        Os 90% existem porque bater no teto EXATO e raro: o corte acontece
        alguns tokens antes, quando o proximo campo nao cabe. Avisar so no
        numero cheio deixaria passar justamente o turno que quase quebrou.
        """
        try:
            usage = getattr(raw, "usage_metadata", None) or {}
            saida = usage.get("output_tokens")
            teto = settings.AI_MAX_COMPLETION_TOKENS
            if saida is None or saida < teto * 0.9:
                return
            logger.warning(
                "[AI /chat] RESPOSTA NO LIMITE DO TETO | output_tokens=%s "
                "| teto=%d. O proximo turno parecido chega cortado.",
                saida,
                teto,
            )
        except Exception:
            logger.warning("[AI /chat] aviso_de_teto_falhou=true", exc_info=True)

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
        branch_state: str,
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

        `branch_state_chars` entrou junto com a secao "Loja" (24/08/2026), e
        nao e decorativo: ele e o unico pedaco do prompt que MUDA SOZINHO, sem
        ninguem editar nada. Uma loja que fecha faz a linha crescer, e sem
        medida propria esse crescimento apareceria como "o contexto aumentou"
        sem dizer qual dos dois blocos aumentou.

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
                "| branch_state_chars=%d "
                "| conversation_chars=%d | conversation_messages=%d "
                "| products_chars=%d | products_count=%d | user_chars=%d",
                len(SYSTEM_PROMPT),
                len(restaurant_context),
                len(branch_state),
                len(str(conversation)),
                len(conversation),
                len(str(retrieved_products)),
                len(retrieved_products),
                len(user_message),
            )
        except Exception:
            logger.warning("[AI /chat prompt] prompt_size_log_falhou=true", exc_info=True)
