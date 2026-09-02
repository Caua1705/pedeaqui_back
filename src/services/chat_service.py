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
from src.ai.services.greeting import greeting_reply, is_greeting
from src.ai.services.retrieval_service import RetrievalService
from src.core.config import settings
from src.repositories.ai_feedback_repository import AIFeedbackRepository
from src.repositories.branch_repository import BranchRepository
from src.repositories.menu_repository import MenuRepository
from src.repositories.product_repository import ProductRepository
from src.repositories.restaurant_repository import RestaurantRepository
from src.schemas.admin_settings_schema import MAX_ASSISTANT_NOTES_LENGTH
from src.schemas.ai_feedback_schema import AIFeedbackRequest, AIFeedbackResponse
from src.services.ai_usage_service import AIUsageService
from src.services.branch_hours_service import BRANCH_TIMEZONE, BranchHoursService
from src.services.branch_operation import resolve_branch_operation, resolver_atendimento
from src.services.menu_service import MenuService
from src.utils.money import format_money_br
from src.utils.normalization import fold_for_match


logger = logging.getLogger("uvicorn.error")

_MAX_SESSION_MESSAGES = 20
_SESSION_TTL = timedelta(hours=1)

# O teto de cartoes por turno. O MESMO numero esta escrito no `SYSTEM_PROMPT`,
# na secao "NO MAXIMO 3 PRODUTOS POR RESPOSTA", e e aquele que decide o que o
# cliente LE — este aqui so decide quantos cartoes saem. Ver `_limitar_cartoes`.
_MAX_CARTOES = 3


class SessionMessage(TypedDict):
    role: str
    content: str


class SessionState(TypedDict):
    messages: list[SessionMessage]
    last_interaction: datetime


_SESSION_HISTORY: dict[str, SessionState] = {}

# Ver `_take_cold_start_flag`.
_cold_start_pending = True

# Ver `_contar_turno_com_llm`.
_resgates_por_nome = 0
_turnos_com_llm = 0

# Ver `_contar_cartao_do_turno`.
_turnos_com_contexto = 0
_turnos_sem_cartao = 0

# `BranchClosedReason` em portugues, para o prompt. As chaves sao o vocabulario
# de `resolver_atendimento`, e a traducao mora aqui e nao la porque o outro
# consumidor daquela funcao — a tela de escolha de filial — quer o codigo cru:
# quem escreve a frase para o cliente ali e o app, e ele a escreve num contexto
# que este prompt nao tem.
#
# Sem `KeyError` possivel: `resolver_atendimento` so devolve motivo quando
# `is_open_now` e falso, e so devolve estes dois.
_MOTIVO_DE_FECHADA = {
    "outside_business_hours": "fora do horario de funcionamento",
    "branch_paused": "a loja pausou o atendimento",
}


# Por quanto tempo o voto do cliente sobre uma resposta do Rapi continua no
# banco. Ver `feedback_retention_cutoff`.
FEEDBACK_RETENTION_DAYS = 90


def feedback_retention_cutoff(now: datetime) -> datetime:
    """Antes deste instante, a linha de `ai_feedback` tem que sair.

    O QUE ISTO FECHA. `ai_feedback.user_message` guarda **em texto puro** o
    que a pessoa digitou para o Rapi, e ela digita coisas como "moro na rua
    X, 200, meu telefone e ...". A tabela nao tem `customer_id`, entao a
    exclusao de conta (`CustomerAnonymizationService`) nunca alcancou essas
    linhas: a pessoa pedia exclusao, o sistema anonimizava tudo, e o texto
    dela ficava. Era o maior residuo nomeado em
    `docs/lgpd-fase2-exclusao-de-conta.md`, secao 6.

    POR QUE RETENCAO E NAO `customer_id`. `POST /chat` e `POST /chat/feedback`
    **nao tem autenticacao nenhuma**, nem opcional — o Rapi existe para quem
    ainda nao tem conta. Nao ha cliente na requisicao para gravar na coluna,
    entao ela nasceria sempre NULL e a anonimizacao continuaria sem alcancar
    nada, so que agora com uma coluna que parece resolver. Autenticar o chat
    para preenche-la seria fechar a porta que o produto quer aberta.

    E a retencao alcanca MAIS: o texto de quem nunca criou conta some
    tambem, e esse a anonimizacao por definicao jamais cobriria.

    POR QUE 90 DIAS. E o que a linha serve para depois de gravada: amostra
    de qualidade das respostas ("o Rapi piorou desde a mudanca do prompt?").
    Nenhuma rota le esta tabela hoje, entao o custo de encurtar e baixo e o
    de esticar e dado pessoal parado. Um trimestre cobre a comparacao
    antes/depois de um deploy sem guardar a conversa do ano passado.
    """
    return now - timedelta(days=FEEDBACK_RETENTION_DAYS)


class ChatService:
    def __init__(self, db: Session, agent: str = "/chat"):
        """`agent` so existe para o LOG — ver `RetrievalService`.

        O agente de voz reusa `retrieval_service`, `_get_active_restaurant`,
        `_get_active_branch` e `_hydrate_products` deste servico, entao as
        linhas de medicao dos dois agentes saiam com o mesmo prefixo
        `[AI /chat perf]`. Passando "/voz", cada um mede o proprio caminho sem
        que exista um segundo cronometro.

        O default mantem o chat de texto escrevendo exatamente o que escrevia:
        nenhum grep existente muda de resultado.
        """
        self.db = db
        self.agent = agent
        self.retrieval_service = RetrievalService(db, agent=agent)
        self.product_repository = ProductRepository(db)
        self.restaurant_repository = RestaurantRepository(db)
        self.branch_repository = BranchRepository(db)
        # Os dois que o estado da loja precisa. Atributos, e nao construidos
        # dentro do `_answer`, pelo mesmo motivo dos de cima: e assim que o
        # teste sem banco troca por dublê depois do `__init__`.
        self.branch_hours_service = BranchHoursService(db)
        self.menu_repository = MenuRepository(db)

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
        branch_id: uuid.UUID,
        session_id: str,
        message: str,
    ) -> ChatResponse:
        started_at = perf_counter()
        cold_start = _take_cold_start_flag()
        # A mensagem do usuario e dado pessoal e nao vai para o log.
        # O digest permite correlacionar requisicoes sem expor o conteudo.
        #
        # `git_sha` abre a linha, e nao fecha, porque e a primeira pergunta de
        # toda medicao: de que build veio ESTE turno? Uma bateria de producao
        # so vale contra um commit conhecido — em 24/08/2026 uma delas mediu o
        # codigo anterior por meia hora, porque tres commits tinham ficado sem
        # `push` e nenhuma linha do log dizia isso.
        #
        # Repetir a versao em toda requisicao parece redundante com a linha de
        # boot, e nao e: o log de producao e recortado por janela de tempo, e o
        # recorte quase nunca alcanca o boot. Sete caracteres por turno e o
        # preco de o recorte se bastar.
        logger.info(
            "[AI /chat] Nova requisicao | git_sha=%s | restaurant_id=%s "
            "| branch_id=%s | session_id=%s | message_chars=%d | message_digest=%s",
            settings.GIT_SHA,
            restaurant_id,
            branch_id,
            session_id,
            len(message),
            _message_digest(message),
        )
        # A primeira requisicao do processo paga tudo que e construido sob
        # demanda e depois fica guardado: a conexao com o Postgres, o cliente
        # HTTP da OpenAI do embedding (`get_embeddings_client`, com
        # `lru_cache`) e o handshake TLS de cada um. Sem esta marca, o turno
        # frio se mistura aos quentes na mesma amostra e o piso medido sai
        # maior do que e — foi o que fez "oi" parecer a pergunta mais lenta.
        logger.info("[AI /chat perf] cold_start=%s", str(cold_start).lower())

        # Barra restaurante inexistente/inativo antes de gastar chamada de
        # embedding e de LLM com um restaurant_id qualquer. Fica fora do try
        # para o 404 nao virar stack trace no log.
        #
        # A linha carregada daqui segue para `_answer`: e dela que sai o nome
        # da casa que vai no prompt, e reler o restaurante la seria uma
        # segunda consulta para o mesmo dado.
        # Estas duas consultas sao o PRIMEIRO toque no banco da requisicao, e
        # e por isso que elas sao cronometradas separadas em vez de juntas num
        # `guard_ms`. A primeira paga o checkout do pool — que num processo
        # recem-subido significa abrir conexao com o Postgres do zero (TCP +
        # TLS), e a cada checkout significa o round trip do `pool_pre_ping`.
        # A segunda ja pega a conexao quente. A diferenca entre as duas, no
        # primeiro turno do processo, e exatamente o que a conexao custou.
        guard_started_at = perf_counter()
        restaurant_started_at = perf_counter()
        restaurant = self._get_active_restaurant(restaurant_id)
        logger.info(
            "[AI /chat perf] restaurant_lookup_ms=%.2f",
            (perf_counter() - restaurant_started_at) * 1000,
        )

        branch_started_at = perf_counter()
        branch = self._get_active_branch(restaurant.id, branch_id)
        logger.info(
            "[AI /chat perf] branch_lookup_ms=%.2f",
            (perf_counter() - branch_started_at) * 1000,
        )
        guard_ms = (perf_counter() - guard_started_at) * 1000

        try:
            return self._answer(restaurant, branch, session_id, message, guard_ms)
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
        branch,
        session_id: str,
        message: str,
        guard_ms: float,
    ) -> ChatResponse:
        """Busca, pergunta ao modelo, confere o que ele devolveu e responde.

        `guard_ms` chega de fora e serve so para a linha de fechamento. Ele e
        medido no `chat`, que e onde a barreira do restaurante e da filial
        acontece, e sem ele a soma das etapas nao bateria com o `total_ms` —
        faltaria justamente a primeira consulta ao banco da requisicao, que e
        a que paga a conexao. Parametro e nao atributo de instancia porque o
        `_answer` ja recebe tudo o que usa; guardar num `self` faria a soma
        depender de uma escrita feita em outro metodo.

        Separada do `chat` para que aquele fique com o que e enquadramento —
        o log de entrada, a barreira do restaurante e o tratamento de erro —
        e este com o pipeline. Junto, um `try` cobria as duas coisas e a
        leitura tinha de separa-las na cabeca.
        """
        # UM `now` para o turno inteiro: a limpeza das sessoes vencidas e o
        # registro deste turno precisam concordar sobre que horas sao, senao
        # uma sessao pode ser limpa e regravada no mesmo pedido.
        now = _utc_now()
        session_started_at = perf_counter()
        _cleanup_inactive_sessions(now)
        conversation = _get_session_conversation(session_id)
        session_ms = (perf_counter() - session_started_at) * 1000
        logger.info("[AI /chat perf] session_ms=%.2f", session_ms)

        if is_greeting(message):
            return self._greeting_answer(
                restaurant=restaurant,
                session_id=session_id,
                message=message,
                now=now,
                guard_ms=guard_ms,
                session_ms=session_ms,
            )

        # O tempo de PAREDE da recuperacao, medido de fora. As etapas de
        # dentro dela (embedding, busca, preco vigente) tem cronometro
        # proprio no `RetrievalService`; este numero existe para a linha de
        # fechamento poder somar as etapas e bater com o `total_ms` — sem ele,
        # a soma teria um buraco e nao daria para saber se ele e uma etapa
        # nao medida ou erro de conta.
        retrieval_started_at = perf_counter()
        retrieved_products = self._retrieve_menu_products(restaurant, branch, message)
        retrieval_total_ms = (perf_counter() - retrieval_started_at) * 1000
        logger.info("[AI /chat perf] retrieval_total_ms=%.2f", retrieval_total_ms)

        context_started_at = perf_counter()
        restaurant_context = self._build_restaurant_context(restaurant)
        context_ms = (perf_counter() - context_started_at) * 1000
        logger.info("[AI /chat perf] context_build_ms=%.2f", context_ms)

        # Cronometro proprio, e nao dentro do `context_build_ms`: este e o
        # unico dos dois que ABRE CONSULTA (ate tres por turno), e somados
        # ninguem saberia se o numero cresceu por causa do banco ou do texto.
        # Se ele aparecer, o conserto e cache no Redis — horario de
        # funcionamento e cadastro, e quase nunca muda.
        branch_state_started_at = perf_counter()
        branch_state = self._build_branch_state(restaurant, branch, now)
        branch_state_ms = (perf_counter() - branch_state_started_at) * 1000
        logger.info("[AI /chat perf] branch_state_ms=%.2f", branch_state_ms)

        llm_started_at = perf_counter()
        llm_response = self._invoke_llm(
            restaurant=restaurant,
            branch=branch,
            restaurant_context=restaurant_context,
            branch_state=branch_state,
            conversation=conversation,
            retrieved_products=retrieved_products,
            message=message,
        )
        llm_total_ms = (perf_counter() - llm_started_at) * 1000
        logger.info("[AI /chat perf] llm_total_ms=%.2f", llm_total_ms)

        post_started_at = perf_counter()
        selected_product_ids = self._validate_selected_product_ids(
            retrieved_products=retrieved_products,
            selected_product_ids=llm_response.selected_product_ids,
        )
        selected_product_ids = self._rescue_products_named_in_text(
            message=llm_response.message,
            retrieved_products=retrieved_products,
            selected_product_ids=selected_product_ids,
        )
        selected_product_ids = _limitar_cartoes(selected_product_ids)
        products = self._hydrate_products(branch.id, selected_product_ids)
        _contar_cartao_do_turno(retrieved_products, products)
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
        post_ms = (perf_counter() - post_started_at) * 1000

        # UMA linha com o turno inteiro, e nao so as oito espalhadas. As
        # espalhadas continuam (ninguem perde grep), mas cada uma sozinha
        # responde "quanto custou esta etapa" e nenhuma responde "onde foi o
        # tempo desta requisicao" — que e a pergunta. Junta-las depois, no
        # log, exige casar linhas por proximidade, e com duas pessoas
        # conversando ao mesmo tempo isso deixa de funcionar.
        logger.info(
            "[AI /chat perf] etapas | guard_ms=%.2f | session_ms=%.2f "
            "| retrieval_total_ms=%.2f | context_build_ms=%.2f "
            "| llm_total_ms=%.2f | post_ms=%.2f | soma_ms=%.2f",
            guard_ms,
            session_ms,
            retrieval_total_ms,
            context_ms,
            llm_total_ms,
            post_ms,
            guard_ms + session_ms + retrieval_total_ms + context_ms + llm_total_ms + post_ms,
        )
        logger.info(
            "[AI /chat] Retorno final | response_type=%s | products_count=%d",
            response.response_type,
            len(response.products),
        )
        return response

    def _greeting_answer(
        self,
        restaurant,
        session_id: str,
        message: str,
        now: datetime,
        guard_ms: float,
        session_ms: float,
    ) -> ChatResponse:
        """Saudacao respondida sem busca e SEM MODELO.

        A primeira versao deste desvio (24/08/2026) so pulava a busca, e o
        turno continuava indo ao modelo: medido em producao, `llm_call_ms=3616`
        para produzir 85 tokens de "oi, tudo bem?". Pagar uma geracao depois de
        ja ter DECIDIDO que a mensagem e uma saudacao e pagar o modelo para
        confirmar o que o portao concluiu de graca.

        O que se perde: a saudacao deixa de variar com o que a pessoa escreveu.
        Como as entradas possiveis sao as catorze de `GREETINGS` e as tres
        respostas ja cobrem qualquer uma delas, o que se perde e variacao que
        ninguem consegue provocar.

        O turno continua sendo GRAVADO no historico. Sem isso, a proxima
        pergunta da sessao chegaria ao modelo sem o "oi" e sem a resposta, e
        um "e o preco desse?" logo depois perderia o unico contexto que tinha.

        As etapas puladas saem ZERADAS na linha de fechamento, e nao ausentes:
        um turno sem `retrieval_total_ms` no log e indistinguivel de um turno
        que quebrou antes de chegar la.
        """
        logger.info("[AI /chat] saudacao | resposta enlatada, sem busca e sem modelo")
        response = ChatResponse(
            response_type="text",
            message=greeting_reply(restaurant.name),
            products=[],
        )
        _store_session_turn(session_id, message, response.message, now)

        logger.info(
            "[AI /chat perf] etapas | guard_ms=%.2f | session_ms=%.2f "
            "| retrieval_total_ms=0.00 | context_build_ms=0.00 "
            "| llm_total_ms=0.00 | post_ms=0.00 | soma_ms=%.2f",
            guard_ms,
            session_ms,
            guard_ms + session_ms,
        )
        logger.info(
            "[AI /chat] Retorno final | response_type=%s | products_count=0",
            response.response_type,
        )
        return response

    def _retrieve_menu_products(self, restaurant, branch, message: str) -> list[dict]:
        """Os produtos do cardapio para esta mensagem.

        O portao de saudacao vive em `_answer`, e nao aqui, desde que a
        saudacao passou a nao chamar o modelo: com o desvio la em cima, um
        segundo `is_greeting` neste ponto seria codigo inalcancavel se
        passasse, e uma segunda definicao de "o que e saudacao" se divergisse.

        AQUI O `similarity` SAI, e este e o unico lugar que o tira. A busca o
        devolve porque a voz precisa dele (ver `_format_retrieved_product`),
        mas esta lista vai INTEIRA para `{retrieved_products}` no prompt do
        texto: deixar passar seria ~10 tokens por produto em todo turno do
        `/chat` e um campo sobre o qual o `system_prompt` nao diz uma palavra.

        A voz nao passa por esta funcao — ela chama `retrieve_products`
        direto, em `VoiceSearchService.buscar`.
        """
        produtos = self.retrieval_service.retrieve_products(
            restaurant_id=restaurant.id,
            branch_id=branch.id,
            question=message,
        )
        return [
            {chave: valor for chave, valor in produto.items() if chave != "similarity"}
            for produto in produtos
        ]

    def _get_active_restaurant(self, restaurant_id: uuid.UUID):
        restaurant = self.restaurant_repository.get_active_by_id(restaurant_id)
        if restaurant is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Restaurante não encontrado",
            )
        return restaurant

    def _get_active_branch(self, restaurant_id: uuid.UUID, branch_id: uuid.UUID):
        """A loja de que este atendimento fala. 404 quando nao e deste restaurante.

        Nao ha queda para a filial padrao aqui, ao contrario do `/menu`. O
        cardapio publico e navegacao — mostrar a loja principal a quem nao
        escolheu nenhuma e uma resposta razoavel. O Rapi RECOMENDA, com preco,
        e uma recomendacao da loja errada e indistinguivel de uma certa: o
        cliente pede, e quem descobre e a cozinha. Entre 404 e adivinhar, aqui
        so 404 e honesto.
        """
        branch = self.branch_repository.get_active_by_id_and_restaurant(
            branch_id, restaurant_id
        )
        if branch is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Filial não encontrada para este restaurante",
            )
        return branch

    @staticmethod
    def _build_restaurant_context(restaurant) -> str:
        """O que o atendente sabe sobre a casa onde ele trabalha.

        Era `f"restaurant_id={uuid}"` — um dado que o modelo nao tem como usar
        para nada.

        O NOME CONTINUA AQUI, mas o motivo virou o oposto do que era. Ate
        24/08/2026 o prompt mandava citar a casa pelo nome, e sem o nome a
        instrucao nao tinha como ser cumprida. Hoje o prompt PROIBE dizer o
        nome: o cliente abriu o link do restaurante e ja sabe onde esta, e
        "Aqui na Junior da Picanha" saia em sete de nove turnos — token em
        toda resposta para informar o que a pessoa acabou de escolher. E o
        mesmo raciocinio que `voice_prompt.branch_context_for` ja registra
        para a loja.

        O nome fica porque o modelo precisa se SITUAR — e o que separa "aqui
        nao temos" de um assistente que nao sabe de que cardapio esta
        falando. Tirar a linha para economizar tokens de entrada seria pagar
        em alucinacao o que se economiza no prompt.

        A segunda linha sai de `assistant_notes`, e ate 23/08/2026 saia de
        `description`. A troca (revisao 20260823_0034) e o conserto de um
        duplo uso: a `description` tambem e a VITRINE publica, e os dois
        destinos pedem textos opostos — o cliente le a vitrine para decidir
        pedir, e o que serve aqui e o contrario do anuncio (o que a casa faz,
        o que ela nao faz, o que o atendente precisa saber para nao
        inventar). Com um campo so, a tela do painel nao conseguia instruir
        nenhum dos dois sem mentir sobre o outro.

        **Nao ha fallback para `description`, e isso e deliberado.** Com ele,
        quem nunca preenchesse as anotacoes continuaria com o anuncio no
        prompt para sempre — que e exatamente o problema que a separacao
        existe para resolver, mantido de pe justamente para quem o tem. Sem
        anotacoes a linha `Sobre a casa` nao sai, do mesmo jeito que ja nao
        saia para quem tinha a descricao vazia.

        As duas defesas continuam, porque isto continua sendo texto do
        LOJISTA entrando no prompt. O corte em `MAX_ASSISTANT_NOTES_LENGTH`
        impede que um texto longo empurre as instrucoes para longe e passe a
        competir com elas; o colapso das quebras de linha impede que ele
        finja ser uma secao nova do prompt. Nenhuma das duas transforma isso
        em texto confiavel — quem escreve ali manda no atendente da propria
        loja, e esse e o limite do estrago.

        O teto vale tambem na ESCRITA (`AdminRestaurantProfileUpdate`), com a
        mesma constante, e um nao dispensa o outro: la ele e 422 com contador
        na tela, e o lojista sabe que o texto nao coube; aqui e a ultima
        defesa, porque a coluna e `text` e continua gravavel por SQL.
        """
        lines = [f"Nome do restaurante: {restaurant.name}"]

        notes = " ".join((restaurant.assistant_notes or "").split())
        if notes:
            lines.append(f"Sobre a casa: {notes[:MAX_ASSISTANT_NOTES_LENGTH]}")

        return "\n".join(lines)

    def _build_branch_state(self, restaurant, branch, now: datetime) -> str:
        """Se a loja esta atendendo AGORA, nas duas unicas perguntas que decidem.

        O QUE ISTO CONSERTA. A uma da manha, com a loja fechada, o Rapi
        recomendava picanha com preco e perguntava se queria que separasse. O
        cliente montava o carrinho e so descobria no checkout, onde
        `ensure_branch_is_open` recusa. Nenhuma regra de desvio pegava isso,
        porque nao houve pergunta sobre horario — o assistente nao estava
        chutando o horario, ele nao sabia que existia horario.

        `_answer` ja carregava a filial (`_get_active_branch`) e a descartava:
        ela servia de barreira 404 e nada mais. Aqui ela vira dado, do mesmo
        jeito que `POST /voice/session` fez em 20/08/2026.

        DOIS FATOS, E SO ELES. Prazo de entrega, taxa, pedido minimo e ate que
        horas a loja fica aberta NAO entram, e a omissao e deliberada: sao os
        assuntos que a secao QUANDO NAO DA PARA RESPONDER manda devolver para
        a tela. O que entra aqui e reenviado em TODO turno, e o modelo trata o
        que le como conhecimento — ponha o prazo de entrega no prompt e ele
        passa a recitar um numero cuja consequencia, quando erra, e a proxima
        compra indo embora.

        O `now` VEM EM DUAS ROUPAS, e trocar uma pela outra quebra em silencio:

        - `find_current_period` compara RELOGIO DE PAREDE (`moment.time()`),
          entao recebe o instante no fuso da loja. Passar o `now` de UTC direto
          leria 04:00 onde sao 01:00, e uma loja aberta ate as 02:00 sairia
          fechada — na madrugada, que e exatamente o caso que motivou isto.
        - `resolve_branch_operation` compara `delivery_paused_until` e trata
          coluna ingenua como UTC (ver `_delivery_esta_pausada`), entao recebe
          o `now` de UTC. Dar a ele o horario local deslocaria a pausa em tres
          horas.

        E o mesmo instante nas duas, e nao duas leituras do relogio: `_answer`
        tem um `now` para o turno inteiro de proposito.

        Quem responde "aberta agora" e `resolver_atendimento`, a MESMA funcao
        que a tela de escolha de filial usa. Reescrever a regra aqui seria a
        armadilha 10 no formato mais caro dela: a tela dizendo "fechada"
        enquanto o Rapi separa a picanha.
        """
        period = self.branch_hours_service.find_current_period(
            branch.id, now.astimezone(BRANCH_TIMEZONE)
        )
        # O SELECT dos settings paga por um campo que ninguem le aqui:
        # `is_open` e `accepts_delivery_now` sao estado do dia e nao herdam
        # nada do restaurante. Mesmo assim a leitura passa por
        # `resolve_branch_operation`, pelo motivo que a tela de filiais ja
        # registra no proprio comentario — um segundo jeito de ler a mesma
        # regra e como a armadilha 10 comeca. Um SELECT ao lado de uma
        # chamada de 1700 a 3800 ms.
        operation = resolve_branch_operation(
            branch, self.menu_repository.get_settings(restaurant.id), now
        )
        is_open_now, closed_reason = resolver_atendimento(period, operation)

        if is_open_now:
            aberta = "sim"
        else:
            aberta = f"nao ({_MOTIVO_DE_FECHADA[closed_reason]})"

        if operation.accepts_delivery_now:
            entrega = "funcionando"
        elif not operation.accepts_delivery:
            entrega = "esta loja nao faz entrega"
        else:
            entrega = "pausada agora"

        return f"Aberta agora: {aberta}\nEntrega: {entrega}"

    def _invoke_llm(
        self,
        restaurant,
        branch,
        restaurant_context: str,
        branch_state: str,
        conversation: list[SessionMessage],
        retrieved_products: list[dict],
        message: str,
    ):
        """`llm_ms` VIROU `llm_total_ms`, e ganhou tres partes por baixo.

        A linha antiga media uma coisa so e chamava tudo de "o LLM": a
        construcao do `ChatOpenAI`, a montagem da cadeia e a chamada de rede.
        As tres acontecem a cada requisicao e so uma delas e a OpenAI
        respondendo — entao o numero que se olhava para decidir "o modelo esta
        lento" nao era o modelo.

        A construcao ganha cronometro proprio aqui porque ela e a suspeita
        nomeada: `ChatLLMService()` monta um `ChatOpenAI` NOVO por requisicao,
        e com ele um cliente HTTP novo, que abre conexao nova com a OpenAI. E
        exatamente o caso que `embedding_service.py` ja mediu e resolveu com
        `lru_cache` (mediana de 654 ms para 340 ms). Medir antes de repetir a
        solucao e o ponto desta rodada.

        **E daqui sai o CUSTO do turno**, e por isso a funcao deixou de ser
        `@staticmethod`. Este e o unico ponto do pipeline que tem as duas
        pontas ao mesmo tempo: o `usage` que a OpenAI devolveu e o restaurante
        de quem e a conversa. A alternativa era devolver `(resposta, uso)` e
        gravar no `_answer` — e isso poria em toda chamada um segundo item que
        as vezes e None por um motivo que so o corpo explica.

        A gravacao nao levanta: ver `AIUsageService.registrar_texto`.
        """
        client_started_at = perf_counter()
        llm_service = ChatLLMService()
        logger.info(
            "[AI /chat perf] llm_client_build_ms=%.2f",
            (perf_counter() - client_started_at) * 1000,
        )

        llm_response = llm_service.invoke(
            restaurant_context=restaurant_context,
            branch_state=branch_state,
            conversation=conversation,
            retrieved_products=retrieved_products,
            user_message=message,
        )
        logger.info(
            "[AI /chat] Structured Output | response_type=%s",
            llm_response.response_type,
        )
        AIUsageService(self.db).registrar_texto(
            restaurant_id=restaurant.id,
            branch_id=branch.id,
            uso=llm_service.ultimo_uso,
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
            _contar_turno_com_llm(houve_resgate=False)
            return selected_product_ids

        rescued = _products_named_in(message, retrieved_products)
        if not rescued:
            _contar_turno_com_llm(houve_resgate=False)
            return selected_product_ids

        resgates, turnos = _contar_turno_com_llm(houve_resgate=True)
        logger.warning(
            "[AI /chat] o modelo citou %d produto(s) no texto e nao selecionou nenhum. "
            "Resgatados pelo nome. | resgates=%d | turnos_com_llm=%d | taxa=%.1f%%",
            len(rescued),
            resgates,
            turnos,
            100.0 * resgates / turnos,
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
        branch_id: uuid.UUID,
        selected_product_ids: list[uuid.UUID],
    ):
        """Os cartoes que vao para a tela, lidos do banco DAQUELA loja.

        Por filial desde a revisao 20260820_0026. E o ultimo ponto do caminho
        em que um id de outra loja ainda poderia virar cartao com preco: aqui
        ele simplesmente nao volta da consulta e o produto sai da resposta.
        """
        hydration_started_at = perf_counter()
        products_by_id = {
            uuid.UUID(str(product.id)): product
            for product in self.product_repository.list_active_by_ids(
                branch_id,
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


def _contar_turno_com_llm(houve_resgate: bool) -> tuple[int, int]:
    """Soma este turno ao placar do resgate por nome, e devolve o par atual.

    O QUE ESTE NUMERO RESPONDE. `_rescue_products_named_in_text` e rede de
    seguranca: ela so age quando o modelo escreveu nomes de produto no texto
    e devolveu `selected_product_ids` vazio. Uma rede que trabalha de vez em
    quando esta certa; uma que trabalha em 1 de cada 9 turnos esta cobrindo um
    defeito do prompt, e a diferenca entre as duas so aparece na TAXA. A linha
    de WARNING sozinha nao a dava: ela conta os acionamentos e nunca os turnos
    em que a selecao veio bem, entao "vi tres avisos hoje" nao distingue tres
    em trinta de tres em nove.

    O DENOMINADOR SAO OS TURNOS QUE CHAMARAM O MODELO, nao as requisicoes do
    `/chat`. Saudacao responde enlatada e volta antes daqui (ver
    `_answer`), e conta-la diluiria a taxa com turnos em que o modelo nunca
    teve chance de errar — quanto mais gente cumprimentar o Rapi, melhor
    pareceria o prompt.

    TRES LIMITES, e nenhum deles e conserto pendente:

    - **Vive no processo.** Zera a cada deploy, como `_SESSION_HISTORY` e
      `_cold_start_pending`. Para a pergunta que ele responde — "a taxa
      mudou depois que eu mexi no prompt?" — isso e util e nao atrapalha: o
      contador reinicia justamente no deploy que muda a variavel.
    - **E por worker.** Com N workers saem N placares independentes, cada um
      com o seu denominador. Nenhum deles esta errado; somar os numeros de
      duas linhas de log diferentes e que estaria.
    - **So aparece quando a rede age.** Turno limpo nao escreve linha
      nenhuma, de proposito: um log por turno em INFO para dizer "nada
      aconteceu" e ruido no caminho quente. O denominador viaja junto do
      proximo aviso, que e quando alguem tem motivo para olhar.

    O grep que vale no radar: `Resgatados pelo nome`.
    """
    global _resgates_por_nome, _turnos_com_llm
    _turnos_com_llm += 1
    if houve_resgate:
        _resgates_por_nome += 1
    return _resgates_por_nome, _turnos_com_llm


def _limitar_cartoes(selected_product_ids: list[uuid.UUID]) -> list[uuid.UUID]:
    """No maximo tres cartoes por turno, e o corte e pelos PRIMEIROS.

    Producao, 24/08/2026: "quanto custa a picanha?" devolveu CINCO produtos —
    importada, suina, black angus, suina 400g, completa. O cliente nao le
    cinco, le dois ou tres e toca; e sao os cinco uuids, a ~26 tokens cada,
    que levaram aquele turno a 237 tokens de saida (com o teto velho de 300,
    ele teria quebrado).

    QUEM ENCURTA A RESPOSTA E O PROMPT, NAO ESTA FUNCAO. Quando ela corta, o
    modelo ja gerou e ja cobrou os cinco uuids: o que ela evita e o carrossel
    divergir do texto, nunca o custo. Achar que este corte resolveu o token e
    o erro que ele convida.

    O QUE ELA FECHA e o meio-termo, que e o caso que o cliente ve: o modelo
    cita tres produtos no texto e seleciona cinco. Ai o texto fala de tres e
    o carrossel mostra cinco, e nada em `_validate_selected_product_ids` ou
    em `_rescue_products_named_in_text` impede isso — a primeira so descarta
    id inventado, e a segunda so age com a selecao VAZIA.

    O CORTE E PELOS PRIMEIROS porque a ordem da selecao e a ordem do TEXTO (o
    prompt manda, `_validate_selected_product_ids` preserva). Cortar pelo fim
    tiraria justamente os produtos que o modelo explicou primeiro.

    E ela roda DEPOIS do resgate, de proposito: o resgate so age com a
    selecao vazia, e nada impede que ele traga quatro nomes citados no texto.

    O WARNING e o ponto desta funcao. Ele e o unico jeito de saber se a regra
    do prompt esta pegando sem reler as respostas a mao — turno cortado aqui
    e turno em que o modelo desobedeceu la. O grep no radar:
    `acima do teto`.

    Nao alcanca a voz: `VoiceSearchService.buscar` nao passa por `_answer`, e
    la nao ha selecao de modelo para limitar — a lista que aparece e a propria
    busca.
    """
    if len(selected_product_ids) <= _MAX_CARTOES:
        return selected_product_ids

    logger.warning(
        "[AI /chat] o modelo selecionou produtos acima do teto. "
        "Carrossel cortado pelos primeiros. | selecionados=%d | teto=%d",
        len(selected_product_ids),
        _MAX_CARTOES,
    )
    return selected_product_ids[:_MAX_CARTOES]


def _contar_cartao_do_turno(retrieved_products: list[dict], products: list) -> None:
    """A busca achou produto e o cliente ficou sem cartao. Com que frequencia?

    POR QUE O CONTADOR DO RESGATE NAO RESPONDE ISSO — e por que ele, sozinho,
    ATRAPALHA. O caso de producao de 24/08/2026: "voces tem sushi?" voltou
    `response_type=text | products_count=0`, e o texto oferecia mostrar os
    peixes da casa sem escrever o nome de nenhum. Sem nome no texto,
    `_rescue_products_named_in_text` nao tinha o que resgatar e saiu pelo
    caminho de `houve_resgate=False` — quer dizer, aquele turno entrou no
    placar do resgate como um turno LIMPO, e ainda diluiu a taxa dele.

    A DIFERENCA ENTRE OS DOIS PLACARES E A REDE DE SEGURANCA NO MEIO:

        `resgates`      o modelo nao selecionou, MAS escreveu os nomes
                        -> a rede pegou, o cartao saiu
        `sem_cartao`    o modelo nao selecionou e nao escreveu nome nenhum
                        -> nao ha o que pegar, o cliente ficou sem cartao

    Sao o pedaco pego e o pedaco que escapa do MESMO defeito. Por isso os
    dois placares ficam, e por isso nenhum deles substitui o outro.

    O DENOMINADOR SO CONTA TURNO COM CONTEXTO. Pergunta cuja busca nao trouxe
    nada ("voces abrem domingo?") termina sem cartao porque nao havia cartao a
    mostrar, e conta-la aqui misturaria acerto com defeito. `retrieved_products`
    vazio sai antes de somar qualquer coisa.

    E UM NUMERO PARA COMPARAR, NAO UM ALARME. Taxa diferente de zero e
    esperada: em "nao temos sushi" a busca devolve peixe acima do piso de
    similaridade e o modelo pode, com razao, nao querer oferecer cartao
    nenhum. O que este placar serve para responder e se a MESMA bateria
    piorou entre dois deploys — o mesmo turno trouxe 2 ou 3 cartoes nas duas
    baterias anteriores e nenhum nesta, e sem contador isso so aparece para
    quem estava olhando a tela naquela hora.

    O DENOMINADOR CONTA ACERTO JUNTO COM DEFEITO, e e por isso que a taxa se
    chama `taxa_teto` no log: ela e um TETO do defeito, nunca a medida dele.
    Producao, 24/08/2026 — "voces entregam no Papicu?" disparou
    `sem_cartao=1 | turnos_com_contexto=1 | taxa_teto=100.0%`. A busca achou
    cinco produtos por sobreposicao lexical, mas a pergunta e de ENTREGA: nao
    ter cartao ali e a resposta CERTA, e o contador reportou 100% por acerto.

    O que separaria os dois casos e a SIMILARIDADE da busca — pergunta de
    cardapio traz produto por proximidade, pergunta de entrega traz por
    coincidencia de palavra. Ela nao chega ate aqui:
    `RetrievalService._format_retrieved_product` a descarta antes do cache.
    Poe-la no denominador e mudanca no payload do cache de busca mais uma
    bateria para calibrar a barra — e uma barra chutada seria pior que o teto
    honesto que esta ai. Ate la o numero vale para o que sempre valeu:
    comparar a MESMA bateria entre dois deploys. Subiu, olhe as perguntas.

    Vale o mesmo que vale para `_contar_turno_com_llm`: vive no processo,
    zera no deploy, e e por worker.
    """
    global _turnos_com_contexto, _turnos_sem_cartao

    # A busca nao trouxe nada: zero cartao e a resposta CERTA, nao um caso.
    if not retrieved_products:
        return

    _turnos_com_contexto += 1
    if products:
        return

    _turnos_sem_cartao += 1
    logger.warning(
        "[AI /chat] a busca achou %d produto(s) e nenhum chegou ao cliente. "
        "Pode ser ACERTO — confira a pergunta. "
        "| sem_cartao=%d | turnos_com_contexto=%d | taxa_teto=%.1f%%",
        len(retrieved_products),
        _turnos_sem_cartao,
        _turnos_com_contexto,
        100.0 * _turnos_sem_cartao / _turnos_com_contexto,
    )


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


def _take_cold_start_flag() -> bool:
    """True uma unica vez por processo, na primeira requisicao de `/chat`.

    Um `global` e nao um contador de turnos: a pergunta nao e "quantas
    requisicoes ja passaram", e sim "esta e a que paga a construcao do que
    fica guardado depois" — a conexao do pool, o cliente de embedding do
    `lru_cache`, os handshakes TLS dos dois. Com mais de um worker cada
    processo tem o seu, o que esta certo: cada um paga o proprio aquecimento,
    e o cliente que cair no worker frio espera de novo (armadilha 20).
    """
    global _cold_start_pending
    if not _cold_start_pending:
        return False
    _cold_start_pending = False
    return True


def _message_digest(message: str) -> str:
    return hashlib.sha256(message.encode("utf-8")).hexdigest()[:12]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
