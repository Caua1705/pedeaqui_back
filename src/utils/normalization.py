import re
import unicodedata

from src.core.constants import (
    DEFAULT_TRAFFIC_SOURCE,
    MAX_TRAFFIC_SOURCE_LENGTH,
)


_DIGITS_RE = re.compile(r"\D+")
# O e-mail, em duas metades legiveis.
#
# O regex antigo era `[^@\s]+@[^@\s]+\.[^@\s]+`: exigia so "um arroba e um
# ponto depois dele". Passavam "a@b..c", "a@-.-", ".@b.c" e "a@b.c." — e um
# e-mail invalido aceito no cadastro nao da erro nenhum na hora; ele vira o
# codigo de verificacao que nunca chega, e o cliente que nao consegue entrar
# sem saber por que.
#
# A parte local aceita ponto ENTRE pedacos, nunca na ponta nem dobrado. Os
# outros sinais sao os que o RFC 5322 permite sem aspas — `+` inclusive, que
# muita gente usa para etiquetar (`joana+ifood@`), e recusar seria recusar
# cliente legitimo.
_EMAIL_LOCAL = r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*"
# Cada rotulo do dominio comeca e termina em alfanumerico (o hifen so vive no
# meio, o que mata "-.-"), e o ultimo pedaco e alfabetico com 2+ letras — o
# que mata o ponto final de "a@b.c." e o ".." de "a@b..c".
_EMAIL_DOMAIN = r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?\.)+[A-Za-z]{2,}"
_EMAIL_RE = re.compile(rf"{_EMAIL_LOCAL}@{_EMAIL_DOMAIN}")

# Teto do RFC 5321. Nao e capricho: sem ele o campo aceita uma string de
# megabytes, que atravessa validacao, banco e o corpo do e-mail de
# verificacao.
MAX_EMAIL_LENGTH = 254
_NON_SLUG_RE = re.compile(r"[^a-z0-9]+")
_WHITESPACE_RE = re.compile(r"\s+")
# Como CPF se escreve: digitos, ponto, hifen e espaco. Nada mais.
_CPF_PUNCTUATION_RE = re.compile(r"[\d.\-\s]+")


def normalize_email(email: str | None) -> str:
    """E-mail pronto para comparar: sem espaco nas pontas, em minusculas.

    Aceita None e devolve "" — nao por gosto, mas porque a alternativa era
    pior. As duas funcoes deste par tinham a MESMA assinatura declarada
    (`value: str`) e respostas opostas para o mesmo None:
    `normalize_digits(None)` devolvia "" e `normalize_email(None)` levantava
    AttributeError. Quem escrevia um validador olhando para uma delas
    acertava ou errava conforme a que tivesse aberto primeiro.

    A uniformizacao foi para o lado tolerante porque alargar contrato nao
    quebra chamador nenhum, enquanto tornar `normalize_digits` estrito
    viraria 500 em qualquer caminho que hoje passa None e recebe "".

    Quem precisa distinguir "ausente" de "vazio" confere ANTES de chamar —
    e o que `CustomerAddressBase.normalize_zipcode` faz, para preservar o
    None em vez de virar "".
    """
    return (email or "").strip().lower()


def normalize_digits(value: str | None) -> str:
    """So os digitos. Aceita None e devolve "", como `normalize_email`."""
    return _DIGITS_RE.sub("", value or "")


def normalize_text(value: str) -> str:
    """Texto livre pronto para GRAVAR e para COMPARAR.

    O `strip()` e obvio. O NFC nao, e e ele que importa:

    "Filé" tem duas representacoes Unicode legitimas — composta (`é`, um
    code point) e decomposta (`e` + acento combinante, dois). Na tela do
    painel sao identicas. Para o Postgres sao bytes diferentes, e `=`,
    `LIKE` e `ILIKE` comparam bytes: um produto gravado decomposto **nao e
    encontrado** por quem digita a forma composta na busca. O lojista
    conclui que o produto nao existe e cadastra de novo.

    Conferido no banco de producao (PG 17.6):

        'Filé'(NFC) = 'Filé'(NFD)         -> false
        'Filé'(NFD) ILIKE '%Filé%'(NFC)   -> false

    Normalizar na ESCRITA e o que mantem a base inteira numa forma so;
    normalizar o termo de BUSCA e o que faz a consulta encontrar. Os dois
    lados precisam chamar isto — um sozinho nao resolve.

    NFC e nao NFD porque e a forma que a web usa por padrao (o HTML manda
    formularios em NFC) e a que ocupa menos bytes.
    """
    return unicodedata.normalize("NFC", value).strip()


def fold_for_match(value: str) -> str:
    """Texto achatado para COMPARAR — nunca para gravar.

    Minusculas, sem acento, espacos colapsados. Serve para procurar o nome de
    um produto dentro da resposta do modelo: ele escreve "**Torta de Limão**",
    o banco guarda "Torta de limao", e sem achatar os DOIS lados a busca por
    substring nao acha nada.

    Nao confundir com `normalize_text`, que e o que se GRAVA (NFC, acento
    preservado). Gravar o resultado disto apagaria os acentos do cardapio.
    """
    decomposed = unicodedata.normalize("NFKD", value)
    without_accents = "".join(
        character for character in decomposed if not unicodedata.combining(character)
    )
    return _WHITESPACE_RE.sub(" ", without_accents.lower()).strip()


def slugify(value: str) -> str:
    """Texto livre para slug de categoria ou produto.

    O painel recebe o nome digitado pelo lojista ("Pizza Calabresa 30cm") e
    `categories.slug` / `products.slug` viram parte da URL publica do
    cardapio, entao acento, espaco e pontuacao precisam sumir aqui — o
    cliente que compartilha o link nao pode receber um %C3%A7 no meio.

    Vazio quando a entrada nao tem nenhum caractere aproveitavel (um nome so
    de emoji, por exemplo); quem chama decide o que fazer nesse caso.

    Imune a NFC/NFD de graca, e vale saber por que: o `NFKD` decompoe as duas
    formas no mesmo resultado e o `ascii "ignore"` joga fora os acentos
    combinantes. "Filé" composto e decomposto dao `file` os dois. Se algum
    dia alguem trocar este NFKD por NFC, o slug passa a depender da forma da
    entrada e URLs do cardapio publico mudam sem ninguem pedir.
    """
    without_accents = unicodedata.normalize("NFKD", value)
    ascii_only = without_accents.encode("ascii", "ignore").decode("ascii")
    return _NON_SLUG_RE.sub("-", ascii_only.lower()).strip("-")


def normalize_traffic_source(value: str | None) -> str:
    """O rotulo de origem pronto para gravar e para AGRUPAR no relatorio.

    Existe porque o valor vem de um QR impresso, de um cartao de sacola ou de
    um link que alguem digitou — nao de uma lista fechada. Sem normalizar,
    `qr-mesa-4`, `QR mesa 04` e `qrmesa4` viram TRES linhas do relatorio para
    a mesma mesa, e o erro so aparece depois de os imas estarem impressos.

    Reusa `slugify` de proposito: a forma que se quer aqui e exatamente a de
    um slug (minusculo, sem acento, hifen no lugar de pontuacao), e escrever
    a segunda implementacao seria a segunda chance de errar a imunidade a
    NFC/NFD que ele ja tem de graca (armadilha 31).

    **Rotulo irreconhecivel nao e recusado: vira `direct`.** Recusar
    transformaria um QR impresso com defeito em pedido perdido, e quinhentos
    imas com o rotulo errado se consertam no relatorio — um pedido recusado,
    nao. Vale para ausente, vazio e para o que sobra vazio depois do slug
    (um rotulo so de emoji, por exemplo).

    O corte no teto acontece DEPOIS do slug e leva outro `strip("-")`: cortar
    "promocao-de-agosto..." no meio de um hifen deixaria o rotulo terminando
    em traco, e `promo-` e `promo` seriam duas linhas do relatorio de novo.
    """
    if not value:
        return DEFAULT_TRAFFIC_SOURCE

    slug = slugify(value)[:MAX_TRAFFIC_SOURCE_LENGTH].strip("-")
    return slug or DEFAULT_TRAFFIC_SOURCE


def is_valid_email(email: str) -> bool:
    normalized = normalize_email(email)
    if len(normalized) > MAX_EMAIL_LENGTH:
        return False
    return bool(_EMAIL_RE.fullmatch(normalized))


def _check_digit(digits: str) -> int:
    """O digito verificador de um trecho de CPF, pelo modulo 11.

    Os dois digitos do CPF saem da MESMA conta, mudando so o tamanho do
    trecho: o primeiro sai dos 9 primeiros digitos, o segundo dos 10
    primeiros (ja com o primeiro verificador dentro). Escrita duas vezes, a
    formula tinha dois lugares para errar um peso e nenhum aviso de que os
    dois deveriam concordar.

    O peso desce a partir de `len + 1`, e o resto 10 vira 0 — e a regra da
    Receita, nao um caso de borda nosso.
    """
    weight = len(digits) + 1
    total = sum(int(digit) * (weight - index) for index, digit in enumerate(digits))
    check = (total * 10) % 11
    return 0 if check == 10 else check


def is_valid_cpf(cpf: str) -> bool:
    """True so para um CPF de verdade, escrito como CPF.

    ORFA desde a frente 5: o CPF saiu do cadastro (revisao 0019) e nada em
    `src/` chama esta funcao hoje. Fica corrigida, e nao apagada, porque se
    o CPF voltar — a decisao amarrada a nota fiscal — ele volta com a
    validacao certa em vez desta.

    O defeito que ela tinha: `normalize_digits` joga fora TODO caractere que
    nao e digito antes da conta, entao "a5b2c9d9e8f2g2h4i7j2k5" virava
    "52998224725" e PASSAVA. Um campo de CPF aceitando texto arbitrario nao
    e teoria: e o que grava lixo na coluna e faz a conferencia manual
    depois nao bater com nada.

    Por isso a pontuacao aceita e explicita — ponto, hifen e espaco, que e
    como CPF se escreve — em vez de "qualquer coisa, eu tiro os digitos".
    """
    if not cpf or not _CPF_PUNCTUATION_RE.fullmatch(cpf):
        return False

    digits = normalize_digits(cpf)
    if len(digits) != 11:
        return False

    # 111.111.111-11 e os outros nove repetidos PASSAM na conta dos digitos
    # verificadores. Por isso sao barrados antes dela, e nao por ela.
    if len(set(digits)) == 1:
        return False

    if _check_digit(digits[:9]) != int(digits[9]):
        return False
    return _check_digit(digits[:10]) == int(digits[10])


# Categorias Unicode que NAO podem chegar a uma impressora termica: `Cc` sao
# os caracteres de controle (0x00-0x1F, 0x7F), `Cf` os de formatacao
# invisivel (zero-width, marcas de direcao) e `Zl`/`Zp` os separadores de
# linha/paragrafo exoticos, que nenhuma codepage de balcao conhece.
_CONTROL_CATEGORIES = ("Cc", "Cf", "Zl", "Zp")
# Tres ou mais quebras viram uma linha em branco. Quem cola texto de um
# editor traz sequencias de \n\n\n\n sem perceber, e cada uma vira
# centimetro de bobina.
_BLANK_LINES_RE = re.compile(r"\n{3,}")


def normalize_receipt_text(value: str) -> str:
    """Texto livre do lojista pronto para ir a uma impressora termica.

    Aplica-se so ao que o lojista escreve PARA a bobina — hoje a mensagem do
    rodape da via do cliente. Nome de produto e observacao do cliente seguem
    com `normalize_text`; a diferenca esta no unico ponto abaixo que nao e
    cosmetico.

    **Caractere de controle e comando de impressora, nao caractere.** O
    agente escreve `content` direto no fluxo ESC/POS
    (`print_agent/escpos.py`), e a codepage passa `0x1B` adiante intacto —
    entao um ESC colado no meio da mensagem (de um copiar-e-colar, ou de
    alguem tentando) deixa de ser texto e vira `ESC ...`, reprogramando a
    impressora no meio da comanda. `encode_text` nao defende contra isso: o
    trabalho dele e a queda de qualidade do que a TABELA nao tem (acento,
    emoji), e `0x1B` a tabela tem.

    O resto e o que faz o texto caber num layout de largura fixa: `\t`
    vira espaco (a tabulacao conta 1 caractere e imprime varios, o que
    estoura a coluna sem a conta perceber), `\r\n` vira `\n` (a quebra de
    linha da impressora e responsabilidade do agente, nao do lojista),
    espaco no fim de linha some e linha em branco repetida colapsa.

    NFC pelo mesmo motivo de `normalize_text`, com um agravante: aqui a
    forma decomposta nao so atrapalha a busca — ela nao EXISTE na CP850, e
    a mensagem sairia sem um acento (armadilha 28).
    """
    unified = value.replace("\r\n", "\n").replace("\r", "\n")

    kept: list[str] = []
    for character in unified:
        if character == "\n":
            kept.append("\n")
            continue
        if character == "\t":
            kept.append(" ")
            continue
        if unicodedata.category(character) in _CONTROL_CATEGORIES:
            continue
        kept.append(character)

    lines = [line.rstrip() for line in "".join(kept).split("\n")]
    collapsed = _BLANK_LINES_RE.sub("\n\n", "\n".join(lines))
    return unicodedata.normalize("NFC", collapsed).strip()
