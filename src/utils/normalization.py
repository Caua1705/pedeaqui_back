import re
import unicodedata


_DIGITS_RE = re.compile(r"\D+")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_NON_SLUG_RE = re.compile(r"[^a-z0-9]+")


def normalize_email(email: str) -> str:
    return email.strip().lower()


def normalize_digits(value: str) -> str:
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


def is_valid_email(email: str) -> bool:
    return bool(_EMAIL_RE.fullmatch(normalize_email(email)))


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
