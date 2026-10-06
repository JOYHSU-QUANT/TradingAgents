"""A rule strategy: hold a token while it trends, sized to a volatility target.

Every token but the quote is judged on its own, from the closes in the
view, suspect bars left out:

- It is *in trend* when the latest close is above the simple moving
  average of the last ``trend_window`` closes, the latest among them.
  Out of trend, its weight is zero.
- In trend, its weight is ``target_vol`` over its realised volatility,
  capped at ``max_weight``. Realised volatility is the sample standard
  deviation of the last ``vol_window`` log returns, annualised by the
  square root of ``bars_per_year``. A token whose closes did not move at
  all gets ``max_weight``.
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
target is all quote, which sells whatever the portfolio holds. The target
is answered only when some token's share has drifted more than ``band``
from it (:func:`.rebalance.rebalance_or_hold`), so a weight moving a
little as volatility changes does not trade every bar, and a trend that
flips while a token weighs less than ``band`` is not traded either.

Params, as a config writes them, every one of them required::

    trend_window: 50      # bars in the moving average, at least 2
    vol_window: 20        # log returns in the volatility, at least 2
    bars_per_year: 365    # for annualising; 365 for daily bars
    target_vol: "0.40"    # annualised, as a fraction: 40%
    max_weight: "0.5"     # the most one token may weigh
    band: "0.05"

The windows and ``bars_per_year`` all go with the config's bar length: a
config with hourly bars wants them given in hours, and 8760 to the year.

Numbers are quoted: a YAML float has already lost digits by the time it is
read, so one is refused rather than rounded. Weights are cut to four
decimal places.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from ..domain.decimal_context import DECIMAL_CONTEXT, decimal_sum, floor_to_places, parse_decimal
from ..domain.types import Bar, Hold, MarketView, Portfolio, TargetWeights
from .rebalance import rebalance_or_hold, require_band, require_params

__all__ = ["TrendVolWeights"]

_PARAMS: Final = frozenset(
    {"trend_window", "vol_window", "bars_per_year", "target_vol", "max_weight", "band"}
)
# A weight's decimal places: finer than any band or trade threshold worth setting.
_WEIGHT_PLACES: Final = 4
_ZERO: Final = Decimal(0)
_ONE: Final = Decimal(1)


def _require_count(value: object, what: str, *, at_least: int) -> None:
    """Refuse a number of bars that is not an integer of at least ``at_least``."""
    if isinstance(value, bool) or not isinstance(value, int) or value < at_least:
        raise ValueError(f"{what} must be an integer of at least {at_least}, got {value!r}")


def _require_decimal(
    value: object, what: str, within: Callable[[Decimal], bool], bounds: str
) -> None:
    """Refuse a value that is not a finite ``Decimal`` ``within`` the ``bounds`` named."""
    if not isinstance(value, Decimal) or not value.is_finite() or not within(value):
        raise ValueError(f"{what} must be a Decimal {bounds}, got {value!r}")


def _mean(values: Sequence[Decimal]) -> Decimal:
    return DECIMAL_CONTEXT.divide(decimal_sum(values), Decimal(len(values)))


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
        _require_count(self.trend_window, "trend_window", at_least=2)
        _require_count(self.vol_window, "vol_window", at_least=2)
        _require_count(self.bars_per_year, "bars_per_year", at_least=1)
        _require_decimal(self.target_vol, "target_vol", lambda value: value > 0, "above 0")
        _require_decimal(self.max_weight, "max_weight", lambda value: 0 < value <= 1, "in (0, 1]")
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
        return rebalance_or_hold(portfolio, self.target(view, portfolio.quote), self.band)

    def target(self, view: MarketView, quote: str) -> TargetWeights:
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
                symbol: floor_to_places(DECIMAL_CONTEXT.divide(weight, invested), _WEIGHT_PLACES)
                for symbol, weight in risky.items()
            }
            invested = decimal_sum(risky.values())
        # The weights have four places, so the remainder is exact and never negative.
        return TargetWeights({**risky, quote: DECIMAL_CONTEXT.subtract(_ONE, invested)})

    def _weight(self, closes: Sequence[Decimal], annualiser: Decimal) -> Decimal:
        """One token's weight from its closes, the latest last; zero out of trend."""
        if closes[-1] <= _mean(closes[-self.trend_window :]):
            return _ZERO
        window = closes[-(self.vol_window + 1) :]
        returns = [
            DECIMAL_CONTEXT.ln(DECIMAL_CONTEXT.divide(later, earlier))
            for earlier, later in zip(window, window[1:], strict=False)
        ]
        mean = _mean(returns)
        deviations = [DECIMAL_CONTEXT.subtract(value, mean) for value in returns]
        variance = DECIMAL_CONTEXT.divide(
            decimal_sum(DECIMAL_CONTEXT.multiply(each, each) for each in deviations),
            Decimal(len(returns) - 1),
        )
        volatility = DECIMAL_CONTEXT.multiply(DECIMAL_CONTEXT.sqrt(variance), annualiser)
        sized = (
            self.max_weight
            if volatility == 0
            else DECIMAL_CONTEXT.divide(self.target_vol, volatility)
        )
        return floor_to_places(min(sized, self.max_weight), _WEIGHT_PLACES)


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
