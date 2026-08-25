"""O texto que a ferramenta devolve ao modelo. Sem `db`: e formatacao pura.

**Por que existe, e por que ele e mais que formatacao.** Este resumo e TUDO o
que o modelo sabe sobre o cardapio. O que nao estiver aqui nao existe para
ele — e dois defeitos de 25/08/2026 nasceram exatamente disso:

    R$ 34,40 falado como "quarenta e quatro e quarenta"
        o preco vinha so em digitos, e o modelo traduzia de cabeca

    "e essa serve para quantas pessoas?" respondido com "nao vem com a
    quantidade servida especifica"
        a descricao existia no `produtos` (que vai para a TELA) e nao aqui;
        o modelo nao negou um dado, ele nao tinha o dado

O segundo e o que este arquivo mais protege. Ele parecia defeito de prompt, e
qualquer regra escrita contra ele teria sido regra exigindo o que o contrato
nao entregava.
"""

import uuid

from src.ai.voice.search_service import VoiceSearchService, _LIMITE_DA_DESCRICAO
from src.schemas.product_schema import ProductResponse


def _produto(
    nome: str,
    preco: str,
    descricao: str | None = None,
    serve: int | None = None,
) -> ProductResponse:
    """O SCHEMA DE VERDADE, e nao um `SimpleNamespace` com os campos que a
    funcao sob teste calha de ler.

    **O dublê solto ja custou um defeito inteiro, e foi este arquivo que
    deixou de pega-lo.** Em 25/08/2026 `serves_people` entrou no
    `AdminProductResponse` (o do painel) e em `_serve_quantas_pessoas`, mas
    nao no `ProductResponse` — que e o que `MenuService.product_response`
    devolve, e portanto o que a hidratacao entrega a ferramenta de voz. Os
    testes daqui passaram, porque o `SimpleNamespace` tinha o atributo, e
    TODA busca de voz levantava `AttributeError` no caminho real. Quem
    denunciou foi o teste de fumaca com banco, um dia depois.

    A licao e a mesma do correlato da armadilha 42, do lado do schema: um
    dublê que aceita qualquer atributo nao testa o contrato, testa o dublê.
    Construir o schema real custa quatro UUIDs e faz campo que nao existe
    virar erro na hora.

    `preco` continua chegando como string, para o valor ficar legivel na
    chamada — mas o schema o guarda como `float`, que e o que
    `money_to_float` grava no caminho real. Um `Decimal` aqui esconderia
    essa conversao.

    **E `preco` deixou de aceitar `None`**: `products.price` e
    `numeric(10,2) NOT NULL` e `ProductResponse.price` e `float` obrigatorio,
    entao produto sem preco nao existe deste lado. O caso reachable e ZERO, e
    e ele que os testes usam.
    """
    return ProductResponse(
        id=uuid.uuid4(),
        restaurant_id=uuid.uuid4(),
        branch_id=uuid.uuid4(),
        category_id=uuid.uuid4(),
        name=nome,
        description=descricao,
        serves_people=serve,
        price=float(preco),
    )


def test_cada_produto_traz_os_quatro_campos():
    resumo = VoiceSearchService.resumo_para_o_modelo(
        [_produto("Baiao de dois", "34.40", "Baiao e batata frita.", serve=2)]
    )

    assert resumo.splitlines()[1] == (
        "Baiao de dois | trinta e quatro e quarenta "
        "| Baiao e batata frita. | serve 2 pessoas"
    )


def test_o_preco_em_digitos_NAO_vai_mais_para_o_modelo():
    """A regra que o campo obrigava ("nunca diga os digitos") so existia por
    causa dele. O modelo nao pode fala-lo, nao pode somar e nao pode
    arredondar — e a tela ja mostra o valor. O que ele oferecia era uma segunda
    forma do mesmo numero ao lado da forma pronta, e foi de uma troca entre as
    duas que saiu "quarenta e quatro e quarenta" para um produto de R$ 34,40."""
    linha = VoiceSearchService.resumo_para_o_modelo([_produto("X", "57.16")]).splitlines()[1]

    assert "R$" not in linha
    assert "57,16" not in linha
    assert "cinquenta e sete e dezesseis" in linha


def test_produto_sem_preco_nao_recebe_numero_nem_fala():
    """"-" no campo do preco, e o prompt manda nao falar preco desse produto.
    Um zero aqui viraria "de graca" em audio.

    ZERO e nao `None`: a coluna e NOT NULL e o schema exige o campo, entao
    "o lojista nao precificou" chega deste lado como `0.00`."""
    linha = VoiceSearchService.resumo_para_o_modelo([_produto("Brinde", "0.00")]).splitlines()[1]

    assert linha == "Brinde | - | - | -"


def test_serve_vem_por_extenso_e_concorda_no_singular():
    """O campo e para ser DITO. "2" obrigaria o modelo a escolher entre "dois"
    e "duas", que e a decisao que o preco por extenso ja tirou dele."""
    uma = VoiceSearchService.resumo_para_o_modelo([_produto("X", "10.00", serve=1)])
    varias = VoiceSearchService.resumo_para_o_modelo([_produto("X", "10.00", serve=4)])

    assert uma.splitlines()[1].endswith("| serve 1 pessoa")
    assert varias.splitlines()[1].endswith("| serve 4 pessoas")


def test_serve_nulo_vira_traco_e_nunca_o_numero_um():
    """NULO e "o lojista nao disse", e nao "serve uma pessoa". Escrever 1 aqui
    seria o backend inventando o fato que a coluna existe para parar de
    inventar — e enquanto o cadastro estiver vazio, e o caso de TODO produto."""
    linha = VoiceSearchService.resumo_para_o_modelo(
        [_produto("X", "10.00", "Uma descricao.")]
    ).splitlines()[1]

    assert linha.endswith("| -")
    assert "serve" not in linha


def test_a_descricao_do_lojista_chega_inteira_quando_cabe():
    linha = VoiceSearchService.resumo_para_o_modelo(
        [_produto("X", "10.00", "Baiao e batata frita.")]
    ).splitlines()[1]

    assert linha.split(" | ")[2] == "Baiao e batata frita."


def test_descricao_longa_e_cortada_no_espaco_e_nao_no_caractere():
    """"Serve 2 pes" e pior que uma frase a menos: o modelo le o pedaco como
    se fosse a informacao inteira."""
    descricao = "palavra " * 60
    linha = VoiceSearchService.resumo_para_o_modelo(
        [_produto("X", "10.00", descricao)]
    ).splitlines()[1]

    cortada = linha.split(" | ")[2]
    assert cortada.endswith("...")
    assert len(cortada) <= _LIMITE_DA_DESCRICAO + 3
    assert "palavr..." not in cortada


def test_descricao_vazia_ou_so_espaco_vira_traco():
    """O campo e texto livre do lojista, e ha linha em branco no banco."""
    for vazia in (None, "", "   ", "\n\t "):
        linha = VoiceSearchService.resumo_para_o_modelo(
            [_produto("X", "10.00", vazia)]
        ).splitlines()[1]
        assert linha.split(" | ")[2] == "-"


def test_a_descricao_perde_quebra_de_linha():
    """Um "\n" dentro do campo partiria a linha do produto em duas, e o
    modelo leria a segunda metade como se fosse outro produto."""
    linha_unica = VoiceSearchService.resumo_para_o_modelo(
        [_produto("X", "10.00", "Primeira linha.\nSegunda linha.")]
    )

    assert len(linha_unica.splitlines()) == 2


def test_no_maximo_cinco_produtos():
    resumo = VoiceSearchService.resumo_para_o_modelo(
        [_produto(f"P{n}", "10.00") for n in range(9)]
    )

    assert len(resumo.splitlines()) == 6  # o cabecalho mais cinco


def test_busca_vazia_continua_negando_por_LOJA():
    """A frase e lida junto com o prompt, e a mais frouxa das duas e a que o
    modelo repete em audio. Desde que o cardapio e da filial, "nao temos" sem
    recorte e falso — o produto pode estar na outra unidade."""
    assert VoiceSearchService.resumo_para_o_modelo([]) == "Nenhum produto encontrado nesta loja."


def test_o_formato_nao_e_explicado_dentro_do_resultado():
    """Explicacao de formato mora no PROMPT, que e cacheado a partir do turno
    2. Aqui ela seria cobrada inteira em toda busca, para dizer sempre a mesma
    coisa."""
    resumo = VoiceSearchService.resumo_para_o_modelo([_produto("X", "10.00")])

    assert "nome |" not in resumo
    assert "descricao" not in resumo
