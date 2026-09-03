"""A anti-vacuidade do varredor de teste mudo.

`scripts/testes_mudos.py` responde **zero** contra as duas suites, e zero so
vale se estiver provado que ele sabe acusar — regra que ja se pagou quatro
vezes nesta serie.

Aqui ela vale em dobro, porque o varredor procura **exatamente a falha que ele
proprio poderia ter**: um teste escrito que nao roda. Se as classes DESTE
arquivo nao fossem coletadas, o varredor de teste mudo seria, ele mesmo, um
teste mudo.

## A arvore e UMA so, com iscas e legitimos convivendo

Um `auditar()` por teste custaria um subprocesso de pytest por caso. Mais que
o tempo, plantar tudo junto e mais forte: prova que a presenca dos legitimos
nao apaga as iscas e que a presenca das iscas nao contamina os legitimos.

Os legitimos nao sao inventados — sao os padroes que a suite de verdade usa, e
o terceiro deles (heranca INDIRETA de `TestCase`) e o que matou a primeira
versao do script, que tentava decidir a coleta por AST.
"""

import textwrap
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from scripts.testes_mudos import auditar


ARQUIVOS = {
    # ---------------------------------------------------------------- iscas
    "tests/test_isca_sem_prefixo.py": """
        # ISCA: classe sem prefixo `Test` e sem herdar `TestCase`. O pytest
        # coleta ZERO dela, sem erro e sem aviso. E o defeito real de
        # 02/09/2026, que custou nove testes que nunca rodaram.
        class OPainelTests:
            def test_um(self):
                assert True

            def test_dois(self):
                assert True
    """,
    "tests/test_isca_com_init.py": """
        # ISCA: prefixo certo, mas `__init__` faz o pytest PULAR a classe
        # inteira. Ele emite um `PytestCollectionWarning`, que numa saida de
        # duas mil linhas e o mesmo que nada.
        class TestComInit:
            def __init__(self):
                self.coisa = 1

            def test_um(self):
                assert True
    """,
    "tests/test_isca_muda.py": """
        # ISCA: nome de teste, zero casos. Suite com arquivo mudo e pior que
        # suite sem o arquivo — o segundo se nota.
        VALOR = 1

        def ajudar():
            return VALOR
    """,
    "tests/ajudantes_de_pedido.py": """
        # ISCA: tem teste dentro e o nome nao casa com `python_files`, entao o
        # pytest nem abre o arquivo.
        def test_que_nunca_roda():
            assert True
    """,
    # ------------------------------------------------------------- legitimos
    "tests/test_legitimo_prefixo.py": """
        # LEGITIMO: o jeito mais comum, prefixo `Test` numa classe simples.
        class TestNormal:
            def test_um(self):
                assert True


        def test_solto():
            assert True
    """,
    "tests/test_legitimo_unittest.py": """
        import unittest


        # LEGITIMO: sufixo `Tests` e nome que NAO casa com `python_classes`,
        # mas herda `TestCase` — e ai a coleta e por heranca, nao por nome. E o
        # padrao da maior parte desta suite.
        class MinhaCoisaTests(unittest.TestCase):
            def test_um(self):
                self.assertTrue(True)
    """,
    "tests/test_legitimo_heranca_indireta.py": """
        import unittest


        class _Base(unittest.TestCase):
            def ajudar(self):
                return 1


        # LEGITIMO, E O CASO QUE MATOU A PRIMEIRA VERSAO DO SCRIPT.
        # `TestCase` esta duas classes acima. A versao so-AST acusou sete
        # classes reais desta forma (`DefaultDeliveryFeeFallbackTests` e
        # irmas), e as sete estavam sendo coletadas o tempo todo.
        class FilhoTests(_Base):
            def test_um(self):
                self.assertEqual(self.ajudar(), 1)
    """,
    "tests/test_legitimo_ajudantes.py": """
        # LEGITIMO: classe de apoio dentro de um arquivo de teste. Ela NAO tem
        # metodo `test_*`, entao nao ha teste escrito para ficar mudo — acusar
        # aqui encheria a saida de todo dublê da suite.
        class FakeRepositorio:
            def get(self, coisa_id):
                return None

            def salvar(self, coisa):
                return coisa


        class TestUsaOFake:
            def test_um(self):
                assert FakeRepositorio().get(1) is None
    """,
    "tests/fabricas_plantadas.py": """
        # LEGITIMO: helper sem nome de teste E sem teste dentro. E o caso de
        # `tests/fabricas.py` e `tests/rotas_do_app.py`.
        def restaurante(nome="Loja"):
            return {"nome": nome}
    """,
}


def _plantar(diretorio: str) -> Path:
    raiz = Path(diretorio)
    (raiz / "tests").mkdir(parents=True, exist_ok=True)
    for caminho, codigo in ARQUIVOS.items():
        (raiz / caminho).write_text(textwrap.dedent(codigo), encoding="utf-8")
    return raiz


class VarredorDeTesteMudoTests(unittest.TestCase):
    """Uma arvore, uma auditoria, uma asserção por caso."""

    @classmethod
    def setUpClass(cls):
        cls._temporario = TemporaryDirectory()
        cls.addClassCleanup(cls._temporario.cleanup)
        cls.achados = auditar(_plantar(cls._temporario.name))

    def _acusados(self, chave: str) -> str:
        return "\n".join(self.achados[chave])

    # ------------------------------------------------------------ as iscas

    def test_a_classe_sem_prefixo_e_acusada(self):
        acusados = self._acusados("nao_coletados")

        self.assertIn("OPainelTests::test_um", acusados)
        self.assertIn("OPainelTests::test_dois", acusados)

    def test_a_acusacao_leva_arquivo_e_linha(self):
        """O achado tem que ser acionavel sem procurar.

        Nome de classe sozinho obrigaria um grep na suite inteira, e e
        justamente numa suite grande que este defeito se esconde.
        """
        linha = next(
            achado for achado in self.achados["nao_coletados"] if "OPainelTests" in achado
        )

        self.assertIn("tests/test_isca_sem_prefixo.py", linha)
        self.assertRegex(linha, r"linha \d+")

    def test_a_classe_com_init_e_acusada(self):
        """Prefixo certo e coleta zero mesmo assim.

        E a prova de que o script nao esta reimplementando "comeca com Test":
        um varredor que so olhasse o nome daria esta como boa.
        """
        self.assertIn("TestComInit::test_um", self._acusados("nao_coletados"))

    def test_o_arquivo_mudo_e_acusado(self):
        self.assertIn("tests/test_isca_muda.py", self._acusados("mudos"))

    def test_o_arquivo_fora_do_padrao_de_nome_e_acusado(self):
        self.assertIn("tests/ajudantes_de_pedido.py", self._acusados("fora_do_padrao"))

    # -------------------------------------------------------- os legitimos

    def test_a_classe_com_prefixo_NAO_e_acusada(self):
        self.assertNotIn("TestNormal", self._acusados("nao_coletados"))

    def test_a_funcao_solta_NAO_e_acusada(self):
        self.assertNotIn("test_solto", self._acusados("nao_coletados"))

    def test_o_TestCase_de_nome_diferente_NAO_e_acusado(self):
        self.assertNotIn("MinhaCoisaTests", self._acusados("nao_coletados"))

    def test_a_heranca_INDIRETA_de_TestCase_NAO_e_acusada(self):
        """O caso que matou a primeira versao. Sem ele, o script volta a ser
        um reimplementador de regra de coleta e acusa codigo correto."""
        self.assertNotIn("FilhoTests", self._acusados("nao_coletados"))

    def test_a_classe_de_APOIO_NAO_e_acusada(self):
        """`FakeRepositorio` nao tem metodo `test_*`: nao ha teste mudo ali.

        Acusa-la encheria a saida de todo dublê da suite, e saida cheia de
        ruido e saida que ninguem le — inclusive no dia em que ela estiver
        certa.
        """
        self.assertNotIn("FakeRepositorio", self._acusados("nao_coletados"))

    def test_o_helper_sem_teste_dentro_NAO_e_acusado(self):
        """`fabricas.py` e `rotas_do_app.py` sao exatamente isto: nome fora do
        padrao e nenhum `def test*` dentro."""
        self.assertNotIn("fabricas_plantadas.py", self._acusados("fora_do_padrao"))

    def test_arquivo_legitimo_nenhum_entra_como_mudo(self):
        self.assertEqual(
            [achado for achado in self.achados["mudos"] if "legitimo" in achado],
            [],
        )

    # ------------------------------------------------- o total, e o formato

    def test_o_numero_de_achados_e_o_esperado(self):
        """Amarra os tres grupos de uma vez.

        Um achado NOVO nesta arvore significa que o script passou a acusar
        alguma coisa que ninguem plantou — e isso e tao defeito quanto deixar
        de acusar.
        """
        self.assertEqual(
            {chave: len(grupo) for chave, grupo in self.achados.items()},
            {"nao_coletados": 3, "mudos": 1, "fora_do_padrao": 1},
        )


class ASuiteDeVerdadeTests(unittest.TestCase):
    """O numero contra o repositorio. FALHA no portao, e nao aviso: nunca
    houve divida herdada aqui — o unico caso ja nasceu e morreu no mesmo dia."""

    def test_a_suite_da_api_nao_tem_teste_mudo(self):
        achados = auditar()

        self.assertEqual(
            {chave: grupo for chave, grupo in achados.items() if grupo},
            {},
        )

    def test_a_suite_do_print_agent_nao_tem_teste_mudo(self):
        """A segunda suite do repositorio, auditada a partir da raiz DELA.

        Auditada de fora, o `norecursedirs = print-agent` do `pytest.ini` da
        API faz o pytest coletar zero e os nove arquivos do agente saem como
        mudos. Foi o primeiro resultado que este script deu, e nao era achado:
        era a ferramenta chamada errada.
        """
        from scripts.testes_mudos import ROOT_DIR

        achados = auditar(raiz=ROOT_DIR / "print-agent")

        self.assertEqual(
            {chave: grupo for chave, grupo in achados.items() if grupo},
            {},
        )


if __name__ == "__main__":
    unittest.main()
