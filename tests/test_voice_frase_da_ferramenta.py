"""A frase pronta que a ferramenta devolve. Sem `db`: e montagem pura.

**O que estes testes guardam, e por que eles existem em vez de assercoes no
prompt.** Ate 25/08/2026, duas decisoes eram do modelo e viviam como texto:

    quantos produtos citar   uma secao de 13 linhas do `voice_prompt.py`, cujo
                             trabalho era pedir que ele ignorasse tres dos
                             cinco produtos que a propria ferramenta mandava
    se fala o preco          "UM produto na frase: fale o preco. DOIS: so os
                             nomes" — regra sobre a frase que ele montava

As duas viraram codigo aqui. A diferenca pratica e esta suite: regra de prompt
so se testa procurando texto, e texto encontrado nao prova obediencia. Um `if`
sobre uma lista se testa com dados.

E o terceiro movimento da mesma familia — `preco_por_extenso` (o preco parou de
errar quando chegou na forma falada) e `list_active_by_price` (o superlativo
parou de errar quando o banco ordenou). Nos tres, o conserto foi entregar o
dado na forma em que ele vai ser usado.

NENHUM PRODUTO DE RESTAURANTE REAL aparece aqui, pelo mesmo motivo que ele nao
aparece mais no prompt: nomes inventados testam a montagem igual, e nome real
vira exemplo que alguem copia para o prompt depois.
"""

import uuid

from src.ai.voice.search_service import VoiceSearchService
from src.schemas.product_schema import ProductResponse


def _produto(nome: str, preco: str = "10.00") -> ProductResponse:
    """O schema real, e nao um `SimpleNamespace`: campo que o
    `ProductResponse` nao tem tem que virar erro aqui, e nao em producao.
    O relato inteiro esta em `test_voice_resumo_da_busca._produto`.

    `preco` nao aceita `None` porque `ProductResponse.price` e `float`
    obrigatorio — produto sem preco chega deste lado como zero.
    """
    return ProductResponse(
        id=uuid.uuid4(),
        restaurant_id=uuid.uuid4(),
        branch_id=uuid.uuid4(),
        category_id=uuid.uuid4(),
        name=nome,
        price=float(preco),
    )


# --------------------------------------------------------------------------
# O TETO DE DOIS, agora aplicado do lado de ca
# --------------------------------------------------------------------------


def test_um_produto_leva_o_preco_junto():
    """Quem esta com o telefone no ouvido nao ve a tela, e confirmar um pedido
    sem dizer o valor e pior do que falar demais."""
    frase = VoiceSearchService.frase_para_o_modelo([_produto("Prato Um", "23.90")])

    assert frase == "Tem Prato Um por vinte e tres e noventa."


def test_dois_produtos_saem_sem_preco_nenhum():
    """Os valores estao na tela, ao lado de cada um. Dois precos falados numa
    frase so foi o caso que originou a regra."""
    frase = VoiceSearchService.frase_para_o_modelo(
        [_produto("Prato Um", "23.90"), _produto("Prato Dois", "31.50")]
    )

    assert frase == "Tem Prato Um e Prato Dois."
    assert "vinte" not in frase
    assert "trinta" not in frase


def test_mais_de_dois_para_em_dois_e_avisa_que_ha_mais():
    """O teto e da FRASE, e nao da busca: os cinco continuam indo para a tela e
    para os DADOS. Enumerar o resto e o inventario que o teto existe para nao
    ler."""
    produtos = [_produto(f"Prato {n}") for n in range(1, 6)]

    frase = VoiceSearchService.frase_para_o_modelo(produtos)

    assert frase == "Tem Prato 1 e Prato 2, e mais alguns."
    assert "Prato 3" not in frase


def test_produto_sem_preco_nao_ganha_numero_inventado():
    """`preco_por_extenso` devolve None para preco ausente, zerado ou fora da
    faixa. Dizer "por zero reais" seria oferecer de graca o que ninguem
    precificou.

    O caso que chega aqui e o ZERO, e nao o nulo: a coluna e NOT NULL."""
    assert VoiceSearchService.frase_para_o_modelo([_produto("Prato Um", "0.00")]) == "Tem Prato Um."


# --------------------------------------------------------------------------
# A NEGATIVA, que ate 25/08/2026 era do modelo
#
# O caso que a trouxe para ca: "voces tem picanha?" numa churrascaria, o modelo
# nao entendeu a palavra, chamou `listar_categorias` em vez da busca, e
# respondeu "nao tem no cardapio, mas tem Executivos e Bebidas".
#
# Enquanto a negativa fosse texto que o modelo escrevia, ele conseguia
# escreve-la SEM TER BUSCADO. Sendo uma frase que so a busca devolve, "nao
# temos" passa a ter uma origem unica — e e isso que estes testes guardam.
# --------------------------------------------------------------------------


def test_busca_vazia_devolve_a_negativa_pronta():
    """Isto INVERTEU o teste que estava aqui, que afirmava frase vazia na busca
    sem resultado. A frase vazia era o buraco por onde a negativa voltava a ser
    decisao do modelo."""
    assert VoiceSearchService.frase_para_o_modelo([]) == "Aqui a gente nao tem isso."


def test_a_negativa_leva_ate_duas_categorias_para_oferecer():
    """Escolher qual categoria oferecer era mais uma decisao do modelo ("se
    vierem categorias junto, ofereca uma delas") sobre uma lista que este
    metodo tem inteira na mao."""
    frase = VoiceSearchService.frase_para_o_modelo(
        [], [("Categoria Um", 8), ("Categoria Dois", 3), ("Categoria Tres", 1)]
    )

    assert frase == "Aqui a gente nao tem isso, mas tem Categoria Um e Categoria Dois."
    assert "Categoria Tres" not in frase


def test_a_negativa_com_uma_categoria_so_nao_fica_com_e_solto():
    frase = VoiceSearchService.frase_para_o_modelo([], [("Categoria Um", 8)])

    assert frase == "Aqui a gente nao tem isso, mas tem Categoria Um."


def test_a_negativa_vem_antes_da_oferta():
    """Apresentar outra coisa antes de dizer que nao tem o que ele pediu faz o
    cliente ouvir que ganhou uma resposta quando levou uma negativa."""
    frase = VoiceSearchService.frase_para_o_modelo([], [("Categoria Um", 8)])

    assert frase.index("nao tem") < frase.index("mas tem")


def test_a_busca_que_achou_algo_nunca_traz_negativa():
    """A negativa e o ramo da lista vazia, e so ele. Uma negativa colada numa
    frase com produto seria o atendente dizendo que nao tem o que acabou de
    oferecer."""
    frase = VoiceSearchService.frase_para_o_modelo(
        [_produto("Prato Um", "23.90")], [("Categoria Um", 8)]
    )

    assert "nao tem" not in frase


# --------------------------------------------------------------------------
# O RESULTADO INTEIRO: dois blocos rotulados
# --------------------------------------------------------------------------


def test_o_resultado_traz_a_frase_e_os_dados_rotulados():
    """Rotulo em vez de formato posicional. O contrato implicito de "quatro
    campos separados por |" custava nove bullets do prompt para ser explicado;
    o rotulo custa alguns tokens por busca."""
    resultado = VoiceSearchService.resultado_para_o_modelo([_produto("Prato Um", "23.90")])

    assert resultado.startswith("FRASE: Tem Prato Um por vinte e tres e noventa.")
    assert "DADOS (nao leia em voz alta" in resultado
    assert "Prato Um | vinte e tres e noventa" in resultado


def test_a_busca_vazia_leva_as_categorias_junto():
    """O que aposenta "so busque um termo mais amplo se nao devolver nada":
    quem sabe que a busca voltou vazia e o backend. Mandar o que a loja TEM
    junto com o "nao achei" e dado, e nao mais uma instrucao."""
    resultado = VoiceSearchService.resultado_para_o_modelo([], [("Categoria Um", 8)])

    assert resultado.startswith("FRASE: Aqui a gente nao tem isso, mas tem Categoria Um.")
    assert "Nenhum produto encontrado nesta loja." in resultado
    assert "Categoria Um (8)" in resultado


def test_a_categoria_nao_entra_quando_a_busca_achou_algo():
    """Elas sao a saida da negativa, e nao um apendice de toda busca: em toda
    resposta seriam tokens cobrados para o modelo ler o que ele nao vai usar."""
    resultado = VoiceSearchService.resultado_para_o_modelo(
        [_produto("Prato Um")], [("Categoria Um", 8)]
    )

    assert "Categoria Um" not in resultado


def test_a_busca_nunca_devolve_frase_vazia():
    """FRASE vazia era o sinal de "nao achei" e virou a negativa pronta. O
    rotulo sozinho sobrou so para `listar_categorias`, onde ele quer dizer
    outra coisa: a loja nao tem nada vendavel agora."""
    resultado = VoiceSearchService.resultado_para_o_modelo([], None)

    assert resultado.startswith("FRASE: Aqui a gente nao tem isso.")
