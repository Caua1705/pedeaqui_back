"""O log aguenta o que a operacao escreve nele.

O arquivo e UTF-8 e aceita qualquer coisa. O console do Windows nao: ele abre
na codepage do sistema (850 ou 1252 no Brasil) e um caractere de fora dela
faz o `emit` do handler levantar UnicodeEncodeError. O `logging` engole a
excecao, mas imprime "--- Logging error ---" com traceback na tela **e perde
a linha** — e no dia da instalacao e essa janela que alguem esta olhando para
saber se conectou.

Nome de setor vem do painel, digitado pelo lojista: travessao, aspa curva e
emoji entram por ali sem ninguem prever.
"""

import io
import logging
import tempfile
import unittest
from pathlib import Path

from print_agent.logging_setup import setup_logging


# Um travessao ("—") nao existe na CP850 nem na CP437. E o que o lojista
# digita sem perceber, porque o Word e o WhatsApp trocam "-" por ele.
SETOR = "Praça Quente — Térreo"


class ConsoleEncodingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.log_file = Path(self._tmp.name) / "agente.log"
        self._real_stdout = None

    def tearDown(self):
        for handler in list(logging.getLogger().handlers):
            logging.getLogger().removeHandler(handler)
        self._tmp.cleanup()

    def console(self, encoding):
        """Um stdout que se comporta como o console do Windows."""
        return io.TextIOWrapper(io.BytesIO(), encoding=encoding, errors="strict")

    def test_a_character_outside_the_console_codepage_does_not_lose_the_line(self):
        import sys

        console = self.console("cp850")
        real, sys.stdout = sys.stdout, console
        try:
            setup_logging(self.log_file)
            logging.getLogger("t").error("setor '%s' sem impressora", SETOR)
        finally:
            sys.stdout = real

        console.flush()
        printed = console.buffer.getvalue().decode("cp850")

        # A linha saiu, e o travessao virou "?" em vez de derrubar o handler.
        self.assertIn("Praça Quente ? Térreo", printed)
        self.assertNotIn("Logging error", printed)

    def test_the_file_keeps_the_character_the_console_could_not_show(self):
        # A degradacao e SO da tela. O arquivo e o que o lojista manda por
        # WhatsApp quando alguma coisa nao imprimiu, e ele continua fiel.
        setup_logging(self.log_file)
        logging.getLogger("t").error("setor '%s' sem impressora", SETOR)
        logging.shutdown()

        self.assertIn(SETOR, self.log_file.read_text(encoding="utf-8"))

    def test_a_stdout_that_cannot_be_reconfigured_still_logs(self):
        # `sys.stdout` sob pytest, sob um wrapper de servico ou trocado por um
        # StringIO nao tem `reconfigure`. Isso nao pode derrubar o boot.
        import sys

        real, sys.stdout = sys.stdout, io.StringIO()
        try:
            setup_logging(self.log_file)
            logging.getLogger("t").info("setor '%s'", SETOR)
            printed = sys.stdout.getvalue()
        finally:
            sys.stdout = real

        self.assertIn(SETOR, printed)

    def test_it_survives_having_no_console_at_all(self):
        # Build "windowed" do PyInstaller: `sys.stdout` e None e `sys.stderr`
        # tambem. Sem tratamento, cada linha de log viraria AttributeError.
        import sys

        real, sys.stdout = sys.stdout, None
        real_err, sys.stderr = sys.stderr, None
        try:
            setup_logging(self.log_file)
            logging.getLogger("t").error("setor '%s' sem impressora", SETOR)
        finally:
            sys.stdout, sys.stderr = real, real_err

        logging.shutdown()

        self.assertIn(SETOR, self.log_file.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
