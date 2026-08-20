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
"""

import uuid

from sqlalchemy import Row, distinct, func, select
from sqlalchemy.orm import Session

from src.models.order_model import Order
from src.repositories.order_repository import NON_BILLABLE_ORDER_STATUSES
from src.utils.normalization import normalize_text


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
        branch_id: uuid.UUID | None = None,
        search: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Row]:
        """Uma linha por telefone, com o resumo dos pedidos daquele cliente.

        `total_spent` ignora cancelado e recusado (mesma lista do relatorio
        de comissao): pedido cancelado nao e dinheiro que entrou, e somar
        faria o lojista ver como melhor cliente quem mais desistiu.

        `billable_orders_count` usa EXATAMENTE o mesmo filtro, e existe por
        causa da divisao: o ticket medio e `total_spent` dividido por ele, e
        nunca por `orders_count`. Um numerador filtrado sobre um denominador
        que nao e sub-reporta o ticket de todo cliente que ja cancelou
        alguma coisa — e o erro nao aparece em lugar nenhum, so num numero
        um pouco menor do que deveria.
        """
        faturaveis = Order.status.not_in(NON_BILLABLE_ORDER_STATUSES)
        spent = func.sum(Order.total).filter(faturaveis)
        stmt = (
            select(
                Order.customer_phone_snapshot.label("customer_phone"),
                func.count(Order.id).label("orders_count"),
                # `count` nunca devolve NULL, entao aqui nao ha `coalesce`
                # para fazer — diferente do `sum` da linha de baixo.
                func.count(Order.id).filter(faturaveis).label("billable_orders_count"),
                func.coalesce(spent, 0).label("total_spent"),
                func.min(Order.created_at).label("first_order_at"),
                func.max(Order.created_at).label("last_order_at"),
            )
            .where(*self._conditions(restaurant_id, branch_id, search))
            .group_by(Order.customer_phone_snapshot)
            # Quem pediu por ultimo primeiro: e a ordem util para o lojista
            # que abre a tela para achar o cliente que acabou de ligar.
            .order_by(func.max(Order.created_at).desc())
            .limit(limit)
            .offset(offset)
        )
        return list(self.db.execute(stmt).all())

    def count_customers(
        self,
        restaurant_id: uuid.UUID,
        branch_id: uuid.UUID | None = None,
        search: str | None = None,
    ) -> int:
        stmt = select(func.count(distinct(Order.customer_phone_snapshot))).where(
            *self._conditions(restaurant_id, branch_id, search)
        )
        return self.db.scalar(stmt) or 0

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
        """WHERE compartilhado pela pagina e pela contagem.

        Junto pelo mesmo motivo de OrderRepository._admin_list_conditions:
        um filtro que existe so em um dos lados devolve pagina que nao bate
        com o total.
        """
        conditions = [Order.restaurant_id == restaurant_id]
        if branch_id is not None:
            conditions.append(Order.branch_id == branch_id)
        if search:
            conditions.append(_build_customer_search_condition(search))
        return conditions
