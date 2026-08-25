"""O texto que `listar_categorias` devolve ao modelo. Sem `db`: e formatacao pura.

POR QUE A FERRAMENTA EXISTE. Em 25/08/2026, perguntado "quais sao as
categorias?", o atendente de voz respondeu "tem pratos como arroz, com varios
tipos, carnes e algumas opcoes de acompanhamentos" — um cardapio plausivel e
inventado.

E nao era defeito de prompt. "Categorias" nao se parece com prato nenhum, entao
a busca por significado nao tem assunto para morder: e a mesma forma do "o mais
caro do cardapio", que virou ordenacao por SQL tres dias antes. Pergunta sobre o
cardapio INTEIRO se responde listando, e listar e SQL.
"""

from src.ai.voice.search_service import VoiceSearchService


def test_uma_categoria_por_linha_com_a_contagem():
    resumo = VoiceSearchService.resumo_das_categorias([("Carnes", 8), ("Bebidas", 3)])

    assert resumo.splitlines() == [
        "Categorias desta loja:",
        "Carnes (8)",
        "Bebidas (3)",
    ]


def test_loja_sem_categoria_nega_por_LOJA_e_nao_por_restaurante():
    """A frase e lida junto com o prompt, e a mais frouxa das duas e a que o
    modelo repete em audio. Desde que o cardapio e da filial, "este restaurante
    nao tem cardapio" e falso — a outra unidade pode ter."""
    resumo = VoiceSearchService.resumo_das_categorias([])

    assert resumo == "Nenhuma categoria com produto disponivel nesta loja."
    assert "restaurante" not in resumo


def test_o_formato_nao_e_explicado_dentro_do_resultado():
    """Explicacao de formato mora no PROMPT, que e cacheado a partir do turno
    2. Aqui seria cobrada em toda chamada para dizer sempre a mesma coisa."""
    resumo = VoiceSearchService.resumo_das_categorias([("Carnes", 8)])

    assert "produtos" not in resumo
    assert "quantos" not in resumo


def test_a_contagem_vai_em_digito_e_nao_por_extenso():
    """Ela NAO e para ser dita — e para o modelo escolher as duas maiores em vez
    de recitar doze nomes. O preco falado ao lado ensina qual campo e literal;
    escrever "oito" aqui convidaria a ler a contagem em voz alta."""
    resumo = VoiceSearchService.resumo_das_categorias([("Carnes", 8)])

    assert "(8)" in resumo
    assert "oito" not in resumo
