"""Leitura do config.ini.

O config e escrito a mao, muitas vezes por telefone, numa maquina de balcao.
Estes testes protegem o que acontece quando ele esta errado:

1. **Configuracao invalida derruba o agente na hora**, com mensagem. Um
   agente que sobe sem credencial fica no ar sem imprimir nada, e ninguem
   percebe ate o cliente reclamar.
2. **Caminho relativo sai da pasta do config**, nao do diretorio de
   trabalho: o servico do Windows sobe com o cwd em System32.
3. **Nome de setor casa sem acento e sem caixa**, porque ele vem do painel.
"""

import tempfile
import unittest
from pathlib import Path

from print_agent.config import ConfigError, load_config, normalize_sector


MINIMAL = """
[api]
base_url = https://api.exemplo.com/
token = abc

[printers]
cozinha = IMP-COZINHA
"""


class ConfigTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.path = self.root / "config.ini"

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, content):
        self.path.write_text(content, encoding="utf-8")
        return self.path


class LoadTests(ConfigTestCase):
    def test_it_reads_the_minimum(self):
        config = load_config(self.write(MINIMAL))

        self.assertEqual(config.api_base_url, "https://api.exemplo.com")
        self.assertEqual(config.token, "abc")
        self.assertEqual(config.printers, {"cozinha": "IMP-COZINHA"})

    def test_a_missing_file_is_a_config_error(self):
        with self.assertRaises(ConfigError):
            load_config(self.root / "nao-existe.ini")

    def test_without_a_credential_it_refuses_to_start(self):
        with self.assertRaises(ConfigError):
            load_config(self.write(
                "[api]\nbase_url = https://x\n\n[printers]\ncozinha = IMP\n"
            ))

    def test_email_and_password_are_accepted_instead_of_a_token(self):
        config = load_config(self.write(
            "[api]\nbase_url = https://x\nemail = a@b.com\npassword = s3nha\n"
            "\n[printers]\ncozinha = IMP\n"
        ))

        self.assertIsNone(config.token)
        self.assertEqual(config.email, "a@b.com")

    def test_a_password_with_a_percent_sign_does_not_break_the_parser(self):
        # `interpolation=None` existe por isto: o ConfigParser padrao trata
        # '%' como interpolacao e recusaria o arquivo inteiro.
        config = load_config(self.write(
            "[api]\nbase_url = https://x\nemail = a@b.com\npassword = 100%%segura\n"
            "\n[printers]\ncozinha = IMP\n"
        ))

        self.assertIn("%", config.password)

    def test_without_printers_it_refuses_to_start(self):
        with self.assertRaises(ConfigError):
            load_config(self.write("[api]\nbase_url = https://x\ntoken = abc\n"))

    def test_an_empty_printers_section_refuses_to_start(self):
        with self.assertRaises(ConfigError):
            load_config(self.write(
                "[api]\nbase_url = https://x\ntoken = abc\n\n[printers]\n"
            ))

    def test_a_non_numeric_number_is_a_config_error(self):
        with self.assertRaises(ConfigError):
            load_config(self.write(MINIMAL + "\n[printing]\ncodepage = dois\n"))

    def test_a_non_boolean_flag_is_a_config_error(self):
        with self.assertRaises(ConfigError):
            load_config(self.write(MINIMAL + "\n[printing]\ncut = talvez\n"))


class PathTests(ConfigTestCase):
    def test_relative_paths_hang_off_the_config_folder(self):
        # O servico do Windows sobe com o cwd em System32.
        config = load_config(self.write(MINIMAL))

        self.assertEqual(config.state_file, self.root / "state" / "printed-orders.json")
        self.assertEqual(config.log_file, self.root / "logs" / "print-agent.log")

    def test_an_absolute_path_is_respected(self):
        absolute = Path(self.root / "outro" / "estado.json").resolve()
        config = load_config(self.write(MINIMAL + f"\n[state]\nfile = {absolute}\n"))

        self.assertEqual(config.state_file, absolute)


class SectorMatchingTests(ConfigTestCase):
    def test_the_sector_is_matched_without_accent_or_case(self):
        config = load_config(self.write(
            "[api]\nbase_url = https://x\ntoken = abc\n"
            "\n[printers]\nPraça Quente = IMP-QUENTE\n"
        ))

        self.assertEqual(config.printer_for("PRACA QUENTE"), "IMP-QUENTE")
        self.assertEqual(config.printer_for("praça quente"), "IMP-QUENTE")

    def test_default_is_not_treated_as_a_sector(self):
        config = load_config(self.write(
            "[api]\nbase_url = https://x\ntoken = abc\n"
            "\n[printers]\ndefault = IMP-PADRAO\n"
        ))

        self.assertEqual(config.printers, {})
        self.assertEqual(config.printer_for("Qualquer Coisa"), "IMP-PADRAO")

    def test_an_unmapped_sector_without_a_default_returns_nothing(self):
        config = load_config(self.write(MINIMAL))

        self.assertIsNone(config.printer_for("Bar"))

    def test_extra_spaces_do_not_create_a_different_sector(self):
        self.assertEqual(normalize_sector("  Praça   Quente "), "praca quente")


if __name__ == "__main__":
    unittest.main()
