"""A anti-vacuidade do varredor de estado entre workers.

`scripts/estado_entre_workers.py` responde **zero** contra `src/`, com 15
sitios declarados em `ESPERADOS`. Zero so vale se estiver provado que ele sabe
acusar — e aqui a prova importa mais que de costume, porque o `Dockerfile` sobe
UM worker: **nada desta classe quebra hoje**, entao nao ha sintoma nenhum para
denunciar um varredor cego. O teste e a unica coisa entre o zero e a cegueira.

A primeira medicao perguntou "que nome de modulo e estrutura mutavel?" e achou
24 — as 24 eram tabela CONSTANTE. O criterio virou **quem escreve**, e por isso
metade dos casos plantados aqui sao estruturas que ninguem pode acusar.
"""

import textwrap
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from scripts.estado_entre_workers import auditar


ARQUIVOS = {
    # ---------------------------------------------------------------- iscas
    "src/services/cache_caseiro.py": """
        # ISCA: dicionario de modulo escrito por chave em tempo de requisicao.
        # Com N workers, o cliente que gravou no worker A nao acha no B.
        _POR_CLIENTE = {}


        def guardar(cliente_id, valor):
            _POR_CLIENTE[cliente_id] = valor


        def ler(cliente_id):
            return _POR_CLIENTE.get(cliente_id)
    """,
    "src/services/contador.py": """
        # ISCA: contador com `global`. Foi a forma que a medicao por
        # "estrutura mutavel" NAO pegava — um `int` nao e dict, list nem set.
        _TURNOS = 0


        def contar():
            global _TURNOS
            _TURNOS += 1
            return _TURNOS
    """,
    "src/services/fila.py": """
        # ISCA: lista de modulo com `append`. E o desenho que o stream SSE do
        # painel evitou de proposito — o pedido criado no worker A nunca
        # chegaria ao painel conectado no worker B.
        _PENDENTES = []


        def enfileirar(pedido):
            _PENDENTES.append(pedido)
    """,
    "src/services/escrita_por_atributo.py": """
        # ISCA: escrita por ATRIBUTO num objeto de modulo. Nao e `global`, nao
        # e chave, nao e metodo mutante — e estado de processo do mesmo jeito.
        class Configuracao:
            def __init__(self):
                self.modo = "normal"


        configuracao = Configuracao()


        def virar_modo(novo):
            configuracao.modo = novo
    """,
    "src/services/singleton_com_estado.py": """
        # ISCA: instancia de modulo de tipo do repositorio que NAO e congelado.
        # O objeto existe uma vez por processo, e o que ele guardar vai junto.
        class Registro:
            def __init__(self):
                self.vistos = {}

            def marcar(self, chave):
                self.vistos[chave] = True


        registro = Registro()
    """,
    "src/services/cache_de_processo.py": """
        from functools import lru_cache


        # ISCA: cache de PROCESSO sobre dado de requisicao. Para um cliente
        # (conexao, engine) seria o uso certo; para o produto de uma filial e a
        # armadilha inteira, e sem prazo.
        @lru_cache
        def produto_da_filial(filial_id, slug):
            return {"filial": filial_id, "slug": slug}
    """,
    # ------------------------------------------------------------- legitimos
    "src/services/tabelas.py": """
        # LEGITIMO: tabela CONSTANTE. Cada processo tem a sua, identica, e
        # ninguem escreve. Foram 24 destas na primeira medicao, e acusa-las
        # seria o varredor inutil.
        ROTULOS = {
            "delivery": "Entrega",
            "pickup": "Retirada",
        }

        TRANSICOES = {"pending": ("accepted", "cancelled")}


        def rotular(tipo):
            return ROTULOS[tipo]
    """,
    "src/services/inertes.py": """
        import logging
        import re
        from datetime import timedelta
        from decimal import Decimal
        from zoneinfo import ZoneInfo


        # LEGITIMO: os tipos declarados em TIPOS_INERTES, um de cada.
        logger = logging.getLogger("uvicorn.error")
        _SO_LETRAS = re.compile(r"[a-z]+")
        FUSO = ZoneInfo("America/Sao_Paulo")
        ZERO = Decimal("0.00")
        UMA_HORA = timedelta(hours=1)
        CONHECIDOS = frozenset({"a", "b"})
    """,
    "src/services/congelada.py": """
        from dataclasses import dataclass


        # LEGITIMO, E REGRA MECANICA: classe do repositorio com `frozen=True`
        # nao tem como guardar estado. Sao as sentinelas `SEM_FILTRO`,
        # `SEM_PADRAO` e `SEM_CASHBACK` — listar as tres a mao seria dar
        # manutencao numa lista que envelhece.
        @dataclass(frozen=True)
        class Termos:
            percentual: int = 0


        SEM_TERMOS = Termos()
    """,
    "src/services/local.py": """
        # LEGITIMO: o dicionario e LOCAL da funcao. Nasce e morre na
        # requisicao, e nao ha nada para dividir entre workers.
        def agrupar(linhas):
            por_filial = {}
            for linha in linhas:
                por_filial.setdefault(linha.filial_id, []).append(linha)
            return por_filial
    """,
}


class VarredorDeEstadoEntreWorkersTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._temporario = TemporaryDirectory()
        cls.addClassCleanup(cls._temporario.cleanup)
        raiz = Path(cls._temporario.name)
        for caminho, codigo in ARQUIVOS.items():
            destino = raiz / caminho
            destino.parent.mkdir(parents=True, exist_ok=True)
            destino.write_text(textwrap.dedent(codigo), encoding="utf-8")
        cls.achados = auditar(raiz)

    def _texto(self, chave: str) -> str:
        return "\n".join(str(achado) for achado in self.achados[chave])

    # ------------------------------------------------------------- as iscas

    def test_o_dicionario_escrito_por_chave_e_acusado(self):
        self.assertIn("_POR_CLIENTE", self._texto("escritos"))

    def test_o_contador_com_global_e_acusado(self):
        """A forma que a medicao por "estrutura mutavel" nao pegava: um `int`
        nao e dict, list nem set — e e estado de processo igual."""
        self.assertIn("_TURNOS", self._texto("escritos"))

    def test_a_lista_com_append_e_acusada(self):
        self.assertIn("_PENDENTES", self._texto("escritos"))

    def test_a_escrita_por_ATRIBUTO_e_acusada(self):
        """Nao e `global`, nao e chave, nao e metodo mutante.

        Faltava no script, e o caso real que a expos foi `settings` — um objeto
        de modulo cujo campo alguem pode reescrever em tempo de requisicao.
        """
        self.assertIn("configuracao", self._texto("escritos"))

    def test_a_instancia_de_tipo_do_repositorio_e_acusada(self):
        self.assertIn("registro", self._texto("instancias"))

    def test_o_lru_cache_e_acusado(self):
        """Ele nao e defeito por si — e decisao que precisa de motivo escrito.

        Cinco funcoes do `src/` estao em `ESPERADOS` justamente assim: sao
        CLIENTES (Redis, OpenAI, engine), e um por processo e o desenho.
        """
        self.assertIn("produto_da_filial", self._texto("caches"))

    def test_a_acusacao_leva_arquivo_e_linha(self):
        linha = next(a for a in self.achados["escritos"] if a.nome == "_POR_CLIENTE")

        self.assertEqual(linha.arquivo, "src/services/cache_caseiro.py")
        self.assertGreater(linha.linha, 0)

    # --------------------------------------------------------- os legitimos

    def test_a_tabela_CONSTANTE_nao_e_acusada(self):
        """As 24 da primeira medicao. Tabela so lida e compartilhada de graca:
        cada processo tem a sua, identica, e ninguem escreve."""
        texto = self._texto("escritos") + self._texto("instancias")

        self.assertNotIn("ROTULOS", texto)
        self.assertNotIn("TRANSICOES", texto)

    def test_os_tipos_inertes_nao_sao_acusados(self):
        texto = self._texto("instancias")

        for nome in ("logger", "_SO_LETRAS", "FUSO", "ZERO", "UMA_HORA", "CONHECIDOS"):
            with self.subTest(nome=nome):
                self.assertNotIn(nome, texto)

    def test_a_dataclass_CONGELADA_nao_e_acusada(self):
        """Regra mecanica, e nao lista escrita a mao: classe congelada nova
        entra sozinha, e uma que DEIXAR de ser congelada volta a ser achado."""
        self.assertNotIn("SEM_TERMOS", self._texto("instancias"))

    def test_o_dicionario_LOCAL_da_funcao_nao_e_acusado(self):
        """Nasce e morre na requisicao. Acusa-lo seria acusar quase toda funcao
        do repositorio, e saida cheia de ruido e saida que ninguem le."""
        self.assertNotIn("por_filial", self._texto("escritos"))

    # -------------------------------------------------------------- o total

    def test_o_numero_de_achados_e_o_esperado(self):
        """Achado NOVO nesta arvore significa que o script passou a acusar algo
        que ninguem plantou — tao defeito quanto deixar de acusar."""
        self.assertEqual(
            {chave: len(grupo) for chave, grupo in self.achados.items()},
            # 4 escritos, 2 instancias (`registro` e o `configuracao` da isca
            # do atributo), 1 cache, e 16 declarados que nao existem nesta
            # arvore plantada.
            {"escritos": 4, "instancias": 2, "caches": 1, "declarados": 16},
        )


class OEstadoDeHojeTests(unittest.TestCase):
    """O numero contra o `src/` de verdade.

    FALHA no portao, e nao aviso. Aqui ha divida herdada — 15 sitios legitimos
    —, mas ela esta DECLARADA com motivo em `ESPERADOS`, e e a lista que serve
    de linha de base. Sitio novo fora dela e regressao.
    """

    def test_nao_ha_estado_de_processo_fora_da_lista(self):
        achados = auditar()

        self.assertEqual(
            {chave: [str(a) for a in grupo] for chave, grupo in achados.items() if grupo},
            {},
        )

    def test_a_lista_de_ESPERADOS_nao_tem_entrada_MORTA(self):
        """A metade que impede a lista de virar cemiterio.

        Uma entrada que nao casa com nada no codigo e o comeco de uma lista
        escrita a mao que envelhece — que e exatamente o defeito que este
        varredor existe para nao repetir. Ela sai no grupo `declarados`.
        """
        self.assertEqual(auditar()["declarados"], [])


if __name__ == "__main__":
    unittest.main()
