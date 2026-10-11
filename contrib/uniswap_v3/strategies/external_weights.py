"""The external-weights strategy: hold what an outside coordinator set, and hold still without it.

:class:`ExternalWeights` makes no forecast. Its target at a bar is the
targets the view carries at that bar
(:attr:`~..domain.types.MarketView.latest_targets`), which the ``target``
command wrote from the carry coordinator's handoff (carry plan §3.4): each
traded token at the weight the coordinator set, cut to four decimal places
as every strategy's weights are, and what they leave to the quote token.
The target is answered only when some token's share has drifted more than
``band`` from it (:func:`.rebalance.rebalance_or_hold`), as the other
strategies do, with one exception: a token the portfolio does not hold
whose target is above zero, or one it holds whose target is zero, is
traded whatever the band. Those are the coordinator's entry and exit, and
a band would swallow them: an entry weight at or below the band (a spot
run several times the perp run's equity gets there) would never be bought
from nothing, leaving the perp leg short beside no spot at all, and an
exit would leave a sliver of the token behind for good.

A bar with no target is held as it is: ``Hold``, which is the spot leg's
half of the plan's D5 (a handoff that is missing or stale leaves BOTH legs
where they are, because a spot leg sold beside a perp leg still short is a
naked position). The visit warns of such a bar on stderr; the strategy
itself cannot, and the engine records a hold like any other.

A bar with a target on some traded tokens and not on others is refused
(``ValueError``, which the engine reports and stops the run on): the
coordinator writes one handoff per coin and the run trades that coin's
spot token, so a half-covered bar is a config that trades a token no
coordinator sizes, or a coordinator that failed, and neither is a bar to
decide quietly. So is a set of targets that add up to more than 1, which
no portfolio can hold.

The targets are those of the source the run's config names
(``targets.source``). A config that names none hands the strategy a view
without one, and the strategy refuses the first bar, so that the run does
not hold still forever with nobody noticing.

Params, as a config writes them::

    band: "0.05"          # in [0, 1)

The band is quoted, as every strategy's numbers are.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from ..domain.decimal_context import decimal_sum, parse_decimal, plain
from ..domain.types import Hold, MarketView, Portfolio, TargetWeights
from .rebalance import (
    floor_weight,
    rebalance_or_hold,
    require_band,
    require_params,
    weights_with_quote,
)

__all__ = ["ExternalWeights"]

_PARAMS: Final = frozenset({"band"})
_ONE: Final = Decimal(1)


@dataclass(frozen=True)
class ExternalWeights:
    """The coordinator's weights at the bar, when it set them; ``Hold`` when it did not."""

    band: Decimal

    def __post_init__(self) -> None:
        require_band(self.band)

    @classmethod
    def from_params(cls, params: Mapping[str, object]) -> ExternalWeights:
        """Build from a config's ``strategy.params``."""
        params = require_params("external_weights", params, _PARAMS)
        return cls(band=parse_decimal(params["band"], "band"))

    def decide(self, view: MarketView, portfolio: Portfolio) -> TargetWeights | Hold:
        """The coordinator's target when a token enters or exits, or some share has drifted out of the band; else ``Hold``.

        A bar the coordinator set no target at is held whatever the
        portfolio holds (module docstring). The engine hands a portfolio
        holding the quote and exactly the tokens the bar prices, which are
        the tokens a target covers.
        """
        target = self.target_for(view, portfolio.quote)
        if target is None:
            return Hold()
        if portfolio.total_value != 0 and any(
            (portfolio.balances[symbol] == 0) != (weight == 0)
            for symbol, weight in target.weights.items()
            if symbol != portfolio.quote
        ):
            # An entry from nothing, or an exit to nothing: the band does not apply.
            return target
        return rebalance_or_hold(portfolio, target, self.band)

    def target_for(self, view: MarketView, quote: str) -> TargetWeights | None:
        """The targets at the bar being decided as weights over the bar's tokens and ``quote``; ``None`` without any.

        A view that carries no source's targets is refused, and so is a bar
        with targets on some of its tokens and not on others, or with
        targets that add up to more than 1.
        """
        if view.target_source is None:
            raise ValueError(
                "external_weights reads targets, and the view carries none: the config needs "
                "a targets section naming their source"
            )
        aimed = view.latest_targets
        if not aimed:
            return None
        time = view.latest.time
        priced = sorted(view.latest.prices)
        missing = [symbol for symbol in priced if symbol not in aimed]
        if missing:
            raise ValueError(
                f"the targets of {view.target_source!r} at {time} cover {sorted(aimed)} and not "
                f"{missing}; external_weights takes a target on every traded token at a bar, "
                f"or none"
            )
        risky = {symbol: floor_weight(aimed[symbol].weight) for symbol in priced}
        total = decimal_sum(risky.values())
        if total > _ONE:
            raise ValueError(
                f"the targets of {view.target_source!r} at {time} add up to {plain(total)}, more "
                f"than the whole portfolio"
            )
        return weights_with_quote(risky, quote)
