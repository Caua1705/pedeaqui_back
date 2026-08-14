"""As caixas de aviso do Windows.

O `MessageBoxW` em si nao e testavel fora do Windows com area de trabalho, e
nao e ele que tem regra: o `_message_box` engole tudo de proposito, para uma
caixa que nao consegue abrir nunca derrubar a impressao.

O que TEM regra e a deduplicacao. Sem ela, papel acabado numa loja movimentada
empilha uma caixa por pedido na tela do balcao, e a decima cobre a primeira —
que era a unica que dizia qual impressora falhou.
"""

import unittest

from print_agent import alerts


class ShowWarningOnceTests(unittest.TestCase):
    def setUp(self):
        self.shown = []
        # Troca a caixa de verdade por um registro: o teste e sobre QUANTAS
        # vezes se pede a caixa, nao sobre o Windows desenha-la.
        original = alerts.show_warning_async
        alerts.show_warning_async = lambda message, title=alerts.TITLE: self.shown.append(message)
        self.addCleanup(setattr, alerts, "show_warning_async", original)

        alerts._already_shown.clear()
        self.addCleanup(alerts._already_shown.clear)

    def test_a_mesma_impressora_avisa_uma_vez_so(self):
        alerts.show_warning_once("impressora:Cozinha", "sem papel")
        alerts.show_warning_once("impressora:Cozinha", "sem papel")
        alerts.show_warning_once("impressora:Cozinha", "sem papel")

        self.assertEqual(self.shown, ["sem papel"])

    def test_impressoras_diferentes_avisam_cada_uma(self):
        """Duas pracas paradas sao dois problemas, e cada um tem seu conserto."""
        alerts.show_warning_once("impressora:Cozinha", "cozinha sem papel")
        alerts.show_warning_once("impressora:Bar", "bar desligado")

        self.assertEqual(self.shown, ["cozinha sem papel", "bar desligado"])


if __name__ == "__main__":
    unittest.main()
