"""Traducao do texto para ESC/POS.

O agente nao reformata nada — mas ele embrulha, e o embrulho tem que estar
certo. Estes testes protegem:

1. **O conteudo sai intacto.** Se algum dia alguem "melhorar" o alinhamento
   aqui, a formatacao deixa de ser unica na API e cada loja passa a imprimir
   uma comanda diferente.
2. **A fonte volta ao normal no fim.** Sem isso, a via seguinte sai gigante
   em impressora que ignora o ESC @ do proximo trabalho.
3. **Acento nunca derruba a impressao.** A via com "Joao" no lugar de "João"
   serve; a via que nao sai, nao.
"""

import unicodedata
import unittest

from print_agent.escpos import (
    INITIALIZE,
    PARTIAL_CUT,
    SELECT_CODEPAGE,
    SELECT_SIZE,
    SIZE_LARGE,
    SIZE_NORMAL,
    build_payload,
    encode_text,
    expected_codepage,
)


# Itens reais do cardapio do Júnior da Picanha. Estao aqui com o acento de
# verdade de proposito: o teste que exercita acento com "Joao" nao testa nada.
CARDAPIO = [
    "Picanha à Moda",
    "Filé à Parmegiana",
    "Sortidão",
    "Açaí 500ml",
    "Coração de Frango",
    "Maminha ao Molho Madeíra",
]


class PayloadTests(unittest.TestCase):
    def test_it_starts_by_resetting_the_printer(self):
        # Limpa fonte e estilo que uma via anterior possa ter deixado
        # ligados na impressora.
        self.assertTrue(build_payload("oi").startswith(INITIALIZE))

    def test_the_text_arrives_untouched(self):
        content = "2x Prato feito                          64,00"

        payload = build_payload(content)

        self.assertIn(content.encode("cp850"), payload)

    def test_large_font_selects_double_width_and_height(self):
        payload = build_payload("oi", font_size="large")

        self.assertIn(SELECT_SIZE + bytes([SIZE_LARGE]), payload)

    def test_it_returns_to_the_normal_font_before_finishing(self):
        payload = build_payload("oi", font_size="large")

        self.assertTrue(
            payload.index(SELECT_SIZE + bytes([SIZE_NORMAL]))
            > payload.index(SELECT_SIZE + bytes([SIZE_LARGE]))
        )

    def test_an_unknown_font_falls_back_to_normal(self):
        # Via com fonte errada e defeito visivel; via nao impressa e pedido
        # perdido.
        payload = build_payload("oi", font_size="gigante")

        self.assertIn(SELECT_SIZE + bytes([SIZE_NORMAL]), payload)

    def test_the_cut_can_be_turned_off(self):
        self.assertIn(PARTIAL_CUT, build_payload("oi", cut=True))
        self.assertNotIn(PARTIAL_CUT, build_payload("oi", cut=False))

    def test_lines_end_the_way_the_printer_expects(self):
        payload = build_payload("linha 1\nlinha 2")

        self.assertIn(b"linha 1\r\nlinha 2\r\n", payload)

    def test_the_content_always_ends_with_a_line_break(self):
        # Sem a quebra final a ultima linha fica no buffer ate o proximo
        # trabalho, e a comanda sai faltando uma linha de forma
        # aparentemente intermitente.
        self.assertIn(b"fim\r\n", build_payload("fim"))

    def test_carriage_returns_are_not_doubled(self):
        payload = build_payload("linha\r\noutra")

        self.assertNotIn(b"\r\r", payload)


class EncodingTests(unittest.TestCase):
    def test_accents_that_fit_the_codepage_are_kept(self):
        self.assertEqual(encode_text("João", "cp850"), "João".encode("cp850"))

    def test_what_does_not_fit_loses_the_accent_instead_of_becoming_a_question_mark(self):
        # "Jo?o" e pior de ler e assusta mais quem esta no balcao.
        encoded = encode_text("João", "ascii")

        self.assertEqual(encoded, b"Joao")

    def test_an_unknown_codepage_still_prints_something(self):
        # Erro de digitacao no config.ini nao pode calar a impressora.
        self.assertEqual(encode_text("João", "cp-inexistente"), b"Joao")

    def test_a_character_with_no_equivalent_becomes_a_question_mark(self):
        encoded = encode_text("cafe \U0001f600", "cp850")

        self.assertIn(b"cafe ", encoded)

    def test_plain_text_is_untouched(self):
        self.assertEqual(encode_text("PEDIDO #1234", "cp850"), b"PEDIDO #1234")


class RealMenuTests(unittest.TestCase):
    """O cardapio como ele e, nao um "Joao" de mentira.

    A comanda vai para uma termica que NAO entende UTF-8: ela le byte a byte
    na tabela seleciona por `ESC t n`. Um "à" mandado em UTF-8 sao dois
    bytes, e a impressora imprimiria os dois — "Ã " no lugar de "à". Os
    testes abaixo travam que o texto sai na codepage, e nao em UTF-8.
    """

    def test_every_item_keeps_its_accent_in_cp850(self):
        for item in CARDAPIO:
            with self.subTest(item=item):
                self.assertEqual(encode_text(item, "cp850"), item.encode("cp850"))

    def test_the_accent_is_one_byte_not_utf8(self):
        # O teste que pega o defeito na veia: em UTF-8 "à" e b"\xc3\xa0"; na
        # CP850 e b"\x85". Se algum dia isto voltar a mandar UTF-8 cru, e
        # aqui que aparece.
        encoded = encode_text("Picanha à Moda", "cp850")

        self.assertEqual(encoded, b"Picanha \x85 Moda")
        self.assertNotIn("à".encode("utf-8"), encoded)

    def test_the_payload_carries_the_menu_in_the_printer_codepage(self):
        content = "\n".join(f"1x {item}" for item in CARDAPIO)

        payload = build_payload(content, codepage=2, encoding="cp850")

        for item in CARDAPIO:
            with self.subTest(item=item):
                self.assertIn(item.encode("cp850"), payload)
                self.assertNotIn(item.encode("utf-8"), payload)

    def test_the_codepage_is_selected_by_an_escpos_command(self):
        # Sem o `ESC t 2` a impressora le os bytes na tabela que estiver
        # ligada de fabrica, e o mesmo byte 0x85 vira outra letra.
        payload = build_payload("Sortidão", codepage=2, encoding="cp850")

        self.assertIn(SELECT_CODEPAGE + bytes([2]), payload)
        self.assertLess(
            payload.index(SELECT_CODEPAGE), payload.index("Sortidão".encode("cp850"))
        )

    def test_decomposed_accents_survive(self):
        # Teclado de iPhone/macOS manda "é" como "e" + acento combinante. Na
        # tela do painel e identico ao composto; para a CP850 o combinante
        # nao existe, e sem o NFC o cardapio inteiro imprimia sem acento.
        for item in CARDAPIO:
            with self.subTest(item=item):
                decomposed = unicodedata.normalize("NFD", item)

                self.assertNotEqual(decomposed, unicodedata.normalize("NFC", item))
                self.assertEqual(encode_text(decomposed, "cp850"), item.encode("cp850"))

    def test_one_impossible_character_does_not_deaccent_the_whole_ticket(self):
        # Lojista poe emoji no nome do item, e o painel aceita. Antes, esse
        # emoji rebaixava a LINHA INTEIRA: "Sortidao ? File".
        encoded = encode_text("Sortidão 🍖 Filé", "cp850")

        self.assertEqual(encoded, "Sortidão ? Filé".encode("cp850"))

    def test_a_codepage_the_menu_does_not_fit_degrades_per_character(self):
        # CP437 nao tem "ã" nem "à", mas tem "é". So o que nao cabe cai.
        self.assertEqual(encode_text("Sortidão", "cp437"), b"Sortidao")
        self.assertEqual(encode_text("Filé", "cp437"), "Filé".encode("cp437"))


class CodepagePairTests(unittest.TestCase):
    """O par `codepage` + `encoding` do config.ini.

    Trocar so um dos dois imprime acento errado sem estourar nada — o defeito
    mais caro de diagnosticar por telefone.
    """

    def test_the_default_pair_agrees(self):
        from print_agent.config import DEFAULT_CODEPAGE, DEFAULT_ENCODING

        self.assertEqual(expected_codepage(DEFAULT_ENCODING), DEFAULT_CODEPAGE)

    def test_known_encodings_map_to_their_escpos_number(self):
        self.assertEqual(expected_codepage("cp850"), 2)
        self.assertEqual(expected_codepage("CP-850"), 2)
        self.assertEqual(expected_codepage("cp437"), 0)
        self.assertEqual(expected_codepage("latin1"), 16)

    def test_an_unknown_encoding_is_not_treated_as_wrong(self):
        # Impressora de fabricante exotico numera as tabelas do jeito dela.
        self.assertIsNone(expected_codepage("cp932"))


if __name__ == "__main__":
    unittest.main()
