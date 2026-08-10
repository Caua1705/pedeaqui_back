"""Agregacoes da aba Desempenho do painel.

Separado de `OrderRepository` porque a natureza da consulta e outra: la sao
linhas de pedido carregadas para virar objeto; aqui e `GROUP BY` com `SUM` e
`COUNT` que nunca instanciam um `Order`. Trazer milhares de pedidos para
somar em Python funcionaria ate o primeiro restaurante movimentado.

O recorte de quem entra na conta NAO e reescrito aqui. Vem de
`billable_order_conditions` / `excluded_order_conditions`, as mesmas funcoes
que o extrato de comissao usa. E a unica forma de garantir que o faturamento
da tela e a base do extrato falem do mesmo conjunto de pedidos.

Todas as consultas recebem `start_at`/`end_at` ja convertidos para instantes
UTC pelo service; nenhuma delas conhece data solta. A unica excecao e o
agrupamento por dia, que precisa saber o fuso para decidir a que dia local
pertence um instante — ver `sales_by_day`.
"""

import uuid
from datetime import datetime

from sqlalchemy import Date, cast, desc, func, select
from sqlalchemy.orm import Session

from src.core.constants import PLATFORM_TIMEZONE
from src.models.order_item_model import OrderItem
from src.models.order_model import Order
from src.repositories.order_repository import (
    billable_order_conditions,
    excluded_order_conditions,
)


def _local_day(column):
    """A data LOCAL de um instante, para agrupar por dia.

    `timezone('America/Fortaleza', created_at)` e a forma funcional do
    `AT TIME ZONE` do Postgres: converte o timestamptz para o horario de
    parede da operacao. Sem isso, um pedido das 22h de Fortaleza (01h UTC do
    dia seguinte) apareceria no dia errado do grafico — e o total do grafico
    nao bateria com o do resumo, que ja recorta pelo fuso local.
    """
    return cast(func.timezone(PLATFORM_TIMEZONE, column), Date)


# Somas que aparecem em mais de um relatorio. `coalesce` porque `SUM` de
# conjunto vazio e NULL no SQL, e um periodo sem venda tem que devolver zero
# e nao nulo — o painel mostra "R$ 0,00", nao "R$ None".
def _money_sum(column):
    return func.coalesce(func.sum(column), 0)


class AdminReportRepository:
    def __init__(self, db: Session):
        self.db = db

    def sales_totals(
        self,
        restaurant_id: uuid.UUID,
        start_at: datetime,
        end_at: datetime,
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
        ).where(*billable_order_conditions(restaurant_id, start_at, end_at))
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
    ) -> list[tuple[str, int, object]]:
        """Divisao entrega/retirada: (order_type, pedidos, faturamento)."""
        stmt = (
            select(Order.order_type, func.count(Order.id), _money_sum(Order.total))
            .where(*billable_order_conditions(restaurant_id, start_at, end_at))
            .group_by(Order.order_type)
        )
        return [(row[0], row[1], row[2]) for row in self.db.execute(stmt).all()]

    def sales_by_day(
        self,
        restaurant_id: uuid.UUID,
        start_at: datetime,
        end_at: datetime,
    ) -> list[tuple[object, int, object]]:
        """Serie diaria: (dia local, pedidos, faturamento).

        Devolve so os dias que tiveram pedido. Preencher os dias vazios com
        zero e trabalho do service, que e quem conhece o periodo pedido — o
        banco nao tem como saber que o lojista pediu um mes inteiro.
        """
        day = _local_day(Order.created_at).label("day")
        stmt = (
            select(day, func.count(Order.id), _money_sum(Order.total))
            .where(*billable_order_conditions(restaurant_id, start_at, end_at))
            .group_by(day)
            .order_by(day)
        )
        return [(row[0], row[1], row[2]) for row in self.db.execute(stmt).all()]

    def totals_by_payment_method(
        self,
        restaurant_id: uuid.UUID,
        start_at: datetime,
        end_at: datetime,
    ) -> list[tuple[str | None, int, object]]:
        """Quebra por forma de pagamento: (metodo, pedidos, faturamento).

        `payment_method` e NULLABLE em `orders`, e o nulo chega aqui como
        nulo de proposito: trocar por "outro" no SQL misturaria pedido sem
        forma registrada com pedido que escolheu "outro" de verdade. Quem
        decide como exibir e o service.
        """
        stmt = (
            select(Order.payment_method, func.count(Order.id), _money_sum(Order.total))
            .where(*billable_order_conditions(restaurant_id, start_at, end_at))
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
    ) -> list[tuple[uuid.UUID | None, str, int, int, object]]:
        """Produtos mais vendidos: (product_id, nome, pedidos, unidades, receita).

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
        """
        stmt = (
            select(
                OrderItem.product_id,
                OrderItem.product_name_snapshot,
                func.count(func.distinct(OrderItem.order_id)),
                func.coalesce(func.sum(OrderItem.quantity), 0),
                _money_sum(OrderItem.total),
            )
            .join(Order, Order.id == OrderItem.order_id)
            .where(*billable_order_conditions(restaurant_id, start_at, end_at))
            .group_by(OrderItem.product_id, OrderItem.product_name_snapshot)
            .order_by(desc(func.coalesce(func.sum(OrderItem.quantity), 0)))
            .limit(limit)
        )
        return [(row[0], row[1], row[2], row[3], row[4]) for row in self.db.execute(stmt).all()]

    def cancellation_totals(
        self,
        restaurant_id: uuid.UUID,
        start_at: datetime,
        end_at: datetime,
    ) -> dict:
        """Quantos pedidos ficaram de fora do faturamento, e quanto somavam.

        O valor perdido e informativo: sao pedidos que nao entraram em
        receita nenhuma. Serve para o lojista dimensionar o problema ("perdi
        R$ 800 em recusa esse mes"), nao para conciliacao financeira.
        """
        stmt = select(
            func.count(Order.id),
            _money_sum(Order.total),
        ).where(*excluded_order_conditions(restaurant_id, start_at, end_at))
        row = self.db.execute(stmt).one()
        return {"orders_count": row[0] or 0, "amount_total": row[1]}

    def cancellations_by_status(
        self,
        restaurant_id: uuid.UUID,
        start_at: datetime,
        end_at: datetime,
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
            .where(*excluded_order_conditions(restaurant_id, start_at, end_at))
            .group_by(Order.status, Order.payment_status)
            .order_by(desc(func.count(Order.id)))
        )
        return [(row[0], row[1], row[2], row[3]) for row in self.db.execute(stmt).all()]
