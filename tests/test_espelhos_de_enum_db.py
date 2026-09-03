"""A constante do código e o CHECK do banco falam a mesma lista — armadilha 15.

São duas escritas do mesmo domínio, em lugares que não se olham: uma revisão
do Alembic e um `.py`. Nada as obriga a mudar juntas, e o sintoma não aparece
em nenhum dos dois lados — aparece no checkout de um cliente:

- **valor só no BANCO**: a filial oferece a forma de pagamento na tela e o
  pedido é recusado na criação com 400. O lojista vê a opção funcionando no
  painel dele e o cliente não consegue fechar;
- **valor só no CÓDIGO**: passa pela validação do schema e morre no INSERT.

Havia 23 colunas de enum no banco e duas com alguém cobrando a igualdade —
as duas escritas ontem, em `test_particao_dos_status.py`, e por outro motivo.

O zero deste arquivo é o mesmo zero dos outros varredores, e vale pelas mesmas
provas: que ele **enxerga as quatro formas** em que uma lista fechada é
escrita aqui, que ele **acusa a divergência nos dois sentidos**, e que ele
está olhando para um banco que de fato tem colunas de enum — um regex quebrado
daria zero colunas, e zero colunas daria zero divergências.

O varredor também acusa **constraint duplicada** (duas CHECK com a definição
idêntica, avaliadas as duas em toda escrita — a armadilha 4 na forma que o
`audit_indexes.py` não vê). Aqui a prova tem duas metades e a segunda é a que
importa: ele acusa o par idêntico **e deixa passar duas CHECK diferentes sobre
a mesma coluna**. O achado pede um DROP, e um DROP na constraint errada não dá
erro nenhum no dia em que roda — foi exatamente o falso positivo da primeira
versão, que acusava a lista de tipos de desconto junto com a regra que amarra
o valor ao tipo.
"""

import textwrap
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from scripts import espelhos_de_enum
from scripts.espelhos_de_enum import (
    SEM_ESPELHO,
    ConjuntosDoCodigo,
    _colunas_de_enum,
    auditar,
)
from tests.conftest import url_do_banco_de_teste


FONTES = {
    "src/core/constants.py": """
        # tupla de literais
        FORMAS = ("pix", "cash")
    """,
    "src/models/cupom_model.py": """
        # tupla de CONSTANTES, que é como as visibilidades do cupom são escritas
        VISIBILIDADE_PUBLICA = "public"
        VISIBILIDADE_PRIVADA = "private"
        VISIBILIDADES = (VISIBILIDADE_PUBLICA, VISIBILIDADE_PRIVADA)
    """,
    "src/schemas/segmento_schema.py": """
        from enum import Enum
        from typing import Literal

        from pydantic import BaseModel


        # classe `str, Enum` — o contrato que sai no /openapi.json
        class Segmento(str, Enum):
            NOVO = "novo"
            FIEL = "fiel"


        # alias de Literal
        TipoDeDesconto = Literal["percent", "fixed"]


        # campo anotado com Literal
        class Avaliacao(BaseModel):
            feedback: Literal["like", "dislike"]
    """,
}


def _arvore_plantada(raiz: Path) -> None:
    for caminho, conteudo in FONTES.items():
        arquivo = raiz / caminho
        arquivo.parent.mkdir(parents=True, exist_ok=True)
        arquivo.write_text(textwrap.dedent(conteudo), encoding="utf-8")


@pytest.fixture
def codigo_plantado():
    with TemporaryDirectory() as temporario:
        raiz = Path(temporario)
        _arvore_plantada(raiz)
        yield raiz


class TestAsQuatroFormasDeDeclararUmConjunto:
    """Uma forma que pare de ser lida deixa uma família inteira sem espelho —
    e sem espelho o par volta a ser conferido de olho, que é o estado que este
    arquivo existe para tirar."""

    def test_tupla_de_literais(self, codigo_plantado):
        conjuntos = ConjuntosDoCodigo(codigo_plantado).por_caminho

        assert conjuntos["src/core/constants.py:FORMAS"] == {"pix", "cash"}

    def test_tupla_de_constantes(self, codigo_plantado):
        conjuntos = ConjuntosDoCodigo(codigo_plantado).por_caminho

        assert conjuntos["src/models/cupom_model.py:VISIBILIDADES"] == {"public", "private"}

    def test_classe_str_enum(self, codigo_plantado):
        conjuntos = ConjuntosDoCodigo(codigo_plantado).por_caminho

        assert conjuntos["src/schemas/segmento_schema.py:Segmento"] == {"novo", "fiel"}

    def test_alias_de_literal(self, codigo_plantado):
        conjuntos = ConjuntosDoCodigo(codigo_plantado).por_caminho

        assert conjuntos["src/schemas/segmento_schema.py:TipoDeDesconto"] == {"percent", "fixed"}

    def test_campo_anotado_com_literal(self, codigo_plantado):
        conjuntos = ConjuntosDoCodigo(codigo_plantado).por_caminho

        assert conjuntos["src/schemas/segmento_schema.py:Avaliacao.feedback"] == {
            "like",
            "dislike",
        }


def _banco_falso(colunas):
    return lambda url: colunas


class TestEleAcusa:
    """As quatro coisas que podem estar erradas entre as duas escritas."""

    @staticmethod
    def _auditar(monkeypatch, raiz, colunas, espelhos):
        monkeypatch.setattr(espelhos_de_enum, "_colunas_de_enum", _banco_falso(colunas))
        monkeypatch.setattr(espelhos_de_enum, "ESPELHOS", espelhos)
        return auditar("url-nao-usada", raiz)

    def test_valor_que_existe_so_no_banco(self, monkeypatch, codigo_plantado):
        """O caso caro: a filial oferece `voucher` na tela e o pedido é
        recusado na criação, porque a constante não o conhece."""
        achados = self._auditar(
            monkeypatch,
            codigo_plantado,
            {"ck_formas": {"tabela": "t", "coluna": "forma", "valores": {"pix", "cash", "voucher"}, "definicao": "d1"}},
            {"ck_formas": "src/core/constants.py:FORMAS"},
        )

        assert [a.tipo for a in achados] == ["divergem"]
        assert "'voucher'" in achados[0].detalhe

    def test_valor_que_existe_so_no_codigo(self, monkeypatch, codigo_plantado):
        achados = self._auditar(
            monkeypatch,
            codigo_plantado,
            {"ck_formas": {"tabela": "t", "coluna": "forma", "valores": {"pix"}, "definicao": "d2"}},
            {"ck_formas": "src/core/constants.py:FORMAS"},
        )

        assert [a.tipo for a in achados] == ["divergem"]
        assert "'cash'" in achados[0].detalhe

    def test_coluna_de_enum_nova_que_ninguem_declarou(self, monkeypatch, codigo_plantado):
        """A revisão que cria uma coluna de enum e não é conferida contra o
        código. É por onde a divergência entra."""
        achados = self._auditar(
            monkeypatch,
            codigo_plantado,
            {"ck_nova": {"tabela": "t", "coluna": "nova", "valores": {"a", "b"}, "definicao": "d3"}},
            {},
        )

        assert [a.tipo for a in achados] == ["sem registro"]

    def test_espelho_que_sumiu_do_codigo(self, monkeypatch, codigo_plantado):
        achados = self._auditar(
            monkeypatch,
            codigo_plantado,
            {"ck_formas": {"tabela": "t", "coluna": "forma", "valores": {"pix", "cash"}, "definicao": "d4"}},
            {"ck_formas": "src/core/constants.py:CONSTANTE_QUE_NAO_EXISTE"},
        )

        assert [a.tipo for a in achados] == ["espelho sumiu"]

    def test_constraint_que_sumiu_do_banco(self, monkeypatch, codigo_plantado):
        """Entrada que descreve constraint que não existe mais é cemitério."""
        achados = self._auditar(
            monkeypatch,
            codigo_plantado,
            {},
            {"ck_que_nao_existe_mais": (SEM_ESPELHO, "motivo qualquer")},
        )

        assert [a.tipo for a in achados] == ["constraint sumiu"]

    def test_duas_CHECK_com_a_MESMA_definicao(self, monkeypatch, codigo_plantado):
        """`admin_users` tinha uma: `admin_users_role_check` e
        `ck_admin_users_role`, idênticas, as duas avaliadas em toda escrita."""
        identica = "CHECK ((papel = ANY (ARRAY['a'::text, 'b'::text])))"
        achados = self._auditar(
            monkeypatch,
            codigo_plantado,
            {
                "t_papel_check": {"tabela": "t", "coluna": "papel", "valores": {"a", "b"}, "definicao": identica},
                "ck_t_papel": {"tabela": "t", "coluna": "papel", "valores": {"a", "b"}, "definicao": identica},
            },
            {
                "t_papel_check": (SEM_ESPELHO, "declarada"),
                "ck_t_papel": (SEM_ESPELHO, "declarada"),
            },
        )

        assert [a.tipo for a in achados] == ["duplicata"]

    def test_duas_CHECK_DIFERENTES_sobre_a_mesma_coluna_nao_sao_duplicata(
        self, monkeypatch, codigo_plantado
    ):
        """O falso positivo da primeira versão, e é o que custa caro: em
        `restaurant_coupons`, a lista de tipos de desconto e a regra que amarra
        o VALOR ao tipo falam da mesma coluna e citam os mesmos literais.
        Derrubar a segunda abriria cupom percentual de valor zero."""
        achados = self._auditar(
            monkeypatch,
            codigo_plantado,
            {
                "ck_tipo": {
                    "tabela": "t",
                    "coluna": "tipo",
                    "valores": {"percent", "fixed"},
                    "definicao": "CHECK ((tipo = ANY (ARRAY['percent'::text, 'fixed'::text])))",
                },
                "ck_valor": {
                    "tabela": "t",
                    "coluna": "tipo",
                    "valores": {"percent", "fixed"},
                    "definicao": (
                        "CHECK (((tipo = ANY (ARRAY['percent'::text, 'fixed'::text])) "
                        "AND (valor > (0)::numeric)))"
                    ),
                },
            },
            {"ck_tipo": (SEM_ESPELHO, "declarada"), "ck_valor": (SEM_ESPELHO, "declarada")},
        )

        assert achados == []

    def test_SEM_ESPELHO_declarado_nao_e_achado(self, monkeypatch, codigo_plantado):
        """`SEM_ESPELHO` é resposta válida e escrita: é a fronteira do que
        este portão alcança, anotada onde dá para ver."""
        achados = self._auditar(
            monkeypatch,
            codigo_plantado,
            {"ck_solto": {"tabela": "t", "coluna": "solto", "valores": {"a", "b"}, "definicao": "d5"}},
            {"ck_solto": (SEM_ESPELHO, "os dois valores vivem soltos no código")},
        )

        assert achados == []


@pytest.mark.db
class TestOBancoDeVerdade:
    def test_toda_coluna_de_enum_esta_declarada_e_todo_espelho_bate(self, db):
        # A fixture `db` nao e usada para consultar: ela existe para o schema
        # ja estar em `head` quando o varredor abrir a propria conexao.
        achados = auditar(url_do_banco_de_teste())

        assert [f"[{a.tipo}] {a}" for a in achados] == []

    def test_o_varredor_esta_de_fato_olhando_para_colunas_de_enum(self, db):
        """Um regex quebrado daria zero colunas, e zero colunas daria zero
        divergências: o verde por ausência é indistinguível do verde por
        acerto. `orders.status` é a que tem que estar lá em qualquer caso."""
        colunas = _colunas_de_enum(url_do_banco_de_teste())

        assert len(colunas) >= 20
        assert colunas["orders_status_check"]["coluna"] == "status"
        assert "out_for_delivery" in colunas["orders_status_check"]["valores"]

    def test_a_CHECK_duplicada_de_admin_users_foi_derrubada(self, db):
        """A revisão `20260904_0048`. `ck_admin_users_role` fica; o nome que o
        Postgres gerou sozinho sai. Duas idênticas eram avaliadas as duas em
        todo INSERT e todo UPDATE de `admin_users`."""
        colunas = _colunas_de_enum(url_do_banco_de_teste())

        assert "ck_admin_users_role" in colunas
        assert "admin_users_role_check" not in colunas
