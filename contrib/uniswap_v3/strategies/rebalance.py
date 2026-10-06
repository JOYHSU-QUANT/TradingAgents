"""What the strategies share: the params and decimal checks, the weight places, the band, and the trigger.

A strategy's weights are cut to four decimal places (:func:`floor_weight`),
so that what they leave to the quote token (:func:`weights_with_quote`) is
exact. A strategy with a ``band`` answers
its target only when some token's actual share of the portfolio sits more
than ``band`` (absolute weight) away from its target; otherwise the
portfolio is held. The trigger lives here so that every strategy with a
band means the same thing by it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from decimal import Decimal
from typing import Final

from ..domain.decimal_context import DECIMAL_CONTEXT, decimal_sum, floor_to_places
from ..domain.types import Hold, Portfolio, TargetWeights

__all__ = [
    "floor_weight",
    "rebalance_or_hold",
    "require_band",
    "require_decimal",
    "require_params",
    "weights_with_quote",
]

# A weight's decimal places: finer than any band or trade threshold worth setting.
_WEIGHT_PLACES: Final = 4
_ONE: Final = Decimal(1)


def require_params(name: str, params: object, expected: frozenset[str]) -> Mapping[str, object]:
    """``params`` when it is a mapping with exactly the ``expected`` keys; else raises."""
    if not isinstance(params, Mapping) or set(params) != expected:
        got = sorted(map(str, params)) if isinstance(params, Mapping) else params
        raise ValueError(f"{name} takes exactly the params {sorted(expected)}, got {got!r}")
    return params


def require_decimal(
    value: object, what: str, within: Callable[[Decimal], bool], bounds: str
) -> None:
    """Refuse a value that is not a finite ``Decimal`` ``within`` the ``bounds`` named."""
    if not isinstance(value, Decimal) or not value.is_finite() or not within(value):
        raise ValueError(f"{what} must be a Decimal {bounds}, got {value!r}")


def require_band(band: object) -> None:
    """Refuse a band that is not a finite ``Decimal`` in ``[0, 1)``."""
    require_decimal(band, "band", lambda value: not value.is_signed() and value < 1, "in [0, 1)")


def floor_weight(value: Decimal) -> Decimal:
    """``value`` cut down to a weight's decimal places."""
    return floor_to_places(value, _WEIGHT_PLACES)


def weights_with_quote(risky: Mapping[str, Decimal], quote: str) -> TargetWeights:
    """``risky``, weights already cut to their places, with what they leave to ``quote``.

    With so few places the remainder is exact, and it is not negative when
    the weights add up to no more than 1.
    """
    return TargetWeights(
        {**risky, quote: DECIMAL_CONTEXT.subtract(_ONE, decimal_sum(risky.values()))}
    )


def rebalance_or_hold(
    portfolio: Portfolio, target: TargetWeights, band: Decimal
) -> TargetWeights | Hold:
    """``target`` when some token's share of ``portfolio`` is more than ``band`` from it, else ``Hold``.

    A portfolio worth nothing has no shares to compare and is held.
    """
    total = portfolio.total_value
    if total == 0:
        return Hold()
    for symbol, weight in target.weights.items():
        share = DECIMAL_CONTEXT.divide(portfolio.value_of(symbol), total)
        if DECIMAL_CONTEXT.subtract(share, weight).copy_abs() > band:
            return target
    return Hold()
