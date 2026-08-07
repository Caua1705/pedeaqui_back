import re
import unicodedata


_DIGITS_RE = re.compile(r"\D+")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_NON_SLUG_RE = re.compile(r"[^a-z0-9]+")


def normalize_email(email: str) -> str:
    return email.strip().lower()


def normalize_digits(value: str) -> str:
    return _DIGITS_RE.sub("", value or "")


def slugify(value: str) -> str:
    """Texto livre para slug de categoria ou produto.

    O painel recebe o nome digitado pelo lojista ("Pizza Calabresa 30cm") e
    `categories.slug` / `products.slug` viram parte da URL publica do
    cardapio, entao acento, espaco e pontuacao precisam sumir aqui — o
    cliente que compartilha o link nao pode receber um %C3%A7 no meio.

    Vazio quando a entrada nao tem nenhum caractere aproveitavel (um nome so
    de emoji, por exemplo); quem chama decide o que fazer nesse caso.
    """
    without_accents = unicodedata.normalize("NFKD", value)
    ascii_only = without_accents.encode("ascii", "ignore").decode("ascii")
    return _NON_SLUG_RE.sub("-", ascii_only.lower()).strip("-")


def is_valid_email(email: str) -> bool:
    return bool(_EMAIL_RE.fullmatch(normalize_email(email)))


def is_valid_cpf(cpf: str) -> bool:
    digits = normalize_digits(cpf)
    if len(digits) != 11 or len(set(digits)) == 1:
        return False

    first_sum = sum(int(digits[index]) * (10 - index) for index in range(9))
    first_digit = (first_sum * 10) % 11
    if first_digit == 10:
        first_digit = 0
    if first_digit != int(digits[9]):
        return False

    second_sum = sum(int(digits[index]) * (11 - index) for index in range(10))
    second_digit = (second_sum * 10) % 11
    if second_digit == 10:
        second_digit = 0
    return second_digit == int(digits[10])
