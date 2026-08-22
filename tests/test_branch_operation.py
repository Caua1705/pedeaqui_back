"""A regra de heranca da operacao da filial (revisao 20260818_0025).

`resolve_branch_operation` e uma funcao pura, e por isso ela tem teste
proprio: e o UNICO lugar que combina o que esta na filial com o padrao do
restaurante, e todo o resto do sistema (pedido, estimativa de entrega,
cardapio, painel) le a resposta dela. Se ela errar, os quatro erram juntos e
concordando — que e o tipo de defeito que ninguem percebe.

O que os testes descrevem, em uma frase: **NULL na filial significa "herda",
e so NULL.** `False` e `0` sao escolhas do lojista e nao podem cair no valor
do restaurante.
"""

import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

from src.services.branch_operation import resolve_branch_operation


# Instante fixo para a pausa da entrega: `resolve_branch_operation` aceita
# `agora` justamente para o teste nao depender do relogio da maquina.
AGORA = datetime(2026, 8, 21, 22, 0, tzinfo=timezone.utc)


def filial(**sobrescritas):
    campos = {
        "is_open": True,
        "accepts_delivery": True,
        "accepts_pickup": True,
        "delivery_paused_until": None,
        "delivery_pause_reason": None,
        "min_order_value": None,
        "service_fee_enabled": None,
        "service_fee_amount": None,
        "estimated_delivery_time_min": None,
        "estimated_delivery_time_max": None,
        "default_delivery_fee": None,
        "free_delivery_enabled": None,
        "free_delivery_min_order_value": None,
    }
    campos.update(sobrescritas)
    return SimpleNamespace(**campos)


def restaurante(**sobrescritas):
    campos = {
        "min_order_value": Decimal("20.00"),
        "service_fee_enabled": True,
        "service_fee_amount": Decimal("0.99"),
        "estimated_delivery_time_min": 30,
        "estimated_delivery_time_max": 60,
        "default_delivery_fee": Decimal("9.00"),
        "free_delivery_enabled": None,
        "free_delivery_min_order_value": None,
    }
    campos.update(sobrescritas)
    return SimpleNamespace(**campos)


class HerancaTests(unittest.TestCase):
    def test_filial_sem_nada_herda_tudo(self):
        operacao = resolve_branch_operation(filial(), restaurante())

        self.assertEqual(operacao.min_order_value, Decimal("20.00"))
        self.assertTrue(operacao.service_fee_enabled)
        self.assertEqual(operacao.service_fee_amount, Decimal("0.99"))
        self.assertEqual(operacao.estimated_delivery_time_min, 30)
        self.assertEqual(operacao.default_delivery_fee, Decimal("9.00"))

    def test_o_valor_da_filial_vence_o_do_restaurante(self):
        operacao = resolve_branch_operation(
            filial(min_order_value=Decimal("40.00")), restaurante()
        )

        self.assertEqual(operacao.min_order_value, Decimal("40.00"))
        # Os outros continuam herdados: sobrescrever um campo nao congela os
        # demais, senao mudar o padrao pararia de chegar nesta filial.
        self.assertEqual(operacao.service_fee_amount, Decimal("0.99"))

    def test_false_na_filial_e_escolha_e_nao_ausencia(self):
        """O caso que um `or` teria quebrado.

        "Esta loja nao cobra taxa de servico" e `False`, nao NULL — e cair no
        `True` do restaurante faria a filial cobrar uma taxa que o lojista
        desligou, sem nada no log.
        """
        operacao = resolve_branch_operation(
            filial(service_fee_enabled=False), restaurante(service_fee_enabled=True)
        )

        self.assertFalse(operacao.service_fee_enabled)

    def test_zero_na_filial_e_escolha_e_nao_ausencia(self):
        operacao = resolve_branch_operation(
            filial(min_order_value=Decimal("0.00")),
            restaurante(min_order_value=Decimal("20.00")),
        )

        self.assertEqual(operacao.min_order_value, Decimal("0.00"))

    def test_restaurante_sem_linha_de_configuracao_cai_nos_defaults(self):
        """A linha de `restaurant_settings` sempre foi opcional no schema.

        Restaurante que nunca passou pelo painel continua vendendo: sem
        minimo, sem taxa de servico cobrada, e sem taxa de contingencia.
        """
        operacao = resolve_branch_operation(filial(), None)

        self.assertEqual(operacao.min_order_value, Decimal("0.00"))
        self.assertEqual(operacao.service_fee_amount, Decimal("0.00"))
        self.assertIsNone(operacao.estimated_delivery_time_min)
        self.assertIsNone(operacao.default_delivery_fee)


class EstadoDoDiaTests(unittest.TestCase):
    """As tres chaves nao herdam nada, e o teste existe para que continuem assim."""

    def test_a_pausa_da_filial_nao_consulta_o_restaurante(self):
        operacao = resolve_branch_operation(filial(is_open=False), restaurante())
        self.assertFalse(operacao.is_open)

    def test_duas_filiais_do_mesmo_restaurante_respondem_diferente(self):
        padrao = restaurante()
        pausada = resolve_branch_operation(filial(is_open=False), padrao)
        aberta = resolve_branch_operation(filial(is_open=True), padrao)

        self.assertFalse(pausada.is_open)
        self.assertTrue(aberta.is_open)

    def test_entrega_e_retirada_sao_independentes(self):
        operacao = resolve_branch_operation(
            filial(accepts_delivery=False, accepts_pickup=True), restaurante()
        )

        self.assertFalse(operacao.accepts_delivery)
        self.assertTrue(operacao.accepts_pickup)


class FreteGratisTests(unittest.TestCase):
    """A campanha da marca, e o direito de a filial recusa-la.

    Herda como termo comercial, com uma diferenca que vale ler: o ligado
    default e FALSO, ao contrario da taxa de servico. Taxa de servico ligada
    sem valor cobra zero e nao machuca ninguem; frete gratis ligado por
    omissao da a entrega de graca em nome de um lojista que nao pediu.
    """

    def test_sem_ninguem_configurar_a_campanha_esta_desligada(self):
        operacao = resolve_branch_operation(filial(), restaurante())

        self.assertFalse(operacao.free_delivery_enabled)
        self.assertIsNone(operacao.free_delivery_min_order_value)

    def test_a_filial_herda_a_campanha_da_marca(self):
        operacao = resolve_branch_operation(
            filial(),
            restaurante(
                free_delivery_enabled=True,
                free_delivery_min_order_value=Decimal("60.00"),
            ),
        )

        self.assertTrue(operacao.free_delivery_enabled)
        self.assertEqual(operacao.free_delivery_min_order_value, Decimal("60.00"))

    def test_a_filial_recusa_a_campanha_da_marca_com_false(self):
        """O caso que exige o booleano ao lado do valor: a loja de 12 km que
        nao aguenta a campanha da rede. Sem ele, `NULL` significaria "herda" e
        nao existiria numero que dissesse "desligado" — `0` seria "gratis
        sempre", o oposto."""
        operacao = resolve_branch_operation(
            filial(free_delivery_enabled=False),
            restaurante(
                free_delivery_enabled=True,
                free_delivery_min_order_value=Decimal("60.00"),
            ),
        )

        self.assertFalse(operacao.free_delivery_enabled)

    def test_a_filial_pode_ter_o_proprio_teto(self):
        operacao = resolve_branch_operation(
            filial(free_delivery_min_order_value=Decimal("90.00")),
            restaurante(
                free_delivery_enabled=True,
                free_delivery_min_order_value=Decimal("60.00"),
            ),
        )

        self.assertTrue(operacao.free_delivery_enabled)
        self.assertEqual(operacao.free_delivery_min_order_value, Decimal("90.00"))


class PausaDaEntregaTests(unittest.TestCase):
    """A pausa temporaria, que e um PRAZO e nao uma chave.

    `accepts_delivery` continua respondendo "esta filial entrega?";
    `accepts_delivery_now` desconta a pausa. Quem le so o primeiro aceita
    pedido de uma filial pausada.
    """

    def test_sem_pausa_a_entrega_esta_valendo(self):
        operacao = resolve_branch_operation(filial(), restaurante())

        self.assertTrue(operacao.accepts_delivery)
        self.assertTrue(operacao.accepts_delivery_now)

    def test_a_pausa_no_futuro_desliga_a_entrega_de_agora(self):
        operacao = resolve_branch_operation(
            filial(
                delivery_paused_until=AGORA + timedelta(minutes=40),
                delivery_pause_reason="chuva forte",
            ),
            restaurante(),
            agora=AGORA,
        )

        # A chave estrutural NAO foi tocada: a entrega volta sozinha.
        self.assertTrue(operacao.accepts_delivery)
        self.assertFalse(operacao.accepts_delivery_now)
        self.assertEqual(operacao.delivery_pause_reason, "chuva forte")

    def test_a_pausa_vencida_devolve_a_entrega_sozinha(self):
        """A propriedade inteira pela qual a pausa existe: ninguem precisa
        lembrar de reabrir. Um booleano no banco pediria esse alguem, e o dia
        em que a pausa e usada e o dia em que ninguem lembra."""
        operacao = resolve_branch_operation(
            filial(delivery_paused_until=AGORA - timedelta(minutes=1)),
            restaurante(),
            agora=AGORA,
        )

        self.assertTrue(operacao.accepts_delivery_now)

    def test_a_pausa_nao_reabre_a_entrega_desligada_na_chave(self):
        # As duas se somam: pausa vencida nao religa o que `order-types`
        # desligou.
        operacao = resolve_branch_operation(
            filial(accepts_delivery=False, delivery_paused_until=None),
            restaurante(),
            agora=AGORA,
        )

        self.assertFalse(operacao.accepts_delivery_now)

    def test_coluna_sem_fuso_nao_derruba_o_checkout(self):
        """Linha gravada por script ou corrigida a mao pode chegar ingenua, e
        comparar ingenuo com consciente levanta TypeError — que aqui viraria
        500 no checkout de todo cliente daquela filial."""
        operacao = resolve_branch_operation(
            filial(delivery_paused_until=(AGORA + timedelta(minutes=40)).replace(tzinfo=None)),
            restaurante(),
            agora=AGORA,
        )

        self.assertFalse(operacao.accepts_delivery_now)


class DinheiroTests(unittest.TestCase):
    def test_valores_saem_com_duas_casas(self):
        operacao = resolve_branch_operation(
            filial(min_order_value=Decimal("19.999")), restaurante()
        )

        self.assertEqual(operacao.min_order_value, Decimal("20.00"))

    def test_taxa_de_contingencia_nula_continua_nula(self):
        """Aqui o nulo tem significado proprio: "nao ha contingencia".

        `to_decimal(None)` o transformaria em zero, e zero e justamente o
        valor que `DeliveryEstimateService` trata como desligado — mesmo
        resultado por acidente, e um acidente a menos de que depender
        (armadilha 11).
        """
        operacao = resolve_branch_operation(filial(), restaurante(default_delivery_fee=None))

        self.assertIsNone(operacao.default_delivery_fee)


if __name__ == "__main__":
    unittest.main()
