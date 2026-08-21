"""Consultas de clientes do painel (BLOCO D da Fase 3).

**Nenhuma consulta deste arquivo toca a tabela `customers`.** A conta do
cliente e global da plataforma: um SELECT em `customers` devolveria tambem
quem nunca pediu neste restaurante, e o painel de um lojista passaria a
listar a base de todos os outros. O que existe aqui e a agregacao de
`orders` por telefone dentro de UM restaurante — quem nunca pediu la
simplesmente nao aparece, porque nao ha linha.

Por isso os dados vem dos snapshots do pedido (`customer_name_snapshot`,
`customer_phone_snapshot`) e nao do cadastro: e exatamente o que o cliente
informou AO FAZER O PEDIDO naquele restaurante. Isso tambem cobre o pedido
de visitante, que nao tem `customer_id`.

O agrupamento e por telefone e nao por `customer_id` porque o visitante nao
tem id — agrupar por id descartaria justamente os pedidos sem conta.

## A agregacao virou subquery, e isso e a frente dos filtros (21/08/2026)

`segment` e `average_ticket` saem de agregados (`count`, `sum`, `min`,
`max`), e o Postgres nao deixa referenciar o alias de um `SELECT` no `HAVING`
da mesma consulta. Filtrar por eles ali exigiria repetir o `CASE` inteiro
dentro do `HAVING`.

Entao a agregacao virou `_aggregate`, uma subquery, e os cinco filtros sao um
`WHERE` do lado de FORA. Duas consequencias que valem mais que a economia de
digitacao:

- **os filtros valem antes do `LIMIT`.** Filtrar depois de paginar devolveria
  pagina com tres linhas de cinquenta;
- **a pagina e a contagem partem da mesma subquery e do mesmo `WHERE`.** E o
  motivo pelo qual `_conditions` ja existia: filtro que vale so em um dos
  lados devolve pagina que nao bate com o total.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Row, func, select
from sqlalchemy.orm import Session

from src.models.order_model import Order
from src.repositories.order_repository import NON_BILLABLE_ORDER_STATUSES
from src.schemas.admin_customer_schema import CustomerSegment
from src.services.customer_segment import (
    average_ticket_expression,
    cadence_expression,
    days_since_expression,
    segment_expression,
)
from src.utils.normalization import normalize_text


@dataclass(frozen=True)
class CustomerListFilters:
    """Os cinco filtros da tela, ja no tipo em que a consulta os compara.

    As datas chegam da querystring como `date` no fuso da operacao e viram
    instantes UTC no service (`src/utils/date_window.py`) — aqui elas ja sao
    o limite exato, com o fim EXCLUSIVO.

    Objeto e nao cinco parametros soltos porque eles atravessam DUAS
    consultas, a pagina e a contagem, e precisam chegar identicos nas duas.
    """

    segment: CustomerSegment | None = None
    last_order_from: datetime | None = None
    last_order_to: datetime | None = None
    min_ticket: Decimal | None = None
    max_ticket: Decimal | None = None


SEM_FILTRO = CustomerListFilters()


def _build_customer_search_condition(search: str):
    """Busca de cliente no painel: telefone ou nome.

    So digitos vira busca por telefone; qualquer outra coisa vira ILIKE no
    nome. O telefone e guardado normalizado (so digitos, ver
    CustomerInput.validate_and_normalize_phone), entao a comparacao por
    prefixo funciona com o que o lojista digita do jeito que ele digita.

    `%` e `_` sao escapados pela mesma razao das outras buscas: sem isso um
    "%" sozinho lista a base inteira.

    E o `normalize_text` poe o termo em NFC, porque o ILIKE do nome compara
    bytes e "Antônio" tem duas formas Unicode identicas na tela. O ramo do
    telefone nao se importa (so digitos), mas normalizar antes do `isdigit`
    mantem um caminho so.
    """
    cleaned = normalize_text(search)
    escaped = cleaned.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    if cleaned.isdigit():
        return Order.customer_phone_snapshot.like(f"{escaped}%", escape="\\")
    return Order.customer_name_snapshot.ilike(f"%{escaped}%", escape="\\")


class AdminCustomerRepository:
    def __init__(self, db: Session):
        self.db = db

    def list_customers(
        self,
        restaurant_id: uuid.UUID,
        now: datetime,
        branch_id: uuid.UUID | None = None,
        search: str | None = None,
        filters: CustomerListFilters = SEM_FILTRO,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Row]:
        """Uma linha por telefone, com o resumo e a classificacao do cliente."""
        agregado = self._aggregate(restaurant_id, now, branch_id, search)
        stmt = (
            select(agregado)
            .where(*self._filter_conditions(agregado, filters))
            # Quem pediu por ultimo primeiro: e a ordem util para o lojista
            # que abre a tela para achar o cliente que acabou de ligar.
            .order_by(agregado.c.last_order_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(self.db.execute(stmt).all())

    def count_customers(
        self,
        restaurant_id: uuid.UUID,
        now: datetime,
        branch_id: uuid.UUID | None = None,
        search: str | None = None,
        filters: CustomerListFilters = SEM_FILTRO,
    ) -> int:
        """Quantos clientes o recorte inteiro tem, com os MESMOS filtros.

        Conta as linhas da subquery, e nao `distinct(telefone)` sobre
        `orders`: depois dos filtros, "quantos telefones existem" e "quantos
        sobreviveram ao filtro" deixaram de ser a mesma pergunta.
        """
        agregado = self._aggregate(restaurant_id, now, branch_id, search)
        stmt = (
            select(func.count())
            .select_from(agregado)
            .where(*self._filter_conditions(agregado, filters))
        )
        return self.db.scalar(stmt) or 0

    def _aggregate(
        self,
        restaurant_id: uuid.UUID,
        now: datetime,
        branch_id: uuid.UUID | None,
        search: str | None,
    ):
        """Uma linha por telefone, com tudo o que a tela mostra E filtra.

        `total_spent` ignora cancelado e recusado (mesma lista do relatorio
        de comissao): pedido cancelado nao e dinheiro que entrou, e somar
        faria o lojista ver como melhor cliente quem mais desistiu.

        `billable_orders_count` usa EXATAMENTE o mesmo filtro, e existe por
        causa da divisao: o ticket medio e `total_spent` dividido por ele, e
        nunca por `orders_count`. Um numerador filtrado sobre um denominador
        que nao e sub-reporta o ticket de todo cliente que ja cancelou
        alguma coisa — e o erro nao aparece em lugar nenhum, so num numero
        um pouco menor do que deveria.

        `cadence_days` sai junto e nao serve a filtro nenhum: e o numero que
        EXPLICA o rotulo quando o lojista perguntar por que o vizinho da
        lista, com o mesmo tempo sem pedir, tem outra etiqueta.
        """
        faturaveis = Order.status.not_in(NON_BILLABLE_ORDER_STATUSES)
        orders_count = func.count(Order.id)
        # `count` nunca devolve NULL, entao aqui nao ha `coalesce` para
        # fazer — diferente do `sum` da linha de baixo.
        billable_orders_count = func.count(Order.id).filter(faturaveis)
        total_spent = func.coalesce(func.sum(Order.total).filter(faturaveis), 0)
        first_order_at = func.min(Order.created_at)
        last_order_at = func.max(Order.created_at)

        return (
            select(
                Order.customer_phone_snapshot.label("customer_phone"),
                orders_count.label("orders_count"),
                billable_orders_count.label("billable_orders_count"),
                total_spent.label("total_spent"),
                average_ticket_expression(
                    total_spent, billable_orders_count
                ).label("average_ticket"),
                first_order_at.label("first_order_at"),
                last_order_at.label("last_order_at"),
                days_since_expression(last_order_at, now).label("days_since_last_order"),
                cadence_expression(
                    orders_count, first_order_at, last_order_at
                ).label("cadence_days"),
                segment_expression(
                    orders_count, first_order_at, last_order_at, now
                ).label("segment"),
            )
            .where(*self._conditions(restaurant_id, branch_id, search))
            .group_by(Order.customer_phone_snapshot)
            .subquery()
        )

    @staticmethod
    def _filter_conditions(agregado, filters: CustomerListFilters) -> list:
        """Os cinco filtros, do lado de FORA da agregacao.

        Cada um so entra quando veio preenchido: filtro ausente nao pode
        virar comparacao com `None`, que nao casaria linha nenhuma.
        """
        conditions = []
        if filters.segment is not None:
            conditions.append(agregado.c.segment == filters.segment.value)
        if filters.last_order_from is not None:
            conditions.append(agregado.c.last_order_at >= filters.last_order_from)
        if filters.last_order_to is not None:
            # Fim EXCLUSIVO: o service manda a meia-noite do dia seguinte,
            # senao o pedido das 23:59:59.7 fica de fora do proprio dia.
            conditions.append(agregado.c.last_order_at < filters.last_order_to)
        if filters.min_ticket is not None:
            conditions.append(agregado.c.average_ticket >= filters.min_ticket)
        if filters.max_ticket is not None:
            conditions.append(agregado.c.average_ticket <= filters.max_ticket)
        return conditions

    def get_latest_names(
        self,
        restaurant_id: uuid.UUID,
        phones: list[str],
    ) -> dict[str, str]:
        """Nome mais recente de cada telefone da pagina.

        Consulta separada porque o nome nao e agregavel: `max(nome)` daria o
        maior em ordem alfabetica, e o cliente que corrigiu o proprio nome
        no ultimo pedido continuaria aparecendo com o antigo. `DISTINCT ON`
        resolve isso pegando a primeira linha de cada telefone na ordem
        pedida.

        Recebe so os telefones da pagina (no maximo `limit`), entao a
        consulta e curta mesmo em restaurante com base grande.
        """
        if not phones:
            return {}

        stmt = (
            select(Order.customer_phone_snapshot, Order.customer_name_snapshot)
            .distinct(Order.customer_phone_snapshot)
            .where(
                Order.restaurant_id == restaurant_id,
                Order.customer_phone_snapshot.in_(phones),
            )
            .order_by(Order.customer_phone_snapshot, Order.created_at.desc())
        )
        return {phone: name for phone, name in self.db.execute(stmt).all()}

    @staticmethod
    def _conditions(
        restaurant_id: uuid.UUID,
        branch_id: uuid.UUID | None,
        search: str | None,
    ) -> list:
        """WHERE de DENTRO da agregacao — restaurante, filial e busca.

        Separado dos cinco filtros de fora porque age em outro nivel: este
        escolhe QUAIS PEDIDOS entram na conta de cada cliente, e o de fora
        escolhe quais clientes sobrevivem depois da conta feita. Trocar um
        pelo outro muda o resultado sem erro nenhum — um `last_order_from`
        aqui dentro nao filtraria "cliente que pediu depois de tal dia", e
        sim recalcularia todo o historico dele a partir daquela data.
        """
        conditions = [Order.restaurant_id == restaurant_id]
        if branch_id is not None:
            conditions.append(Order.branch_id == branch_id)
        if search:
            conditions.append(_build_customer_search_condition(search))
        return conditions
