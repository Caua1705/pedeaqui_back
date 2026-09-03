"""Quanto custou uma chamada ao modelo, em dolar.

A pergunta que isto existe para responder e "a comissao paga a conta do
assistente?". Ela nao se responde com contagem de token: audio de entrada
custa 40x o texto de entrada no mesmo modelo, e um turno de texto com muito
cache custa um decimo de um sem cache. So o valor em dinheiro compara.

## Os precos sao uma COPIA, e ela envelhece

A tabela abaixo foi conferida em **02/09/2026** contra
`https://developers.openai.com/api/docs/pricing`. Ela e nossa, o numero e
deles, e eles mudam sem avisar. Duas consequencias de desenho:

- **modelo que nao esta na tabela custa `None`, nunca zero.** Zero seria um
  numero que soma, e um restaurante inteiro apareceria de graca num relatorio
  que existe para dizer quanto ele custa. `None` propaga ate a coluna
  `cost_usd`, que e nullable, e a rota de leitura conta separado quantas
  chamadas ficaram sem preco. E o mesmo criterio da armadilha 49: numero
  desatualizado degrada a MENSAGEM, nao a correcao;
- **os tokens continuam gravados mesmo sem preco.** Quando a tabela for
  atualizada, da para reprocessar — o dado bruto nao se perdeu.

## Por que duas tabelas e duas funcoes, e nao uma generica

Texto tem tres precos (entrada, entrada em cache, saida). Voz tem seis, porque
audio e texto sao cobrados em faixas diferentes nas duas direcoes. Uma
estrutura unica com seis campos deixaria tres deles sempre nulos no texto, e
toda leitura passaria a comecar com "este campo vale aqui?". Duas funcoes que
se leem inteiras valem mais.

## `cached` e SUBCONJUNTO da entrada

Vale nos dois lados, e ja esta escrito no model de `ai_voice_sessions` e no
`usage_metadata` do langchain: `input_tokens` ja inclui o que veio do cache, e
`cached` diz que fatia foi essa. Somar os dois conta o cache duas vezes. Por
isso as duas funcoes SUBTRAEM antes de multiplicar.
"""

from dataclasses import dataclass
from decimal import Decimal


#: Os precos sao publicados por milhao de tokens; as contas sao por token.
POR_MILHAO = Decimal("1000000")

#: Casas do valor gravado. Um turno de `/chat` custa da ordem de US$ 0,0005 —
#: com duas casas, todo turno seria zero e a soma do mes tambem.
CASAS_DO_CUSTO = Decimal("0.000001")


@dataclass(frozen=True)
class PrecoDeTexto:
    """USD por milhao de tokens, para um modelo que so fala texto."""

    entrada: Decimal
    entrada_em_cache: Decimal
    saida: Decimal


@dataclass(frozen=True)
class PrecoDeVoz:
    """USD por milhao de tokens, para um modelo da Realtime.

    Seis faixas porque audio e texto sao cobrados separado nas duas direcoes.
    A diferenca nao e detalhe: no `gpt-realtime-mini` um token de audio de
    saida custa mais de oito vezes um de texto de saida.
    """

    entrada_texto: Decimal
    entrada_texto_em_cache: Decimal
    entrada_audio: Decimal
    entrada_audio_em_cache: Decimal
    saida_texto: Decimal
    saida_audio: Decimal


# Conferido em 02/09/2026 em developers.openai.com/api/docs/pricing.
#
# Os tres estao aqui, e nao so o `MODEL_NAME` de hoje, porque essa variavel
# sai do ambiente justamente para mudar sem deploy (ver o docstring de
# `ChatLLMService`). Um modelo trocado no `.env` numa terca-feira nao pode
# apagar o custo daquela semana.
PRECOS_DE_TEXTO: dict[str, PrecoDeTexto] = {
    "gpt-5": PrecoDeTexto(
        entrada=Decimal("1.25"),
        entrada_em_cache=Decimal("0.125"),
        saida=Decimal("10.00"),
    ),
    "gpt-5-mini": PrecoDeTexto(
        entrada=Decimal("0.25"),
        entrada_em_cache=Decimal("0.025"),
        saida=Decimal("2.00"),
    ),
    "gpt-5-nano": PrecoDeTexto(
        entrada=Decimal("0.05"),
        entrada_em_cache=Decimal("0.005"),
        saida=Decimal("0.40"),
    ),
}

PRECOS_DE_VOZ: dict[str, PrecoDeVoz] = {
    "gpt-realtime": PrecoDeVoz(
        entrada_texto=Decimal("4.00"),
        entrada_texto_em_cache=Decimal("0.40"),
        entrada_audio=Decimal("32.00"),
        entrada_audio_em_cache=Decimal("0.40"),
        saida_texto=Decimal("16.00"),
        saida_audio=Decimal("64.00"),
    ),
    "gpt-realtime-mini": PrecoDeVoz(
        entrada_texto=Decimal("0.60"),
        entrada_texto_em_cache=Decimal("0.06"),
        entrada_audio=Decimal("10.00"),
        entrada_audio_em_cache=Decimal("0.30"),
        saida_texto=Decimal("2.40"),
        saida_audio=Decimal("20.00"),
    ),
}


def custo_de_texto(
    modelo: str,
    entrada: int,
    entrada_em_cache: int,
    saida: int,
) -> Decimal | None:
    """O custo de UM turno do `/chat`, ou None se o modelo nao tem preco aqui.

    `entrada` e o total que a OpenAI cobrou de entrada, com o cache dentro;
    `entrada_em_cache` e a fatia dele que saiu mais barata.
    """
    preco = PRECOS_DE_TEXTO.get(modelo)
    if preco is None:
        return None

    em_cache = min(max(entrada_em_cache, 0), max(entrada, 0))
    cheia = max(entrada, 0) - em_cache

    total = (
        cheia * preco.entrada
        + em_cache * preco.entrada_em_cache
        + max(saida, 0) * preco.saida
    ) / POR_MILHAO
    return total.quantize(CASAS_DO_CUSTO)


def custo_de_voz(
    modelo: str,
    entrada_audio: int,
    entrada_texto: int,
    entrada_em_cache: int,
    saida_audio: int,
    saida_texto: int,
) -> Decimal | None:
    """O custo de UMA sessao de voz, ou None se o modelo nao tem preco aqui.

    O `cached_tokens` da Realtime nao diz se o que veio do cache era audio ou
    texto: e um numero so. Aqui ele e descontado do AUDIO primeiro, e o que
    sobrar do texto. Numa conversa falada o audio e a quase totalidade da
    entrada, entao essa e a leitura que erra menos — e ela erra para o lado
    barato, o que a torna um PISO do custo e nao um teto. O jeito de nao
    precisar dessa escolha seria o navegador reportar o cache separado por
    faixa, que e mudanca de contrato com o front.
    """
    preco = PRECOS_DE_VOZ.get(modelo)
    if preco is None:
        return None

    audio = max(entrada_audio, 0)
    texto = max(entrada_texto, 0)
    cache = max(entrada_em_cache, 0)

    audio_em_cache = min(cache, audio)
    texto_em_cache = min(cache - audio_em_cache, texto)

    total = (
        (audio - audio_em_cache) * preco.entrada_audio
        + audio_em_cache * preco.entrada_audio_em_cache
        + (texto - texto_em_cache) * preco.entrada_texto
        + texto_em_cache * preco.entrada_texto_em_cache
        + max(saida_audio, 0) * preco.saida_audio
        + max(saida_texto, 0) * preco.saida_texto
    ) / POR_MILHAO
    return total.quantize(CASAS_DO_CUSTO)
