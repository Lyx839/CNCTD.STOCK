"""Deutsche Zahlen- und Währungsformatierung. Geldbeträge werden intern in Cent (int) geführt."""

from decimal import ROUND_HALF_UP, Decimal


def _group(int_part: str) -> str:
    groups = []
    while len(int_part) > 3:
        groups.insert(0, int_part[-3:])
        int_part = int_part[:-3]
    groups.insert(0, int_part)
    return ".".join(groups)


def fmt_money(cents: int, currency: str = "€") -> str:
    sign = "-" if cents < 0 else ""
    cents = abs(int(cents))
    euros, rest = divmod(cents, 100)
    text = f"{sign}{_group(str(euros))},{rest:02d}"
    return f"{text} {currency}" if currency else text


def fmt_qty(qty: float) -> str:
    """Menge ohne überflüssige Nachkommastellen: 5 -> '5', 2.5 -> '2,5'."""
    d = Decimal(str(qty)).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP).normalize()
    sign = "-" if d < 0 else ""
    d = abs(d)
    text = f"{d:f}"
    if "." in text:
        int_part, frac = text.split(".")
        return f"{sign}{_group(int_part)},{frac}"
    return f"{sign}{_group(text)}"


def line_total_cents(qty: float, unit_cents: int) -> int:
    value = Decimal(str(qty)) * Decimal(int(unit_cents))
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def percent_of(cents: int, rate: float) -> int:
    value = Decimal(int(cents)) * Decimal(str(rate)) / Decimal(100)
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def euros_to_cents(value: float) -> int:
    return int((Decimal(str(value)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def cents_to_euros(cents: int) -> float:
    return int(cents) / 100.0
