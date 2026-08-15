from decimal import Decimal, ROUND_HALF_UP


ZERO = Decimal("0.00")


def to_decimal(value: Decimal | int | float | str | None) -> Decimal:
    if value is None:
        return ZERO
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def quantize_money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def money_to_float(value: Decimal | int | float | str | None) -> float:
    return float(quantize_money(to_decimal(value)))


def format_money_br(value: Decimal | int | float | str | None) -> str:
    """Dinheiro do jeito que tem que aparecer escrito: "R$ 23,90".

    Existe para o preco que vai ao MODELO ja sair pronto. O prompt manda
    copiar a string exatamente, e `ChatService._log_price_divergence` compara
    string com string — sem parse, sem float e sem arredondamento no meio do
    caminho, que sao as tres formas de o texto do Rapi acabar dizendo um
    numero que o cartao nao mostra.

    Sem separador de milhar de proposito: "R$ 1.234,50" introduz um ponto que
    o modelo pode reproduzir como separador decimal.
    """
    return f"R$ {quantize_money(to_decimal(value))}".replace(".", ",")
