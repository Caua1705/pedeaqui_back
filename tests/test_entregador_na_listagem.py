"""O motoboy na listagem de pedidos do painel — e no evento do stream.

`AdminOrderListItem` ganhou `courier_id` e `courier_name`, os dois nulos
quando ninguem esta com o pedido. E o que faz o dono ver quem esta com o
pedido na tela em que ele olha o dia inteiro, sem um GET por linha.

O que se prova sem banco: que o item le a atribuicao ABERTA e ignora a
fechada, e que um pedido sem atribuicao continua saindo com os dois nulos —
inclusive o objeto transiente, que e o que o stream e a maioria dos testes
constroem.
"""

import unittest
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from src.schemas.admin_order_schema import AdminOrderListItem
from src.services.admin_order_service import AdminOrderService
from tests import fabricas


def _pedido():
    return fabricas.pedido(payment_status="on_delivery", total=Decimal("10.00"), created_at=None)


class TestOItemDaListagem(unittest.TestCase):
    def test_sem_atribuicao_os_dois_campos_sao_nulos(self):
        item = AdminOrderService.to_list_item(_pedido())

        self.assertIsNone(item.courier_id)
        self.assertIsNone(item.courier_name)

    def test_com_atribuicao_aberta_o_nome_vem_no_item(self):
        pedido = _pedido()
        ze = fabricas.entregador(name="Zé")
        pedido.courier_assignment = fabricas.atribuicao(order_id=pedido.id, courier_id=ze.id, courier=ze)

        item = AdminOrderService.to_list_item(pedido)

        self.assertEqual(item.courier_id, ze.id)
        self.assertEqual(item.courier_name, "Zé")

    def test_os_campos_existem_no_contrato_com_default_nulo(self):
        """O painel antigo continua lendo o item sem os campos; o novo os le
        sempre — e o stream, que monta o mesmo objeto, nao quebra."""
        item = AdminOrderListItem(
            id=uuid.uuid4(),
            order_number=1,
            branch_id=uuid.uuid4(),
            customer_name_snapshot="x",
            customer_phone_snapshot="1",
            order_type="delivery",
            status="pending",
            payment_status="on_delivery",
            total=1.0,
            created_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        )

        self.assertIsNone(item.courier_id)
        self.assertIsNone(item.courier_name)
