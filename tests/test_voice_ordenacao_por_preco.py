"""A ordenacao por preco da busca de voz, e a prova de que o `/chat` nao mudou.

Sem marcador `db`: sao a tabela `ORDENACOES`, a funcao de ordenar e a chave do
cache. O caminho de SQL (`list_active_by_price`) e coberto pelo teste de
fumaca com banco.

**Por que existe.** Duas perguntas de uma sessao real ficaram sem resposta —
"qual a bebida mais barata?" e "manda o mais caro do cardapio" — e nenhuma das
duas era defeito de prompt. A busca e por SIGNIFICADO: os cinco mais parecidos
com "bebida" nao sao as cinco mais baratas, e nenhuma regra escrita conserta
isso.

O que este arquivo trava, alem da ordem em si, e o efeito colateral em codigo
COMPARTILHADO: a voz passou a pedir busca larga, e a chave do cache de busca
nao distinguia tamanho de conjunto.
"""

from types import SimpleNamespace

import pytest

from src.ai.services.chat_cache import ChatCache
from src.ai.services.retrieval_service import TOP_K_PADRAO
from src.ai.voice.realtime_client import SEARCH_TOOL
from src.ai.voice.search_service import ORDENACOES, _ordenados_por_preco


def _produto(nome: str, preco: float | None) -> SimpleNamespace:
    return SimpleNamespace(name=nome, price=preco)


# --------------------------------------------------------------------------
# A ORDEM
# --------------------------------------------------------------------------


def test_crescente_poe_o_mais_barato_na_frente():
    ordenados = _ordenados_por_preco(
        [_produto("caro", 79.20), _produto("barato", 4.50), _produto("medio", 34.40)],
        crescente=True,
    )

    assert [p.name for p in ordenados] == ["barato", "medio", "caro"]


def test_decrescente_poe_o_mais_caro_na_frente():
    ordenados = _ordenados_por_preco(
        [_produto("caro", 79.20), _produto("barato", 4.50), _produto("medio", 34.40)],
        crescente=False,
    )

    assert [p.name for p in ordenados] == ["caro", "medio", "barato"]


@pytest.mark.parametrize("sem_valor", [None, 0, 0.0])
def test_produto_sem_preco_sai_da_lista_ordenada(sem_valor):
    """Preco ausente nao e preco zero. No topo de "o mais barato" ele seria o
    atendente oferecendo de graca o que ninguem precificou — e a voz e o
    caminho em que o cliente aceita isso de ouvido, sem tela onde conferir."""
    ordenados = _ordenados_por_preco(
        [_produto("sem preco", sem_valor), _produto("com preco", 10.00)],
        crescente=True,
    )

    assert [p.name for p in ordenados] == ["com preco"]


def test_produto_sem_preco_so_some_no_caminho_ordenado():
    """A remocao e local: fora de uma consulta ordenada ele continua
    aparecendo. Quem nao tem preco nao pode ser ordenado, mas pode ser
    mostrado."""
    from src.ai.voice.search_service import VoiceSearchService

    resumo = VoiceSearchService.resumo_para_o_modelo(
        [SimpleNamespace(name="Brinde", price=None, description=None, serves_people=None)]
    )

    assert "Brinde" in resumo


# --------------------------------------------------------------------------
# OS QUATRO VALORES
# --------------------------------------------------------------------------


def test_a_tabela_e_o_enum_da_ferramenta_dizem_a_mesma_coisa():
    """Duas listas escritas a mao em arquivos diferentes: uma que o modelo le
    (o enum da tool) e uma que o servidor executa. Divergindo, o modelo pede
    uma ordenacao que a rota recusa com 422 — e o sintoma seria "as vezes ele
    nao responde superlativo"."""
    parametros = SEARCH_TOOL["parameters"]["properties"]

    assert set(parametros["ordenar"]["enum"]) == set(ORDENACOES)


def test_a_ordenacao_fica_fora_de_required():
    """Obrigatoria, o modelo teria de escolher uma ordem em toda busca, e o
    cardapio inteiro sairia ordenado por preco sem ninguem ter pedido. E o
    mesmo motivo do `preco_maximo`, que ja estava documentado ali."""
    assert "ordenar" not in SEARCH_TOOL["parameters"]["required"]


@pytest.mark.parametrize(
    ("valor", "crescente", "da_loja"),
    [
        ("mais_barato_da_busca", True, False),
        ("mais_caro_da_busca", False, False),
        ("mais_barato_da_loja", True, True),
        ("mais_caro_da_loja", False, True),
    ],
)
def test_cada_valor_significa_o_que_o_nome_diz(valor, crescente, da_loja):
    assert ORDENACOES[valor] == (crescente, da_loja)


# --------------------------------------------------------------------------
# O EFEITO EM CODIGO COMPARTILHADO
#
# Estes tres sao a resposta a "isso mexe no /chat?". A resposta e nao — e
# estes testes sao o que impede a resposta de mudar sozinha depois.
# --------------------------------------------------------------------------


def test_a_chave_do_cache_sem_top_k_e_a_mesma_de_sempre():
    """O `/chat` nao passa `top_k`, e a chave dele tem que continuar
    BYTE A BYTE a que era antes: chave diferente e cache frio em producao,
    e ninguem pediu isso ao texto."""
    cache = ChatCache()
    antes = cache.retrieval_key("r", "b", "tem sobremesa?")
    agora = cache.retrieval_key("r", "b", "tem sobremesa?", None, top_k=None)

    assert antes == agora
    assert ":k" not in antes


def test_o_top_k_padrao_nao_entra_na_chave():
    """A voz sem ordenacao pede exatamente `TOP_K_PADRAO`, e tem que cair na
    MESMA entrada de cache do `/chat` — os dois estao perguntando a mesma
    coisa e esperando o mesmo conjunto."""
    cache = ChatCache()

    assert cache.retrieval_key("r", "b", "x") == cache.retrieval_key(
        "r", "b", "x", None, top_k=None if TOP_K_PADRAO == 5 else TOP_K_PADRAO
    )


def test_busca_larga_nao_divide_a_entrada_com_a_busca_curta():
    """O defeito que a ordenacao acordou. Sem `top_k` na chave, a consulta de
    quarenta da voz e a de cinco do `/chat` dividem a mesma entrada por 20
    minutos, e quem chegar primeiro serve o outro: ou a voz ordena cinco
    achando que sao quarenta, ou o texto recebe quarenta para escolher tres.

    Nenhum dos dois levanta erro, e e por isso que precisa de teste."""
    cache = ChatCache()

    assert cache.retrieval_key("r", "b", "bebida") != cache.retrieval_key(
        "r", "b", "bebida", None, top_k=40
    )
