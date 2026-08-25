import uuid
from datetime import datetime

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from src.core.constants import PAYMENT_STATUSES_WITH_LIVE_CHARGE
from src.models.order_item_model import OrderItem
from src.models.order_item_option_model import OrderItemOption
from src.models.order_model import Order
from src.utils.normalization import normalize_text
from src.utils.security import hash_tracking_token, verify_tracking_token
from src.models.order_status_history_model import OrderStatusHistory
from src.models.branch_model import Branch
from src.models.restaurant_model import Restaurant


# Pedido nesses status nao virou venda, entao nao gera comissao. Fica aqui
# e nao no service porque as duas consultas do relatorio (o extrato e a
# contagem do que sobrou de fora) precisam concordar exatamente.
NON_BILLABLE_ORDER_STATUSES = ("cancelled", "rejected")


def billable_order_conditions(
    restaurant_id: uuid.UUID,
    start_at: datetime,
    end_at: datetime,
    branch_id: uuid.UUID | None = None,
) -> list:
    """WHERE de "o que virou venda neste periodo".

    Funcao de modulo, e nao metodo, porque quem precisa dela nao e so este
    repositorio: os relatorios de desempenho (AdminReportRepository) tem que
    contar exatamente o mesmo conjunto que o extrato de comissao. Duas
    copias da regra e o caminho garantido para o painel dizer que o
    faturamento foi X e o extrato dizer que a base foi de menos pedidos que
    isso, sem ninguem conseguir explicar a diferenca.

    `end_at` e EXCLUSIVO: quem chama passa o instante em que o dia seguinte
    comeca, para nao perder pedido feito 23:59:59.7.

    `branch_id` nulo significa "o restaurante inteiro", nunca "filial
    nenhuma": e o recorte de quem enxerga todas as lojas. Ele entra AQUI e
    nao em cada consulta de relatorio, pelo mesmo motivo do paragrafo acima —
    um relatorio que filtrasse a filial por conta propria seria a chance de o
    faturamento da tela e a base do extrato falarem de conjuntos diferentes.
    """
    conditions = [
        Order.restaurant_id == restaurant_id,
        Order.created_at >= start_at,
        Order.created_at < end_at,
        Order.status.notin_(NON_BILLABLE_ORDER_STATUSES),
        Order.payment_status != "refunded",
    ]
    if branch_id is not None:
        conditions.append(Order.branch_id == branch_id)
    return conditions


def excluded_order_conditions(
    restaurant_id: uuid.UUID,
    start_at: datetime,
    end_at: datetime,
    branch_id: uuid.UUID | None = None,
) -> list:
    """O complemento exato de `billable_order_conditions` no mesmo periodo.

    Complemento de verdade: `status IN (...) OR payment_status = 'refunded'`
    e a negacao de `status NOT IN (...) AND payment_status <> 'refunded'`.
    Um pedido do periodo cai em um dos dois conjuntos e nunca nos dois, que
    e o que permite ao relatorio de cancelamentos e ao de faturamento serem
    lidos lado a lado.

    `branch_id` entra do mesmo jeito que no billable, e tem que entrar: com o
    recorte valendo so num dos dois, a taxa de cancelamento sairia com os
    cancelados de UMA loja sobre os faturados da rede.
    """
    conditions = [
        Order.restaurant_id == restaurant_id,
        Order.created_at >= start_at,
        Order.created_at < end_at,
        or_(
            Order.status.in_(NON_BILLABLE_ORDER_STATUSES),
            Order.payment_status == "refunded",
        ),
    ]
    if branch_id is not None:
        conditions.append(Order.branch_id == branch_id)
    return conditions


# Tudo o que `OrderService.to_order_detail_response` le, carregado de uma
# vez. Ficam juntos porque as tres consultas de detalhe (painel, cliente
# logado e token de acompanhamento) desembocam no MESMO montador de
# resposta: uma delas esquecer os adicionais nao daria erro, so uma consulta
# extra por item na hora de serializar.
_ORDER_DETAIL_LOADERS = (
    selectinload(Order.items).selectinload(OrderItem.options),
    selectinload(Order.status_history),
)


def _build_search_condition(search: str):
    """Busca do painel: numero do pedido ou nome do cliente.

    O lojista digita as duas coisas no mesmo campo — ora o numero que o
    cliente leu no telefone, ora "dona maria". So da para saber qual e
    olhando o que foi digitado: so digitos vira comparacao exata de
    order_number (que e BigInteger; um ILIKE ali forcaria cast da coluna
    inteira e perderia o indice), qualquer outra coisa vira ILIKE no nome.

    `%` e `_` sao escapados: sem isso um cliente chamado "Ana_" nao seria
    encontrado e um "%" sozinho listaria a base toda.

    O `normalize_text` poe o termo em NFC antes do ILIKE, que compara bytes:
    "Antônio" composto e decomposto sao iguais na tela e diferentes no
    banco. `customer_name_snapshot` e gravado normalizado pelo mesmo motivo.
    """
    termo = normalize_text(search)
    if termo.isdigit():
        return Order.order_number == int(termo)

    return Order.customer_name_snapshot.ilike(f"%{_escape_like(termo)}%", escape="\\")


def _escape_like(termo: str) -> str:
    """Neutraliza os curingas do LIKE dentro do que o lojista digitou.

    A contrabarra vem primeiro: escapando-a depois, ela escaparia tambem as
    contrabarras que as duas linhas seguintes acabaram de inserir.
    """
    return termo.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


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
        #
        # A busca e pelo HASH: o token em claro nao existe mais no banco, e o
        # que chega pela URL e transformado aqui antes de virar WHERE. Um
        # dump vazado passa a nao conter nenhuma credencial utilizavel.
        token_hash = hash_tracking_token(tracking_token)
        stmt = (
            select(Order)
            .options(*_ORDER_DETAIL_LOADERS)
            .where(
                Order.restaurant_id == restaurant_id,
                Order.tracking_token_hash == token_hash,
            )
        )
        order = self.db.scalar(stmt)
        if order is None:
            return None

        # Reconferencia em tempo constante, pela armadilha 18 — que cita o
        # token de acompanhamento por nome.
        #
        # SEJA HONESTO SOBRE O QUE ISTO COMPRA. Se a linha voltou, o `=` do
        # Postgres ja disse que os hashes sao iguais, e este `compare_digest`
        # devolve True sempre. Ele NAO fecha um canal de tempo: a comparacao
        # de verdade acontece dentro do banco, sobre um indice.
        #
        # O que ele compra e a falha fechada se o WHERE acima deixar de ser
        # igualdade exata um dia — um `ilike` para "facilitar o suporte", um
        # `like` com o escape errado (o mesmo defeito que ja apareceu em
        # admin_menu_repository), uma collation que aproxime formas Unicode
        # (armadilha 31). Em qualquer um desses casos o hash deixa de
        # identificar um pedido so, e esta linha e a que ainda diz nao.
        if not verify_tracking_token(tracking_token, order.tracking_token_hash):
            return None
        return order

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
            .options(*_ORDER_DETAIL_LOADERS)
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
        # Cada linha ja e o par (status, quantidade) que o dicionario quer.
        return dict(self.db.execute(stmt).all())

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
        return list(self.db.execute(stmt).tuples().all())

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
        branch_id: uuid.UUID | None = None,
    ) -> list[Order]:
        """Pedidos que geram comissao no periodo.

        `end_at` e EXCLUSIVO: o service passa o instante em que o dia
        seguinte comeca, para nao perder pedido feito 23:59:59.7.

        Cancelado, recusado e estornado ficam de fora — nao houve venda.
        """
        stmt = (
            select(Order)
            .where(
                *billable_order_conditions(restaurant_id, start_at, end_at, branch_id)
            )
            .order_by(Order.created_at.asc())
        )
        return list(self.db.scalars(stmt).all())

    def count_excluded_from_commission(
        self,
        restaurant_id: uuid.UUID,
        start_at: datetime,
        end_at: datetime,
        branch_id: uuid.UUID | None = None,
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
                *excluded_order_conditions(restaurant_id, start_at, end_at, branch_id)
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
            .options(*_ORDER_DETAIL_LOADERS)
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
        return list(self.db.execute(stmt).tuples().all())

    def list_orders_in_flight(
        self,
        customer_id: uuid.UUID,
        terminal_statuses: tuple[str, ...],
    ) -> list[Order]:
        """Pedidos da pessoa que ainda nao acabaram, do mais antigo ao novo.

        A lista de estados terminais entra por PARAMETRO em vez de ser lida
        aqui: quem sabe o que e terminal e a maquina de estados, e o
        repositorio so consulta.
        """
        stmt = (
            select(Order)
            .where(
                Order.customer_id == customer_id,
                Order.status.not_in(terminal_statuses),
            )
            .order_by(Order.created_at.asc())
        )
        return list(self.db.scalars(stmt).all())

    def last_order_at_by_restaurant(
        self,
        customer_id: uuid.UUID,
        statuses: tuple[str, ...],
    ) -> dict[uuid.UUID, datetime]:
        """Quando foi o ultimo pedido da pessoa em cada restaurante.

        E o relogio da validade do cashback: a validade conta a partir do
        ULTIMO PEDIDO, nao da data do credito, e por isso ela anda para
        frente a cada pedido novo (`docs/cashback.md`, secao 3).

        `statuses` entra por PARAMETRO pelo mesmo motivo de
        `list_orders_in_flight`: quem sabe quais pedidos contam e a maquina
        de estados. Quem chama passa `KITCHEN_ORDER_STATUSES` — pedido que
        chegou a cozinha —, e nao ha uma quarta definicao de "quais pedidos"
        neste projeto.

        A leitura e do status ATUAL, e nao do historico. Consequencia que
        vale saber: o pedido aceito e cancelado depois deixa de contar, e o
        relogio volta para o pedido bom anterior. Ler
        `order_status_history` para descobrir que um dia ele passou por
        `accepted` custaria uma consulta a mais na tela de saldo para
        estender a validade de um pedido que nao aconteceu.
        """
        stmt = (
            select(Order.restaurant_id, func.max(Order.created_at))
            .where(
                Order.customer_id == customer_id,
                Order.status.in_(statuses),
            )
            .group_by(Order.restaurant_id)
        )
        return {row[0]: row[1] for row in self.db.execute(stmt).all()}

    def last_order_at_by_customer(
        self,
        restaurant_id: uuid.UUID,
        statuses: tuple[str, ...],
    ) -> dict[uuid.UUID, datetime]:
        """O ultimo pedido de CADA pessoa naquele restaurante.

        O mesmo relogio de `last_order_at_by_restaurant`, pelo outro eixo: la
        e uma pessoa em varias lojas (a tela de saldo), aqui e uma loja com
        varias pessoas (a expiracao, que varre restaurante a restaurante).

        Uma consulta por restaurante, e nao uma por cliente: a varredura
        diaria passa por todo mundo que tem saldo, e o N+1 aqui seria uma
        consulta por pessoa.
        """
        stmt = (
            select(Order.customer_id, func.max(Order.created_at))
            .where(
                Order.restaurant_id == restaurant_id,
                Order.customer_id.is_not(None),
                Order.status.in_(statuses),
            )
            .group_by(Order.customer_id)
        )
        return {row[0]: row[1] for row in self.db.execute(stmt).all()}

    def list_all_by_customer(self, customer_id: uuid.UUID) -> list[Order]:
        """Todos os pedidos da pessoa, sem join nem ordem.

        Existe separado de `list_orders_by_customer` porque este aqui alimenta
        uma ESCRITA (a anonimizacao), e o join com restaurante e filial daquele
        so serve para montar a tela de historico.
        """
        stmt = select(Order).where(Order.customer_id == customer_id)
        return list(self.db.scalars(stmt).all())

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

    def list_orders_awaiting_refund(
        self,
        since: datetime,
        limit: int = 200,
    ) -> list[Order]:
        """Pedidos que acabaram sem virar venda e ainda tem cobranca viva.

        **Nao existe coluna de "estorno pendente", e nao precisa existir**:
        pedido terminal sem venda com `payment_status` ainda em pending,
        in_review ou paid JA e a descricao completa do que falta fazer. Uma
        coluna de fila seria um segundo lugar para a mesma verdade, e o
        primeiro a sair de sincronia num rollback.

        Assim que o estorno (ou o cancelamento da cobranca) e aplicado, o
        pedido sai do conjunto sozinho — `refunded` e `failed` nao estao na
        lista.

        Sem filtro por restaurante de proposito: quem chama e a varredura de
        manutencao, que nao e um tenant. `since` limita a varredura porque a
        tabela cresce para sempre e o que esta parado ha meses ja passou do
        prazo de estorno do gateway — e ai o conserto e humano, nao uma
        retentativa a mais.
        """
        stmt = (
            select(Order)
            .where(
                Order.status.in_(NON_BILLABLE_ORDER_STATUSES),
                Order.payment_flow == "online",
                Order.payment_status.in_(PAYMENT_STATUSES_WITH_LIVE_CHARGE),
                Order.provider_payment_id.isnot(None),
                Order.created_at >= since,
            )
            .order_by(Order.created_at)
            .limit(limit)
        )
        return list(self.db.scalars(stmt).all())

    def attach_payment_intent(
        self,
        order: Order,
        *,
        provider: str,
        provider_payment_id: str,
        payment_status: str = "pending",
    ) -> Order:
        order.payment_provider = provider
        order.provider_payment_id = provider_payment_id
        # O default cobre o pix, cujo veredito so chega por webhook: uma nova
        # tentativa depois de uma recusa volta para "pending", e se ja estava
        # pending isto e um no-op.
        #
        # No CARTAO quem chama passa o desfecho que o gateway ja respondeu no
        # proprio POST — aprovado, recusado ou em analise.
        order.payment_status = payment_status
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
