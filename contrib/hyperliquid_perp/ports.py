"""Exchange-facing ports (interfaces) the perp domain depends on.

These ``Protocol`` classes are the seam between the domain layer and any
concrete exchange. Phase 1 ships one implementation
(:mod:`.exchanges.hyperliquid`), but the domain code only ever type-hints
against these, so a paper/backtest exchange can be dropped in later without
touching the domain.

Structural typing: an implementation does not subclass these — it just needs
matching method signatures.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .domains.perp.schema import (
    AccountSnapshot,
    Candle,
    FundingPoint,
    MarketSnapshot,
    PerpPosition,
)


@runtime_checkable
class ExchangeMarketData(Protocol):
    """Read-only public market data. No auth, no wallet required."""

    def get_market_snapshot(self, coin: str) -> MarketSnapshot:
        """Latest mark/oracle/funding/OI snapshot for ``coin``."""
        ...

    def get_candles(self, coin: str, interval: str, lookback: int) -> list[Candle]:
        """Up to ``lookback`` most recent ``interval`` candles, oldest first."""
        ...

    def get_funding_history(self, coin: str, window_days: int) -> list[FundingPoint]:
        """Funding observations over the trailing ``window_days``, oldest first."""
        ...


@runtime_checkable
class ExchangeAccount(Protocol):
    """Account/position reads. Needs the (public) wallet address."""

    def get_account_snapshot(self, wallet_address: str) -> AccountSnapshot:
        """Margin summary plus all open positions for the wallet."""
        ...

    def get_position(self, wallet_address: str, coin: str) -> PerpPosition | None:
        """The open position for ``coin``, or ``None`` when flat."""
        ...
