"""The one decimal context for this package's money math.

Prices, values and weights are computed under this context rather than the
ambient one, which is global and mutable: a backtest must produce the same
digits on every run and every machine. 28 significant digits, the decimal
default, with default traps.
"""

from __future__ import annotations

from decimal import Context

__all__ = ["DECIMAL_CONTEXT"]

DECIMAL_CONTEXT = Context(prec=28)
