"""The AI-gated strategy: the rule strategy's weights, cut by what an outside judge said.

:class:`AiGatedWeights` wraps :class:`~.trend_vol_weights.TrendVolWeights`,
its ``rule``, and gates the rule's target with the verdicts the view carries
at the bar being decided (:attr:`~..domain.types.MarketView.latest_verdicts`):

- A token the rule holds, with a verdict on it, is held at the rule's weight
  times the multiplier of the verdict's rating: with the defaults, all of it
  on ``Buy``, three quarters on ``Overweight``, half on ``Hold``, a quarter
  on ``Underweight`` and none on ``Sell``.
- A token with no verdict at the bar, or a ``REVIEW`` one (the judge was
  asked and gave nothing usable), takes on no new risk and keeps the rule's
  exits: its target is its present share of the portfolio, capped at the
  rule's weight. Below the weight it is left where it is; above it, it is
  sold down to the weight.
- A token the rule has out of trend is at zero whatever was said: the judge
  cannot put the portfolio into a token against its trend.

So every token's target is at most the rule's, always: the rule is the
guardrail, and the judge decides how much of what the rule allows is held,
never more. The targets are cut to four decimal places, as the rule's are,
and whatever they leave goes to the quote token. A share left where it is
is cut too, so it sits up to 0.0001 below the holding: the sliver is sold
only when another token takes the portfolio out of its band, and then only
when it is worth the execution's ``min_trade_value``, which on a portfolio
under some 100,000 of the quote it never is. The band is the rule's, and
the target is answered only when some share has drifted more than it
(:func:`.rebalance.rebalance_or_hold`).

The verdicts are those of the source the run's config names
(``verdicts.source``). A config that names none hands the strategy a view
without one, and the strategy raises on the first bar: it does not run on
as the rule with a judge that is never heard, which nothing would show.

Params, as a config writes them, every one of them required::

    rule:                  # trend_vol_weights' params, every one of them
      trend_window: 50
      vol_window: 20
      bars_per_year: 365
      target_vol: "0.40"
      max_weight: "0.5"
      band: "0.05"
    multipliers:           # one per rating, each in [0, 1], not rising from Buy to Sell
      Buy: "1"
      Overweight: "0.75"
      Hold: "0.5"
      Underweight: "0.25"
      Sell: "0"

``REVIEW`` takes no multiplier: it is handled as no verdict. The decimals
are quoted, as the rule's are.

A backtest replays the verdicts the store holds and asks no judge. A bar
with no verdict in the store is decided under the no-verdict policy, and
every decision keeps which verdicts it saw
(:attr:`~..domain.records.Decision.verdicts`): none for a bar without one,
the digest of a ``REVIEW`` for one with that, so the path taken can be read
back from the store.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType
from typing import Final

from ..domain.decimal_context import DECIMAL_CONTEXT, parse_decimal
from ..domain.types import Hold, MarketView, Portfolio, TargetWeights
from ..domain.verdicts import RATINGS, Rating
from .rebalance import (
    floor_weight,
    rebalance_or_hold,
    require_decimal,
    require_params,
    weights_with_quote,
)
from .trend_vol_weights import TrendVolWeights

__all__ = ["AiGatedWeights"]

_PARAMS: Final = frozenset({"rule", "multipliers"})
_TIERS: Final = [rating.value for rating in RATINGS]
_ZERO: Final = Decimal(0)
_ONE: Final = Decimal(1)


def _tier(key: object) -> Rating:
    """The rating a ``multipliers`` key names; ``REVIEW``, and anything that is no rating, is refused."""
    if key in _TIERS:
        return Rating(key)
    raise ValueError(f"multipliers names {key!r}, and the ratings with a multiplier are {_TIERS}")


@dataclass(frozen=True)
class AiGatedWeights:
    """The ``rule``'s weights, each cut by its token's rating, or to what is held when there is none."""

    rule: TrendVolWeights
    multipliers: Mapping[Rating, Decimal]

    def __post_init__(self) -> None:
        if not isinstance(self.rule, TrendVolWeights):
            raise ValueError(f"rule must be a TrendVolWeights, got {self.rule!r}")
        if not isinstance(self.multipliers, Mapping) or set(self.multipliers) != set(RATINGS):
            raise ValueError(
                f"multipliers must name exactly the ratings {_TIERS}, got {self.multipliers!r}"
            )
        for rating in RATINGS:
            require_decimal(
                self.multipliers[rating],
                f"multipliers[{rating.value!r}]",
                lambda value: not value.is_signed() and value <= _ONE,
                "in [0, 1]",
            )
        for bullish, bearish in zip(RATINGS, RATINGS[1:], strict=False):
            if self.multipliers[bullish] < self.multipliers[bearish]:
                raise ValueError(
                    f"multipliers must not rise from Buy to Sell: {bearish.value} "
                    f"{self.multipliers[bearish]} is above {bullish.value} "
                    f"{self.multipliers[bullish]}"
                )
        object.__setattr__(self, "multipliers", MappingProxyType(dict(self.multipliers)))

    @classmethod
    def from_params(cls, params: Mapping[str, object]) -> AiGatedWeights:
        """Build from a config's ``strategy.params``."""
        params = require_params("ai_gated_weights", params, _PARAMS)
        try:
            rule = TrendVolWeights.from_params(params["rule"])  # type: ignore[arg-type]
        except ValueError as exc:
            raise ValueError(f"rule: {exc}") from exc
        multipliers = params["multipliers"]
        if not isinstance(multipliers, Mapping):
            raise ValueError(
                f"multipliers must map each rating to its multiplier, got {multipliers!r}"
            )
        return cls(
            rule=rule,
            multipliers={
                _tier(key): parse_decimal(value, f"multipliers[{key!r}]")
                for key, value in multipliers.items()
            },
        )

    @property
    def band(self) -> Decimal:
        """How far a share may drift from its target before the target is answered: the rule's."""
        return self.rule.band

    def decide(self, view: MarketView, portfolio: Portfolio) -> TargetWeights | Hold:
        """The target when some token has drifted out of the band, else ``Hold``.

        The engine hands a portfolio holding the quote and exactly the
        tokens the bar prices, which are the tokens the target covers.
        """
        return rebalance_or_hold(portfolio, self.target_for(view, portfolio), self.band)

    def target_for(self, view: MarketView, portfolio: Portfolio) -> TargetWeights:
        """The rule's target for ``view``, each token gated by its verdict, or by what ``portfolio`` holds.

        A view that carries no source's verdicts is refused: the strategy
        is not to run without a judge.
        """
        if view.verdict_source is None:
            raise ValueError(
                "ai_gated_weights reads verdicts, and the view carries none: the config needs "
                "a verdicts section naming their source"
            )
        quote = portfolio.quote
        allowed = self.rule.target_for(view, quote).weights
        said = view.latest_verdicts
        total = portfolio.total_value
        risky: dict[str, Decimal] = {}
        for symbol in sorted(view.latest.prices):
            weight = allowed[symbol]
            verdict = said.get(symbol)
            if verdict is None or verdict.rating.is_review:
                # No new risk: what is held stays (to the weights' places), and what the
                # rule would not hold is sold.
                held = (
                    _ZERO
                    if total == 0
                    else floor_weight(DECIMAL_CONTEXT.divide(portfolio.value_of(symbol), total))
                )
                risky[symbol] = min(held, weight)
            else:
                risky[symbol] = floor_weight(
                    DECIMAL_CONTEXT.multiply(weight, self.multipliers[verdict.rating])
                )
        # No weight is above the rule's, so together they are at most 1.
        return weights_with_quote(risky, quote)
