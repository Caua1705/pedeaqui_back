"""Agregacoes da aba Desempenho do painel.

Separado de `OrderRepository` porque a natureza da consulta e outra: la sao
linhas de pedido carregadas para virar objeto; aqui e `GROUP BY` com `SUM` e
`COUNT` que nunca instanciam um `Order`. Trazer milhares de pedidos para
somar em Python funcionaria ate o primeiro restaurante movimentado.

O recorte de quem entra na conta NAO e reescrito aqui. Vem de
`billable_order_conditions` / `excluded_order_conditions`, as mesmas funcoes
que o extrato de comissao usa. E a unica forma de garantir que o faturamento
da tela e a base do extrato falem do mesmo conjunto de pedidos.

Todas recebem tambem `branch_id`, nulavel, onde nulo significa "o
restaurante inteiro" — o recorte de quem enxerga todas as lojas — e nunca
"filial nenhuma". Ele NAO e aplicado neste arquivo: entra pelas mesmas
`billable_order_conditions` / `excluded_order_conditions`, pelo motivo do
paragrafo acima.

Todas as consultas recebem `start_at`/`end_at` ja convertidos para instantes
UTC pelo service; nenhuma delas conhece data solta. A unica excecao e o
agrupamento por dia, que precisa saber o fuso para decidir a que dia local
pertence um instante — ver `sales_by_day`.
"""

import uuid
from datetime import datetime

from sqlalchemy import Date, Integer, Numeric, Text, and_, cast, desc, func, select
from sqlalchemy.orm import Session

from src.core.constants import PLATFORM_TIMEZONE
from src.models.cashback_transaction_model import CashbackTransaction
from src.models.order_item_model import OrderItem
from src.models.order_model import Order
from src.models.order_status_history_model import OrderStatusHistory
from src.repositories.order_repository import (
    BILLABLE_ORDER_STATUSES,
    BILLABLE_PAYMENT_STATUSES,
    billable_order_conditions,
    excluded_order_conditions,
)


def _local(column):
    """O instante no horario de PAREDE da operacao.

    Base de `_local_day`, `_local_hour` e `_local_weekday`: os tres fazem a
    mesma conversao e precisam concordar. Sem ela, "as 22h" de um pedido de
    Fortaleza vira 1h do dia seguinte, e o pico da noite aparece de
    madrugada.
    """
    return func.timezone(PLATFORM_TIMEZONE, column)


def _local_day(column):
    """A data LOCAL de um instante, para agrupar por dia.

    `timezone('America/Fortaleza', created_at)` e a forma funcional do
    `AT TIME ZONE` do Postgres: converte o timestamptz para o horario de
    parede da operacao. Sem isso, um pedido das 22h de Fortaleza (01h UTC do
    dia seguinte) apareceria no dia errado do grafico — e o total do grafico
    nao bateria com o do resumo, que ja recorta pelo fuso local.
    """
    return cast(_local(column), Date)


# Somas que aparecem em mais de um relatorio. `coalesce` porque `SUM` de
# conjunto vazio e NULL no SQL, e um periodo sem venda tem que devolver zero
# e nao nulo — o painel mostra "R$ 0,00", nao "R$ None".
def _money_sum(column):
    return func.coalesce(func.sum(column), 0)


def _local_hour(column):
    """A hora local, 0 a 23."""
    return cast(func.extract("hour", _local(column)), Integer)


def _local_weekday(column):
    """O dia da semana com 0 = SEGUNDA e 6 = domingo.

    O `dow` do Postgres e 0 = DOMINGO e o `isodow` e 1 = segunda; o nosso e o
    `datetime.weekday()` do Python, 0 = segunda. `isodow - 1` e a conversao,
    e ela e escrita aqui uma vez porque errar isso e a armadilha 1 inteira:
    o numero sai consistente do lado de ca e a tela mostra o dia errado, sem
    erro em lugar nenhum.
    """
    return cast(func.extract("isodow", _local(column)), Integer) - 1


def _minutos(inicio, fim):
    """A diferenca entre dois instantes, em minutos, como `numeric`.

    `EXTRACT(EPOCH FROM (a - b)) / 60`. Numeric e nao float porque o
    resultado atravessa `percentile_cont` e chega ao schema como `Decimal`:
    um float aqui viraria `12.333333333333334` na resposta.
    """
    return cast(func.extract("epoch", fim - inicio), Numeric) / 60


def _estatisticas(coluna):
    """Mediana, p90, media e contagem de uma duracao, na mesma varredura.

    `percentile_cont` e `avg` ignoram nulo sozinhos, e o `count` da coluna
    tambem — e e por isso que os quatro podem sair juntos de um universo em
    que nem todo pedido tem aquele estagio.
    """
    return (
        func.percentile_cont(0.5).within_group(coluna),
        func.percentile_cont(0.9).within_group(coluna),
        func.avg(coluna),
        func.count(coluna),
    )


def _identidade_do_item():
    """O que faz duas linhas de item serem "o mesmo produto" no ranking.

    A chave de catalogo quando ela existe, e o proprio `product_id` quando
    nao. As duas metades sao necessarias:

    - **com chave** — a picanha das duas lojas e um item so. E a pergunta
      inteira que `catalog_key` existe para responder.
    - **sem chave** — o comportamento e o de antes da revisao 20260820_0026:
      cada linha de `products` e um produto. Sem esta metade, dois produtos
      diferentes que por acaso tenham o mesmo nome (e nenhuma chave) seriam
      somados como se fossem um.

    O `cast` para texto e o que permite as duas caberem na mesma expressao;
    o valor nunca e exibido.
    """
    return func.coalesce(
        OrderItem.catalog_key_snapshot,
        cast(OrderItem.product_id, Text),
    )


class AdminReportRepository:
    def __init__(self, db: Session):
        self.db = db

    def sales_totals(
        self,
        restaurant_id: uuid.UUID,
        start_at: datetime,
        end_at: datetime,
        branch_id: uuid.UUID | None = None,
    ) -> dict:
        """Os numeros do topo da tela, em uma consulta so.

        Uma consulta e nao cinco porque todos saem do mesmo varrimento de
        linhas; separa-los multiplicaria por cinco o custo do periodo para
        devolver exatamente a mesma coisa.
        """
        stmt = select(
            func.count(Order.id),
            _money_sum(Order.total),
            _money_sum(Order.subtotal),
            _money_sum(Order.delivery_fee),
            _money_sum(Order.service_fee),
            _money_sum(Order.discount_total),
            _money_sum(Order.commission_amount),
        ).where(
            *billable_order_conditions(restaurant_id, start_at, end_at, branch_id)
        )
        row = self.db.execute(stmt).one()
        return {
            "orders_count": row[0] or 0,
            "revenue_total": row[1],
            "subtotal_total": row[2],
            "delivery_fee_total": row[3],
            "service_fee_total": row[4],
            "discount_total": row[5],
            "commission_total": row[6],
        }

    def totals_by_order_type(
        self,
        restaurant_id: uuid.UUID,
        start_at: datetime,
        end_at: datetime,
        branch_id: uuid.UUID | None = None,
    ) -> list[tuple[str, int, object]]:
        """Divisao entrega/retirada: (order_type, pedidos, faturamento)."""
        stmt = (
            select(Order.order_type, func.count(Order.id), _money_sum(Order.total))
            .where(
                *billable_order_conditions(restaurant_id, start_at, end_at, branch_id)
            )
            .group_by(Order.order_type)
        )
        return [(row[0], row[1], row[2]) for row in self.db.execute(stmt).all()]

    def sales_by_day(
        self,
        restaurant_id: uuid.UUID,
        start_at: datetime,
        end_at: datetime,
        branch_id: uuid.UUID | None = None,
    ) -> list[tuple[object, int, object]]:
        """Serie diaria: (dia local, pedidos, faturamento).

        Devolve so os dias que tiveram pedido. Preencher os dias vazios com
        zero e trabalho do service, que e quem conhece o periodo pedido — o
        banco nao tem como saber que o lojista pediu um mes inteiro.
        """
        day = _local_day(Order.created_at).label("day")
        stmt = (
            select(day, func.count(Order.id), _money_sum(Order.total))
            .where(
                *billable_order_conditions(restaurant_id, start_at, end_at, branch_id)
            )
            .group_by(day)
            .order_by(day)
        )
        return [(row[0], row[1], row[2]) for row in self.db.execute(stmt).all()]

    def totals_by_payment_method(
        self,
        restaurant_id: uuid.UUID,
        start_at: datetime,
        end_at: datetime,
        branch_id: uuid.UUID | None = None,
    ) -> list[tuple[str | None, int, object]]:
        """Quebra por forma de pagamento: (metodo, pedidos, faturamento).

        `payment_method` e NULLABLE em `orders`, e o nulo chega aqui como
        nulo de proposito: trocar por "outro" no SQL misturaria pedido sem
        forma registrada com pedido que escolheu "outro" de verdade. Quem
        decide como exibir e o service.
        """
        stmt = (
            select(Order.payment_method, func.count(Order.id), _money_sum(Order.total))
            .where(
                *billable_order_conditions(restaurant_id, start_at, end_at, branch_id)
            )
            .group_by(Order.payment_method)
            .order_by(desc(_money_sum(Order.total)))
        )
        return [(row[0], row[1], row[2]) for row in self.db.execute(stmt).all()]

    def top_products(
        self,
        restaurant_id: uuid.UUID,
        start_at: datetime,
        end_at: datetime,
        limit: int,
        branch_id: uuid.UUID | None = None,
    ) -> list[tuple[uuid.UUID | None, str, str | None, int, int, object]]:
        """Mais vendidos: (product_id, nome, catalog_key, pedidos, unidades, receita).

        Agrupa pelo NOME gravado no item (`product_name_snapshot`), nao pelo
        nome atual do produto. O item guarda o snapshot justamente porque o
        cardapio muda: um produto renomeado de "Picanha" para "Picanha
        Premium" no meio do periodo apareceria com o nome novo em vendas
        antigas, e o lojista nao reconheceria o proprio relatorio.

        Consequencia aceita: renomear um produto o divide em duas linhas do
        relatorio. E o comportamento correto — foram dois itens diferentes no
        cardapio de quem comprou.

        `product_id` e nullable na tabela (produto pode ter sido apagado) e
        vai junto so para o painel conseguir linkar para a tela de edicao.

        A receita e `SUM(order_items.total)`, o valor da linha do item. NAO
        desconta cupom nem cashback: aqueles sao do pedido inteiro e nao tem
        rateio por item gravado em lugar nenhum. Por isso a soma desta tela e
        MAIOR que o faturamento do resumo quando houve desconto, e a resposta
        diz isso em `revenue_note`.

        **A CHAVE DE CATALOGO E O QUE JUNTA AS LOJAS.** Com o cardapio por
        filial (revisao 20260820_0026), a picanha do Centro e a da Aldeota sao
        duas linhas de `products` com ids diferentes: agrupar por `product_id`
        listaria "Picanha" duas vezes e o dono nunca veria quanto vendeu na
        rede. `_identidade_do_item` resolve isso sem perder o caso antigo — ver
        a funcao.
        """
        identidade = _identidade_do_item()
        stmt = (
            select(
                # `min` e nao a coluna crua porque `product_id` nao esta no
                # GROUP BY. Quando a identidade E o produto, o grupo tem um
                # id so e o `min` devolve exatamente ele; quando e a chave de
                # catalogo, o grupo tem um por loja e o `min` aponta para uma
                # delas — o painel usa este campo so para linkar a tela de
                # edicao, e com o recorte por filial ele volta a ser exato.
                func.min(cast(OrderItem.product_id, Text)),
                OrderItem.product_name_snapshot,
                # Mesma situacao, resultado EXATO: dentro de um grupo a chave
                # ou e a mesma em todas as linhas, ou e nula em todas.
                func.min(OrderItem.catalog_key_snapshot),
                func.count(func.distinct(OrderItem.order_id)),
                func.coalesce(func.sum(OrderItem.quantity), 0),
                _money_sum(OrderItem.total),
            )
            .join(Order, Order.id == OrderItem.order_id)
            .where(
                *billable_order_conditions(restaurant_id, start_at, end_at, branch_id)
            )
            .group_by(identidade, OrderItem.product_name_snapshot)
            .order_by(desc(func.coalesce(func.sum(OrderItem.quantity), 0)))
            .limit(limit)
        )
        return [
            (row[0], row[1], row[2], row[3], row[4], row[5])
            for row in self.db.execute(stmt).all()
        ]

    def cancellation_totals(
        self,
        restaurant_id: uuid.UUID,
        start_at: datetime,
        end_at: datetime,
        branch_id: uuid.UUID | None = None,
    ) -> dict:
        """Quantos pedidos ficaram de fora do faturamento, e quanto somavam.

        O valor perdido e informativo: sao pedidos que nao entraram em
        receita nenhuma. Serve para o lojista dimensionar o problema ("perdi
        R$ 800 em recusa esse mes"), nao para conciliacao financeira.
        """
        stmt = select(
            func.count(Order.id),
            _money_sum(Order.total),
        ).where(
            *excluded_order_conditions(restaurant_id, start_at, end_at, branch_id)
        )
        row = self.db.execute(stmt).one()
        return {"orders_count": row[0] or 0, "amount_total": row[1]}

    def cancellations_by_status(
        self,
        restaurant_id: uuid.UUID,
        start_at: datetime,
        end_at: datetime,
        branch_id: uuid.UUID | None = None,
    ) -> list[tuple[str, str, int, object]]:
        """Quebra por (status, payment_status) dos pedidos que nao viraram venda.

        Os dois eixos juntos, e nao so `status`, porque um pedido `completed`
        com `payment_status='refunded'` tambem esta neste conjunto — e ele e
        um caso operacional bem diferente de um `rejected`: a comida saiu e o
        dinheiro voltou.
        """
        stmt = (
            select(
                Order.status,
                Order.payment_status,
                func.count(Order.id),
                _money_sum(Order.total),
            )
            .where(
                *excluded_order_conditions(restaurant_id, start_at, end_at, branch_id)
            )
            .group_by(Order.status, Order.payment_status)
            .order_by(desc(func.count(Order.id)))
        )
        return [(row[0], row[1], row[2], row[3]) for row in self.db.execute(stmt).all()]

    # -----------------------------------------------------------------
    # /reports/sales-by-hour
    # -----------------------------------------------------------------

    def sales_by_hour(
        self,
        restaurant_id: uuid.UUID,
        start_at: datetime,
        end_at: datetime,
        branch_id: uuid.UUID | None = None,
    ) -> list[tuple[int, int, object]]:
        """Por hora local: (hora, pedidos, faturamento).

        So as horas que tiveram pedido. Preencher as 24 e trabalho do
        service, pelo mesmo motivo de `sales_by_day`: o banco nao sabe que a
        tela desenha o dia inteiro.
        """
        hora = _local_hour(Order.created_at).label("hour")
        stmt = (
            select(hora, func.count(Order.id), _money_sum(Order.total))
            .where(
                *billable_order_conditions(restaurant_id, start_at, end_at, branch_id)
            )
            .group_by(hora)
            .order_by(hora)
        )
        return [(int(row[0]), row[1], row[2]) for row in self.db.execute(stmt).all()]

    def sales_by_weekday_hour(
        self,
        restaurant_id: uuid.UUID,
        start_at: datetime,
        end_at: datetime,
        branch_id: uuid.UUID | None = None,
    ) -> list[tuple[int, int, int, object]]:
        """O mapa dia x hora: (weekday, hora, pedidos, faturamento).

        Consulta propria e nao mais um `GROUP BY` na de cima: as duas
        respondem perguntas diferentes ("a que horas eu vendo" e "em que dia
        e hora"), e juntar obrigaria o service a somar as 24 a partir das
        168 — o que faria o total do grafico depender de o mapa estar certo.

        `weekday` 0 = segunda. Ver `_local_weekday`.
        """
        dia = _local_weekday(Order.created_at).label("weekday")
        hora = _local_hour(Order.created_at).label("hour")
        stmt = (
            select(dia, hora, func.count(Order.id), _money_sum(Order.total))
            .where(
                *billable_order_conditions(restaurant_id, start_at, end_at, branch_id)
            )
            .group_by(dia, hora)
            .order_by(dia, hora)
        )
        return [
            (int(row[0]), int(row[1]), row[2], row[3])
            for row in self.db.execute(stmt).all()
        ]

    # -----------------------------------------------------------------
    # /reports/neighborhoods
    # -----------------------------------------------------------------

    def sales_by_neighborhood(
        self,
        restaurant_id: uuid.UUID,
        start_at: datetime,
        end_at: datetime,
        branch_id: uuid.UUID | None = None,
    ) -> list[tuple[str | None, str | None, int, object]]:
        """Por bairro: (bairro, cidade, pedidos, faturamento). SO ENTREGA.

        O bairro sai do SNAPSHOT do pedido (`address_neighborhood`), e nao
        de `customer_addresses`: o endereco cadastrado muda, e o relatorio
        precisa dizer para onde a comida foi naquele dia.

        **Cidade entra no agrupamento**, e nao so o bairro: "Centro" de
        Fortaleza e "Centro" de Maracanau sao dois lugares, e soma-los faria
        a tela propor estender a area para o bairro errado.

        Bairro nulo continua nulo — ver `NeighborhoodSalesItem`.
        """
        stmt = (
            select(
                Order.address_neighborhood,
                Order.address_city,
                func.count(Order.id),
                _money_sum(Order.total),
            )
            .where(
                *billable_order_conditions(restaurant_id, start_at, end_at, branch_id),
                Order.order_type == "delivery",
            )
            .group_by(Order.address_neighborhood, Order.address_city)
            .order_by(desc(_money_sum(Order.total)))
        )
        return [(row[0], row[1], row[2], row[3]) for row in self.db.execute(stmt).all()]

    def count_non_delivery_orders(
        self,
        restaurant_id: uuid.UUID,
        start_at: datetime,
        end_at: datetime,
        branch_id: uuid.UUID | None = None,
    ) -> int:
        """Quantos pedidos faturados do periodo NAO sao entrega.

        E a diferenca entre o total desta tela e o de `/reports/summary`.
        Publicada porque, sem ela, "faturei 8 mil no resumo e 5 mil por
        bairro" nao tem explicacao visivel.
        """
        stmt = select(func.count(Order.id)).where(
            *billable_order_conditions(restaurant_id, start_at, end_at, branch_id),
            Order.order_type != "delivery",
        )
        return self.db.execute(stmt).scalar() or 0

    # -----------------------------------------------------------------
    # /reports/customers
    # -----------------------------------------------------------------

    def customers_by_recency(
        self,
        restaurant_id: uuid.UUID,
        start_at: datetime,
        end_at: datetime,
        branch_id: uuid.UUID | None = None,
    ) -> dict:
        """Clientes novos x recorrentes no periodo, com o faturamento de cada.

        **A identidade e `customer_phone_snapshot`**, a mesma de
        `/admin/customers` — agrupar por `customer_id` descartaria o pedido
        de visitante, que nao tem conta, e "12 clientes" aqui contaria menos
        gente que a tela de Clientes.

        **"Primeiro pedido" e no RESTAURANTE, e ignora o recorte de filial e
        o de data.** As duas metades sao decisao, e cada uma tem um jeito
        errado de escrever:

        - **ignorar a filial**: com o recorte, quem pediu na Aldeota depois
          de dois anos no Centro nasceria "novo" na Aldeota, e a soma das
          filiais teria mais clientes novos que o restaurante. "Novo" e do
          negocio, nao da porta;
        - **ignorar a data**: a subconsulta varre a vida inteira do cliente
          naquele restaurante. Limita-la ao periodo faria TODO cliente do
          recorte parecer novo, que e o mesmo que nao ter a pergunta.

        Nunca atravessa restaurante: o telefone e agregado dentro de
        `restaurant_id`, entao quem pede em duas marcas e novo nas duas.
        """
        primeiro = (
            select(
                Order.customer_phone_snapshot.label("phone"),
                func.min(Order.created_at).label("first_at"),
            )
            .where(
                Order.restaurant_id == restaurant_id,
                Order.status.in_(BILLABLE_ORDER_STATUSES),
                Order.payment_status.in_(BILLABLE_PAYMENT_STATUSES),
            )
            .group_by(Order.customer_phone_snapshot)
            .subquery()
        )

        # O cliente e novo quando a estreia dele cai DENTRO do recorte. A
        # comparacao usa os mesmos limites da consulta principal, entao um
        # cliente que estreou no periodo e necessariamente conta aqui.
        e_novo = and_(
            primeiro.c.first_at >= start_at,
            primeiro.c.first_at < end_at,
        ).label("novo")

        stmt = (
            select(
                e_novo,
                func.count(func.distinct(Order.customer_phone_snapshot)),
                _money_sum(Order.total),
            )
            .join(primeiro, primeiro.c.phone == Order.customer_phone_snapshot)
            .where(
                *billable_order_conditions(restaurant_id, start_at, end_at, branch_id)
            )
            .group_by(e_novo)
        )

        novos, recorrentes = 0, 0
        receita_nova, receita_recorrente = 0, 0
        for novo, quantos, receita in self.db.execute(stmt).all():
            if novo:
                novos, receita_nova = quantos, receita
            else:
                recorrentes, receita_recorrente = quantos, receita
        return {
            "new_customers_count": novos,
            "returning_customers_count": recorrentes,
            "new_revenue_total": receita_nova,
            "returning_revenue_total": receita_recorrente,
            # A soma e exata: um telefone tem UM `first_at`, entao ele cai em
            # exatamente um dos dois lados. Contar `DISTINCT` de novo aqui
            # custaria uma varredura para devolver a mesma coisa.
            "customers_count": novos + recorrentes,
        }

    def cashback_redeemed_totals(
        self,
        restaurant_id: uuid.UUID,
        start_at: datetime,
        end_at: datetime,
        branch_id: uuid.UUID | None = None,
    ) -> dict:
        """Quanto de saldo entrou nos pedidos faturados do periodo.

        Sai de `orders.cashback_redeemed_amount` e nao do razao: o resgate
        e gravado no pedido na criacao, e e esse valor que o cliente viu
        abatido. Ler a linha negativa de `cashback_transactions` daria o
        mesmo numero por outro caminho e passaria a divergir no dia em que
        um pedido fosse cancelado e o saldo devolvido — ali o razao tem duas
        linhas e o pedido continua com uma.
        """
        stmt = select(
            _money_sum(Order.cashback_redeemed_amount),
            func.count(Order.id).filter(Order.cashback_redeemed_amount > 0),
        ).where(*billable_order_conditions(restaurant_id, start_at, end_at, branch_id))
        row = self.db.execute(stmt).one()
        return {"redeemed_total": row[0], "orders_with_redeem_count": row[1] or 0}

    def cashback_earned_total(
        self,
        restaurant_id: uuid.UUID,
        start_at: datetime,
        end_at: datetime,
        branch_id: uuid.UUID | None = None,
    ):
        """O credito GERADO no periodo, pelas linhas `earned` do razao.

        Sem filtro de `status`: `available`, `used`, `expired` e `cancelled`
        dizem o que aconteceu com o saldo DEPOIS, e a pergunta aqui e quanto
        a campanha gerou. Filtrar por `available` faria o numero de um mes
        encolher sozinho conforme os clientes gastassem.

        **`cashback_transactions` nao tem `branch_id`**, e com recorte de
        filial o credito e atribuido pelo PEDIDO que o gerou (`order_id`).
        Consequencia, e ela e escrita na descricao da rota: credito sem
        pedido — hoje so ajuste manual por SQL — nao entra em recorte de
        filial nenhum, porque nao ha como dizer de qual loja ele e.
        """
        stmt = select(_money_sum(CashbackTransaction.amount)).where(
            CashbackTransaction.restaurant_id == restaurant_id,
            CashbackTransaction.type == "earned",
            CashbackTransaction.created_at >= start_at,
            CashbackTransaction.created_at < end_at,
        )
        if branch_id is not None:
            stmt = stmt.join(Order, Order.id == CashbackTransaction.order_id).where(
                Order.branch_id == branch_id
            )
        return self.db.execute(stmt).scalar()

    # -----------------------------------------------------------------
    # /reports/operations
    # -----------------------------------------------------------------

    def operation_durations(
        self,
        restaurant_id: uuid.UUID,
        start_at: datetime,
        end_at: datetime,
        branch_id: uuid.UUID | None = None,
    ) -> dict:
        """Os tempos entre os carimbos, agregados.

        **O marco de cada estagio e o PRIMEIRO carimbo daquele status**
        (`min`), e nao o ultimo. Um pedido pode voltar a `preparing` depois
        de `ready` — e a promessa que a loja fez foi cumprida ou nao na
        primeira vez que ela disse "pronto".

        **O inicio de `accept_minutes` e `orders.created_at`, e nao a linha
        `pending` do historico.** As duas sao o mesmo instante (o
        `OrderStatusHistory(status="pending")` nasce na mesma transacao do
        pedido, e `now()` e o da transacao), e usar duas fontes para o mesmo
        instante e o caminho para elas divergirem. A coluna vale tambem para
        pedido anterior aquela linha existir.

        `late` compara com `delivery_prep_time_max` do PROPRIO pedido, que
        foi congelado na criacao: o cliente leu aquele prazo na vitrine, e
        julgar contra a configuracao de hoje mediria a loja pela promessa
        que ela nao fez.
        """
        marco = self._marcos_do_pedido(restaurant_id, start_at, end_at, branch_id)

        aceite = _minutos(marco.c.created_at, marco.c.accepted_at)
        preparo = _minutos(marco.c.accepted_at, marco.c.ready_at)
        entrega = _minutos(marco.c.out_for_delivery_at, marco.c.completed_at)

        # Ha prazo prometido E preparo medido: e o unico universo em que a
        # pergunta "atrasou?" tem resposta.
        tem_prazo = and_(
            marco.c.delivery_prep_time_max.is_not(None),
            marco.c.accepted_at.is_not(None),
            marco.c.ready_at.is_not(None),
        )
        atrasou = and_(tem_prazo, preparo > marco.c.delivery_prep_time_max)

        stmt = select(
            func.count(marco.c.id),
            *_estatisticas(aceite),
            *_estatisticas(preparo),
            *_estatisticas(entrega),
            func.count(marco.c.id).filter(atrasou),
            func.count(marco.c.id).filter(tem_prazo),
        )
        row = self.db.execute(stmt).one()
        return {
            "orders_count": row[0] or 0,
            "accept": {"median": row[1], "p90": row[2], "average": row[3], "orders_count": row[4]},
            "prep": {"median": row[5], "p90": row[6], "average": row[7], "orders_count": row[8]},
            "delivery": {"median": row[9], "p90": row[10], "average": row[11], "orders_count": row[12]},
            "late_orders_count": row[13] or 0,
            "late_orders_base_count": row[14] or 0,
        }

    def _marcos_do_pedido(
        self,
        restaurant_id: uuid.UUID,
        start_at: datetime,
        end_at: datetime,
        branch_id: uuid.UUID | None,
    ):
        """Uma linha por pedido faturado, com o instante de cada estagio.

        `LEFT JOIN` e nao `JOIN`: pedido sem historico nenhum continua
        contando no universo (`orders_count`) com os tempos nulos. Com o
        `JOIN`, ele sumiria do denominador e a tela diria que o periodo teve
        menos pedidos do que teve.

        `out_for_delivery` so e lido para pedido de ENTREGA. Na retirada o
        estado nao existe, e um `NULL` ali ja resolveria — mas a condicao
        explicita e o que impede um carimbo indevido (importacao, correcao a
        mao) de virar "tempo de entrega" numa retirada.
        """
        def primeiro(status: str, extra=None):
            condicao = OrderStatusHistory.status == status
            if extra is not None:
                condicao = and_(condicao, extra)
            return func.min(OrderStatusHistory.created_at).filter(condicao)

        e_entrega = Order.order_type == "delivery"
        return (
            select(
                Order.id.label("id"),
                Order.created_at.label("created_at"),
                Order.delivery_prep_time_max.label("delivery_prep_time_max"),
                primeiro("accepted").label("accepted_at"),
                primeiro("ready").label("ready_at"),
                primeiro("out_for_delivery", e_entrega).label("out_for_delivery_at"),
                primeiro("completed", e_entrega).label("completed_at"),
            )
            .select_from(Order)
            .outerjoin(OrderStatusHistory, OrderStatusHistory.order_id == Order.id)
            .where(
                *billable_order_conditions(restaurant_id, start_at, end_at, branch_id)
            )
            .group_by(Order.id, Order.created_at, Order.delivery_prep_time_max)
            .subquery()
        )
