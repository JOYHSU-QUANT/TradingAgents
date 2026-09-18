"""The ONE place this package names a package outside itself.

Two of them: ``contrib.hyperliquid_perp``, whose domain types and analytics
make a research figure comparable with what the paper trader saw, and
``tradingagents``, whose LLM client factory the hypothesis loop asks for a
hypothesis through (plan §3.2 lists it as a Phase B borrow).

Two reasons the borrow is funnelled through a single module rather than
spelled at each use site:

1. **It is auditable.** The plan's hard constraint is that AutoResearch reads
   those packages and never changes them. A reviewer (and
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
- the CHAT MODEL, for the same reason and by the same measurement: importing
  ``tradingagents.llm_clients`` costs 251 ms on this box and loads
  ``langchain_core``, and only ``research`` asks a model anything
  (:func:`build_chat_model`);
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
from contrib.hyperliquid_perp.domains.perp.research_signal import MAX_SIGNAL_AGE_INTERVALS
from contrib.hyperliquid_perp.domains.perp.schema import (
    Candle,
    CandleInterval,
    FundingPoint,
    MarketRegime,
    ResearchBias,
    ResearchConfidence,
    ResearchDrawdown,
    ResearchSignal,
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
    "MAX_SIGNAL_AGE_INTERVALS",
    "UPSTREAM_PACKAGES",
    "REGIME_INDICATORS",
    "Candle",
    "CandleInterval",
    "ContextAnalytics",
    "ExchangeError",
    "ExchangeThrottledError",
    "FundingPoint",
    "MarketDataConfig",
    "MarketRegime",
    "ResearchBias",
    "ResearchConfidence",
    "ResearchDrawdown",
    "ResearchSignal",
    "VocabEnum",
    "build_chat_model",
    "build_market_data",
    "context_analytics",
    "epoch_ms",
    "from_epoch_ms",
    "interval_to_ms",
    "parse_interval",
    "required_candles",
    "supported_indicators",
]

# The packages this one is allowed to name at all. Everything under them is
# declared below; anything outside them is not borrowed but vendored, and
# ``tests/test_upstream.py`` reads the sources to hold both halves.
UPSTREAM_PACKAGES: tuple[str, ...] = ("contrib.hyperliquid_perp", "tradingagents")

# What this package borrows, as ``(dotted module, attribute)`` pairs — the
# audit list plan §3.2 asks for, and the sequence the pin tests walk. The
# lazily-imported reader and chat model below are in it too: "not imported at
# module scope" is a load-time choice, not an exemption from the audit.
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
    # How stale the reader lets the handoff document get. Borrowed so the
    # producer can tell an operator the schedule the READER will actually
    # enforce — a second copy of the number here would keep printing the old
    # cadence for as long as it took someone to notice the section had gone.
    ("contrib.hyperliquid_perp.domains.perp.research_signal", "MAX_SIGNAL_AGE_INTERVALS"),
    ("contrib.hyperliquid_perp.domains.perp.schema", "Candle"),
    ("contrib.hyperliquid_perp.domains.perp.schema", "CandleInterval"),
    ("contrib.hyperliquid_perp.domains.perp.schema", "FundingPoint"),
    ("contrib.hyperliquid_perp.domains.perp.schema", "MarketRegime"),
    # The handoff document's contract (plan §7 / PR C1). Borrowed rather than
    # re-declared for the reason this module exists: the trading package READS
    # the document this one writes, so its vocabulary and its encoding have to
    # be one definition. Two copies of three closed vocabularies would agree
    # on the day they were written and drift on the day one side gained a
    # fourth band — with every test on both sides still green, because each
    # would be pinning its own copy.
    ("contrib.hyperliquid_perp.domains.perp.schema", "ResearchBias"),
    ("contrib.hyperliquid_perp.domains.perp.schema", "ResearchConfidence"),
    ("contrib.hyperliquid_perp.domains.perp.schema", "ResearchDrawdown"),
    ("contrib.hyperliquid_perp.domains.perp.schema", "ResearchSignal"),
    ("contrib.hyperliquid_perp.domains.perp.schema", "interval_to_ms"),
    ("contrib.hyperliquid_perp.domains.perp.schema", "parse_interval"),
    ("contrib.hyperliquid_perp.exchanges.hyperliquid.errors", "ExchangeError"),
    ("contrib.hyperliquid_perp.exchanges.hyperliquid.errors", "ExchangeThrottledError"),
    ("contrib.hyperliquid_perp.exchanges.hyperliquid.market_data", "HyperliquidMarketData"),
    ("contrib.hyperliquid_perp.exchanges.hyperliquid.sdk_client", "HyperliquidClient"),
    ("tradingagents.llm_clients", "create_llm_client"),
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


def build_chat_model(provider: str, model: str, base_url: str | None = None, **kwargs):
    """The repo's own chat client for ``provider``/``model``, ready to ``invoke``.

    The factory is imported INSIDE the call, and the measurement in the module
    docstring is why: every other command here computes nothing with a model,
    and a module-scope import would put ``langchain_core`` behind ``vocab``.

    Handed back as the library's object rather than wrapped, because the wrap
    belongs on the other side of this seam: the adapter that turns an answer
    into a hypothesis, and a transport failure into
    ``ports.HypothesistError``, is in :mod:`~contrib.autoresearch.hypothesis`.
    Wrapping here would put this package's own policy inside its audit list.

    The provider's own refusal of an unknown name is left to propagate as the
    ``ValueError`` it already is — the CLI's exit-1 lane catches that family,
    and the factory's sentence names the provider better than a paraphrase.
    """
    from tradingagents.llm_clients import create_llm_client

    return create_llm_client(provider=provider, model=model, base_url=base_url, **kwargs).get_llm()


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
