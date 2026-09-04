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

from datetime import datetime
from typing import Protocol, runtime_checkable

from .domains.perp.schema import Candle, FundingPoint, MarketSnapshot


@runtime_checkable
class ExchangeMarketData(Protocol):
    """Read-only public market data. No auth, no wallet required.

    The two windowed reads take their upper bound as an explicit, tz-aware
    ``end`` and never substitute the host's clock (issue #124): the caller
    decides which clock cuts the window — the context build hands in the
    exchange's own, read before the fetch — and the reader's job is only to
    honour it. A default here would let a host-clock offset back into the
    indicators unseen.

    FAILURE IS A TYPE, not a convention: an implementation signals that the
    VENUE failed — refused, throttled, timed out, answered malformed — by
    raising :class:`~.exchanges.hyperliquid.errors.ExchangeError` or a
    subclass, and consumers catch that family and nothing wider. Anything else
    reaching a consumer is a defect on our side, and it must be allowed to say
    so. Written down because prose was all that held it and the three
    consumers had already drifted to three different answers:
    ``engine_bridge`` caught nothing, ``cli._provider``'s rate lookup caught
    the family, and ``paper.market_feed`` caught everything — where a drifted
    call signature read as an exchange outage and left market data paused
    forever, one WARNING per tick, about an exchange that was answering
    (issues #157, #193). A scripted or backtest feed dropped in here owes the
    same discipline: a simulated venue failure is an ``ExchangeError``, and a
    bug in the script must not be able to impersonate one.

    The port records the WHOLE public read surface, not the needs of any one
    consumer. ``PortSnapshotProvider`` calls only ``get_market_snapshot``;
    the windowed reads' consumers are ``engine_bridge._build_context`` and
    ``cli._provider``, which hold the concrete reader. Splitting a narrower
    snapshot-only protocol out for the provider was considered and declined
    (issue #157): a scripted or backtest feed dropped in for the provider
    carries two methods it is never asked for — a type-hint obligation only,
    since nothing ``isinstance``-checks the port — and that cost is smaller
    than two protocols whose ``end`` contract has to be kept in step.
    """

    def get_market_snapshot(self, coin: str) -> MarketSnapshot:
        """Latest mark/oracle/funding/OI snapshot for ``coin``."""
        ...

    def get_candles(
        self, coin: str, interval: str, lookback: int, *, end: datetime
    ) -> list[Candle]:
        """Up to ``lookback`` ``interval`` candles CLOSED as of ``end``, oldest first."""
        ...

    def get_funding_history(
        self, coin: str, window_days: int, *, end: datetime
    ) -> list[FundingPoint]:
        """Funding observations over the ``window_days`` trailing ``end``, oldest first."""
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

    def require_exchange_action(self, symbol: str | None) -> None:
        """Raise unless the base preconditions every signed mutation shares hold.

        The check for the signed actions that are not orders (cancel,
        scheduleCancel, updateLeverage), and a strict prefix of the two order
        checks. ``symbol`` has no default on purpose: ``None`` must be written
        out, declaring the action account-wide, so no signed action can miss the
        allowlist by omission. ``updateLeverage`` passes its coin — a leverage
        change aimed outside ``allowed_symbols`` is refused; the cancels pass
        ``None`` even though they name a coin (2026-08-17, issue #28). See
        :mod:`~contrib.hyperliquid_perp.live.order_gate` for both decisions.
        """
        ...
