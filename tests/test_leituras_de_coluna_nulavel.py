"""O varredor de leitura de coluna nulável enxerga — e um teto não prova isso.

`scripts/leituras_de_coluna_nulavel.py` roda no CI como **aviso com teto**:

    python scripts/leituras_de_coluna_nulavel.py --url "$TEST_DATABASE_URL" --limite 208

O teto é uma linha de base honesta — 182 → 201 → 206 → 208, cada degrau olhado
um a um e justificado no comentário do workflow. Mas ele é **unilateral**, e
essa é a razão deste arquivo: um portão de teto responde *"entrou leitura
nova?"* e é cego para o varredor **parar de enxergar**. Um `ast.Attribute` que
deixe de casar, um nó novo do Python, uma mudança em
`divergencias_orm_schema.comparar()` — qualquer um leva a contagem a zero, e
zero passa no teto com folga. **O verde fica mais verde justamente quando o
varredor cega.**

Era o único varredor deste repositório sem teste. Os outros nove têm um
arquivo provando que eles veem; este tinha só o número.

O que se prova aqui:

- as **cinco formas que quebram** são todas acusadas — cada uma é um jeito
  diferente de o `None` virar exceção, e cobrir quatro é deixar a quinta viva;
- as **seis formas protegidas** passam. Sem isso o varredor viraria ruído, e o
  destino de um varredor ruidoso é o teto subir para calá-lo;
- os **dois casos que ele ignora de propósito** — `Modelo.coluna` de expressão
  SQL, e escrita em vez de leitura;
- e a **não-vacuidade contra o `src/` de verdade**, que é o par que fecha o
  buraco do teto: uma coluna real acha pontos, uma coluna inventada acha zero.
  Um sozinho não vale — "acha algo" pode ser um varredor que acusa tudo, e
  "acha zero" pode ser um varredor cego.

A lista de colunas é PARÂMETRO das duas funções públicas, então nada aqui
precisa de Postgres: quem lê o banco é o `main`, para montar essa lista.
"""

import textwrap

import pytest

from scripts import leituras_de_coluna_nulavel
from scripts.leituras_de_coluna_nulavel import campos_de_schema_sem_nulo, leituras


COLUNA = {"valid_until"}


@pytest.fixture
def plantar(tmp_path, monkeypatch):
    """Escreve um `src/` de mentira e aponta o varredor para ele."""

    def escrever(codigo: str):
        pasta = tmp_path / "src"
        pasta.mkdir(exist_ok=True)
        (pasta / "modulo.py").write_text(textwrap.dedent(codigo), encoding="utf-8")
        monkeypatch.setattr(leituras_de_coluna_nulavel, "ROOT_DIR", tmp_path)
        monkeypatch.setattr(leituras_de_coluna_nulavel, "DIRETORIOS", ("src",))
        return leituras(COLUNA)

    return escrever


def formas(achados):
    return [forma for _arquivo, _linha, _coluna, forma, _texto in achados]


class TestAsCincoFormasQueQuebram:
    def test_atributo_ou_metodo_em_cima_do_valor(self, plantar):
        """`_aware(cupom.valid_until)` foi o caso real: `.tzinfo` num `None`
        levantava `AttributeError` no CHECKOUT (armadilha 54)."""
        achados = plantar("def f(cupom):\n    return cupom.valid_until.tzinfo\n")

        assert formas(achados) == ["atributo/metodo em cima do valor"]

    def test_comparacao_de_ordem(self, plantar):
        achados = plantar("def f(cupom, agora):\n    return cupom.valid_until >= agora\n")

        assert formas(achados) == ["comparacao de ordem"]

    def test_aritmetica(self, plantar):
        achados = plantar("def f(cupom, delta):\n    return cupom.valid_until - delta\n")

        assert formas(achados) == ["aritmetica"]

    def test_argumento_nomeado(self, plantar):
        """É por aqui que o `None` entra num schema de resposta que declara o
        campo sem `| None` — e vira 500 na serialização, derrubando a resposta
        inteira e não só o item quebrado."""
        achados = plantar("def f(cupom):\n    return Resposta(valido_ate=cupom.valid_until)\n")

        assert formas(achados) == ["argumento nomeado (schema de resposta?)"]

    def test_argumento_posicional(self, plantar):
        achados = plantar("def f(cupom):\n    return formatar(cupom.valid_until)\n")

        assert formas(achados) == ["argumento posicional"]

    def test_igualdade_nao_conta(self, plantar):
        """`None == x` é `False`, não `TypeError`. Comparar por igualdade com
        nulo é jeito legítimo (ainda que torto) de perguntar se há valor — e
        acusar isso encheria a saída do que não quebra."""
        achados = plantar("def f(cupom, agora):\n    return cupom.valid_until == agora\n")

        assert achados == []


class TestOQueEstaProtegidoPassa:
    """Varredor ruidoso tem um destino só: o teto sobe para calá-lo."""

    @pytest.mark.parametrize(
        "codigo",
        [
            "def f(c):\n    if c.valid_until is None:\n        return 1\n",
            "def f(c):\n    return c.valid_until or PADRAO\n",
            "def f(c):\n    if not c.valid_until:\n        return 1\n",
            "def f(c):\n    if c.valid_until:\n        return 1\n",
            "def f(c):\n    return getattr(c, 'valid_until', None)\n",
        ],
        ids=["is None", "or", "not", "if", "getattr"],
    )
    def test_a_guarda_explicita_nao_e_acusada(self, plantar, codigo):
        assert plantar(codigo) == []

    def test_FALSO_POSITIVO_CONHECIDO_a_guarda_por_curto_circuito(self, plantar):
        """`c.valid_until and c.valid_until.tzinfo` é acusado, e não deveria.

        O varredor olha o PAI imediato de cada acesso. O primeiro
        `c.valid_until` tem `BoolOp` por pai e passa; o segundo tem `.tzinfo`
        por pai e cai como "atributo em cima do valor" — mesmo só executando
        quando o primeiro foi verdadeiro. Rastrear isso pediria saber que os
        dois acessos são a MESMA expressão, que é análise de fluxo e não de
        forma.

        **Fica documentado em vez de consertado**, por dois motivos. O barato:
        o custo de um falso positivo aqui é ruído numa saída que uma pessoa lê,
        e não um `DROP` na constraint errada. O que decide: consertar mexeria na
        linha de base do CI, e a linha de base tem história escrita degrau a
        degrau no workflow — é mudança com dono, não de passagem.

        Este teste existe para o dia em que alguém encontrar essa saída e achar
        que descobriu um defeito. Descobriu; já estava anotado."""
        achados = plantar("def f(c):\n    return c.valid_until and c.valid_until.tzinfo\n")

        assert formas(achados) == ["atributo/metodo em cima do valor"]


class TestOQueEleIgnoraDeProposito:
    def test_coluna_de_expressao_sql_nao_le_valor(self, plantar):
        """`RestaurantCoupon.valid_until >= agora` não lê valor nenhum: é a
        coluna, e o `None` vira `IS NULL` do lado do banco. Acusar isto seria
        acusar exatamente o `WHERE` que a armadilha 54 mandou escrever."""
        achados = plantar("def f(agora):\n    return RestaurantCoupon.valid_until >= agora\n")

        assert achados == []

    def test_escrita_nao_e_leitura(self, plantar):
        achados = plantar("def f(c, valor):\n    c.valid_until = valor\n")

        assert achados == []


class TestNaoVacuidade:
    """O par que fecha o buraco do teto. Um sozinho não vale nada."""

    def test_uma_coluna_de_verdade_acha_pontos_no_src(self):
        """Contra o `src/` de verdade. Se isto zerar, o varredor cegou — e o
        `--limite 208` do CI teria ficado verde sem dizer uma palavra."""
        assert len(leituras({"email"})) > 0

    def test_uma_coluna_inventada_acha_zero(self):
        """A outra metade: sem ela, o teste acima passaria com um varredor que
        acusa qualquer acesso a atributo."""
        assert leituras({"coluna_que_nao_existe_em_lugar_nenhum"}) == []

    def test_a_segunda_secao_tambem_enxerga(self):
        """`campos_de_schema_sem_nulo` percorre os `BaseModel` de `src/`. Ela
        quebra por um motivo diferente do das leituras — um `import` que passe
        a falhar é engolido por `except Exception: continue` —, então precisa
        da própria prova."""
        assert len(campos_de_schema_sem_nulo({"email"})) > 0
        assert campos_de_schema_sem_nulo({"coluna_que_nao_existe_em_lugar_nenhum"}) == []
