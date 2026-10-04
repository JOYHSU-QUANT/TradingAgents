"""The one decimal context for this package's money math.

Prices, values and weights are computed under this context rather than the
ambient one, which is global and mutable: a backtest must produce the same
digits on every run and every machine. Every field is spelled out, since a
``Context`` built with only ``prec`` copies the rest from the equally mutable
``decimal.DefaultContext``: 28 significant digits, round half even, and the
three default traps.

Token amounts that are added, subtracted or cut to a token's decimal places
go through :data:`EXACT_CONTEXT` instead: a balance of an 18-decimal token
can hold more than 28 digits, and a ledger must not round one away. That
context is wide enough for any two amounts a chain can carry and traps a
result it would have had to round.
"""

from __future__ import annotations

import re
from decimal import (
    ROUND_DOWN,
    ROUND_HALF_EVEN,
    Context,
    Decimal,
    DivisionByZero,
    Inexact,
    InvalidOperation,
    Overflow,
)
from typing import Final

__all__ = [
    "DECIMAL_CONTEXT",
    "EXACT_CONTEXT",
    "MAX_MAGNITUDE",
    "floor_to_places",
    "parse_decimal",
    "plain",
]

DECIMAL_CONTEXT: Final = Context(
    prec=28,
    rounding=ROUND_HALF_EVEN,
    Emin=-999999,
    Emax=999999,
    capitals=1,
    clamp=0,
    flags=[],
    traps=[InvalidOperation, DivisionByZero, Overflow],
)

# How many digits from the decimal point an amount's leading digit may sit. A
# uint256 has 78 digits, so no token amount, price or weight is outside it,
# and arithmetic on two checked values stays inside the contexts' exponents.
MAX_MAGNITUDE: Final = 77

# Wide enough for a sum or difference of two such amounts: at most 156 digits.
EXACT_CONTEXT: Final = Context(
    prec=200,
    rounding=ROUND_HALF_EVEN,
    Emin=-999999,
    Emax=999999,
    capitals=1,
    clamp=0,
    flags=[],
    traps=[InvalidOperation, DivisionByZero, Overflow, Inexact],
)
# As wide, without the trap: cutting digits off is the point of a cut.
_CUTTING: Final = EXACT_CONTEXT.copy()
_CUTTING.traps[Inexact] = False


def floor_to_places(value: Decimal, places: int) -> Decimal:
    """A non-negative ``value`` cut, never rounded up, to ``places`` decimal places."""
    return value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_DOWN, context=_CUTTING)


def plain(value: Decimal) -> str:
    """``value`` as a message prints it: no exponent, and no zeros after its last digit."""
    return format(value.normalize(_CUTTING), "f")


# Plain digits only. ``Decimal`` itself also reads "0_5", " 0.5", "NaN" and
# full-width digits, none of which is what a config author meant to write.
_DECIMAL: Final = re.compile(r"-?[0-9]+(\.[0-9]+)?")


def parse_decimal(value: object, what: str) -> Decimal:
    """A number as a config writes it: a quoted decimal, an integer or a ``Decimal``.

    A float is refused rather than rounded: it has already lost digits by
    the time it is read.
    """
    if isinstance(value, Decimal):
        return value
    if (
        isinstance(value, bool)
        or not isinstance(value, int | str)
        or (isinstance(value, str) and not _DECIMAL.fullmatch(value))
    ):
        raise ValueError(f'{what} must be a quoted decimal such as "0.25", got {value!r}')
    return Decimal(value)
