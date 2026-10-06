"""What the strategies share: the exact-params check, the band, and the rebalance trigger.

A strategy with a ``band`` answers its target only when some token's
actual share of the portfolio sits more than ``band`` (absolute weight)
away from its target; otherwise the portfolio is held. The trigger lives
here so that every strategy with a band means the same thing by it.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal

from ..domain.decimal_context import DECIMAL_CONTEXT
from ..domain.types import Hold, Portfolio, TargetWeights

__all__ = ["rebalance_or_hold", "require_band", "require_params"]


def require_params(name: str, params: object, expected: frozenset[str]) -> Mapping[str, object]:
    """``params`` when it is a mapping with exactly the ``expected`` keys; else raises."""
    if not isinstance(params, Mapping) or set(params) != expected:
        got = sorted(map(str, params)) if isinstance(params, Mapping) else params
        raise ValueError(f"{name} takes exactly the params {sorted(expected)}, got {got!r}")
    return params


def require_band(band: object) -> None:
    """Refuse a band that is not a finite ``Decimal`` in ``[0, 1)``."""
    if not isinstance(band, Decimal) or not band.is_finite() or band.is_signed() or band >= 1:
        raise ValueError(f"band must be a Decimal in [0, 1), got {band!r}")


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
