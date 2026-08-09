"""Memoria do que ja foi impresso.

E o arquivo que impede o pior defeito possivel deste agente: reconectar as
22h e cuspir a fila do dia inteiro pela impressora. O stream entrega ao menos
uma vez de proposito, e o servidor fecha a conexao a cada 15 minutos — sem
esta memoria, repetir seria o comportamento normal, nao a excecao.
"""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from print_agent.state import PrintedOrders


class StateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "state" / "printed.json"

    def tearDown(self):
        self._tmp.cleanup()

    def test_a_marked_order_is_remembered(self):
        state = PrintedOrders(self.path)
        state.mark("pedido-1")

        self.assertIn("pedido-1", state)
        self.assertNotIn("pedido-2", state)

    def test_the_memory_survives_a_restart(self):
        # O caso que motiva o arquivo: queda de energia, atualizacao do
        # Windows, alguem que fechou a janela.
        PrintedOrders(self.path).mark("pedido-1")

        self.assertIn("pedido-1", PrintedOrders(self.path))

    def test_it_creates_the_folder_it_needs(self):
        PrintedOrders(self.path).mark("pedido-1")

        self.assertTrue(self.path.is_file())

    def test_old_entries_are_pruned(self):
        state = PrintedOrders(self.path, retention_days=7)
        state.mark("antigo", moment=datetime.now(timezone.utc) - timedelta(days=30))
        state.mark("recente")

        reloaded = PrintedOrders(self.path, retention_days=7)
        self.assertNotIn("antigo", reloaded)
        self.assertIn("recente", reloaded)

    def test_a_corrupted_file_does_not_stop_the_agent(self):
        # Voltar a imprimir tudo e ruim; parar de imprimir e pior.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{isto nao e json", encoding="utf-8")

        state = PrintedOrders(self.path)

        self.assertEqual(len(state), 0)
        state.mark("pedido-1")
        self.assertIn("pedido-1", PrintedOrders(self.path))

    def test_a_file_that_is_not_an_object_does_not_stop_the_agent(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("[1, 2, 3]", encoding="utf-8")

        self.assertEqual(len(PrintedOrders(self.path)), 0)

    def test_the_file_is_readable_json(self):
        # Quem esta na loja abre este arquivo para conferir. Um formato
        # binario resolveria o mesmo problema e nao poderia ser lido por
        # telefone.
        PrintedOrders(self.path).mark("pedido-1")

        content = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertIn("pedido-1", content)

    def test_no_temporary_file_is_left_behind(self):
        # A gravacao e atomica (temporario + replace). O temporario ficando
        # para tras encheria a pasta com o tempo.
        state = PrintedOrders(self.path)
        for index in range(5):
            state.mark(f"pedido-{index}")

        leftovers = list(self.path.parent.glob("*.tmp"))
        self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
