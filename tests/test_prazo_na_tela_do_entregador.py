"""O prazo prometido ao cliente, e o que a tela do motoboy faz com ele.

O que estes testes travam:

- **a âncora é `delivery_estimated_at`, não `created_at`.** Os dois valem o
  mesmo instante hoje (mesmo INSERT), e a diferença aparece quando não há
  estimativa: `created_at` existe em todo pedido, inclusive na retirada que
  nunca teve promessa. Ancorar nele daria prazo a quem não tem prazo;
- **sem promessa, sem prazo.** `None` e nunca "agora", que a tela leria como
  "chegou a hora" — o pior valor possível para a ausência de um;
- **a soma não estoura.** Estimativa gravada sem teto de janela não nasce pelo
  ORM, mas existe no banco (as colunas são nuláveis, e há pedido anterior a
  elas). Somar sobre nulo ali seria `TypeError` na mão do motoboy, na rua;
- **o serializer entrega os três juntos**, porque é assim que eles são
  gravados — e a tela precisa saber que ou vêm todos ou não vem nenhum.
"""

import unittest
from datetime import datetime, timedelta, timezone

from src.services.courier_delivery_service import CourierDeliveryService
from src.services.order_deadline import prazo_prometido
from tests import fabricas as fab


CHECKOUT = datetime(2026, 9, 5, 19, 0, tzinfo=timezone.utc)


def pedido_de_entrega(**sobrescritas):
    """Um pedido de entrega COM promessa: os três campos, como o INSERT grava."""
    campos = {
        "order_type": "delivery",
        "status": "ready",
        "delivery_estimated_at": CHECKOUT,
        "delivery_eta_min": 40,
        "delivery_eta_max": 55,
        "created_at": CHECKOUT,
    }
    campos.update(sobrescritas)
    return fab.pedido(**campos)


class PrazoPrometidoTests(unittest.TestCase):
    def test_e_o_checkout_mais_o_teto_da_janela(self) -> None:
        pedido = pedido_de_entrega()

        self.assertEqual(prazo_prometido(pedido), CHECKOUT + timedelta(minutes=55))

    def test_o_teto_e_o_maximo_e_nunca_o_minimo(self) -> None:
        """É contra o MÁXIMO que "atrasado" é verdade.

        Contar do mínimo faria todo pedido entregue dentro da janela prometida
        aparecer atrasado — e a tela mostraria vermelho num dia perfeito."""
        pedido = pedido_de_entrega()

        prazo = prazo_prometido(pedido)

        self.assertNotEqual(prazo, CHECKOUT + timedelta(minutes=40))
        self.assertEqual(prazo, CHECKOUT + timedelta(minutes=55))

    def test_retirada_nao_tem_prazo(self) -> None:
        """Retirada nunca passa por `_estimate_delivery`, então os três campos
        são nulos. `created_at` está lá — e é por isso que ele não serve de
        âncora."""
        pedido = fab.pedido(order_type="pickup", created_at=CHECKOUT)

        self.assertIsNone(prazo_prometido(pedido))

    def test_estimativa_sem_teto_de_janela_devolve_None_em_vez_de_estourar(
        self,
    ) -> None:
        """A linha não nasce pelo ORM. Ela existe no banco — as colunas são
        nuláveis, e há pedido anterior a elas."""
        pedido = pedido_de_entrega(delivery_eta_max=None)

        self.assertIsNone(prazo_prometido(pedido))

    def test_o_prazo_nao_se_move_com_o_pedido(self) -> None:
        """Nada reescreve os três campos depois da criação.

        O relógio começa no CHECKOUT, e `delivery_eta_max` já inclui o preparo:
        um pedido aceito 20 min depois chega ao entregador com parte do prazo
        gasta. O número continua verdadeiro sobre o CLIENTE — que está mesmo
        esperando além do prometido —, e é a tela que não pode apresentá-lo
        como desempenho do motoboy."""
        pedido = pedido_de_entrega(status="accepted")
        antes = prazo_prometido(pedido)

        pedido.status = "out_for_delivery"

        self.assertEqual(prazo_prometido(pedido), antes)


class RespostaDoEntregadorTests(unittest.TestCase):
    def test_os_tres_campos_saem_juntos(self) -> None:
        pedido = pedido_de_entrega()
        corrida = fab.atribuicao(order_id=pedido.id)

        resposta = CourierDeliveryService._order_response(corrida, pedido)

        self.assertEqual(resposta.delivery_due_at, CHECKOUT + timedelta(minutes=55))
        self.assertEqual(resposta.delivery_eta_min, 40)
        self.assertEqual(resposta.delivery_eta_max, 55)

    def test_sem_promessa_os_tres_sao_nulos_e_nenhum_e_zero(self) -> None:
        """Zero seria "chegou a hora"; nulo é "não há prazo". A tela precisa
        conseguir separar os dois para não desenhar um contador que não
        existe."""
        pedido = fab.pedido(order_type="delivery", status="ready", created_at=CHECKOUT)
        corrida = fab.atribuicao(order_id=pedido.id)

        resposta = CourierDeliveryService._order_response(corrida, pedido)

        self.assertIsNone(resposta.delivery_due_at)
        self.assertIsNone(resposta.delivery_eta_min)
        self.assertIsNone(resposta.delivery_eta_max)

    def test_a_corrida_recebida_depois_do_prazo_e_visivel_pela_resposta(self) -> None:
        """A pergunta de (c), respondida pelos dados que a resposta já leva.

        `assigned_at` depois de `delivery_due_at` significa que o prazo venceu
        ANTES de o motoboy receber a corrida — e é assim que a tela separa "a
        loja demorou a aceitar" de "atrasou na rua", sem nenhum campo a mais."""
        pedido = pedido_de_entrega()
        corrida = fab.atribuicao(
            order_id=pedido.id, assigned_at=CHECKOUT + timedelta(minutes=70)
        )

        resposta = CourierDeliveryService._order_response(corrida, pedido)

        self.assertGreater(resposta.assigned_at, resposta.delivery_due_at)


if __name__ == "__main__":
    unittest.main()
