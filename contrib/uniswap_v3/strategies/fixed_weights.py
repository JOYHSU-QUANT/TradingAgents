"""A placeholder strategy: hold fixed weights, rebalance when one drifts out of its band.

It exists to drive the engine and its tests; it reads no prices and makes
no forecast. ``weights`` is the target share of portfolio value per token
and ``band`` is how far, in absolute weight, a token's actual share may sit
from its target before the strategy asks for the target again: with a band
of ``0.05``, a 30% target is left alone between 25% and 35% inclusive.

Params, as a config writes them::

    weights: {USDC: "0.5", WETH: "0.3", WBTC: "0.2"}
    band: "0.05"

Numbers are quoted: a YAML float has already lost digits by the time it is
read, so one is refused rather than rounded.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Final

from ..domain.decimal_context import DECIMAL_CONTEXT
from ..domain.types import Hold, MarketView, Portfolio, TargetWeights

__all__ = ["FixedWeights"]

_PARAMS: Final = frozenset({"weights", "band"})


def _decimal(value: object, what: str) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool) or not isinstance(value, int | str):
        raise ValueError(f'{what} must be a quoted decimal such as "0.25", got {value!r}')
    try:
        return Decimal(value)
    except InvalidOperation:
        raise ValueError(f'{what} must be a quoted decimal such as "0.25", got {value!r}') from None


@dataclass(frozen=True)
class FixedWeights:
    """Rebalance to ``target`` whenever any token's share is more than ``band`` away from it."""

    target: TargetWeights
    band: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.target, TargetWeights):
            raise ValueError(f"target must be TargetWeights, got {self.target!r}")
        if (
            not isinstance(self.band, Decimal)
            or not self.band.is_finite()
            or not 0 <= self.band < 1
        ):
            raise ValueError(f"band must be a Decimal in [0, 1), got {self.band!r}")

    @classmethod
    def from_params(cls, params: Mapping[str, object]) -> FixedWeights:
        """Build from a config's ``strategy.params``."""
        if set(params) != _PARAMS:
            raise ValueError(
                f"fixed_weights takes exactly the params {sorted(_PARAMS)}, got {sorted(params)}"
            )
        weights = params["weights"]
        if not isinstance(weights, Mapping):
            raise ValueError(f"weights must map token symbol to weight, got {weights!r}")
        return cls(
            target=TargetWeights(
                {symbol: _decimal(weight, f"weights[{symbol!r}]") for symbol, weight in weights.items()}
            ),
            band=_decimal(params["band"], "band"),
        )

    def decide(self, view: MarketView, portfolio: Portfolio) -> TargetWeights | Hold:
        """The target when some token has drifted out of its band, else ``Hold``.

        A portfolio that does not hold exactly the target's tokens is a
        wiring mistake and raises. One worth nothing has no shares to
        compare and is held.
        """
        targets = self.target.weights
        if set(portfolio.balances) != set(targets):
            raise ValueError(
                f"fixed_weights targets {sorted(targets)} but the portfolio holds "
                f"{sorted(portfolio.balances)}"
            )
        total = portfolio.total_value
        if total == 0:
            return Hold()
        for symbol, target in targets.items():
            share = DECIMAL_CONTEXT.divide(portfolio.value_of(symbol), total)
            if DECIMAL_CONTEXT.subtract(share, target).copy_abs() > self.band:
                return self.target
        return Hold()
