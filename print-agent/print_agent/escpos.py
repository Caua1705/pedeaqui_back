"""Tradução do texto pronto para os bytes que a impressora entende.

Esta e a UNICA coisa que o agente sabe fazer que a API nao poderia ter
feito: selecionar fonte, codepage e corte sao estados da impressora, nao
caracteres — nao existe jeito de expressa-los dentro de um texto de 48
colunas.

Tudo o mais (largura, alinhamento, o que entra em cada via) ja veio pronto
da API. Aqui o texto nao e reformatado, so embrulhado.

Os comandos usados, todos do conjunto basico que qualquer termica de balcao
implementa:

    ESC @      1B 40        reinicia a impressora (limpa fonte e estilo que
                            uma via anterior possa ter deixado ligados)
    ESC t n    1B 74 n      seleciona a tabela de caracteres (codepage)
    GS ! n     1D 21 n      tamanho da fonte; nibble alto = largura,
                            nibble baixo = altura, 0 = normal, 1 = dobro
    ESC d n    1B 64 n      avanca n linhas (tira o texto de dentro da
                            guilhotina antes do corte)
    GS V 66 n  1D 56 42 n   corte parcial deixando n pontos de avanco
"""

import unicodedata


ESC = b"\x1b"
GS = b"\x1d"

INITIALIZE = ESC + b"@"
SELECT_CODEPAGE = ESC + b"t"
SELECT_SIZE = GS + b"!"
FEED_LINES = ESC + b"d"
PARTIAL_CUT = GS + b"VB"

# Valores de `GS ! n`. `large` e dobro de largura E de altura: dobrar so a
# altura deixa a letra alta e fina, que de longe se le pior que a normal.
SIZE_NORMAL = 0x00
SIZE_LARGE = 0x11

FONT_SIZES = {
    "normal": SIZE_NORMAL,
    "large": SIZE_LARGE,
}


def encode_text(text: str, encoding: str) -> bytes:
    """Texto na codepage da impressora, sem nunca falhar.

    Duas quedas de qualidade, nesta ordem, porque uma comanda com um
    caractere errado ainda serve e uma comanda que nao imprime nao:

    1. O que a codepage aceita vai como esta ("João").
    2. O que ela nao aceita perde o acento ("João" -> "Joao"). Sem esta
       etapa o `errors="replace"` entregaria "Jo?o", que e pior de ler e
       assusta mais quem esta no balcao.

    E so entao o que sobrar vira "?".
    """
    try:
        return text.encode(encoding)
    except UnicodeEncodeError:
        pass
    except LookupError:
        # Codepage que o Python nem conhece (erro de digitacao no
        # config.ini): melhor imprimir ASCII do que nao imprimir.
        return _strip_accents(text).encode("ascii", errors="replace")

    # O que sobrar depois de tirar o acento (emoji, simbolo de moeda
    # estrangeira) vira "?" — a via sai, com um caractere feio.
    return _strip_accents(text).encode(encoding, errors="replace")


def _strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def build_payload(
    content: str,
    font_size: str = "normal",
    codepage: int = 2,
    encoding: str = "cp850",
    cut: bool = True,
    feed_lines: int = 4,
) -> bytes:
    """Uma via inteira, dos bytes de inicializacao ao corte.

    `font_size` vem da API (`large` na via de producao, `normal` na do
    cliente) e o agente so traduz. E o unico campo da tarefa que ele
    interpreta: o `content` ele nao toca.

    Fonte desconhecida cai em normal em vez de estourar — uma via impressa
    com a fonte errada e um defeito visivel; uma via nao impressa e um
    pedido perdido.
    """
    size = FONT_SIZES.get(font_size, SIZE_NORMAL)

    payload = bytearray()
    payload += INITIALIZE
    payload += SELECT_CODEPAGE + bytes([codepage & 0xFF])
    payload += SELECT_SIZE + bytes([size])
    payload += encode_text(_normalize_newlines(content), encoding)
    # Volta ao normal antes de terminar: em impressora que ignora o ESC @ do
    # proximo trabalho, a via seguinte sairia gigante.
    payload += SELECT_SIZE + bytes([SIZE_NORMAL])
    payload += FEED_LINES + bytes([max(0, min(feed_lines, 255))])
    if cut:
        payload += PARTIAL_CUT + bytes([0])
    return bytes(payload)


def _normalize_newlines(content: str) -> str:
    """Fim de linha da impressora, e uma quebra no fim.

    A API monta o conteudo com `\\n` e sem quebra final. A termica precisa de
    `\\r\\n` em varios modelos, e sem a quebra final a ultima linha fica no
    buffer ate o proximo trabalho — a comanda sai faltando a ultima linha e
    o defeito parece intermitente.
    """
    unified = content.replace("\r\n", "\n").replace("\r", "\n")
    if not unified.endswith("\n"):
        unified += "\n"
    return unified.replace("\n", "\r\n")
