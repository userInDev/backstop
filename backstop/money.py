"""Minor units in, decimal strings out, at the provider boundary only."""

from decimal import Decimal

# Currencies PayPal treats as zero-decimal. Everything else here is 2.
ZERO_DECIMAL = {"JPY", "KRW", "VND", "CLP", "ISK", "HUF", "TWD"}


def exponent(currency: str) -> int:
    return 0 if currency.upper() in ZERO_DECIMAL else 2


def to_provider(amount_minor: int, currency: str) -> str:
    e = exponent(currency)
    if e == 0:
        return str(int(amount_minor))
    return f"{Decimal(amount_minor) / (10 ** e):.{e}f}"


def from_provider(value: str, currency: str) -> int:
    e = exponent(currency)
    return int((Decimal(str(value)) * (10 ** e)).to_integral_value())
