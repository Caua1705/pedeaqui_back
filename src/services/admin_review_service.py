"""A aba de avaliacoes do painel.

O que o lojista vem procurar aqui e uma coisa so: **o que consertar.** Por
isso a resposta nao e uma lista solta — o agregado vem junto, e e dele que
sai a frase que muda alguma coisa ("7 das 12 notas baixas desta semana foram
atraso").
"""

import uuid
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from src.core.constants import PLATFORM_TIMEZONE
from src.repositories.admin_review_repository import AdminReviewRepository
from src.schemas.admin_review_schema import (
    AdminOrderReviewItem,
    AdminReviewsResponse,
    AdminReviewSummary,
)


REVIEW_PANEL_TIMEZONE = ZoneInfo(PLATFORM_TIMEZONE)

DEFAULT_REVIEW_LIMIT = 50
MAX_REVIEW_LIMIT = 200

# Teto do periodo consultavel de uma vez. Mesmo espirito do teto da listagem
# de pedidos: a tela nao mostra mil linhas, e um periodo aberto so descobre
# isso no timeout.
MAX_REVIEW_PERIOD_DAYS = 366

# As cinco notas possiveis, para o histograma sair completo mesmo quando
# alguma nao apareceu no periodo.
RATING_SCALE = (1, 2, 3, 4, 5)


class AdminReviewService:
    def __init__(self, db: Session):
        self.review_repository = AdminReviewRepository(db)

    def list_reviews(
        self,
        restaurant_id: uuid.UUID,
        start_date: date,
        end_date: date,
        branch_id: uuid.UUID | None,
        max_rating: int | None,
        limit: int,
        offset: int,
    ) -> AdminReviewsResponse:
        start_at, end_at = self._period_bounds(start_date, end_date)

        # O agregado NAO leva o `max_rating`: filtrar a lista para "so as
        # notas baixas" nao pode mudar a media do periodo. Sem isto, o
        # lojista que clica no filtro ve a media desabar e conclui que a
        # semana piorou.
        histograma = self.review_repository.rating_histogram(
            restaurant_id, start_at, end_at, branch_id
        )
        etiquetas = self.review_repository.problem_tag_histogram(
            restaurant_id, start_at, end_at, branch_id
        )
        linhas = self.review_repository.list_reviews(
            restaurant_id, start_at, end_at, branch_id, max_rating, limit, offset
        )

        return AdminReviewsResponse(
            summary=self._build_summary(histograma, etiquetas),
            items=[
                AdminOrderReviewItem(
                    order_number=order_number,
                    branch_id=branch,
                    rating=avaliacao.rating,
                    problem_tag=avaliacao.problem_tag,
                    comment=avaliacao.comment,
                    created_at=avaliacao.created_at,
                )
                for avaliacao, order_number, branch in linhas
            ],
        )

    @staticmethod
    def _build_summary(
        histograma: list[tuple[int, int]],
        etiquetas: list[tuple[str, int]],
    ) -> AdminReviewSummary:
        """Total e media saem do HISTOGRAMA, e nao de consultas proprias.

        E o que garante que a media exibida e as barras da mesma tela nunca
        se contradigam. Com `COUNT`/`AVG` separados, bastaria um `WHERE`
        divergir para o painel mostrar media 4,2 sobre barras que somam 4,6 —
        e nao ha como alguem depurar isso olhando a tela.
        """
        por_nota = {nota: 0 for nota in RATING_SCALE}
        for nota, quantidade in histograma:
            por_nota[nota] = quantidade

        total = sum(por_nota.values())
        soma = sum(nota * quantidade for nota, quantidade in por_nota.items())
        # Periodo sem avaliacao devolve `None`, e nao 0.0: media zero seria
        # lida como "todo mundo odiou", que e o oposto de "ninguem avaliou".
        media = round(soma / total, 2) if total else None

        return AdminReviewSummary(
            total=total,
            average=media,
            by_rating=por_nota,
            by_problem_tag={etiqueta: quantidade for etiqueta, quantidade in etiquetas},
        )

    @staticmethod
    def _period_bounds(start_date: date, end_date: date) -> tuple[datetime, datetime]:
        """Converte o recorte de datas para instantes UTC.

        As datas chegam no fuso da operacao (America/Fortaleza), nao em UTC:
        "as avaliacoes de ontem" para o lojista sao as do dia dele. Sem essa
        conversao, tres horas de avaliacoes cairiam no dia errado.

        O fim e o comeco do dia SEGUINTE (exclusivo) para nao perder a
        avaliacao gravada as 23:59:59.7.

        **E a TERCEIRA copia desta conta no projeto**, junto com
        `admin_report_service._period_bounds` e
        `admin_order_service._period_bounds`. Nao e descuido: as tres tem
        assinaturas e validacoes diferentes (aqui as datas sao obrigatorias e
        ha teto de periodo; no pedido elas sao opcionais e ha 400 proprio), e
        uma funcao unica com flag para cobrir os tres casos e exatamente a
        abstracao esperta que este projeto recusa. Se um dia as tres
        convergirem, a extracao vira uma linha.
        """
        if end_date < start_date:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="end_date nao pode ser anterior a start_date",
            )
        if (end_date - start_date).days + 1 > MAX_REVIEW_PERIOD_DAYS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Periodo maximo da consulta: {MAX_REVIEW_PERIOD_DAYS} dias",
            )

        start_at = datetime.combine(start_date, time.min, tzinfo=REVIEW_PANEL_TIMEZONE)
        end_at = datetime.combine(
            end_date + timedelta(days=1), time.min, tzinfo=REVIEW_PANEL_TIMEZONE
        )
        return start_at, end_at
