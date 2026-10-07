"""A rule strategy: hold a token while it trends, sized to a volatility target.

Every token but the quote is judged on its own, from the closes in the
view, suspect bars left out:

- It is *in trend* when the latest close is above the simple moving
  average of the last ``trend_window`` closes, the latest among them.
  Out of trend, its weight is zero.
- In trend, its weight is ``target_vol`` over its realised volatility,
  capped at ``max_weight``. Realised volatility is the sample standard
  deviation of the last ``vol_window`` log returns, annualised by the
  square root of ``bars_per_year``. A token whose returns in the window
  are all equal, so that no volatility is measured, gets ``max_weight``.
- When the weights of the tokens in trend add up to more than 1, they are
  scaled down to add up to 1. What is left goes to the quote token.

``target_vol`` is a risk budget per token, not for the portfolio: tokens
in trend together, each sized to it, leave the portfolio more volatile
than it when they move together, up to their number times it.

The windows count bars, not time, and the series is the view's unsuspect
bars joined across any gap: after a suspect or missing stretch the window
reaches further back, and the one return across the gap is larger than a
bar's. With fewer bars than the longer window needs, no trend can be
confirmed and no volatility measured: every token is out of trend and the
target is all quote, which sells any holding whose share is above
``band``. The target is answered only when some token's share has drifted
more than ``band`` from it (:func:`.rebalance.rebalance_or_hold`), so a
weight moving a little as volatility changes does not trade every bar,
and a trend that flips while a token weighs less than ``band`` is not
traded either.

Params, as a config writes them, every one of them required::

    trend_window: 50      # bars in the moving average, at least 2
    vol_window: 20        # log returns in the volatility, at least 2
    bars_per_year: 365    # for annualising, at least 1; 365 for daily bars
    target_vol: "0.40"    # annualised, as a fraction: 40%; above 0
    max_weight: "0.5"     # the most one token may weigh, in (0, 1]
    band: "0.05"          # in [0, 1)

The windows and ``bars_per_year`` all go with the bar length, from the
config's ``bars.interval_seconds`` or the ``--interval-seconds`` flag: a
run on hourly bars wants them given in hours, and 8760 to the year.

The decimals are quoted: a YAML float has already lost digits by the time
it is read, so one is refused rather than rounded, while a whole number
such as ``1`` is read as itself. Weights are cut to four decimal places.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from ..domain.decimal_context import (
    DECIMAL_CONTEXT,
    decimal_sum,
    log_returns,
    mean,
    parse_decimal,
    sample_volatility,
)
from ..domain.types import Bar, Hold, MarketView, Portfolio, TargetWeights
from ..domain.verdicts import require_count
from .rebalance import (
    floor_weight,
    rebalance_or_hold,
    require_band,
    require_decimal,
    require_params,
    weights_with_quote,
)

__all__ = ["TrendVolWeights"]

_PARAMS: Final = frozenset(
    {"trend_window", "vol_window", "bars_per_year", "target_vol", "max_weight", "band"}
)
_ZERO: Final = Decimal(0)
_ONE: Final = Decimal(1)


@dataclass(frozen=True)
class TrendVolWeights:
    """Weight each token in trend at ``target_vol`` over its volatility, the rest in the quote."""

    trend_window: int
    vol_window: int
    bars_per_year: int
    target_vol: Decimal
    max_weight: Decimal
    band: Decimal

    def __post_init__(self) -> None:
        require_count(self.trend_window, "trend_window", at_least=2)
        require_count(self.vol_window, "vol_window", at_least=2)
        require_count(self.bars_per_year, "bars_per_year", at_least=1)
        require_decimal(self.target_vol, "target_vol", lambda value: value > 0, "above 0")
        require_decimal(self.max_weight, "max_weight", lambda value: 0 < value <= 1, "in (0, 1]")
        require_band(self.band)

    @classmethod
    def from_params(cls, params: Mapping[str, object]) -> TrendVolWeights:
        """Build from a config's ``strategy.params``."""
        params = require_params("trend_vol_weights", params, _PARAMS)
        # The counts are handed on as written and checked in __post_init__.
        return cls(
            trend_window=params["trend_window"],  # type: ignore[arg-type]
            vol_window=params["vol_window"],  # type: ignore[arg-type]
            bars_per_year=params["bars_per_year"],  # type: ignore[arg-type]
            target_vol=parse_decimal(params["target_vol"], "target_vol"),
            max_weight=parse_decimal(params["max_weight"], "max_weight"),
            band=parse_decimal(params["band"], "band"),
        )

    @property
    def bars_needed(self) -> int:
        """How many unsuspect bars the view must hold before any token can be in trend."""
        return max(self.trend_window, self.vol_window + 1)

    def decide(self, view: MarketView, portfolio: Portfolio) -> TargetWeights | Hold:
        """The target when some token has drifted out of its band, else ``Hold``.

        The engine hands a portfolio holding the quote and exactly the
        tokens the bar prices, which are the tokens the target covers.
        """
        return rebalance_or_hold(portfolio, self.target_for(view, portfolio.quote), self.band)

    def target_for(self, view: MarketView, quote: str) -> TargetWeights:
        """The weights the view calls for, whatever the portfolio holds."""
        symbols = sorted(view.latest.prices)
        recent = _recent(view, self.bars_needed)
        if len(recent) < self.bars_needed:
            risky = dict.fromkeys(symbols, _ZERO)
        else:
            annualiser = DECIMAL_CONTEXT.sqrt(Decimal(self.bars_per_year))
            risky = {
                symbol: self._weight(_closes(recent, symbol), annualiser) for symbol in symbols
            }
        invested = decimal_sum(risky.values())
        if invested > _ONE:
            risky = {
                symbol: floor_weight(DECIMAL_CONTEXT.divide(weight, invested))
                for symbol, weight in risky.items()
            }
        return weights_with_quote(risky, quote)

    def _weight(self, closes: Sequence[Decimal], annualiser: Decimal) -> Decimal:
        """One token's weight from its closes, the latest last; zero out of trend."""
        if closes[-1] <= mean(closes[-self.trend_window :]):
            return _ZERO
        volatility = sample_volatility(log_returns(closes[-(self.vol_window + 1) :]), annualiser)
        sized = (
            self.max_weight
            if volatility == 0
            else DECIMAL_CONTEXT.divide(self.target_vol, volatility)
        )
        return floor_weight(min(sized, self.max_weight))


def _recent(view: MarketView, count: int) -> list[Bar]:
    """The last ``count`` bars of ``view`` that are not suspect, oldest first; fewer when it holds fewer."""
    recent: list[Bar] = []
    for bar in reversed(view.bars):
        if not bar.suspect:
            recent.append(bar)
            if len(recent) == count:
                break
    recent.reverse()
    return recent


def _closes(bars: Sequence[Bar], symbol: str) -> list[Decimal]:
    closes = []
    for bar in bars:
        if symbol not in bar.prices:
            raise ValueError(f"the bar at {bar.time} has no price for {symbol!r}")
        closes.append(bar.prices[symbol])
    return closes
