"""O que o corpo da avaliação aceita e recusa, antes de chegar ao banco.

Sem banco: é validação de schema. O que se protege aqui é o agregado do
painel — a etiqueta de problema só existe para o lojista poder ler "7 das 12
notas baixas desta semana foram atraso", e essa frase deixa de ser verdade se
uma nota 5 entrar na conta com etiqueta colada.
"""

import unittest

from pydantic import ValidationError

from src.core.constants import REVIEW_PROBLEM_TAGS
from src.schemas.order_review_schema import (
    LOW_RATING_CEILING,
    MAX_REVIEW_COMMENT_LENGTH,
    CreateOrderReviewRequest,
)


class NotaTests(unittest.TestCase):
    def test_de_um_a_cinco_passa(self):
        for nota in range(1, 6):
            self.assertEqual(CreateOrderReviewRequest(rating=nota).rating, nota)

    def test_zero_e_recusado(self):
        with self.assertRaises(ValidationError):
            CreateOrderReviewRequest(rating=0)

    def test_seis_e_recusado(self):
        with self.assertRaises(ValidationError):
            CreateOrderReviewRequest(rating=6)

    def test_nota_e_obrigatoria(self):
        with self.assertRaises(ValidationError):
            CreateOrderReviewRequest()


class EtiquetaDeProblemaTests(unittest.TestCase):
    def test_toda_etiqueta_da_constante_e_aceita_pelo_schema(self):
        """A outra metade da armadilha 15.

        O teste de banco prova que o CHECK aceita a constante; este prova que
        o schema aceita. Etiqueta que exista só no banco nunca chega a ser
        oferecida ao cliente, e o defeito é invisível dos dois lados.
        """
        for etiqueta in REVIEW_PROBLEM_TAGS:
            with self.subTest(etiqueta=etiqueta):
                payload = CreateOrderReviewRequest(rating=1, problem_tag=etiqueta)
                self.assertEqual(payload.problem_tag, etiqueta)

    def test_etiqueta_desconhecida_e_recusada(self):
        with self.assertRaises(ValidationError):
            CreateOrderReviewRequest(rating=1, problem_tag="estacionamento")

    def test_etiqueta_com_nota_alta_e_recusada(self):
        """Um 5 estrelas com "atrasou" não é opinião complexa: é front
        mandando campo que a tela não devia ter mostrado."""
        with self.assertRaises(ValidationError):
            CreateOrderReviewRequest(rating=5, problem_tag="atrasou")

    def test_o_teto_da_nota_baixa_e_inclusivo(self):
        payload = CreateOrderReviewRequest(rating=LOW_RATING_CEILING, problem_tag="qualidade")

        self.assertEqual(payload.problem_tag, "qualidade")

    def test_um_acima_do_teto_ja_recusa(self):
        with self.assertRaises(ValidationError):
            CreateOrderReviewRequest(rating=LOW_RATING_CEILING + 1, problem_tag="qualidade")

    def test_nota_alta_sem_etiqueta_passa(self):
        self.assertIsNone(CreateOrderReviewRequest(rating=5).problem_tag)


class ComentarioTests(unittest.TestCase):
    def test_no_teto_passa(self):
        payload = CreateOrderReviewRequest(rating=5, comment="a" * MAX_REVIEW_COMMENT_LENGTH)

        self.assertEqual(len(payload.comment), MAX_REVIEW_COMMENT_LENGTH)

    def test_acima_do_teto_e_recusado(self):
        with self.assertRaises(ValidationError):
            CreateOrderReviewRequest(rating=5, comment="a" * (MAX_REVIEW_COMMENT_LENGTH + 1))

    def test_so_espaco_vira_nulo(self):
        """Sem isto o painel mostra um balão de comentário vazio."""
        self.assertIsNone(CreateOrderReviewRequest(rating=5, comment="   ").comment)

    def test_comentario_de_verdade_nao_e_tocado(self):
        payload = CreateOrderReviewRequest(rating=3, comment="  veio frio  ")

        self.assertEqual(payload.comment, "  veio frio  ")


class CorpoDesconhecidoTests(unittest.TestCase):
    def test_campo_que_a_rota_nao_conhece_e_422(self):
        """`extra="forbid"`: campo a mais é erro do front, e falhar alto aqui
        é mais barato que ignorar em silêncio — foi o que fez o cashback
        parecer quebrado (o `extra="ignore"` de CreateOrderRequest)."""
        with self.assertRaises(ValidationError):
            CreateOrderReviewRequest(rating=5, nota_do_entregador=4)


if __name__ == "__main__":
    unittest.main()
