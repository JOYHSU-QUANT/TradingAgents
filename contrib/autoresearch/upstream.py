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

Everything re-exported here is a *domain* import: value types, the interval
vocabulary, and the epoch-ms conversions. They cost nothing at import time —
``domains.perp.schema`` and ``common.instants`` sit at the bottom of that
package's graph. The exchange READER is deliberately not among them: reaching
it pulls in the Hyperliquid SDK, which a store-only or gap-check invocation
has no use for, so it is built by :func:`build_market_data` with the import
inside the call.
"""

from __future__ import annotations

from contrib.hyperliquid_perp.common.instants import epoch_ms, from_epoch_ms
from contrib.hyperliquid_perp.domains.perp.schema import (
    Candle,
    CandleInterval,
    FundingPoint,
    interval_to_ms,
    parse_interval,
)
from contrib.hyperliquid_perp.exchanges.hyperliquid.errors import (
    ExchangeError,
    ExchangeThrottledError,
)

__all__ = [
    "BORROWED",
    "Candle",
    "CandleInterval",
    "ExchangeError",
    "ExchangeThrottledError",
    "FundingPoint",
    "build_market_data",
    "epoch_ms",
    "from_epoch_ms",
    "interval_to_ms",
    "parse_interval",
]

# What this package borrows, as ``(dotted module, attribute)`` pairs — the
# audit list plan §3.2 asks for, and the sequence the pin tests walk. The
# lazily-imported reader below is in it too: "not imported at module scope" is
# a load-time choice, not an exemption from the audit.
BORROWED: tuple[tuple[str, str], ...] = (
    ("contrib.hyperliquid_perp.common.instants", "epoch_ms"),
    ("contrib.hyperliquid_perp.common.instants", "from_epoch_ms"),
    ("contrib.hyperliquid_perp.domains.perp.schema", "Candle"),
    ("contrib.hyperliquid_perp.domains.perp.schema", "CandleInterval"),
    ("contrib.hyperliquid_perp.domains.perp.schema", "FundingPoint"),
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
