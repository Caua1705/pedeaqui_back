"""O preco escrito como se fala. Sem marcador `db`: e uma funcao pura.

**Por que existe.** `R$ 34,40` foi falado como "quarenta e quatro e quarenta"
numa sessao real. O conserto tirou a traducao das maos do modelo e a trouxe
para ca — e o que se ganhou foi exatamente isto: a forma falada virou uma
coisa que um teste consegue afirmar.

Enquanto ela morava na cabeca do modelo, "esta certo" era opiniao sobre uma
sessao. Agora e uma tabela.
"""

from decimal import Decimal

import pytest

from src.utils.money_por_extenso import TETO_EM_REAIS, preco_por_extenso


@pytest.mark.parametrize(
    ("valor", "esperado"),
    [
        # O caso que originou o arquivo.
        ("34.40", "trinta e quatro e quarenta"),
        # A forma curta do balcao, nas faixas que o cardapio real usa.
        ("57.16", "cinquenta e sete e dezesseis"),
        ("24.66", "vinte e quatro e sessenta e seis"),
        ("29.90", "vinte e nove e noventa"),
        ("79.20", "setenta e nove e vinte"),
        # Centavos zerados NAO viram "e zero".
        ("34.00", "trinta e quatro"),
        ("100.00", "cem"),
        ("101.00", "cento e um"),
        # Adolescentes e a irregularidade do portugues.
        ("16.00", "dezesseis"),
        ("15.15", "quinze e quinze"),
        # Milhar, que nao e preco de prato mas e preco de rodizio para grupo.
        ("1000.00", "mil"),
        ("1200.00", "mil e duzentos"),
        ("1234.50", "mil duzentos e trinta e quatro e cinquenta"),
        # O maior valor que a funcao aceita.
        ("9999.99", "nove mil novecentos e noventa e nove e noventa e nove"),
    ],
)
def test_a_forma_falada_de_cada_faixa(valor: str, esperado: str):
    assert preco_por_extenso(Decimal(valor)) == esperado


@pytest.mark.parametrize(
    ("valor", "esperado"),
    [
        ("0.01", "um centavo"),
        ("0.50", "cinquenta centavos"),
        ("0.99", "noventa e nove centavos"),
    ],
)
def test_preco_sem_reais_nao_comeca_com_zero(valor: str, esperado: str):
    """O pudim de um centavo existe no cardapio de teste. "zero reais e um
    centavo" ninguem fala, e soaria como se o produto fosse de graca."""
    assert preco_por_extenso(Decimal(valor)) == esperado


@pytest.mark.parametrize(
    ("valor", "esperado"),
    [
        ("34.05", "trinta e quatro e cinco centavos"),
        ("34.09", "trinta e quatro e nove centavos"),
        ("7.01", "sete e um centavos"),
    ],
)
def test_centavo_de_um_digito_leva_a_palavra_centavos(valor: str, esperado: str):
    """A UNICA quebra de tom do arquivo, e ela e de proposito: sem "centavos",
    "trinta e quatro e cinco" vira R$ 34,50 no ouvido de quem escuta — a mesma
    familia de defeito que este modulo existe para consertar.

    Clareza vence consistencia quando o numero e dinheiro."""
    assert preco_por_extenso(Decimal(valor)) == esperado


def test_a_diferenca_entre_cinco_centavos_e_cinquenta_e_audivel():
    """O teste que justifica o de cima. Se as duas formas colidissem, o
    conserto teria criado a segunda geracao do proprio defeito."""
    assert preco_por_extenso(Decimal("34.05")) != preco_por_extenso(Decimal("34.50"))


@pytest.mark.parametrize(
    "valor",
    [None, "0.00", "-1.00", "-0.01", str(TETO_EM_REAIS), "99999.99"],
)
def test_fora_da_faixa_devolve_none_em_vez_de_palpite(valor):
    """`None` e a ordem para quem chama OMITIR a dica falada. Preco que este
    modulo nao sabe dizer com certeza e preco que o modelo nao recebe pronto:
    ele cai no comportamento antigo, que e pior, mas conhecido.

    Inventar forma falada para numero de borda seria criar a segunda geracao
    do defeito que a primeira existe para matar."""
    assert preco_por_extenso(None if valor is None else Decimal(valor)) is None


def test_nenhuma_forma_falada_traz_reais_ou_centavos_a_toa():
    """A forma longa — "cinquenta e sete reais e dezesseis centavos" — custa
    ~44% mais audio de saida sem dizer nada a mais, e o prompt a proibia por
    escrito. Agora ela e impossivel de produzir, que e melhor do que proibida.

    "centavos" so aparece quando os reais sao zero ou os centavos tem um
    digito; "reais" nunca aparece."""
    for centavos in range(1, 20_000):
        falado = preco_por_extenso(Decimal(centavos) / 100)
        if falado is None:
            continue
        assert "reais" not in falado, falado
        if "centavos" in falado:
            reais, resto = divmod(centavos, 100)
            assert reais == 0 or resto < 10, falado
