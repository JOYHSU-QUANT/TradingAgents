"""Ports (interfaces) between this package's layers.

These ``Protocol`` classes are seams that keep concrete layers decoupled.
``ExchangeMarketData`` faces outward: the paper engine's snapshot provider
(``paper.market_feed.PortSnapshotProvider``) type-hints against it, so a
scripted/backtest market feed can be dropped in without touching that
consumer; the CLI/legacy entry points construct the concrete reader directly
and may call methods beyond this port (e.g. ``get_asset_meta``). ``OrderGate``
faces the other way: it is the application-layer contract the exchange
adapter's signed client judges every mutation against, so the adapter never
imports the application layer for a type hint.

Structural typing: an implementation does not subclass these — it just needs
matching method signatures.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .domains.perp.schema import Candle, FundingPoint, MarketSnapshot


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
class OrderGate(Protocol):
    """Wire-side judgement every signed exchange mutation must pass (§4.1).

    The three checks the signed client calls; the live implementation is
    :class:`~contrib.hyperliquid_perp.live.order_gate.RealOrderGate`. Each
    ``require_*`` raises
    :class:`~contrib.hyperliquid_perp.live.order_gate.LiveOrderGateRejected`
    when the action may not proceed — the live call sites catch that concrete
    type, so any alternative implementation must raise it (or a subclass).
    """

    def require_order(self, symbol: str) -> None:
        """Raise unless a regular (risk-adding) order for ``symbol`` may go out."""
        ...

    def require_protective_order(self, symbol: str) -> None:
        """Raise unless a protection / de-risking order for ``symbol`` may go out."""
        ...

    def require_exchange_action(self) -> None:
        """Raise unless the base preconditions every signed mutation shares hold.

        The only check for signed actions the gate judges without a symbol
        (cancel, scheduleCancel, updateLeverage) — the call carries none, so
        ``allowed_symbols`` is not enforced for them — and a strict prefix of
        the two order checks.
        """
        ...
