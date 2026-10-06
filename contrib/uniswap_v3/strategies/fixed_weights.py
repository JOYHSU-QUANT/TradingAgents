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
from decimal import Decimal
from typing import Final

from ..domain.decimal_context import parse_decimal
from ..domain.types import Hold, MarketView, Portfolio, TargetWeights
from .rebalance import rebalance_or_hold, require_band, require_params

__all__ = ["FixedWeights"]

_PARAMS: Final = frozenset({"weights", "band"})


@dataclass(frozen=True)
class FixedWeights:
    """Rebalance to ``target`` whenever any token's share is more than ``band`` away from it."""

    target: TargetWeights
    band: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.target, TargetWeights):
            raise ValueError(f"target must be TargetWeights, got {self.target!r}")
        require_band(self.band)

    @classmethod
    def from_params(cls, params: Mapping[str, object]) -> FixedWeights:
        """Build from a config's ``strategy.params``."""
        params = require_params("fixed_weights", params, _PARAMS)
        weights = params["weights"]
        if not isinstance(weights, Mapping):
            raise ValueError(f"weights must map token symbol to weight, got {weights!r}")
        return cls(
            target=TargetWeights(
                {
                    symbol: parse_decimal(weight, f"weights[{symbol!r}]")
                    for symbol, weight in weights.items()
                }
            ),
            band=parse_decimal(params["band"], "band"),
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
        return rebalance_or_hold(portfolio, self.target, self.band)
