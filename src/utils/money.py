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
