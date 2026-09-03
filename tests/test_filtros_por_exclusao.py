"""A anti-vacuidade do varredor de filtro por exclusão (armadilha 47).

`scripts/filtros_por_exclusao.py` responde **zero** contra `src/`, com 18
sítios declarados em `ESPERADOS`. Zero só vale se estiver provado que ele sabe
acusar — e aqui a prova importa mais que de costume, porque **a classe inteira
é silenciosa**: `visibility != 'private'` publicava cupom de segmento na
vitrine anônima sem erro, sem log e sem tela onde conferir. Não há sintoma
para denunciar um varredor cego; o teste é a única coisa entre o zero e a
cegueira.

Quatro provas, e a terceira é a que impede o varredor de virar ruído:

1. **ele acusa a armadilha 47 literal** — o `!= COUPON_VISIBILITY_PRIVATE` que
   o conserto de 28/08/2026 tirou do `MenuRepository`;
2. **ele enxerga as quatro fontes de conjunto fechado** — baseline, revisão,
   `CheckConstraint` do ORM e classe `str, Enum`. Uma fonte que pare de ser
   lida deixa uma família inteira invisível, sem nada falhar;
3. **ele deixa passar o que não é enum** — `!=` sobre id, hash e senha são a
   esmagadora maioria dos 85 do repositório, e um varredor que os acusasse
   seria desligado na primeira semana;
4. **ele deixa passar a forma POSITIVA** — `== <valor>` é justamente o
   conserto que a armadilha 47 manda fazer.

E um quinto, que não é sobre o varredor: **todo motivo de `ESPERADOS` diz para
onde cai o valor NOVO.** Sem isso a lista vira carimbo, e um sítio entra nela
por ter sido visto, não por ter sido decidido.
"""

import textwrap
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from scripts.filtros_por_exclusao import ESPERADOS, auditar


# As três palavras que classificam um sítio. Estão no motivo de propósito: é
# a resposta de "para onde cai o valor novo" reduzida a uma palavra que dá
# para conferir.
FORMAS = ("GUARDA INVERTIDA", "NEGACAO COMPLETA", "FILTRO DE CONJUNTO")


ARQUIVOS = {
    # ---------------------------------------------------------------- iscas
    "src/repositories/vitrine.py": """
        # ISCA: a armadilha 47 literal — o `!=` que publicava cupom de
        # SEGMENTO na vitrine anônima do cardápio, com código.
        COUPON_VISIBILITY_PUBLIC = "public"
        COUPON_VISIBILITY_SEGMENT = "segment"
        COUPON_VISIBILITY_PRIVATE = "private"
        COUPON_VISIBILITIES = (
            COUPON_VISIBILITY_PUBLIC,
            COUPON_VISIBILITY_SEGMENT,
            COUPON_VISIBILITY_PRIVATE,
        )


        def cupons_da_vitrine(sessao, Cupom):
            return sessao.query(Cupom).filter(
                Cupom.visibility != COUPON_VISIBILITY_PRIVATE
            )
    """,
    "src/repositories/pedidos.py": """
        # ISCA: `notin_` sobre status, que é o mesmo defeito em SQL. O valor
        # que entrar em `orders.status` amanhã nasce dentro do resultado.
        def pedidos_que_contam(sessao, Pedido):
            return sessao.query(Pedido).filter(
                Pedido.status.notin_(("cancelled", "rejected"))
            )
    """,
    "src/services/estado_do_cupom.py": """
        from enum import Enum


        # ISCA: comparação com MEMBRO de enum. É a forma que a rodada dos dois
        # estados novos de `CustomerCouponState` produziria.
        class EstadoDoCupom(str, Enum):
            APPLICABLE = "applicable"
            MISSING_AMOUNT = "missing_amount"
            LOGIN_REQUIRED = "login_required"


        def pode_usar(estado):
            return estado != EstadoDoCupom.MISSING_AMOUNT
    """,
    "src/models/pagamento_model.py": """
        from sqlalchemy import CheckConstraint


        # ISCA: o conjunto vem do CheckConstraint do ORM, e o `!=` está em
        # outro arquivo — é a terceira fonte fazendo o trabalho dela.
        class Cobranca:
            __table_args__ = (
                CheckConstraint(
                    "fluxo IN ('online', 'delivery')", name="ck_cobrancas_fluxo"
                ),
            )
    """,
    "src/services/cobranca.py": """
        def precisa_de_gateway(cobranca):
            return cobranca.fluxo != "online"
    """,
    # ------------------------------------------------------------ o que passa
    "src/services/comparacoes_legitimas.py": """
        # NÃO é achado: nada disto é valor de conjunto fechado. São a maioria
        # dos `!=` do repositório, e acusá-los desligaria o varredor.
        def confere(pedido, esperado, senha_digitada, senha_guardada, itens):
            if pedido.id != esperado.id:
                return False
            if pedido.request_fingerprint != "fp-gravado":
                return False
            if senha_digitada != senha_guardada:
                return False
            if len(set(itens)) != len(itens):
                return False
            return True
    """,
    "src/services/forma_positiva.py": """
        # NÃO é achado: é exatamente o conserto que a armadilha 47 manda
        # fazer. Um varredor que acusasse a forma positiva puniria o conserto.
        def cupons_da_vitrine(sessao, Cupom):
            return sessao.query(Cupom).filter(Cupom.visibility == "public")


        def esta_pronto(pedido):
            return pedido.status == "ready"
    """,
}

BASELINE_PLANTADA = """
CREATE TABLE public.pedidos (
    status text NOT NULL,
    CONSTRAINT pedidos_status_check CHECK ((status = ANY (ARRAY['pending'::text,
        'accepted'::text, 'ready'::text, 'completed'::text, 'cancelled'::text,
        'rejected'::text])))
);
"""

REVISAO_PLANTADA = '''
"""uma revisao que acrescenta coluna de enum"""


def upgrade():
    op.create_check_constraint(
        "ck_cupons_visibility",
        "restaurant_coupons",
        "visibility IN ('public', 'segment', 'private')",
    )
'''


def _arvore_plantada(raiz: Path) -> None:
    for caminho, conteudo in ARQUIVOS.items():
        arquivo = raiz / caminho
        arquivo.parent.mkdir(parents=True, exist_ok=True)
        arquivo.write_text(textwrap.dedent(conteudo), encoding="utf-8")

    (raiz / "alembic" / "versions").mkdir(parents=True, exist_ok=True)
    (raiz / "alembic" / "schema_baseline.sql").write_text(BASELINE_PLANTADA, encoding="utf-8")
    (raiz / "alembic" / "versions" / "20260828_0043_visibilidade.py").write_text(
        REVISAO_PLANTADA, encoding="utf-8"
    )


class TestORepositorioEstaLimpo(unittest.TestCase):
    def test_nenhuma_negacao_sobre_enum_fora_de_ESPERADOS(self):
        resultado = auditar()

        self.assertEqual([str(a) for a in resultado["achados"]], [])

    def test_nenhuma_entrada_de_ESPERADOS_sumiu_do_codigo(self):
        """Entrada que descreve código que não existe mais é cemitério: ela
        continua verde e não protege nada."""
        resultado = auditar()

        self.assertEqual(resultado["declarados"], [])

    def test_todo_motivo_diz_para_onde_cai_o_valor_novo(self):
        """A pergunta da armadilha 47 é essa, e a resposta cabe numa das três
        formas. Motivo sem forma é sítio que foi VISTO e não DECIDIDO."""
        sem_forma = [
            chave
            for chave, motivo in ESPERADOS.items()
            if not any(forma in motivo for forma in FORMAS)
        ]

        self.assertEqual(sem_forma, [])


class TestEleSabeAcusar(unittest.TestCase):
    def setUp(self):
        self.temporario = TemporaryDirectory()
        self.raiz = Path(self.temporario.name)
        _arvore_plantada(self.raiz)
        self.resultado = auditar(self.raiz)
        self.expressoes = [achado.expressao for achado in self.resultado["achados"]]

    def tearDown(self):
        self.temporario.cleanup()

    def test_acusa_a_armadilha_47_literal(self):
        self.assertIn("Cupom.visibility != COUPON_VISIBILITY_PRIVATE", self.expressoes)

    def test_acusa_o_notin_sobre_status(self):
        self.assertIn("Pedido.status.notin_(('cancelled', 'rejected'))", self.expressoes)

    def test_acusa_a_comparacao_com_membro_de_enum(self):
        self.assertIn("estado != EstadoDoCupom.MISSING_AMOUNT", self.expressoes)

    def test_acusa_usando_o_conjunto_do_CheckConstraint_do_ORM(self):
        """O `!=` está em `cobranca.py` e o conjunto em `pagamento_model.py`:
        é a fonte do ORM sozinha sustentando o achado."""
        self.assertIn("cobranca.fluxo != 'online'", self.expressoes)

    def test_le_as_quatro_fontes_de_conjunto_fechado(self):
        origens = self.resultado["conjuntos"].keys()

        self.assertTrue(any(nome.startswith("baseline:") for nome in origens))
        self.assertTrue(any(nome.startswith("revisao ") for nome in origens))
        self.assertTrue(any(nome.startswith("model src/") for nome in origens))
        self.assertIn("src/services/estado_do_cupom.py:EstadoDoCupom", origens)


class TestEleDeixaPassarOQueNaoE(unittest.TestCase):
    def setUp(self):
        self.temporario = TemporaryDirectory()
        self.raiz = Path(self.temporario.name)
        _arvore_plantada(self.raiz)
        self.arquivos = {achado.arquivo for achado in auditar(self.raiz)["achados"]}

    def tearDown(self):
        self.temporario.cleanup()

    def test_id_hash_senha_e_tamanho_nao_sao_achado(self):
        self.assertNotIn("src/services/comparacoes_legitimas.py", self.arquivos)

    def test_a_forma_POSITIVA_nao_e_achado(self):
        """`== 'public'` é o conserto. Acusá-lo puniria quem consertou."""
        self.assertNotIn("src/services/forma_positiva.py", self.arquivos)


if __name__ == "__main__":
    unittest.main()
