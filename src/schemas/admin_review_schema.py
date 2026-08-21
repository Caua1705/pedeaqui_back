from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from src.schemas.order_review_schema import ReviewProblemTag


class AdminOrderReviewItem(BaseModel):
    """Uma avaliacao, como o lojista a ve.

    NAO leva nome nem telefone do cliente. O `order_number` e o suficiente
    para ele achar o pedido, e `GET /admin/orders/{id}` ja mostra a pessoa —
    repetir dado pessoal numa segunda tela e superficie a mais sem leitor
    novo.
    """

    order_number: int
    branch_id: UUID
    rating: int
    problem_tag: ReviewProblemTag | None = None
    comment: str | None = None
    created_at: datetime


class AdminReviewSummary(BaseModel):
    """O agregado do periodo, e a razao de a etiqueta de problema existir.

    `average` e `float`, e a regra do `Decimal` (armadilha 34) NAO se aplica:
    media de nota nao e dinheiro, nao tem centavo e nao precisa de casa fixa.

    `by_rating` traz as CINCO chaves sempre, inclusive as zeradas. Histograma
    com buraco obriga o front a preencher o que falta, e cada front preenche
    de um jeito.

    `by_problem_tag` traz so as etiquetas que apareceram. Ao contrario das
    notas, a lista pode crescer (`REVIEW_PROBLEM_TAGS`), e devolver zeros de
    etiquetas novas nao ajudaria ninguem.
    """

    total: int
    average: float | None = None
    by_rating: dict[int, int]
    by_problem_tag: dict[str, int]


class AdminReviewsResponse(BaseModel):
    summary: AdminReviewSummary
    items: list[AdminOrderReviewItem]
