"""O preco escrito COMO SE FALA, para o atendente de voz copiar em vez de traduzir.

===========================================================================
POR QUE ISTO EXISTE (25/08/2026)
===========================================================================

Numa sessao da bancada, `R$ 34,40` saiu como **"quarenta e quatro e
quarenta"**. Nao foi arredondamento e nao foi leitura errada dos digitos:

    fonte:   R$ 34,40
    falado:  "quarenta e quatro e quarenta"
                                    ^ este esta certo — os centavos SAO 40

A palavra "quarenta" aparece duas vezes, e a segunda e a correta. O slot dos
reais foi contaminado pela palavra que vinha depois. Os outros precos da mesma
sessao sairam certos ("cinquenta e sete e dezesseis", "vinte e quatro e
sessenta e seis") — e em nenhum deles a palavra dos reais colide com a dos
centavos.

E hipotese com apoio, nao prova: ha uma ocorrencia. **Mas o conserto nao
depende dela estar certa.** Ate 25/08/2026 o modelo fazia uma TRADUCAO de
cabeca — de `34,40` para uma sequencia de palavras — e traducao e geracao, e
geracao erra. Entregando a forma falada pronta no resultado da ferramenta, ele
passa a fazer COPIA, que e a operacao mais confiavel que um modelo tem. Seja o
defeito eco fonetico, seja digito lido errado, seja outra coisa: o passo que
falhou deixa de existir.

O ganho de tabela e igual de importante: a forma falada passa a ser decidida
AQUI, em Python, testada e deterministica. Antes, "R$ 34,40" podia virar
"trinta e quatro reais e quarenta centavos" — a forma longa que o prompt
proibe — e a unica defesa era uma regra de texto.

ISTO NAO E A ARMADILHA 44, e a distincao e a que `voice_prompt.py` fixou na
mesma semana: o perigo nao e string dizivel, e string dizivel **solta no
prefixo**, que persiste a sessao inteira e nao pertence a nada. Esta e
calculada do campo `price` daquele produto, vive um turno, e vai colada na
fonte (`R$ 34,40 (fala: ...)`). E o par fonte -> fala virando DADO em vez de
instrucao, que e a terceira linha daquela regra.

===========================================================================
AS DECISOES DE FORMATO, E POR QUE CADA UMA
===========================================================================

    R$ 34,40   -> "trinta e quatro e quarenta"      a forma curta do balcao
    R$ 34,00   -> "trinta e quatro"                 sem "e zero"
    R$ 0,01    -> "um centavo"                      existe no cardapio de teste
    R$ 34,05   -> "trinta e quatro e cinco centavos"

O ultimo e o unico que quebra o tom, e e de proposito. Sem "centavos", "trinta
e quatro e cinco" vira R$ 34,50 no ouvido de quem escuta — a mesma familia de
defeito que este arquivo existe para consertar. **Clareza vence consistencia
quando o numero e dinheiro.**

E fora da faixa (nulo, zero, negativo, ou >= R$ 10.000) a funcao devolve
`None`, e quem chama OMITE a dica em vez de arriscar. Preco que este modulo
nao sabe dizer com certeza e preco que o modelo nao recebe pronto — ele cai no
comportamento antigo, que e pior, mas conhecido. Inventar uma forma falada
para um numero de borda seria criar a segunda geracao do defeito que a
primeira existe para matar.
"""

from decimal import Decimal

from src.utils.money import quantize_money, to_decimal


# Escrito a mao, e nao gerado: sao vinte e oito palavras que nunca mudam, e uma
# lista literal e conferivel de olho. `0` fica de fora porque nao existe
# "zero reais" falado — o caso de reais zerados vira "N centavos" mais abaixo.
_ATE_VINTE = (
    None,
    "um",
    "dois",
    "tres",
    "quatro",
    "cinco",
    "seis",
    "sete",
    "oito",
    "nove",
    "dez",
    "onze",
    "doze",
    "treze",
    "quatorze",
    "quinze",
    "dezesseis",
    "dezessete",
    "dezoito",
    "dezenove",
)
_DEZENAS = (
    None,
    None,
    "vinte",
    "trinta",
    "quarenta",
    "cinquenta",
    "sessenta",
    "setenta",
    "oitenta",
    "noventa",
)
# "cento" e nao "cem" quando ha resto: 100 e "cem", 101 e "cento e um".
_CENTENAS = (
    None,
    "cento",
    "duzentos",
    "trezentos",
    "quatrocentos",
    "quinhentos",
    "seiscentos",
    "setecentos",
    "oitocentos",
    "novecentos",
)

# R$ 10.000 nao e preco de prato. O teto existe para a funcao nao ter que
# saber dizer "milhao" — e, mais importante, para o caso absurdo cair no
# `None` em vez de sair uma frase esquisita em audio.
TETO_EM_REAIS = 10_000


def _ate_999(numero: int) -> str:
    """Um inteiro de 1 a 999 por extenso. Fora disso e erro de programacao."""
    if not 1 <= numero <= 999:
        raise ValueError(f"fora da faixa desta funcao: {numero}")

    if numero == 100:
        return "cem"

    centena, resto = divmod(numero, 100)
    partes = []
    if centena:
        partes.append(_CENTENAS[centena])
    if resto:
        if resto < 20:
            partes.append(_ATE_VINTE[resto])
        else:
            dezena, unidade = divmod(resto, 10)
            partes.append(_DEZENAS[dezena] if not unidade else f"{_DEZENAS[dezena]} e {_ATE_VINTE[unidade]}")
    return " e ".join(partes)


def _inteiro_por_extenso(numero: int) -> str:
    """De 1 a 9.999. Acima disso quem chama ja devolveu `None`."""
    if numero < 1000:
        return _ate_999(numero)

    milhares, resto = divmod(numero, 1000)
    inicio = "mil" if milhares == 1 else f"{_ate_999(milhares)} mil"
    if not resto:
        return inicio
    # "mil e duzentos", mas "mil duzentos e trinta": o "e" so entra quando o
    # resto e redondo em centena ou menor que cem. E como se fala.
    ligacao = " e " if resto < 100 or resto % 100 == 0 else " "
    return f"{inicio}{ligacao}{_ate_999(resto)}"


def preco_por_extenso(valor: Decimal | int | float | str | None) -> str | None:
    """`Decimal("34.40")` -> `"trinta e quatro e quarenta"`. `None` quando nao da.

    `None` NAO e erro: e a ordem para quem chama omitir a dica falada e deixar
    o preco so em digitos. Ver o cabecalho deste arquivo para por que a borda
    prefere silencio a palpite.
    """
    if valor is None:
        return None

    centavos_totais = int(quantize_money(to_decimal(valor)) * 100)
    if centavos_totais <= 0 or centavos_totais >= TETO_EM_REAIS * 100:
        return None

    reais, centavos = divmod(centavos_totais, 100)

    if not reais:
        # R$ 0,01 -> "um centavo". Sem "zero reais" na frente, que ninguem
        # fala e que soaria como se o produto fosse de graca.
        return "um centavo" if centavos == 1 else f"{_ate_999(centavos)} centavos"

    inteiro = _inteiro_por_extenso(reais)
    if not centavos:
        return inteiro
    if centavos < 10:
        # A EXCECAO DE TOM, e a unica. Sem "centavos", "trinta e quatro e
        # cinco" vira R$ 34,50 no ouvido. Ver o cabecalho.
        return f"{inteiro} e {_ate_999(centavos)} centavos"
    return f"{inteiro} e {_ate_999(centavos)}"
