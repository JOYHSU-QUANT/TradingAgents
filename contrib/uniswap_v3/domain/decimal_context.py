"""The one decimal context for this package's money math.

Prices, values and weights are computed under this context rather than the
ambient one, which is global and mutable: a backtest must produce the same
digits on every run and every machine. Every field is spelled out, since a
``Context`` built with only ``prec`` copies the rest from the equally mutable
``decimal.DefaultContext``: 28 significant digits, round half even, and the
three default traps.
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Context, DivisionByZero, InvalidOperation, Overflow
from typing import Final

__all__ = ["DECIMAL_CONTEXT"]

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
