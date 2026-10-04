"""The one decimal context for this package's money math.

Prices, values and weights are computed under this context rather than the
ambient one, which is global and mutable: a backtest must produce the same
digits on every run and every machine. Every field is spelled out, since a
``Context`` built with only ``prec`` copies the rest from the equally mutable
``decimal.DefaultContext``: 28 significant digits, round half even, and the
three default traps.
"""

from __future__ import annotations

import re
from decimal import ROUND_HALF_EVEN, Context, Decimal, DivisionByZero, InvalidOperation, Overflow
from typing import Final

__all__ = ["DECIMAL_CONTEXT", "parse_decimal"]

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
