"""Fixed-point money handling.

All amounts are stored as integers in micro-PB ("units"): 1 PB = 1_000_000 units.
Floats are never used for money.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from polybit.errors import AmountError

SYMBOL = "PB"
SCALE = 6
UNIT = 10**SCALE
# Largest amount accepted from user input: 1 billion PB.
MAX_AMOUNT = 1_000_000_000 * UNIT


def pb(value: int | str | Decimal) -> int:
    """Convert a whole/decimal PB value to units (for config and tests)."""
    return int(Decimal(str(value)) * UNIT)


def parse_amount(text: str) -> int:
    """Parse user input such as '12', '0.5', '1,000' or '25 PB' into units.

    Raises AmountError for anything that is not a positive amount with at most
    SCALE decimal places and at most MAX_AMOUNT.
    """
    raw = text.strip().replace(",", "").replace("_", "")
    if raw.lower().endswith(SYMBOL.lower()):
        raw = raw[: -len(SYMBOL)].strip()
    try:
        value = Decimal(raw)
    except InvalidOperation:
        raise AmountError(f"`{text}` is not a valid amount.") from None
    if not value.is_finite():
        raise AmountError(f"`{text}` is not a valid amount.")
    if value <= 0:
        raise AmountError("Amount must be greater than zero.")
    if value > Decimal(MAX_AMOUNT) / UNIT:
        raise AmountError(f"Amount must not exceed {format_amount(MAX_AMOUNT)}.")
    exponent = value.normalize().as_tuple().exponent
    if isinstance(exponent, int) and exponent < -SCALE:
        raise AmountError(f"Amounts support at most {SCALE} decimal places.")
    return int(value * UNIT)


def format_amount(units: int, *, symbol: bool = True) -> str:
    """Render units as a human-readable string, e.g. 1500000 -> '1.5 PB'."""
    sign = "-" if units < 0 else ""
    whole, frac = divmod(abs(units), UNIT)
    text = f"{whole:,}"
    if frac:
        text += "." + f"{frac:0{SCALE}d}".rstrip("0")
    return f"{sign}{text} {SYMBOL}" if symbol else f"{sign}{text}"
