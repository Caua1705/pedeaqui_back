import uuid
from datetime import datetime

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from src.models.order_item_model import OrderItem
from src.models.order_item_option_model import OrderItemOption
from src.models.order_model import Order
from src.models.order_status_history_model import OrderStatusHistory
from src.models.branch_model import Branch
from src.models.restaurant_model import Restaurant


# Pedido nesses status nao virou venda, entao nao gera comissao. Fica aqui
# e nao no service porque as duas consultas do relatorio (o extrato e a
# contagem do que sobrou de fora) precisam concordar exatamente.
NON_BILLABLE_ORDER_STATUSES = ("cancelled", "rejected")


def _build_search_condition(search: str):
    """Busca do painel: numero do pedido ou nome do cliente.

    O lojista digita as duas coisas no mesmo campo — ora o numero que o
    cliente leu no telefone, ora "dona maria". So da para saber qual e
    olhando o que foi digitado: so digitos vira comparacao exata de
    order_number (que e BigInteger; um ILIKE ali forcaria cast da coluna
    inteira e perderia o indice), qualquer outra coisa vira ILIKE no nome.

    `%` e `_` sao escapados: sem isso um cliente chamado "Ana_" nao seria
    encontrado e um "%" sozinho listaria a base toda.
    """
    digits = search.strip()
    if digits.isdigit():
        return Order.order_number == int(digits)

    escaped = digits.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return Order.customer_name_snapshot.ilike(f"%{escaped}%", escape="\\")


class OrderRepository:
    def __init__(self, db: Session):
        self.db = db

    def create_order(self, order: Order) -> Order:
        self.db.add(order)
        self.db.flush()
        return order

    def create_order_items(self, items: list[OrderItem]) -> list[OrderItem]:
        self.db.add_all(items)
        self.db.flush()
        return items

    def create_order_item_options(self, options: list[OrderItemOption]) -> list[OrderItemOption]:
        self.db.add_all(options)
        self.db.flush()
        return options

    def create_status_history(self, history: OrderStatusHistory) -> OrderStatusHistory:
        self.db.add(history)
        self.db.flush()
        return history

    def get_order_by_tracking_token(
        self,
        restaurant_id: uuid.UUID,
        tracking_token: str,
    ) -> Order | None:
        # Substituiu a busca por (order_number, telefone), que era
        # enumeravel: order_number vem de uma sequence global.
        stmt = (
            select(Order)
            .options(selectinload(Order.items), selectinload(Order.status_history))
            .where(
                Order.restaurant_id == restaurant_id,
                Order.tracking_token == tracking_token,
            )
        )
        return self.db.scalar(stmt)

    def get_order_detail_for_customer(
        self,
        order_id: uuid.UUID,
        customer_id: uuid.UUID,
    ) -> Order | None:
        # customer_id e obrigatorio pelo mesmo motivo que restaurant_id e em
        # get_order_detail: sem ele a rota entrega o pedido de qualquer um
        # para quem tiver o UUID.
        stmt = (
            select(Order)
            .options(selectinload(Order.items), selectinload(Order.status_history))
            .where(Order.id == order_id, Order.customer_id == customer_id)
        )
        return self.db.scalar(stmt)

    def list_orders_by_restaurant(
        self,
        restaurant_id: uuid.UUID,
        branch_id: uuid.UUID | None = None,
        status: str | None = None,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
        search: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Order]:
        stmt = (
            select(Order)
            .where(*self._admin_list_conditions(
                restaurant_id=restaurant_id,
                branch_id=branch_id,
                status=status,
                start_at=start_at,
                end_at=end_at,
                search=search,
            ))
            .order_by(Order.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(self.db.scalars(stmt).all())

    def count_orders_by_restaurant(
        self,
        restaurant_id: uuid.UUID,
        branch_id: uuid.UUID | None = None,
        status: str | None = None,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
        search: str | None = None,
    ) -> int:
        """Total de pedidos que casam com o filtro, ignorando a pagina.

        Consulta separada porque o painel precisa do numero de paginas: com
        so a pagina em maos nao da para saber se existe uma proxima.
        """
        stmt = select(func.count()).select_from(Order).where(
            *self._admin_list_conditions(
                restaurant_id=restaurant_id,
                branch_id=branch_id,
                status=status,
                start_at=start_at,
                end_at=end_at,
                search=search,
            )
        )
        return self.db.scalar(stmt) or 0

    def count_orders_grouped_by_status(
        self,
        restaurant_id: uuid.UUID,
        branch_id: uuid.UUID | None = None,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
        search: str | None = None,
    ) -> dict[str, int]:
        """Quantos pedidos em cada status, para os badges da tela.

        Um GROUP BY e nao uma consulta por status: sao oito status, e oito
        COUNT(*) sequenciais por atualizacao de badge e trafego a toa no
        banco. `status` de proposito nao entra no filtro — filtrar por
        status aqui zeraria todos os outros contadores.
        """
        stmt = (
            select(Order.status, func.count())
            .where(*self._admin_list_conditions(
                restaurant_id=restaurant_id,
                branch_id=branch_id,
                status=None,
                start_at=start_at,
                end_at=end_at,
                search=search,
            ))
            .group_by(Order.status)
        )
        return {row[0]: row[1] for row in self.db.execute(stmt).all()}

    def list_orders_created_since(
        self,
        restaurant_id: uuid.UUID,
        branch_id: uuid.UUID | None,
        since: datetime,
        limit: int,
    ) -> list[Order]:
        """Pedidos novos desde `since`, para o stream do painel.

        Ordem CRESCENTE, ao contrario da listagem: o stream entrega evento
        na ordem em que aconteceu, e o cursor avanca para o created_at do
        ultimo emitido.
        """
        conditions = [Order.restaurant_id == restaurant_id, Order.created_at > since]
        if branch_id is not None:
            conditions.append(Order.branch_id == branch_id)

        stmt = (
            select(Order)
            .where(*conditions)
            .order_by(Order.created_at.asc())
            .limit(limit)
        )
        return list(self.db.scalars(stmt).all())

    def list_status_changes_since(
        self,
        restaurant_id: uuid.UUID,
        branch_id: uuid.UUID | None,
        since: datetime,
        limit: int,
    ) -> list[tuple[OrderStatusHistory, Order]]:
        """Mudancas de status desde `since`, para o stream do painel.

        Le order_status_history e nao orders.updated_at porque updated_at
        muda por qualquer escrita (confirmacao de pagamento, por exemplo) e
        nao diz para qual status o pedido foi. O historico ja e gravado em
        toda transicao por AdminOrderService.update_order_status.

        A juncao com orders existe para o filtro por restaurante e filial:
        order_status_history nao carrega restaurant_id proprio, entao sem
        ela o stream de um lojista veria a mudanca de status de outro.
        """
        conditions = [
            Order.restaurant_id == restaurant_id,
            OrderStatusHistory.created_at > since,
        ]
        if branch_id is not None:
            conditions.append(Order.branch_id == branch_id)

        stmt = (
            select(OrderStatusHistory, Order)
            .join(Order, Order.id == OrderStatusHistory.order_id)
            .where(*conditions)
            .order_by(OrderStatusHistory.created_at.asc())
            .limit(limit)
        )
        return [(row[0], row[1]) for row in self.db.execute(stmt).all()]

    @staticmethod
    def _admin_list_conditions(
        restaurant_id: uuid.UUID,
        branch_id: uuid.UUID | None,
        status: str | None,
        start_at: datetime | None,
        end_at: datetime | None,
        search: str | None,
    ) -> list:
        """WHERE compartilhado pelas tres consultas da listagem do painel.

        Fica junto porque a pagina, o total e os contadores por status
        precisam concordar exatamente — se um deles esquecer o filtro de
        periodo, o lojista ve badge somando 40 numa lista de 12.
        """
        conditions = [Order.restaurant_id == restaurant_id]
        if branch_id is not None:
            conditions.append(Order.branch_id == branch_id)
        if status:
            conditions.append(Order.status == status)
        if start_at is not None:
            conditions.append(Order.created_at >= start_at)
        if end_at is not None:
            # Exclusivo, como em list_orders_for_commission: o service passa
            # o instante em que o dia seguinte comeca para nao perder o
            # pedido feito 23:59:59.7.
            conditions.append(Order.created_at < end_at)
        if search:
            conditions.append(_build_search_condition(search))
        return conditions

    def list_orders_for_commission(
        self,
        restaurant_id: uuid.UUID,
        start_at: datetime,
        end_at: datetime,
    ) -> list[Order]:
        """Pedidos que geram comissao no periodo.

        `end_at` e EXCLUSIVO: o service passa o instante em que o dia
        seguinte comeca, para nao perder pedido feito 23:59:59.7.

        Cancelado, recusado e estornado ficam de fora — nao houve venda.
        """
        stmt = (
            select(Order)
            .where(
                Order.restaurant_id == restaurant_id,
                Order.created_at >= start_at,
                Order.created_at < end_at,
                Order.status.notin_(NON_BILLABLE_ORDER_STATUSES),
                Order.payment_status != "refunded",
            )
            .order_by(Order.created_at.asc())
        )
        return list(self.db.scalars(stmt).all())

    def count_excluded_from_commission(
        self,
        restaurant_id: uuid.UUID,
        start_at: datetime,
        end_at: datetime,
    ) -> int:
        """Quantos pedidos do periodo ficaram de fora do relatorio.

        Consulta separada de proposito: sem ela o lojista compara o numero
        de pedidos do painel com o do extrato, ve a diferenca e nao tem como
        saber se e regra ou bug.
        """
        stmt = (
            select(func.count())
            .select_from(Order)
            .where(
                Order.restaurant_id == restaurant_id,
                Order.created_at >= start_at,
                Order.created_at < end_at,
                or_(
                    Order.status.in_(NON_BILLABLE_ORDER_STATUSES),
                    Order.payment_status == "refunded",
                ),
            )
        )
        return self.db.scalar(stmt) or 0

    def get_order_detail(self, order_id: uuid.UUID, restaurant_id: uuid.UUID) -> Order | None:
        # restaurant_id e obrigatorio de proposito. Enquanto o filtro era so
        # por Order.id, qualquer lojista com um UUID de pedido em maos lia o
        # pedido de outro restaurante — nome, telefone e endereco do cliente.
        # Deixa-lo opcional convidaria a repetir o erro na proxima rota.
        stmt = (
            select(Order)
            .options(selectinload(Order.items), selectinload(Order.status_history))
            .where(Order.id == order_id, Order.restaurant_id == restaurant_id)
        )
        return self.db.scalar(stmt)

    def list_orders_by_customer(self, customer_id: uuid.UUID) -> list[tuple[Order, str, str]]:
        stmt = (
            select(Order, Restaurant.name, Branch.name)
            .join(Restaurant, Restaurant.id == Order.restaurant_id)
            .join(Branch, Branch.id == Order.branch_id)
            .options(selectinload(Order.items))
            .where(Order.customer_id == customer_id)
            .order_by(Order.created_at.desc())
        )
        return [(row[0], row[1], row[2]) for row in self.db.execute(stmt).all()]

    def update_status(self, order: Order, status: str) -> Order:
        order.status = status
        self.db.add(order)
        self.db.flush()
        return order

    def get_order_by_provider_payment(
        self,
        payment_provider: str,
        provider_payment_id: str,
    ) -> Order | None:
        # Sem filtro por restaurante de proposito: o webhook chega do
        # gateway, nao de um tenant, e o par (provider, provider_payment_id)
        # e unico na tabela.
        stmt = select(Order).where(
            Order.payment_provider == payment_provider,
            Order.provider_payment_id == provider_payment_id,
        )
        return self.db.scalar(stmt)

    def attach_payment_intent(
        self,
        order: Order,
        *,
        provider: str,
        provider_payment_id: str,
    ) -> Order:
        order.payment_provider = provider
        order.provider_payment_id = provider_payment_id
        # Uma nova tentativa depois de uma recusa volta o pagamento para
        # "pending"; se ja estava pending, isto e um no-op.
        order.payment_status = "pending"
        self.db.add(order)
        self.db.flush()
        return order

    def update_payment_status(
        self,
        order: Order,
        payment_status: str,
        paid_at=None,
    ) -> Order:
        order.payment_status = payment_status
        if paid_at is not None:
            order.paid_at = paid_at
        self.db.add(order)
        self.db.flush()
        return order
