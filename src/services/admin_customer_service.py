"""Clientes que pediram NESTE restaurante (BLOCO D da Fase 3).

A lista sai de `orders`, nunca de `customers`. E requisito de seguranca e
nao de feature: a conta do cliente e global da plataforma, entao um SELECT
direto em `customers` entregaria ao lojista tambem quem nunca pediu na loja
dele — inclusive os clientes dos concorrentes que usam a mesma plataforma.

Derivando de `orders`, o isolamento e consequencia da propria consulta: sem
pedido naquele restaurante, nao ha linha para agrupar.
"""

import uuid
from datetime import datetime

from sqlalchemy.orm import Session

from src.api.dependencies.admin_scope import AdminScope
from src.repositories.admin_customer_repository import AdminCustomerRepository
from src.schemas.admin_customer_schema import (
    AdminCustomerListItem,
    AdminCustomerListResponse,
)
from src.services.customer_segment import classify_customer, days_since_last_order
from src.utils.money import money_to_float, to_decimal
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
        limit: int = 50,
        offset: int = 0,
    ) -> AdminCustomerListResponse:
        effective_branch_id = scope.resolve_branch_filter(branch_id)
        normalized_search = self._normalize_search(search)

        rows = self.repository.list_customers(
            restaurant_id=scope.restaurant_id,
            branch_id=effective_branch_id,
            search=normalized_search,
            limit=limit,
            offset=offset,
        )
        total = self.repository.count_customers(
            restaurant_id=scope.restaurant_id,
            branch_id=effective_branch_id,
            search=normalized_search,
        )
        # O nome vem em uma segunda consulta porque nao e agregavel: o
        # cliente que corrigiu o proprio nome no ultimo pedido tem que
        # aparecer com o nome novo, e `max(nome)` daria o maior em ordem
        # alfabetica.
        names = self.repository.get_latest_names(
            scope.restaurant_id,
            [row.customer_phone for row in rows],
        )

        # UM `agora` para a pagina inteira. Chamando o relogio dentro do laco,
        # uma pagina lida na virada do dia classificaria as primeiras linhas
        # contra ontem e as ultimas contra hoje — e a linha que mudasse de
        # rotulo no meio seria impossivel de reproduzir depois.
        agora = utcnow()

        return AdminCustomerListResponse(
            items=[self._item(row, names, agora) for row in rows],
            total=total,
            limit=limit,
            offset=offset,
        )

    @staticmethod
    def _item(row, names: dict[str, str], agora: datetime) -> AdminCustomerListItem:
        """Uma linha da consulta virando uma linha do contrato."""
        return AdminCustomerListItem(
            customer_name=names.get(row.customer_phone, ""),
            customer_phone=row.customer_phone,
            orders_count=row.orders_count,
            billable_orders_count=row.billable_orders_count,
            total_spent=money_to_float(row.total_spent),
            average_ticket=AdminCustomerService._average_ticket(
                row.total_spent, row.billable_orders_count
            ),
            first_order_at=row.first_order_at,
            last_order_at=row.last_order_at,
            days_since_last_order=days_since_last_order(row.last_order_at, agora),
            segment=classify_customer(
                orders_count=row.orders_count,
                first_order_at=row.first_order_at,
                last_order_at=row.last_order_at,
                now=agora,
            ),
        )

    @staticmethod
    def _average_ticket(total_spent, billable_orders_count: int) -> float:
        """O gasto dividido pelos pedidos que geraram gasto.

        Por `billable_orders_count` e NUNCA por `orders_count` — os dois
        contam coisas diferentes, e so um deles combina com o numerador.
        """
        if billable_orders_count <= 0:
            return 0.0
        return money_to_float(to_decimal(total_spent) / billable_orders_count)

    @staticmethod
    def _normalize_search(search: str | None) -> str | None:
        if search is None:
            return None
        cleaned = search.strip()
        if not cleaned:
            return None
        return cleaned[:MAX_SEARCH_LENGTH]
