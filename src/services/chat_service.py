import hashlib
import logging
import uuid
from datetime import datetime, timedelta, timezone
from time import perf_counter
from typing import TypedDict

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from src.ai.schemas.chat_response_schema import ChatResponse
from src.ai.services.chat_llm_service import ChatLLMService
from src.ai.services.retrieval_service import RetrievalService
from src.repositories.ai_feedback_repository import AIFeedbackRepository
from src.repositories.product_repository import ProductRepository
from src.repositories.restaurant_repository import RestaurantRepository
from src.schemas.ai_feedback_schema import AIFeedbackRequest, AIFeedbackResponse
from src.services.menu_service import MenuService
from src.utils.money import format_money_br
from src.utils.normalization import fold_for_match


logger = logging.getLogger("uvicorn.error")

_MAX_SESSION_MESSAGES = 20
_SESSION_TTL = timedelta(hours=1)
# Teto da descricao do restaurante dentro do prompt. Ver `_build_restaurant_context`.
_MAX_CONTEXT_DESCRIPTION = 300


class SessionMessage(TypedDict):
    role: str
    content: str


class SessionState(TypedDict):
    messages: list[SessionMessage]
    last_interaction: datetime


_SESSION_HISTORY: dict[str, SessionState] = {}


class ChatService:
    def __init__(self, db: Session, agent: str = "/chat"):
        """`agent` so existe para o LOG — ver `RetrievalService`.

        O experimento de voz reusa `retrieval_service` e `_hydrate_products`
        deste servico, entao as linhas de medicao dos dois agentes saiam com o
        mesmo prefixo `[AI /chat perf]`. Passando "/voz", cada um mede o
        proprio caminho sem que exista um segundo cronometro.

        O default mantem o chat de texto escrevendo exatamente o que escrevia:
        nenhum grep existente muda de resultado.
        """
        self.db = db
        self.agent = agent
        self.retrieval_service = RetrievalService(db, agent=agent)
        self.product_repository = ProductRepository(db)
        self.restaurant_repository = RestaurantRepository(db)

    def create_feedback(self, request: AIFeedbackRequest) -> AIFeedbackResponse:
        """Registra o voto do cliente sobre uma resposta do Rapi.

        O `success=True` so e dito DEPOIS do commit. Antes ele era devolvido
        sem depender de nada: quem commitava era o repositorio (contra a
        regra de camadas), o service nao controlava a transacao, e a resposta
        afirmava sucesso sobre uma escrita que ele nao tinha como conferir.

        Falha aqui sobe, e nao vira `success=False`: o cliente nao tem o que
        fazer com um voto recusado, e engolir a excecao esconderia do log a
        unica pista de que o feedback parou de ser gravado.
        """
        try:
            AIFeedbackRepository(self.db).create(request)
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

        return AIFeedbackResponse(success=True)

    def chat(
        self,
        restaurant_id: uuid.UUID,
        session_id: str,
        message: str,
    ) -> ChatResponse:
        started_at = perf_counter()
        # A mensagem do usuario e dado pessoal e nao vai para o log.
        # O digest permite correlacionar requisicoes sem expor o conteudo.
        logger.info(
            "[AI /chat] Nova requisicao | restaurant_id=%s | session_id=%s "
            "| message_chars=%d | message_digest=%s",
            restaurant_id,
            session_id,
            len(message),
            _message_digest(message),
        )

        # Barra restaurante inexistente/inativo antes de gastar chamada de
        # embedding e de LLM com um restaurant_id qualquer. Fica fora do try
        # para o 404 nao virar stack trace no log.
        #
        # A linha carregada daqui segue para `_answer`: e dela que sai o nome
        # da casa que vai no prompt, e reler o restaurante la seria uma
        # segunda consulta para o mesmo dado.
        restaurant = self._get_active_restaurant(restaurant_id)

        try:
            return self._answer(restaurant, session_id, message)
        except Exception:
            logger.exception(
                "[AI /chat] Erro no pipeline | restaurant_id=%s | session_id=%s",
                restaurant_id,
                session_id,
            )
            raise
        finally:
            logger.info(
                "[AI /chat perf] total_ms=%.2f",
                (perf_counter() - started_at) * 1000,
            )

    def _answer(
        self,
        restaurant,
        session_id: str,
        message: str,
    ) -> ChatResponse:
        """Busca, pergunta ao modelo, confere o que ele devolveu e responde.

        Separada do `chat` para que aquele fique com o que e enquadramento —
        o log de entrada, a barreira do restaurante e o tratamento de erro —
        e este com o pipeline. Junto, um `try` cobria as duas coisas e a
        leitura tinha de separa-las na cabeca.
        """
        # UM `now` para o turno inteiro: a limpeza das sessoes vencidas e o
        # registro deste turno precisam concordar sobre que horas sao, senao
        # uma sessao pode ser limpa e regravada no mesmo pedido.
        now = _utc_now()
        _cleanup_inactive_sessions(now)
        conversation = _get_session_conversation(session_id)
        logger.info("[AI /chat cache] final_response_cache_hit=false")

        retrieved_products = self.retrieval_service.retrieve_products(
            restaurant_id=restaurant.id,
            question=message,
        )
        llm_response = self._invoke_llm(
            restaurant_context=self._build_restaurant_context(restaurant),
            conversation=conversation,
            retrieved_products=retrieved_products,
            message=message,
        )
        selected_product_ids = self._validate_selected_product_ids(
            retrieved_products=retrieved_products,
            selected_product_ids=llm_response.selected_product_ids,
        )
        selected_product_ids = self._rescue_products_named_in_text(
            message=llm_response.message,
            retrieved_products=retrieved_products,
            selected_product_ids=selected_product_ids,
        )
        products = self._hydrate_products(restaurant.id, selected_product_ids)
        self._log_price_divergence(retrieved_products, products)
        response = self._build_response(
            llm_response=llm_response,
            selected_product_ids=selected_product_ids,
            products=products,
        )

        # Gravado SO depois da resposta pronta: uma falha no meio nao pode
        # deixar a pergunta no historico sem a resposta, senao ela envenena o
        # proximo prompt daquela sessao.
        _store_session_turn(session_id, message, response.message, now)
        logger.info(
            "[AI /chat] Retorno final | response_type=%s | products_count=%d",
            response.response_type,
            len(response.products),
        )
        return response

    def _get_active_restaurant(self, restaurant_id: uuid.UUID):
        restaurant = self.restaurant_repository.get_active_by_id(restaurant_id)
        if restaurant is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Restaurante não encontrado",
            )
        return restaurant

    @staticmethod
    def _build_restaurant_context(restaurant) -> str:
        """O que o atendente sabe sobre a casa onde ele trabalha.

        Era `f"restaurant_id={uuid}"` — um dado que o modelo nao tem como usar
        para nada. O prompt manda falar como funcionario da casa e citar o
        restaurante pelo nome; sem o nome aqui, essa instrucao nao tinha como
        ser cumprida, e o assistente falava de um restaurante que ele nao
        sabia qual era.

        NAO ha campo de tipo de cozinha no cadastro: `restaurants` tem name,
        slug, description, logos e cores, e mais nada. `description` e o texto
        livre que o lojista escreve sobre a casa, e e o mais proximo disso —
        ja e publico, sai em `RestaurantPublicResponse`.

        As duas defesas sobre a `description` existem porque ela e texto do
        LOJISTA entrando no prompt. O corte em `_MAX_CONTEXT_DESCRIPTION`
        impede que uma descricao longa empurre as instrucoes para longe e
        passe a competir com elas; o colapso das quebras de linha impede que
        ela finja ser uma secao nova do prompt. Nenhuma das duas transforma
        isso em texto confiavel — quem escreve ali manda no atendente da
        propria loja, e esse e o limite do estrago.
        """
        lines = [f"Nome do restaurante: {restaurant.name}"]

        description = " ".join((restaurant.description or "").split())
        if description:
            lines.append(f"Sobre a casa: {description[:_MAX_CONTEXT_DESCRIPTION]}")

        return "\n".join(lines)

    @staticmethod
    def _invoke_llm(
        restaurant_context: str,
        conversation: list[SessionMessage],
        retrieved_products: list[dict],
        message: str,
    ):
        llm_started_at = perf_counter()
        llm_response = ChatLLMService().invoke(
            restaurant_context=restaurant_context,
            conversation=conversation,
            retrieved_products=retrieved_products,
            user_message=message,
        )
        logger.info(
            "[AI /chat perf] llm_ms=%.2f",
            (perf_counter() - llm_started_at) * 1000,
        )
        logger.info(
            "[AI /chat] Structured Output | response_type=%s",
            llm_response.response_type,
        )
        return llm_response

    @staticmethod
    def _validate_selected_product_ids(
        retrieved_products: list[dict],
        selected_product_ids: list[uuid.UUID],
    ) -> list[uuid.UUID]:
        """So os ids que a BUSCA devolveu, sem repeticao e na ordem do modelo.

        E a unica coisa entre um produto que o modelo inventou e a tela do
        cliente. A ordem e a da ESCOLHA, e nao a da busca: e nela que o modelo
        explicou os produtos no texto, e trocar desalinha texto e carrossel.
        """
        started_at = perf_counter()

        # Os ids da BUSCA saem do nosso banco, entao aqui a conversao continua
        # estrita de proposito: id malformado vindo daqui seria defeito nosso,
        # e engoli-lo esconderia o defeito em vez de tratar entrada hostil.
        retrieved = {uuid.UUID(str(product["id"])) for product in retrieved_products}

        valid: list[uuid.UUID] = []
        for raw_id in selected_product_ids:
            product_id = _as_product_id(raw_id)
            if product_id is None:
                continue
            if product_id not in retrieved:
                continue
            if product_id in valid:
                continue
            valid.append(product_id)

        logger.info(
            "[AI /chat perf] selected_ids_validation_ms=%.2f",
            (perf_counter() - started_at) * 1000,
        )
        return valid

    @staticmethod
    def _rescue_products_named_in_text(
        message: str,
        retrieved_products: list[dict],
        selected_product_ids: list[uuid.UUID],
    ) -> list[uuid.UUID]:
        """O modelo citou produtos no texto e nao selecionou nenhum: salva a resposta.

        E o defeito visto em producao: texto com "**Pudim**, **Brownie** e
        **Torta de Limao**" em negrito e `selected_product_ids` vazio. Sem
        produto valido, `_response_type` devolve "text", e o cliente le a
        recomendacao sem nenhum cartao para tocar.

        SO age quando a selecao ficou VAZIA. Selecao nao vazia e escolha do
        modelo, e acrescentar produto ali seria adivinhar: em "nao temos
        **Pudim**, mas temos **Brownie**" o Pudim esta citado e nao deve virar
        cartao.

        A DIRECAO e o oposto de `_validate_selected_product_ids`, e por isso as
        duas convivem. Aquela existe para NAO confiar no modelo: id inventado
        nao vira cartao, e nada aqui a enfraquece — o resgate so alcanca
        produto que a NOSSA busca devolveu e que o proprio modelo escreveu no
        texto. Nao ha nada para inventar, e o pior caso e um cartao a mais de
        um produto cujo nome o cliente acabou de ler.
        """
        if selected_product_ids:
            return selected_product_ids

        rescued = _products_named_in(message, retrieved_products)
        if not rescued:
            return selected_product_ids

        logger.warning(
            "[AI /chat] o modelo citou %d produto(s) no texto e nao selecionou nenhum. "
            "Resgatados pelo nome.",
            len(rescued),
        )
        return rescued

    @staticmethod
    def _log_price_divergence(retrieved_products: list[dict], products: list) -> None:
        """Avisa quando o preco que o modelo VIU nao e o que o cartao MOSTRA.

        Os dois saem da mesma tabela `products`, em leituras diferentes: a do
        contexto acontece antes da chamada ao modelo, a do cartao depois. No
        meio cabe cerca de um segundo, e uma alteracao de preco salva pelo
        lojista nessa janela e a unica divergencia que sobra depois de o preco
        ter saido do cache de busca.

        Nao corrige, de proposito. O cartao ja esta certo — ele vem do banco —
        e reescrever a frase do modelo exigiria adivinhar qual pedaco do texto
        falava daquele preco. O que esta linha faz e tornar o caso VISIVEL:
        sem ela, a unica testemunha da divergencia seria o cliente.
        """
        shown_to_model = {
            str(product["id"]): product.get("price") for product in retrieved_products
        }
        for product in products:
            on_card = format_money_br(product.price)
            in_context = shown_to_model.get(str(product.id))
            if in_context is None or in_context == on_card:
                continue
            logger.warning(
                "[AI /chat] preco divergente entre o texto e o cartao | product_id=%s "
                "| contexto=%s | cartao=%s",
                product.id,
                in_context,
                on_card,
            )

    def _hydrate_products(
        self,
        restaurant_id: uuid.UUID,
        selected_product_ids: list[uuid.UUID],
    ):
        hydration_started_at = perf_counter()
        products_by_id = {
            uuid.UUID(str(product.id)): product
            for product in self.product_repository.list_active_by_ids(
                restaurant_id,
                selected_product_ids,
            )
        }
        logger.info(
            "[AI %s perf] hydration_ms=%.2f",
            self.agent,
            (perf_counter() - hydration_started_at) * 1000,
        )
        return [
            MenuService.product_response(products_by_id[product_id])
            for product_id in selected_product_ids
            if product_id in products_by_id
        ]

    @staticmethod
    def _build_response(
        llm_response,
        selected_product_ids: list[uuid.UUID],
        products: list,
    ) -> ChatResponse:
        started_at = perf_counter()
        response = ChatResponse(
            response_type=ChatService._response_type(llm_response, selected_product_ids, products),
            message=llm_response.message,
            products=products,
        )
        if response.products:
            logger.info(
                "[AI /chat] Produtos enviados ao frontend | quantidade=%d",
                len(response.products),
            )
        logger.info(
            "[AI /chat perf] response_build_ms=%.2f",
            (perf_counter() - started_at) * 1000,
        )
        return response

    @staticmethod
    def _response_type(
        llm_response,
        selected_product_ids: list[uuid.UUID],
        products: list,
    ) -> str:
        """Quem manda e o que SOBROU da validacao, nao o que o modelo disse.

        Havendo produto para mostrar, a resposta e de produtos mesmo que o
        modelo tenha dito "text". E o contrario tambem: "products" com a lista
        vazia — todos os ids eram inventados — deixaria a tela com um
        carrossel sem nada dentro, entao vira "text".
        """
        if products:
            return "products"

        if llm_response.response_type == "products":
            logger.warning(
                "[AI /chat] Nenhum produto valido para response_type=products "
                "| selected=%d | validados=%d",
                len(llm_response.selected_product_ids),
                len(selected_product_ids),
            )
            return "text"

        return llm_response.response_type


def _products_named_in(message: str, retrieved_products: list[dict]) -> list[uuid.UUID]:
    """Ids dos produtos da busca cujo nome aparece no texto, na ordem do texto.

    A comparacao e achatada (`fold_for_match`): o modelo escreve
    "**Torta de Limão**", o banco guarda "Torta de limao", e sem achatar os
    DOIS lados nada casa. Os asteriscos do negrito nao atrapalham — o nome
    continua sendo substring de `**nome**`.

    Os nomes sao procurados do MAIS LONGO para o mais curto, e o trecho que
    casa e apagado do texto. Sem isso, "**Coca-Cola Zero**" casaria tambem com
    "Coca-Cola" e o cliente receberia dois cartoes por um unico nome citado.
    """
    text = fold_for_match(message)
    found: list[tuple[int, uuid.UUID]] = []

    for name, product_id in _names_longest_first(retrieved_products):
        position = text.find(name)
        if position < 0:
            continue
        found.append((position, product_id))
        text = text[:position] + (" " * len(name)) + text[position + len(name):]

    found.sort(key=lambda item: item[0])
    return [product_id for _, product_id in found]


def _names_longest_first(retrieved_products: list[dict]) -> list[tuple[str, uuid.UUID]]:
    names: list[tuple[str, uuid.UUID]] = []
    for product in retrieved_products:
        product_id = _as_product_id(product.get("id"))
        name = fold_for_match(str(product.get("name") or ""))
        if product_id is None or not name:
            continue
        names.append((name, product_id))

    names.sort(key=lambda item: len(item[0]), reverse=True)
    return names


def _as_product_id(raw_id) -> uuid.UUID | None:
    """O id escolhido pelo modelo, ou None se nao for um uuid.

    `_validate_selected_product_ids` existe para NAO confiar no modelo — e
    confiava num caso: que o texto que ele devolve e um uuid bem formado. Id
    inventado mas bem formado era descartado em silencio; id malformado
    ("produto-1", "o primeiro") levantava ValueError no meio da requisicao e
    o cliente recebia 500.

    Os dois sao a mesma coisa — o modelo devolveu algo que nao aponta para
    produto nenhum — e por isso passam a ter o mesmo destino. O log fica
    porque a frequencia disso diz se o prompt precisa de conserto: nao e
    erro do cliente, e o modelo desobedecendo o formato pedido.
    """
    try:
        return uuid.UUID(str(raw_id))
    except (ValueError, AttributeError, TypeError):
        logger.warning(
            "[AI /chat] o modelo devolveu um id que nao e uuid: %r. Descartado.",
            raw_id,
        )
        return None


def _get_session_conversation(session_id: str) -> list[SessionMessage]:
    session = _SESSION_HISTORY.get(session_id)
    return session["messages"][-_MAX_SESSION_MESSAGES:] if session else []


def _cleanup_inactive_sessions(now: datetime) -> None:
    expired_session_ids = [
        session_id
        for session_id, session in _SESSION_HISTORY.items()
        if now - session["last_interaction"] > _SESSION_TTL
    ]
    for session_id in expired_session_ids:
        del _SESSION_HISTORY[session_id]


def _store_session_turn(
    session_id: str,
    user_message: str,
    assistant_message: str,
    now: datetime,
) -> None:
    session = _SESSION_HISTORY.setdefault(
        session_id,
        {"messages": [], "last_interaction": now},
    )
    session["messages"].extend(
        [
            {"role": "user", "content": user_message},
            {"role": "assistant", "content": assistant_message},
        ]
    )
    session["messages"] = session["messages"][-_MAX_SESSION_MESSAGES:]
    session["last_interaction"] = now


def _message_digest(message: str) -> str:
    return hashlib.sha256(message.encode("utf-8")).hexdigest()[:12]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
