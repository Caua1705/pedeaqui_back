"""Avaliacao de pedido: quem pode avaliar, quando, e quantas vezes.

Atraso e pedido errado sao a reclamacao numero 1 do consumidor de delivery, e
ate esta frente existir o restaurante so descobria quando o cliente reclamava
em outro lugar. Desenho completo em `docs/avaliacao-de-pedido.md`.

## A credencial e o token de acompanhamento, e nao ha login

A rota e publica porque o cliente pode nem ter conta — pedido de convidado e
caso normal aqui. O que autoriza e o `tracking_token`, o mesmo que ja abre
`GET /orders/track/{token}`: 256 bits, nao enumeravel, gravado so em hash e
sem rota de reemissao (armadilha 19). Quem nao fez o pedido nao tem como
chegar.

A busca reusa `OrderRepository.get_order_by_tracking_token` de proposito,
mesmo carregando itens que esta rota nao usa. Uma segunda implementacao da
comparacao do token seria uma segunda chance de errar o `compare_digest` da
armadilha 18 — e o custo aqui e um `selectinload` a mais num pedido so.
"""

import logging
from datetime import datetime, timedelta

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from src.models.order_model import Order
from src.repositories.order_repository import OrderRepository
from src.repositories.order_review_repository import OrderReviewRepository
from src.schemas.order_review_schema import (
    CreateOrderReviewRequest,
    OrderReviewResponse,
)
from src.services.restaurant_service import RestaurantService
from src.utils.security import utcnow


logger = logging.getLogger("uvicorn.error")


# Por quantos dias o pedido entregue continua avaliavel.
REVIEW_WINDOW_DAYS = 14

# O unico status que abre a avaliacao. Ver `_ensure_order_can_be_reviewed`.
REVIEWABLE_ORDER_STATUS = "completed"

# Por quantos meses o COMENTARIO fica no banco. Ver `review_retention_cutoff`.
REVIEW_COMMENT_RETENTION_DAYS = 365


def review_retention_cutoff(now: datetime) -> datetime:
    """Antes deste instante, o comentario da avaliacao tem que sair.

    **So o comentario. A nota fica**, e essa e a diferenca em relacao as
    outras quatro tabelas do expurgo, que apagam a linha inteira. Nota e
    numero, nao identifica ninguem e e o historico de qualidade do
    restaurante: apagar a linha reescreveria a media do lojista todo mes,
    sem nada no painel explicando por que o ano passado mudou.

    POR QUE ISTO E PRECISO, SE A ANONIMIZACAO JA ALCANCA. Ela alcanca **so
    quem tem conta**. `orders.customer_id` e NULO no pedido de convidado, e
    o comentario daquele pedido nao e alcancavel a partir de conta nenhuma —
    e exatamente a situacao do `ai_feedback` (ver
    `chat_service.feedback_retention_cutoff`), e nenhuma exclusao de conta
    vai cobri-lo, hoje ou nunca.

    POR QUE 12 MESES, E NAO OS 90 DIAS DO ai_feedback. La o texto nao tem
    nenhuma rota que o leia, entao encurtar era de graca. Aqui ele tem
    leitor de verdade — o lojista, na aba de avaliacoes — e valor
    operacional: "o que os clientes reclamaram no ano passado" e a
    comparacao que justifica ter guardado. Um ano cobre a sazonalidade
    inteira sem virar arquivo permanente de texto de gente.
    """
    return now - timedelta(days=REVIEW_COMMENT_RETENTION_DAYS)


def review_window_closes_at(completed_at: datetime) -> datetime:
    """Ate quando este pedido aceita avaliacao.

    Conta da ENTREGA e nao da criacao do pedido, e a diferenca nao e
    cosmetica: um pedido que ficou esquecido em `out_for_delivery` por duas
    semanas e so entao marcado `completed` teria a janela ja fechada no
    instante em que ela deveria abrir — e o cliente veria "prazo encerrado"
    para um pedido que acabou de chegar, sem nada explicando.
    """
    return completed_at + timedelta(days=REVIEW_WINDOW_DAYS)


def completed_at_of(order: Order) -> datetime | None:
    """Quando este pedido virou `completed`, segundo o historico de status.

    `orders` nao tem coluna de conclusao — o marco vive em
    `order_status_history`, que `_ORDER_DETAIL_LOADERS` ja traz junto, entao
    isto nao custa consulta.

    Devolve `None` quando nao ha linha de `completed` no historico. Nao e
    hipotese de laboratorio: pedido migrado, ou marcado por um caminho que
    nao passou pelo `_apply_status_change`, chega aqui como `completed` sem
    o registro da transicao.
    """
    marcos = [
        registro.created_at
        for registro in order.status_history
        if registro.status == REVIEWABLE_ORDER_STATUS and registro.created_at is not None
    ]
    if not marcos:
        return None
    # O MAIS RECENTE. `completed` e terminal, entao a lista tem um item so na
    # pratica; pegar o maior mantem a conta certa se um dia deixar de ter.
    return max(marcos)


class OrderReviewService:
    def __init__(self, db: Session):
        self.db = db
        self.restaurant_service = RestaurantService(db)
        self.order_repository = OrderRepository(db)
        self.order_review_repository = OrderReviewRepository(db)

    def submit(
        self,
        restaurant_slug: str,
        tracking_token: str,
        payload: CreateOrderReviewRequest,
    ) -> OrderReviewResponse:
        """Grava a avaliacao, ou troca a que ja existia para este pedido.

        Trocar, e nao recusar, porque quem apertou uma estrela por engano
        precisa de saida — e porque `uq_order_reviews_order_id` transforma a
        segunda gravacao em erro de integridade se ninguem tratar. E o mesmo
        desenho de `AIFeedbackRepository.create`, que troca o voto existente
        em vez de duplicar.
        """
        restaurant = self.restaurant_service.get_active_restaurant(restaurant_slug)
        order = self.order_repository.get_order_by_tracking_token(restaurant.id, tracking_token)
        if order is None:
            # 404 e nao 403: 403 confirmaria que aquele pedido existe.
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Pedido não encontrado",
            )

        self._ensure_order_can_be_reviewed(order)

        try:
            avaliacao = self._save(order, payload)
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

        # A NOTA vai para o log; o comentario NAO. Texto livre e dado
        # pessoal — a pessoa escreve "moro no 302, falar com a Maria" ali —,
        # e a regra do projeto ja vale para a mensagem de chat.
        logger.info(
            "[Avaliacao] pedido avaliado order_id=%s rating=%s problem_tag=%s",
            order.id,
            payload.rating,
            payload.problem_tag,
        )
        return OrderReviewResponse.model_validate(avaliacao, from_attributes=True)

    def _save(self, order: Order, payload: CreateOrderReviewRequest):
        campos = {
            "rating": payload.rating,
            "problem_tag": payload.problem_tag,
            "comment": payload.comment,
        }
        existente = self.order_review_repository.get_by_order_id(order.id)
        if existente is not None:
            return self.order_review_repository.update(existente, **campos)
        return self.order_review_repository.create(order.id, **campos)

    def _ensure_order_can_be_reviewed(self, order: Order) -> None:
        """As duas recusas, e as duas sao 409: o pedido existe, o momento e que nao serve.

        **So `completed`.** Avaliar um pedido em `preparing` e avaliar o que
        ainda nao chegou. E `cancelled`/`rejected` ficam de fora porque nao
        houve entrega: a nota de um pedido que nunca saiu entraria na media
        do restaurante medindo outra coisa, e reclamacao de cancelamento e
        assunto de outro canal.

        **E dentro da janela.** Sem prazo, um token vazado avalia um pedido
        de um ano atras, e a nota de hoje passa a poder falar de uma cozinha
        que mudou de equipe duas vezes.
        """
        if order.status != REVIEWABLE_ORDER_STATUS:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Este pedido ainda não pode ser avaliado.",
            )

        completed_at = completed_at_of(order)
        if completed_at is None:
            # Sem marco no historico nao da para saber se a janela venceu.
            # Deixar avaliar e o lado seguro: o custo do erro e uma nota
            # atrasada, e o outro lado e recusar a avaliacao de um pedido
            # legitimo por causa de uma linha que faltou no historico.
            logger.warning(
                "[Avaliacao] pedido completed sem marco no historico order_id=%s; "
                "janela nao verificada",
                order.id,
            )
            return

        if utcnow() > review_window_closes_at(completed_at):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"O prazo de {REVIEW_WINDOW_DAYS} dias para avaliar este "
                    "pedido já encerrou."
                ),
            )
