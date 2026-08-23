"""Clientes que pediram NESTE restaurante (BLOCO D da Fase 3).

A lista sai de `orders`, nunca de `customers`. E requisito de seguranca e
nao de feature: a conta do cliente e global da plataforma, entao um SELECT
direto em `customers` entregaria ao lojista tambem quem nunca pediu na loja
dele — inclusive os clientes dos concorrentes que usam a mesma plataforma.

Derivando de `orders`, o isolamento e consequencia da propria consulta: sem
pedido naquele restaurante, nao ha linha para agrupar.

**Este service nao calcula nada da classificacao.** `segment`,
`average_ticket` e `days_since_last_order` chegam prontos da consulta, porque
os filtros da tela precisam deles ANTES do `LIMIT` — a regra inteira mora em
`src/services/customer_segment.py`, em SQL, e ali esta escrito por que nao ha
uma segunda versao em Python.
"""

import uuid
from datetime import date
from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from src.api.dependencies.admin_scope import AdminScope
from src.repositories.admin_customer_repository import (
    AdminCustomerRepository,
    CustomerListFilters,
)
from src.schemas.admin_customer_schema import (
    AdminCustomerListItem,
    AdminCustomerListResponse,
    CustomerSegment,
)
from src.utils.date_window import day_end_exclusive, day_start
from src.utils.money import money_to_float
from src.utils.security import utcnow


# Mesmo teto das outras buscas do painel: nao e regra de negocio, e para
# nao montar um ILIKE de dez mil caracteres vindo da querystring.
MAX_SEARCH_LENGTH = 120


class AdminCustomerService:
    def __init__(self, db: Session):
        self.repository = AdminCustomerRepository(db)

    def list_customers(
        self,
        scope: AdminScope,
        branch_id: uuid.UUID | None = None,
        search: str | None = None,
        segment: CustomerSegment | None = None,
        last_order_from: date | None = None,
        last_order_to: date | None = None,
        min_ticket: Decimal | None = None,
        max_ticket: Decimal | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> AdminCustomerListResponse:
        effective_branch_id = scope.resolve_branch_filter(branch_id)
        normalized_search = self._normalize_search(search)
        filters = self._build_filters(
            segment, last_order_from, last_order_to, min_ticket, max_ticket
        )
        # UM `agora` para a pagina inteira, e ele vai DENTRO da consulta como
        # parametro. Deixar o banco escolher o instante (`now()`) tiraria do
        # teste a capacidade de fixar o relogio; chamar o relogio por linha
        # faria uma pagina lida na virada do dia classificar as primeiras
        # contra ontem e as ultimas contra hoje, e a linha que mudasse de
        # rotulo no meio seria impossivel de reproduzir depois.
        agora = utcnow()

        rows = self.repository.list_customers(
            restaurant_id=scope.restaurant_id,
            now=agora,
            branch_id=effective_branch_id,
            search=normalized_search,
            filters=filters,
            limit=limit,
            offset=offset,
        )
        total = self.repository.count_customers(
            restaurant_id=scope.restaurant_id,
            now=agora,
            branch_id=effective_branch_id,
            search=normalized_search,
            filters=filters,
        )
        # O nome vem em uma segunda consulta porque nao e agregavel: o
        # cliente que corrigiu o proprio nome no ultimo pedido tem que
        # aparecer com o nome novo, e `max(nome)` daria o maior em ordem
        # alfabetica.
        names = self.repository.get_latest_names(
            scope.restaurant_id,
            [row.customer_phone for row in rows],
        )

        return AdminCustomerListResponse(
            items=[self._item(row, names) for row in rows],
            total=total,
            limit=limit,
            offset=offset,
        )

    @staticmethod
    def _build_filters(
        segment: CustomerSegment | None,
        last_order_from: date | None,
        last_order_to: date | None,
        min_ticket: Decimal | None,
        max_ticket: Decimal | None,
    ) -> CustomerListFilters:
        """As datas do lojista viram instantes UTC; os intervalos sao conferidos.

        Recorte invertido responde 400 e nao lista vazia: `min_ticket` maior
        que `max_ticket` e bug de quem chamou, e uma lista vazia deixaria o
        lojista procurando o cliente que sumiu da tela.

        A conversao de fuso e a mesma dos relatorios (`date_window`): a data
        que chega e o dia do lojista, e sem converter, tres horas de pedidos
        caem no dia errado do recorte.
        """
        if last_order_from and last_order_to and last_order_from > last_order_to:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="last_order_from nao pode ser depois de last_order_to.",
            )
        if min_ticket is not None and max_ticket is not None and min_ticket > max_ticket:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="min_ticket nao pode ser maior que max_ticket.",
            )

        return CustomerListFilters(
            segment=segment,
            last_order_from=day_start(last_order_from) if last_order_from else None,
            last_order_to=day_end_exclusive(last_order_to) if last_order_to else None,
            min_ticket=min_ticket,
            max_ticket=max_ticket,
        )

    @staticmethod
    def _item(row, names: dict[str, str]) -> AdminCustomerListItem:
        """Uma linha da consulta virando uma linha do contrato.

        Nenhuma conta aqui: `money_to_float` e serializacao, e o resto ja veio
        calculado pelo banco.
        """
        return AdminCustomerListItem(
            customer_name=names.get(row.customer_phone, ""),
            customer_phone=row.customer_phone,
            orders_count=row.orders_count,
            billable_orders_count=row.billable_orders_count,
            total_spent=money_to_float(row.total_spent),
            average_ticket=money_to_float(row.average_ticket),
            first_order_at=row.first_order_at,
            last_order_at=row.last_order_at,
            days_since_last_order=row.days_since_last_order,
            cadence_days=row.cadence_days,
            segment=row.segment,
        )

    @staticmethod
    def _normalize_search(search: str | None) -> str | None:
        if search is None:
            return None
        cleaned = search.strip()
        if not cleaned:
            return None
        return cleaned[:MAX_SEARCH_LENGTH]
