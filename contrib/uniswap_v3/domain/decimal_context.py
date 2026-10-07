"""The one decimal context for this package's money math.

Prices, values and weights are computed under this context rather than the
ambient one, which is global and mutable: a backtest must produce the same
digits on every run and every machine. Every field is spelled out, since a
``Context`` built with only ``prec`` copies the rest from the equally mutable
``decimal.DefaultContext``: 28 significant digits, round half even, and the
three default traps.

Token amounts that are added or subtracted go through :data:`EXACT_CONTEXT`
instead: a balance of an 18-decimal token can hold more than 28 digits, and
a ledger must not round one away. That context is wide enough for any two
amounts a chain can carry and traps a result it would have had to round. A
cut to a token's decimal places (:func:`floor_to_places`) uses a context as
wide that does not trap, since dropping digits is the point of a cut.

The module also holds the estimators the rule strategy and the agent layer
share (:func:`mean`, :func:`log_returns`, :func:`sample_volatility`) and the
price and percentage text the command line and the agent layer print
(:func:`fixed_text`, :func:`price_text`).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
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
    "decimal_sum",
    "fixed_text",
    "floor_to_places",
    "log_returns",
    "mean",
    "parse_decimal",
    "plain",
    "price_text",
    "sample_volatility",
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


def decimal_sum(values: Iterable[Decimal]) -> Decimal:
    """The sum of ``values`` under :data:`DECIMAL_CONTEXT`, zero when there are none."""
    total = Decimal(0)
    for value in values:
        total = DECIMAL_CONTEXT.add(total, value)
    return total


def mean(values: Sequence[Decimal]) -> Decimal:
    """The arithmetic mean of ``values``, which are at least one."""
    if not values:
        raise ValueError("a mean is taken of at least one value")
    return DECIMAL_CONTEXT.divide(decimal_sum(values), Decimal(len(values)))


def log_returns(closes: Sequence[Decimal]) -> list[Decimal]:
    """The log return from each close to the next, one fewer than the closes."""
    return [
        DECIMAL_CONTEXT.ln(DECIMAL_CONTEXT.divide(later, earlier))
        for earlier, later in zip(closes, closes[1:], strict=False)
    ]


def sample_volatility(returns: Sequence[Decimal], annualiser: Decimal) -> Decimal:
    """The sample standard deviation of ``returns`` (at least two), times ``annualiser``.

    The one estimator the rule strategy sizes on and the judge is shown;
    the annualiser is the square root of the bars in a year.
    """
    if len(returns) < 2:
        raise ValueError(f"a sample deviation is taken of at least two returns, got {len(returns)}")
    centre = mean(returns)
    deviations = [DECIMAL_CONTEXT.subtract(value, centre) for value in returns]
    variance = DECIMAL_CONTEXT.divide(
        decimal_sum(DECIMAL_CONTEXT.multiply(each, each) for each in deviations),
        Decimal(len(returns) - 1),
    )
    return DECIMAL_CONTEXT.multiply(DECIMAL_CONTEXT.sqrt(variance), annualiser)


def fixed_text(value: Decimal, *, signed: bool = False) -> str:
    """``value`` to two decimal places, with its sign when ``signed``, and never a negative zero."""
    text = f"{value:+.2f}" if signed else f"{value:.2f}"
    if text.strip("+-0."):
        return text
    return ("+" if signed else "") + text.lstrip("+-")


def price_text(value: Decimal) -> str:
    """A price as a line prints it: two decimal places from 1 up, six significant digits below."""
    return f"{value:.2f}" if value >= 1 else f"{value:.6g}"


def floor_to_places(value: Decimal, places: int) -> Decimal:
    """A non-negative ``value`` cut, never rounded up, to ``places`` decimal places."""
    # The unit is built from its digits, so no context takes part in making it.
    return value.quantize(Decimal((0, (1,), -places)), rounding=ROUND_DOWN, context=_CUTTING)


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
