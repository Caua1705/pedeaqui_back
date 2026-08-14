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


class EncodingTests(ConfigTestCase):
    """O config.ini e editado no Bloco de Notas da maquina do balcao.

    O Bloco de Notas grava BOM (sempre, no Windows antigo; ao escolher "UTF-8
    com BOM", no novo). Lido como `utf-8` puro, o BOM gruda no comeco da
    primeira linha, `[rapidex]` deixa de ser reconhecido como secao e o erro
    que sai — "File contains no section headers" — aponta para uma linha que
    na tela e um cabecalho de secao. E um beco sem saida para quem esta no
    balcao com o pendrive na mao.
    """

    def test_a_file_saved_with_a_bom_still_loads(self):
        self.path.write_bytes(MINIMAL.encode("utf-8-sig"))

        config = load_config(self.path)

        self.assertEqual(config.api_base_url, "https://api.exemplo.com")
        self.assertEqual(config.printers, {"cozinha": "IMP-COZINHA"})

    def test_a_file_saved_without_a_bom_still_loads(self):
        # O par do teste acima: `utf-8-sig` nao pode ter quebrado o caso
        # normal, que e o do arquivo gerado pelo proprio programa.
        self.path.write_bytes(MINIMAL.encode("utf-8"))

        config = load_config(self.path)

        self.assertEqual(config.api_base_url, "https://api.exemplo.com")

    def test_accented_values_survive_a_bom(self):
        # O nome do setor vem do painel e costuma ter acento. Se a leitura do
        # BOM estivesse errada, o acento seria a segunda vitima.
        self.path.write_bytes(
            ("[api]\nbase_url = https://x\ntoken = abc\n"
             "\n[printers]\nPraça Quente = IMP-QUENTE\n").encode("utf-8-sig")
        )

        config = load_config(self.path)

        self.assertEqual(config.printer_for("praca quente"), "IMP-QUENTE")

    def test_a_file_saved_as_ansi_still_loads(self):
        # "Salvar como > ANSI" e o padrao do Bloco de Notas das maquinas mais
        # antigas — que sao justamente as de balcao. Em ANSI (CP1252) o "ç" e
        # um byte 0xE7 solto, que nao e UTF-8 valido: a leitura levantava
        # UnicodeDecodeError, que nao e ConfigError, escapava pelo __main__ e
        # o lojista via um traceback de Python.
        self.path.write_bytes(
            ("[rapidex]\napi_url = https://x\ntoken = abc\n"
             "printer = Impressora Cozinha Ação\n").encode("cp1252")
        )

        config = load_config(self.path)

        self.assertEqual(config.default_printer, "Impressora Cozinha Ação")

    def test_ansi_sector_names_also_survive(self):
        self.path.write_bytes(
            ("[api]\nbase_url = https://x\ntoken = abc\n"
             "\n[printers]\nPraça Quente = IMP-QUENTE\n").encode("cp1252")
        )

        config = load_config(self.path)

        self.assertEqual(config.printer_for("Praça Quente"), "IMP-QUENTE")

    def test_utf8_is_tried_before_cp1252(self):
        # A ordem importa e nao da para inverter: CP1252 aceita QUALQUER byte,
        # entao tentada primeiro ela leria um arquivo UTF-8 legitimo como
        # mojibake ("Ação" -> "AÃ§Ã£o") sem reclamar, e o nome nunca casaria
        # com o da impressora instalada no Windows.
        self.path.write_bytes(
            ("[rapidex]\napi_url = https://x\ntoken = abc\n"
             "printer = Impressora Ação\n").encode("utf-8")
        )

        config = load_config(self.path)

        self.assertEqual(config.default_printer, "Impressora Ação")


class PathTests(ConfigTestCase):
    def test_relative_paths_hang_off_the_config_folder(self):
        # O servico do Windows sobe com o cwd em System32, e o executavel do
        # PyInstaller roda de uma pasta temporaria que some no fim. Nos dois
        # casos, o que salva e o caminho sair da pasta do config.
        config = load_config(self.write(
            MINIMAL + "\n[state]\nfile = estado/impressos.json\n"
            "\n[log]\nfile = registros/agente.log\n"
        ))

        self.assertEqual(config.state_file, self.root / "estado" / "impressos.json")
        self.assertEqual(config.log_file, self.root / "registros" / "agente.log")

    def test_the_default_files_sit_next_to_the_config(self):
        # Sem subpasta: o lojista e instruido por telefone a abrir a pasta e
        # mandar o arquivo .log, e uma pasta a mais e um passo a mais para
        # errar.
        config = load_config(self.write(MINIMAL))

        self.assertEqual(config.state_file, self.root / "pedidos-impressos.json")
        self.assertEqual(config.log_file, self.root / "rapidex-impressao.log")

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


class SoundTests(ConfigTestCase):
    """O alerta sonoro de comanda impressa.

    Ligado por padrao de proposito: sem console e sem janela, o apito e um
    dos dois sinais que sobraram de que o programa esta trabalhando (o outro
    e a cor do icone). Quem instalar sem tocar em nada tem que ouvir.
    """

    def test_it_is_on_without_anyone_configuring_it(self):
        config = load_config(self.write(MINIMAL))

        self.assertTrue(config.sound)
        self.assertIsNone(config.sound_file)

    def test_it_can_be_turned_off(self):
        config = load_config(self.write(MINIMAL + "\n[agent]\nsound = nao\n"))

        self.assertFalse(config.sound)

    def test_a_relative_wav_starts_at_the_config_folder(self):
        """Nao no diretorio de trabalho: como servico, o cwd e o System32."""
        config = load_config(self.write(MINIMAL + "\n[agent]\nsound_file = alerta.wav\n"))

        self.assertEqual(config.sound_file, self.root / "alerta.wav")

    def test_a_wav_that_does_not_exist_still_boots(self):
        """Trocar "o apito nao tocou" por "a loja parou de imprimir" seria pior."""
        config = load_config(self.write(MINIMAL + "\n[agent]\nsound_file = sumiu.wav\n"))

        self.assertFalse(config.sound_file.exists())


class ConfigDirTests(ConfigTestCase):
    def test_it_remembers_where_the_config_was_read_from(self):
        """O menu da bandeja abre essa pasta.

        E ela nao e sempre a do executavel: `--config` aponta para outro
        lugar, e ditar um caminho com %LOCALAPPDATA% no meio por telefone e
        exatamente o que o item de menu existe para evitar.
        """
        config = load_config(self.write(MINIMAL))

        self.assertEqual(config.config_dir, self.root)


if __name__ == "__main__":
    unittest.main()
