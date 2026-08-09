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

import unittest

from print_agent.escpos import (
    INITIALIZE,
    PARTIAL_CUT,
    SELECT_SIZE,
    SIZE_LARGE,
    SIZE_NORMAL,
    build_payload,
    encode_text,
)


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


if __name__ == "__main__":
    unittest.main()
