"""A reescrita da consulta no backend. Sem `db`: e texto entrando e saindo.

**O que ela tirou do modelo, e o que ela NAO consegue tirar.**

Tirou a escolha da ordenacao. Ate 25/08/2026 isso era um enum de quatro valores
na declaracao da ferramenta, preenchido pelo modelo no meio de uma conversa, e
custava cinco bullets do prompt mais a descricao do parametro. Hoje sai de
`_reescrever_consulta`, que le as palavras que chegaram.

Nao tirou — e nao tem como tirar — a fidelidade ao termo do cliente. O audio vai
do navegador direto para a OpenAI e nunca passa por aqui: quando a consulta
chega, o unico texto que existe deste lado e o que o MODELO escreveu. Reescrever
ali e reescrever o que ja saiu errado. Por isso "busque com a palavra que o
cliente falou" continua sendo regra de prompt, e por isso o log leva `consulta`
e `buscada` na mesma linha — sem as duas, "o modelo mandou errado" e "nos
reescrevemos errado" sao indistinguiveis.

Os termos destes testes sao categorias genericas, e nunca produto de um
restaurante real: o mesmo motivo pelo qual eles sairam do prompt.
"""

import pytest

from src.ai.voice.search_service import _reescrever_consulta


# --------------------------------------------------------------------------
# SEM SUPERLATIVO: so cai o ruido
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("consulta", "esperada"),
    [
        ("quero uma sobremesa", "sobremesa"),
        ("o que tem de bebida", "bebida"),
        ("sobremesa de chocolate", "sobremesa chocolate"),
    ],
)
def test_o_ruido_sai_e_a_ordenacao_nao_entra(consulta, esperada):
    """Palavra que nao nomeia comida nao ajuda a busca por significado. Sem
    superlativo, a ordenacao continua `None` e o caminho e o de sempre."""
    assert _reescrever_consulta(consulta) == (esperada, None)


def test_o_acento_do_termo_sobrevive():
    """O texto dobrado serve para RECONHECER, nunca para substituir: quem busca
    e o embedding, e uma palavra sem acento nao e a mesma entrada que ela com
    acento. Se esta linha cair, toda busca por termo acentuado passa a ser
    outra busca — sem erro e sem log."""
    buscada, ordenar = _reescrever_consulta("quero um pao")

    assert buscada == "pao"
    assert ordenar is None

    acentuada, _ = _reescrever_consulta("quero um pão")
    assert acentuada == "pão"


def test_consulta_que_e_so_ruido_volta_inteira():
    """Cortar tudo deixaria a busca sem entrada. Uma pergunta estranha ainda e
    melhor que uma consulta vazia, que devolve qualquer coisa."""
    assert _reescrever_consulta("o que tem aqui") == ("o que tem aqui", None)


# --------------------------------------------------------------------------
# COM SUPERLATIVO: a ordenacao sai das palavras, e elas saem da consulta
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("consulta", "esperada", "ordenacao"),
    [
        ("a bebida mais barata", "bebida", "mais_barato_da_busca"),
        ("qual a sobremesa mais cara", "sobremesa", "mais_caro_da_busca"),
        ("uma entrada mais em conta", "entrada", "mais_barato_da_busca"),
    ],
)
def test_superlativo_com_assunto_vai_para_a_busca(consulta, esperada, ordenacao):
    """"A bebida mais barata" tem assunto: a busca por significado roda larga e
    o resultado e ordenado por preco. E o texto do superlativo SAI da consulta
    — ele nao se parece com produto nenhum e so empurra a similaridade para
    baixo."""
    assert _reescrever_consulta(consulta) == (esperada, ordenacao)


@pytest.mark.parametrize(
    ("consulta", "ordenacao"),
    [
        ("manda o mais caro", "mais_caro_da_loja"),
        ("o mais barato do cardapio", "mais_barato_da_loja"),
        ("qual o de menor preco", "mais_barato_da_loja"),
    ],
)
def test_superlativo_sozinho_vai_para_a_loja_inteira(consulta, ordenacao):
    """Nao sobrando palavra nenhuma depois de arrancar o superlativo e o ruido,
    o cliente falou do cardapio INTEIRO — e ai nao ha busca por significado que
    ajude, porque "cardapio" nao se parece com nada. O SQL ordena a loja.

    A consulta volta VAZIA de proposito: o caminho `_da_loja` nao a usa, e
    devolver um resto qualquer daria a impressao de que ela pesa em algo."""
    assert _reescrever_consulta(consulta) == ("", ordenacao)


def test_a_ordenacao_devolvida_existe_na_tabela_que_o_servico_executa():
    """As duas listas — as palavras que disparam e os pares (crescente,
    da_loja) — sao escritas a mao em pontos diferentes do arquivo. Divergindo,
    a ordenacao cai silenciosamente no caminho sem ordem, e o sintoma seria "as
    vezes ele nao responde superlativo"."""
    from src.ai.voice.search_service import ORDENACOES

    for consulta in ("a bebida mais barata", "manda o mais caro",
                     "o mais barato do cardapio", "qual a entrada mais cara"):
        _, ordenacao = _reescrever_consulta(consulta)
        assert ordenacao in ORDENACOES, consulta
