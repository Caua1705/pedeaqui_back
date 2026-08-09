"""Adicionais no detalhe do pedido.

O que estes testes protegem:

1. Os snapshots de `order_item_options` chegam ao contrato. Eles ja eram
   gravados na criacao do pedido e nao apareciam em lugar nenhum na
   leitura — a cozinha nao tinha como saber que aquele prato trocou o arroz
   por espaguete.
2. Vem AGRUPADOS pelo grupo de opcao. E o grupo que separa uma troca
   ("Acompanhamento: espaguete") de uma porcao extra ("Adicional:
   espaguete"), que na comanda sao coisas diferentes.
3. A ordem e estavel. `order_item_options` nao tem coluna de ordem nem
   `created_at` e o id e aleatorio: sem ordenacao explicita, a mesma
   comanda sairia embaralhada a cada requisicao.

Os modelos reais sao usados de proposito (sem sessao, so instanciados): o
montador da resposta le relacionamento, e um SimpleNamespace provaria menos.
"""

import unittest
import uuid
from decimal import Decimal

from main import app
from src.models.order_item_model import OrderItem
from src.models.order_item_option_model import OrderItemOption
from src.models.order_model import Order
from src.services.order_service import OrderService


def make_option(group_name, option_name, *, price="0.00", group_id=None):
    return OrderItemOption(
        id=uuid.uuid4(),
        option_group_id=group_id or uuid.uuid4(),
        option_id=uuid.uuid4(),
        option_group_name_snapshot=group_name,
        option_name_snapshot=option_name,
        additional_price_snapshot=Decimal(price),
    )


def make_item(options=(), **overrides):
    values = {
        "id": uuid.uuid4(),
        "product_id": uuid.uuid4(),
        "product_code_snapshot": None,
        "product_name_snapshot": "Prato feito",
        "product_description_snapshot": None,
        "unit_price_snapshot": Decimal("32.00"),
        "quantity": 1,
        "observation": None,
        "total": Decimal("32.00"),
    }
    values.update(overrides)
    item = OrderItem(**values)
    item.options = list(options)
    return item


def make_order(items):
    order = Order(
        id=uuid.uuid4(),
        order_number=1234,
        restaurant_id=uuid.uuid4(),
        branch_id=uuid.uuid4(),
        customer_name_snapshot="Dona Maria",
        customer_phone_snapshot="85999999999",
        order_type="delivery",
        status="preparing",
        payment_status="on_delivery",
        subtotal=Decimal("32.00"),
        delivery_fee=Decimal("5.00"),
        service_fee=Decimal("0.00"),
        # Os defaults destas colunas so sao aplicados no INSERT, e aqui o
        # pedido nunca chega ao banco.
        coupon_discount_amount=Decimal("0.00"),
        cashback_redeemed_amount=Decimal("0.00"),
        discount_total=Decimal("0.00"),
        total=Decimal("37.00"),
    )
    order.items = list(items)
    order.status_history = []
    return order


def detail_of(items):
    return OrderService.to_order_detail_response(make_order(items))


class GroupingTests(unittest.TestCase):
    def test_options_of_the_same_group_stay_together(self):
        group_id = uuid.uuid4()
        item = make_item([
            make_option("Acompanhamento", "Espaguete", group_id=group_id),
            make_option("Acompanhamento", "Farofa", group_id=group_id),
        ])

        groups = detail_of([item]).items[0].option_groups

        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].option_group_name_snapshot, "Acompanhamento")
        self.assertEqual(
            [option.option_name_snapshot for option in groups[0].options],
            ["Espaguete", "Farofa"],
        )

    def test_same_option_name_in_different_groups_does_not_merge(self):
        # O caso que motivou o agrupamento: "espaguete" no lugar do arroz e
        # "espaguete" como porcao extra sao pedidos diferentes na cozinha.
        item = make_item([
            make_option("Acompanhamento", "Espaguete"),
            make_option("Adicional", "Espaguete", price="7.00"),
        ])

        groups = detail_of([item]).items[0].option_groups

        self.assertEqual(
            [group.option_group_name_snapshot for group in groups],
            ["Acompanhamento", "Adicional"],
        )
        self.assertEqual(
            [group.options[0].additional_price_snapshot for group in groups],
            [0.0, 7.0],
        )

    def test_the_order_of_groups_and_options_is_stable(self):
        # Os mesmos adicionais chegando em ordens diferentes do banco
        # precisam sair na mesma ordem, senao a comanda muda de posicao
        # entre dois refreshes da tela.
        salad = uuid.uuid4()
        drink = uuid.uuid4()
        first = make_item([
            make_option("Salada", "Tomate", group_id=salad),
            make_option("Bebida", "Guarana", group_id=drink),
            make_option("Salada", "Alface", group_id=salad),
        ])
        second = make_item([
            make_option("Salada", "Alface", group_id=salad),
            make_option("Salada", "Tomate", group_id=salad),
            make_option("Bebida", "Guarana", group_id=drink),
        ])

        def shape(item):
            return [
                (group.option_group_name_snapshot,
                 [option.option_name_snapshot for option in group.options])
                for group in detail_of([item]).items[0].option_groups
            ]

        self.assertEqual(shape(first), shape(second))
        self.assertEqual(
            shape(first),
            [("Bebida", ["Guarana"]), ("Salada", ["Alface", "Tomate"])],
        )

    def test_item_without_options_gets_an_empty_list(self):
        # Lista vazia e nao nulo: a tela nao precisa de dois caminhos para o
        # item simples, que e a maioria.
        self.assertEqual(detail_of([make_item()]).items[0].option_groups, [])

    def test_the_item_price_already_carries_the_additions(self):
        # `unit_price_snapshot` e gravado com os adicionais somados
        # (OrderService._build_order_item). O valor de cada adicional vem
        # junto so para conferencia: somar de novo cobraria o cliente duas
        # vezes na tela.
        item = make_item(
            [make_option("Adicional", "Bacon", price="6.00")],
            unit_price_snapshot=Decimal("38.00"),
            total=Decimal("38.00"),
        )

        response_item = detail_of([item]).items[0]

        self.assertEqual(response_item.unit_price_snapshot, 38.0)
        self.assertEqual(response_item.option_groups[0].options[0].additional_price_snapshot, 6.0)


class PublishedContractTests(unittest.TestCase):
    """O painel escreve a tela a partir do /openapi.json.

    Um campo que existe na resposta mas nao no documento e um campo que o
    frontend so descobre lendo o backend.
    """

    @classmethod
    def setUpClass(cls):
        cls.schema = app.openapi()

    def test_the_order_item_publishes_the_option_groups(self):
        item = self.schema["components"]["schemas"]["OrderItemResponse"]

        self.assertIn("option_groups", item["properties"])

    def test_the_group_publishes_every_snapshot_the_kitchen_reads(self):
        group = self.schema["components"]["schemas"]["OrderItemOptionGroupResponse"]
        option = self.schema["components"]["schemas"]["OrderItemOptionResponse"]

        self.assertIn("option_group_name_snapshot", group["properties"])
        self.assertIn("options", group["properties"])
        for field in ("option_name_snapshot", "additional_price_snapshot"):
            self.assertIn(field, option["properties"])

    def test_the_admin_detail_route_answers_with_that_schema(self):
        operation = self.schema["paths"]["/admin/orders/{order_id}"]["get"]
        body = operation["responses"]["200"]["content"]["application/json"]["schema"]

        self.assertEqual(body["$ref"], "#/components/schemas/OrderDetailResponse")


if __name__ == "__main__":
    unittest.main()
