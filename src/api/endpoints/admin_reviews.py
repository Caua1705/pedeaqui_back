"""A aba de avaliacoes do painel.

**Papel: `GERENCIA`, e nao `SOMENTE_DONO`.** A divisao usada em
`admin_reports` e "dinheiro do restaurante inteiro e do dono"; avaliacao nao
diz quanto entrou. Quem conserta atraso e pedido errado e quem toca a loja, e
uma nota que so o dono ve nao vira conserto no balcao.

`branch_id` na querystring so RESTRINGE, pela mesma
`AdminScope.resolve_branch_filter` do resto do painel: gerente preso a uma
filial que pedir outra recebe 404, e gerente sem filial nenhuma no token
enxerga o restaurante inteiro so se for dono.
"""

from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from src.api.dependencies.admin_scope import (
    GERENCIA,
    AdminScope,
    exigir_papel,
    get_admin_scope,
)
from src.api.dependencies.database import get_db
from src.schemas.admin_review_schema import AdminReviewsResponse
from src.services.admin_review_service import (
    DEFAULT_REVIEW_LIMIT,
    MAX_REVIEW_LIMIT,
    AdminReviewService,
)


router = APIRouter(prefix="/admin/reviews", tags=["admin reviews"])


@router.get(
    "",
    response_model=AdminReviewsResponse,
    dependencies=[Depends(exigir_papel(GERENCIA))],
)
def list_reviews(
    start_date: date = Query(..., description="Primeiro dia do periodo (inclusive)"),
    end_date: date = Query(..., description="Ultimo dia do periodo (inclusive)"),
    branch_id: UUID | None = Query(
        default=None,
        description="Recorte por filial. Omitido, traz o restaurante inteiro. So restringe.",
    ),
    max_rating: int | None = Query(
        default=None,
        ge=1,
        le=5,
        description="Traz somente notas ATE este valor. O uso real e max_rating=3.",
    ),
    limit: int = Query(default=DEFAULT_REVIEW_LIMIT, ge=1, le=MAX_REVIEW_LIMIT),
    offset: int = Query(default=0, ge=0),
    scope: AdminScope = Depends(get_admin_scope),
    db: Session = Depends(get_db),
) -> AdminReviewsResponse:
    """O que os clientes disseram no periodo, com o agregado junto.

    O periodo recorta a data da AVALIACAO, nao a do pedido: a pergunta e "o
    que os clientes disseram esta semana", e uma nota escrita hoje sobre um
    pedido de terca pertence a hoje.

    **`max_rating` nao mexe no `summary`.** Filtrar a lista para as notas
    baixas nao pode fazer a media do periodo desabar na mesma tela — o
    agregado sempre fala do periodo inteiro.
    """
    return AdminReviewService(db).list_reviews(
        restaurant_id=scope.restaurant_id,
        start_date=start_date,
        end_date=end_date,
        branch_id=scope.resolve_branch_filter(branch_id),
        max_rating=max_rating,
        limit=limit,
        offset=offset,
    )
