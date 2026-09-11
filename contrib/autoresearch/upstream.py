"""The ONE place this package names ``contrib.hyperliquid_perp``.

Two reasons the borrow is funnelled through a single module rather than
spelled at each use site:

1. **It is auditable.** The plan's hard constraint is that AutoResearch reads
   the perp package and never changes it. A reviewer (and
   ``tests/test_upstream.py``, which reads this package's sources) can check
   that constraint by looking at one import list instead of grepping a
   growing package.
2. **Later phases pin what they borrow.** Plan §3.2 requires a pin test per
   borrowed symbol, so a refactor upstream turns a research result that is no
   longer comparable into a red test here FIRST. Pins need one name to pin;
   :data:`BORROWED` is that name, and it is what the pin tests iterate.

Most of what is re-exported here is a *domain* import: value types, the two
vocabularies, and the epoch-ms conversions. They cost nothing at import time
— ``domains.perp.schema``, ``domains.perp.indicator_vocab``,
``common.enum_guard`` and ``common.instants`` sit at the bottom of that
package's graph. Two things are deliberately NOT among them, and both are
built inside a call instead:

- the exchange READER, because reaching it pulls in the Hyperliquid SDK,
  which a store-only or gap-check invocation has no use for
  (:func:`build_market_data`);
- the three ANALYTICS the live path builds its context from, because
  ``domains.perp.indicators`` imports pandas and stockstats — measured at
  511 ms on this box against 57 ms for the whole store layer, so a ``gaps``
  scan that computes no feature would pay ten times its own import cost
  (:func:`context_analytics`).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from contrib.hyperliquid_perp.common.enum_guard import VocabEnum
from contrib.hyperliquid_perp.common.instants import epoch_ms, from_epoch_ms
from contrib.hyperliquid_perp.domains.perp.indicator_vocab import (
    REGIME_INDICATORS,
    required_candles,
    supported_indicators,
)
from contrib.hyperliquid_perp.domains.perp.market_data_config import MarketDataConfig
from contrib.hyperliquid_perp.domains.perp.schema import (
    Candle,
    CandleInterval,
    FundingPoint,
    MarketRegime,
    interval_to_ms,
    parse_interval,
)
from contrib.hyperliquid_perp.exchanges.hyperliquid.errors import (
    ExchangeError,
    ExchangeThrottledError,
)

if TYPE_CHECKING:  # pragma: no cover - annotations only, and it must stay that way
    # ``from __future__ import annotations`` leaves every annotation a string,
    # so nothing here is evaluated at run time and the pandas stack the module
    # docstring keeps out of the store commands stays out.
    from decimal import Decimal

__all__ = [
    "BORROWED",
    "REGIME_INDICATORS",
    "Candle",
    "CandleInterval",
    "ContextAnalytics",
    "ExchangeError",
    "ExchangeThrottledError",
    "FundingPoint",
    "MarketDataConfig",
    "MarketRegime",
    "VocabEnum",
    "build_market_data",
    "context_analytics",
    "epoch_ms",
    "from_epoch_ms",
    "interval_to_ms",
    "parse_interval",
    "required_candles",
    "supported_indicators",
]

# What this package borrows, as ``(dotted module, attribute)`` pairs — the
# audit list plan §3.2 asks for, and the sequence the pin tests walk. The
# lazily-imported reader below is in it too: "not imported at module scope" is
# a load-time choice, not an exemption from the audit.
BORROWED: tuple[tuple[str, str], ...] = (
    ("contrib.hyperliquid_perp.common.enum_guard", "VocabEnum"),
    ("contrib.hyperliquid_perp.common.instants", "epoch_ms"),
    ("contrib.hyperliquid_perp.common.instants", "from_epoch_ms"),
    ("contrib.hyperliquid_perp.domains.perp.context_builder", "classify_regime"),
    ("contrib.hyperliquid_perp.domains.perp.context_builder", "funding_zscore"),
    ("contrib.hyperliquid_perp.domains.perp.indicator_vocab", "REGIME_INDICATORS"),
    ("contrib.hyperliquid_perp.domains.perp.indicator_vocab", "required_candles"),
    ("contrib.hyperliquid_perp.domains.perp.indicator_vocab", "supported_indicators"),
    ("contrib.hyperliquid_perp.domains.perp.indicators", "compute_indicators"),
    ("contrib.hyperliquid_perp.domains.perp.market_data_config", "MarketDataConfig"),
    ("contrib.hyperliquid_perp.domains.perp.schema", "Candle"),
    ("contrib.hyperliquid_perp.domains.perp.schema", "CandleInterval"),
    ("contrib.hyperliquid_perp.domains.perp.schema", "FundingPoint"),
    ("contrib.hyperliquid_perp.domains.perp.schema", "MarketRegime"),
    ("contrib.hyperliquid_perp.domains.perp.schema", "interval_to_ms"),
    ("contrib.hyperliquid_perp.domains.perp.schema", "parse_interval"),
    ("contrib.hyperliquid_perp.exchanges.hyperliquid.errors", "ExchangeError"),
    ("contrib.hyperliquid_perp.exchanges.hyperliquid.errors", "ExchangeThrottledError"),
    ("contrib.hyperliquid_perp.exchanges.hyperliquid.market_data", "HyperliquidMarketData"),
    ("contrib.hyperliquid_perp.exchanges.hyperliquid.sdk_client", "HyperliquidClient"),
)


def build_market_data():
    """A read-only MAINNET Hyperliquid reader — public endpoints, no wallet.

    Returns something satisfying :class:`~contrib.autoresearch.ports.HistoryMarketData`.
    The two in-function imports are what keep the SDK out of the store-only
    and gap-check paths, the way the perp package's own CLI keeps its daemon
    surface out of ``--context-only``.

    The network is not a parameter, and that is a decision about the STORE
    rather than about this function. A row is filed under ``(coin, interval,
    open_time)``, which says nothing about which venue served it, so a testnet
    bar and a mainnet bar for the same instant are the same row — one
    overwrites the other and nothing afterwards can tell that it happened.
    Plan §1 describes mainnet BTC throughout, so the choice is between a
    switch whose only reachable effect is to blend two venues into one series
    and no switch at all. Making the network part of the store's identity is
    the other way to have it, and is what to build if testnet is ever wanted.
    """
    from contrib.hyperliquid_perp.exchanges.hyperliquid.market_data import HyperliquidMarketData
    from contrib.hyperliquid_perp.exchanges.hyperliquid.sdk_client import HyperliquidClient

    return HyperliquidMarketData(HyperliquidClient(network="mainnet"))


@dataclass(frozen=True)
class ContextAnalytics:
    """The three functions the live path turns candles and funding into a view with.

    Handed over as one object rather than three lazy getters because they are
    one decision: a research feature is only comparable with what the trader
    saw if ALL of them are the live path's own — the indicator engine, the
    regime label built on top of it, and the funding z-score beside it. Three
    separate accessors would let a later edit replace one of them with a local
    re-implementation and leave the other two borrowed, which is exactly the
    drift plan §3.2's pins exist to catch.

    The callables sit on the INSTANCE, so ``analytics.classify_regime(...)``
    is a plain call and not a bound method with a stray ``self``.
    """

    compute_indicators: Callable[[Sequence[Candle], Sequence[str]], dict[str, float | None]]
    classify_regime: Callable[[dict[str, float | None], Decimal], MarketRegime]
    funding_zscore: Callable[[Sequence[FundingPoint], Decimal, int, int], tuple[float | None, int]]


def context_analytics() -> ContextAnalytics:
    """The live path's indicator, regime and funding-z-score functions.

    Imported inside the call for the reason the module docstring gives: this
    is the pandas/stockstats half of the perp package, and the store and gap
    commands must not pay for it. A caller that needs these binds the result
    once at ITS module scope — the cost is paid by whatever imports the
    feature engine, and by nothing else.
    """
    from contrib.hyperliquid_perp.domains.perp.context_builder import (
        classify_regime,
        funding_zscore,
    )
    from contrib.hyperliquid_perp.domains.perp.indicators import compute_indicators

    return ContextAnalytics(
        compute_indicators=compute_indicators,
        classify_regime=classify_regime,
        funding_zscore=funding_zscore,
    )
