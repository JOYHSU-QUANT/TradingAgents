"""Market-data snapshots with freshness accounting (execution §1.1 / §5.2 / §5.5).

Every scheduled paper tick issues **one** public market-data request and gets a
:class:`SnapshotResult` back. A snapshot is *valid* only when a genuinely new
response arrives within the request timeout carrying **both** a ``mid_price``
and a ``mark_price`` (execution §1.1: a price that didn't move is not stale — the
question is whether a fresh response was obtained, never whether the number
changed). Every request records ``requested_at`` / ``received_at`` / latency and
its outcome, so the freshness bookkeeping the engine relies on (timeout, the
3-strike ``paused_market_data`` transition, the ``gap_stop_fill``) is auditable.

Two concrete providers:

- :class:`PortSnapshotProvider` — production. Wraps an
  :class:`~..ports.ExchangeMarketData`, stamps timing off the injected clock, and
  fails the snapshot when the port reports a VENUE failure
  (:class:`~..exchanges.hyperliquid.errors.ExchangeError`, the port's declared
  contract), omits ``mid_price``, or answers slower
  than the timeout. A true wall-clock cancellation of a hung request needs the
  caller's own timeout (threads/async); this provider measures round-trip latency
  against the clock and rejects an over-budget response, so a slow answer is
  treated as the timeout the spec describes rather than trusted late.
- :class:`ScriptedSnapshotProvider` — tests. Replays a fixed list of programmed
  results (valid snapshots and failures) so freshness paths are deterministic.

The engine treats mark and mid asymmetrically downstream (SL/TP *trigger* on
mark, fills reference mid — execution §5.2), but a snapshot is only ever handed
to the engine when **both** are present, so neither consumer sees a half-populated
tick.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Protocol, runtime_checkable

from ..exchanges.hyperliquid.errors import ExchangeError
from ..ports import ExchangeMarketData
from .clock import Clock

logger = logging.getLogger(__name__)

__all__ = [
    "PortSnapshotProvider",
    "PriceSnapshot",
    "ScriptedSnapshotProvider",
    "SnapshotOutcome",
    "SnapshotProvider",
    "SnapshotResult",
]


class SnapshotOutcome(str, Enum):
    """Why a market-data request did or didn't yield a usable snapshot.

    ``OK`` is the only valid outcome; the rest are the distinct failure modes the
    engine's 3-strike pause logic counts and the audit trail records.
    """

    OK = "ok"  # fresh response within timeout, both mid and mark present
    TIMEOUT = "timeout"  # no response, or one slower than the request timeout
    ERROR = "error"  # the port raised (request/feed failure)
    INVALID = "invalid"  # response arrived but lacked a usable mid or mark


@dataclass(frozen=True)
class PriceSnapshot:
    """One valid market observation: both prices plus the request timing.

    Only ever constructed for a valid (``OK``) result, so both prices are always
    present and positive — the engine never has to guard a half-populated tick.
    """

    coin: str
    mark_price: Decimal
    mid_price: Decimal
    requested_at: datetime
    received_at: datetime

    def __post_init__(self) -> None:
        if not self.coin or not self.coin.strip():
            raise ValueError("PriceSnapshot.coin must be a non-empty string")
        # Parity with MarketSnapshot / every mark-price entry point: a
        # non-positive price is a corrupt feed value, not a live quote.
        if self.mark_price <= 0:
            raise ValueError(f"PriceSnapshot.mark_price must be > 0, got {self.mark_price}")
        if self.mid_price <= 0:
            raise ValueError(f"PriceSnapshot.mid_price must be > 0, got {self.mid_price}")
        for name in ("requested_at", "received_at"):
            value = getattr(self, name)
            if value.tzinfo is None:
                raise ValueError(f"PriceSnapshot.{name} must be timezone-aware (UTC)")
        if self.received_at < self.requested_at:
            raise ValueError(
                f"PriceSnapshot.received_at {self.received_at} precedes "
                f"requested_at {self.requested_at}"
            )

    @property
    def latency_seconds(self) -> Decimal:
        """Request round-trip latency in seconds (>= 0)."""
        return Decimal(str((self.received_at - self.requested_at).total_seconds()))


@dataclass(frozen=True)
class SnapshotResult:
    """The outcome of one market-data request — valid snapshot or a typed failure.

    ``snapshot`` is present exactly when ``outcome is OK``; a failure records its
    timing (so latency is still auditable) but no prices. The coupling is enforced
    at construction, matching the result-dataclass convention across this package.
    """

    outcome: SnapshotOutcome
    requested_at: datetime
    received_at: datetime
    snapshot: PriceSnapshot | None

    def __post_init__(self) -> None:
        if (self.snapshot is not None) != (self.outcome is SnapshotOutcome.OK):
            raise ValueError(
                "SnapshotResult.snapshot must be present exactly when outcome is OK "
                f"(outcome {self.outcome.value}, snapshot {self.snapshot!r})"
            )
        for name in ("requested_at", "received_at"):
            value = getattr(self, name)
            if value.tzinfo is None:
                raise ValueError(f"SnapshotResult.{name} must be timezone-aware (UTC)")

    @property
    def is_valid(self) -> bool:
        return self.outcome is SnapshotOutcome.OK

    @property
    def latency_seconds(self) -> Decimal:
        return Decimal(str((self.received_at - self.requested_at).total_seconds()))

    @classmethod
    def ok(cls, snapshot: PriceSnapshot) -> SnapshotResult:
        return cls(
            outcome=SnapshotOutcome.OK,
            requested_at=snapshot.requested_at,
            received_at=snapshot.received_at,
            snapshot=snapshot,
        )

    @classmethod
    def failure(
        cls, outcome: SnapshotOutcome, *, requested_at: datetime, received_at: datetime
    ) -> SnapshotResult:
        if outcome is SnapshotOutcome.OK:
            raise ValueError("SnapshotResult.failure requires a non-OK outcome")
        return cls(
            outcome=outcome, requested_at=requested_at, received_at=received_at, snapshot=None
        )


@runtime_checkable
class SnapshotProvider(Protocol):
    """Issues one market-data request and reports a :class:`SnapshotResult`.

    The engine passes the request instant and the timeout; the provider owns the
    "did a fresh, complete response arrive in time?" decision so the engine's
    freshness logic stays provider-agnostic.
    """

    def fetch(
        self, coin: str, *, requested_at: datetime, timeout_seconds: Decimal
    ) -> SnapshotResult: ...


def _timeout_timedelta(timeout_seconds: Decimal) -> timedelta:
    if timeout_seconds <= 0:
        raise ValueError(f"timeout_seconds must be > 0, got {timeout_seconds}")
    return timedelta(seconds=float(timeout_seconds))


class PortSnapshotProvider:
    """Production provider: one :class:`ExchangeMarketData` request per fetch.

    Latency is measured against the injected clock. A response slower than
    ``timeout_seconds`` is rejected as ``TIMEOUT`` (the spec's 5-second budget),
    a raise becomes ``ERROR``, and a snapshot missing ``mid_price`` becomes
    ``INVALID`` (execution §5.2: never fabricate a fill from ``mark`` alone).
    """

    def __init__(self, market: ExchangeMarketData, clock: Clock) -> None:
        self._market = market
        self._clock = clock

    def fetch(
        self, coin: str, *, requested_at: datetime, timeout_seconds: Decimal
    ) -> SnapshotResult:
        budget = _timeout_timedelta(timeout_seconds)
        try:
            snap = self._market.get_market_snapshot(coin)
        except ExchangeError as exc:
            # ONLY the venue-failure family (the port's contract — see
            # ``ports.ExchangeMarketData``). A broad ``except Exception`` here
            # read every defect as an outage: a call site drifted from the
            # reader's signature raises ``TypeError``, this collapsed it to
            # ERROR, and the engine paused market data and stayed paused —
            # one WARNING per tick, forever, about an exchange that was in
            # fact answering (issues #157, #193). The same reader is now read
            # the same way by all three of its consumers.
            #
            # Log the cause before collapsing to the ERROR outcome: downstream this
            # only surfaces as pending/paused market data, so without this an operator
            # cannot tell an exchange outage from bad credentials.
            logger.warning("market snapshot fetch failed for %s: %s", coin, exc, exc_info=True)
            return SnapshotResult.failure(
                SnapshotOutcome.ERROR, requested_at=requested_at, received_at=self._clock.now()
            )
        received_at = self._clock.now()
        # A response that took longer than the budget is stale by the freshness
        # rule regardless of its contents — treat it as the timeout it exceeded.
        if received_at - requested_at > budget:
            return SnapshotResult.failure(
                SnapshotOutcome.TIMEOUT, requested_at=requested_at, received_at=received_at
            )
        if snap.mid_price is None:
            # Both prices are required for a valid tick; a mark-only snapshot must
            # never be promoted into a fill (execution §5.2).
            return SnapshotResult.failure(
                SnapshotOutcome.INVALID, requested_at=requested_at, received_at=received_at
            )
        return SnapshotResult.ok(
            PriceSnapshot(
                coin=coin,
                mark_price=snap.mark_price,
                mid_price=snap.mid_price,
                requested_at=requested_at,
                received_at=received_at,
            )
        )


class ScriptedSnapshotProvider:
    """Test provider replaying a fixed sequence of programmed results.

    Each ``fetch`` returns the next entry, restamped onto the *actual* request
    instant so timing lines up with the driving :class:`ManualClock`. An entry is
    either a ``(mark, mid)`` pair (a valid snapshot, received instantly) or a bare
    :class:`SnapshotOutcome` failure. Running past the end raises — a test that
    ticks more than it scripted is a bug in the test, not a silent empty feed.
    """

    def __init__(
        self, coin: str, script: Sequence[tuple[Decimal, Decimal] | SnapshotOutcome]
    ) -> None:
        self._coin = coin
        self._script = list(script)
        self._i = 0

    def fetch(
        self, coin: str, *, requested_at: datetime, timeout_seconds: Decimal
    ) -> SnapshotResult:
        if coin != self._coin:
            raise ValueError(f"ScriptedSnapshotProvider is for {self._coin!r}, got {coin!r}")
        if self._i >= len(self._script):
            raise AssertionError("ScriptedSnapshotProvider exhausted: more fetches than scripted")
        entry = self._script[self._i]
        self._i += 1
        if isinstance(entry, SnapshotOutcome):
            return SnapshotResult.failure(
                entry, requested_at=requested_at, received_at=requested_at
            )
        mark, mid = entry
        return SnapshotResult.ok(
            PriceSnapshot(
                coin=coin,
                mark_price=mark,
                mid_price=mid,
                requested_at=requested_at,
                received_at=requested_at,
            )
        )
