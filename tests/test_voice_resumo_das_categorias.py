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


# --------------------------------------------------------------------------
# A FRASE PRONTA, E O CURSOR QUE FAZ A SEGUNDA PERGUNTA VALER A PENA
#
# O CASO (25/08/2026): "voces tem mais o que?", perguntado depois de ja ter
# ouvido duas categorias, recebeu a MESMA lista e o atendente falou as MESMAS
# duas. A lista estava certa — o que faltava era alguem saber o que ja tinha
# sido entregue.
#
# A escolha de QUAIS duas era regra de prompt ("fale no maximo DUAS, as
# maiores"), e regra de prompt nao tem memoria de um turno para o outro. Quem
# tem e a sessao: ela guarda o cursor, e estas funcoes o recebem pronto.
# --------------------------------------------------------------------------


CATEGORIAS = [("Carnes", 8), ("Massas", 6), ("Bebidas", 3), ("Sobremesas", 2)]


def test_a_frase_cita_duas_e_diz_que_ha_mais():
    """O mesmo teto de dois da frase da busca, aplicado pelo mesmo motivo:
    recitar doze nomes sao trinta segundos que ninguem pediu."""
    assert VoiceSearchService.frase_das_categorias(CATEGORIAS, 0) == (
        "Tem Carnes e Massas, e mais uns tipos."
    )


def test_a_segunda_pergunta_recebe_as_que_ainda_nao_foram_ditas():
    """E o conserto inteiro, em uma linha: com o cursor em dois, a frase fala
    das duas seguintes. Sem ele, esta chamada devolveria Carnes e Massas de
    novo — que foi o que aconteceu na sessao real."""
    assert VoiceSearchService.frase_das_categorias(CATEGORIAS, 2) == (
        "Tem Bebidas e Sobremesas, e mais uns tipos."
    )


def test_o_fim_da_lista_da_a_volta_em_vez_de_cortar_a_frase():
    """Comecar na ultima devolveria uma categoria so, e a frase de duas viraria
    a de uma por acidente de posicao. Repetir uma que ele ja ouviu e melhor do
    que falar menos porque a lista acabou."""
    assert VoiceSearchService.frase_das_categorias(CATEGORIAS, 3) == (
        "Tem Sobremesas e Carnes, e mais uns tipos."
    )


def test_loja_com_duas_categorias_nao_promete_um_terceiro_tipo():
    """"E mais uns tipos" com a lista inteira dita e propaganda de um cardapio
    que nao existe — e o cliente pergunta o que mais tem para ouvir o mesmo."""
    assert VoiceSearchService.frase_das_categorias([("Carnes", 8), ("Massas", 6)], 0) == (
        "Tem Carnes e Massas."
    )


def test_loja_com_uma_categoria_so_fala_dela():
    assert VoiceSearchService.frase_das_categorias([("Carnes", 8)], 0) == "Tem Carnes."


def test_loja_sem_categoria_nao_tem_frase():
    """Vazia, igual a da busca que nao achou nada: o que dizer depende da
    pergunta, e a pergunta esta com o modelo, nao aqui."""
    assert VoiceSearchService.frase_das_categorias([], 0) == ""


def test_o_resultado_leva_a_frase_e_a_lista_inteira():
    """Os dois blocos rotulados da busca, e a lista INTEIRA no segundo. Ela nao
    serve so para falar: e o que o modelo le antes de dizer que a loja nao tem
    alguma coisa."""
    resultado = VoiceSearchService.resultado_das_categorias(CATEGORIAS, 0)

    linhas = resultado.splitlines()
    assert linhas[0] == "FRASE: Tem Carnes e Massas, e mais uns tipos."
    assert linhas[1].startswith("DADOS (nao leia em voz alta")
    for nome, quantos in CATEGORIAS:
        assert f"{nome} ({quantos})" in resultado


def test_o_resultado_de_loja_vazia_tem_rotulo_de_frase_sem_espaco_sobrando():
    """"FRASE: " com um branco no fim e um rotulo com conteudo invisivel, e o
    modelo le rotulo."""
    resultado = VoiceSearchService.resultado_das_categorias([], 0)

    assert resultado.splitlines()[0] == "FRASE:"
    assert "Nenhuma categoria com produto disponivel nesta loja." in resultado
