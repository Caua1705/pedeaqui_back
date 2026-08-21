"""As consultas de avaliacao do painel.

Separado de `OrderReviewRepository` pela mesma razao que separou
`AdminReportRepository` de `OrderRepository`: la e uma linha carregada para
virar objeto, aqui e `GROUP BY` com `COUNT` que nunca instancia um
`OrderReview`.

**O `JOIN` com `orders` nao e opcional em nenhuma consulta daqui**, e e o que
sustenta o isolamento entre restaurantes: `order_reviews` nao tem
`restaurant_id` nem `branch_id` (ver a revisao 20260820_0028), entao os dois
recortes saem de `orders`. Uma consulta que esquecesse o `JOIN` devolveria as
avaliacoes de todo mundo — por isso o filtro mora em `_scope`, numa funcao
so.
"""

import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.models.order_model import Order
from src.models.order_review_model import OrderReview


def _scope(
    restaurant_id: uuid.UUID,
    start_at: datetime,
    end_at: datetime,
    branch_id: uuid.UUID | None,
) -> list:
    """O recorte que TODA consulta deste arquivo aplica.

    `branch_id` nulo significa "o restaurante inteiro" — o recorte de quem
    enxerga todas as lojas —, nunca "filial nenhuma". Quem decide isso e
    `AdminScope.resolve_branch_filter`, no endpoint.

    O periodo e do `created_at` da AVALIACAO, nao do pedido: a pergunta do
    painel e "o que os clientes disseram esta semana", e uma nota escrita
    hoje sobre um pedido de terca pertence a hoje.
    """
    condicoes = [
        Order.restaurant_id == restaurant_id,
        OrderReview.created_at >= start_at,
        OrderReview.created_at < end_at,
    ]
    if branch_id is not None:
        condicoes.append(Order.branch_id == branch_id)
    return condicoes


class AdminReviewRepository:
    def __init__(self, db: Session):
        self.db = db

    def rating_histogram(
        self,
        restaurant_id: uuid.UUID,
        start_at: datetime,
        end_at: datetime,
        branch_id: uuid.UUID | None,
    ) -> list[tuple[int, int]]:
        """Quantas avaliacoes de cada nota, no periodo.

        O total e a media saem DAQUI, no service, e nao de um `COUNT`/`AVG`
        proprio: assim a media nunca pode contradizer as barras da mesma
        tela, que e o tipo de divergencia que ninguem consegue explicar
        olhando o painel.
        """
        stmt = (
            select(OrderReview.rating, func.count())
            .join(Order, Order.id == OrderReview.order_id)
            .where(*_scope(restaurant_id, start_at, end_at, branch_id))
            .group_by(OrderReview.rating)
        )
        return [(linha[0], linha[1]) for linha in self.db.execute(stmt).all()]

    def problem_tag_histogram(
        self,
        restaurant_id: uuid.UUID,
        start_at: datetime,
        end_at: datetime,
        branch_id: uuid.UUID | None,
    ) -> list[tuple[str, int]]:
        stmt = (
            select(OrderReview.problem_tag, func.count())
            .join(Order, Order.id == OrderReview.order_id)
            .where(
                *_scope(restaurant_id, start_at, end_at, branch_id),
                OrderReview.problem_tag.is_not(None),
            )
            .group_by(OrderReview.problem_tag)
        )
        return [(linha[0], linha[1]) for linha in self.db.execute(stmt).all()]

    def list_reviews(
        self,
        restaurant_id: uuid.UUID,
        start_at: datetime,
        end_at: datetime,
        branch_id: uuid.UUID | None,
        max_rating: int | None,
        limit: int,
        offset: int,
    ) -> list[tuple[OrderReview, int, uuid.UUID]]:
        """A pagina de avaliacoes, com o numero do pedido e a filial.

        `max_rating` e o filtro que o dono de fato usa: "me mostra as notas
        ate 3 desta semana". Nao ha `min_rating` porque nao ha tela para
        "so as notas altas" — quem quer parabenizar a equipe olha a media.

        A ordem e do mais recente para o mais antigo, com `id` desempatando:
        `created_at` repete quando duas avaliacoes caem no mesmo instante, e
        sem o segundo criterio a mesma pagina sai em ordens diferentes a cada
        requisicao (o mesmo motivo do `sorted` da armadilha 14).
        """
        condicoes = _scope(restaurant_id, start_at, end_at, branch_id)
        if max_rating is not None:
            condicoes.append(OrderReview.rating <= max_rating)

        stmt = (
            select(OrderReview, Order.order_number, Order.branch_id)
            .join(Order, Order.id == OrderReview.order_id)
            .where(*condicoes)
            .order_by(OrderReview.created_at.desc(), OrderReview.id.desc())
            .limit(limit)
            .offset(offset)
        )
        return [(linha[0], linha[1], linha[2]) for linha in self.db.execute(stmt).all()]
