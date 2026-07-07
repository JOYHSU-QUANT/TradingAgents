"""Tests for market-data snapshot freshness accounting (execution §1.1 / §5.2)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from contrib.hyperliquid_perp.domains.perp.schema import MarketSnapshot
from contrib.hyperliquid_perp.paper.clock import ManualClock
from contrib.hyperliquid_perp.paper.market_feed import (
    PortSnapshotProvider,
    PriceSnapshot,
    ScriptedSnapshotProvider,
    SnapshotOutcome,
    SnapshotResult,
)

D = Decimal
_T0 = datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)


def _market_snapshot(mid: Decimal | None) -> MarketSnapshot:
    return MarketSnapshot(
        coin="BTC",
        mark_price=D(50000),
        oracle_price=D(50000),
        prev_day_price=D(49000),
        open_interest=D(100),
        day_ntl_volume=D(1000),
        funding=D("0.0000125"),
        mid_price=mid,
    )


class _StubMarket:
    def __init__(self, snapshot=None, raise_exc=None, delay_s=0):
        self._snapshot = snapshot
        self._raise = raise_exc
        self._delay = delay_s
        self.clock: ManualClock | None = None

    def get_market_snapshot(self, coin):
        if self._raise is not None:
            raise self._raise
        if self._delay and self.clock is not None:
            self.clock.advance(self._delay)
        return self._snapshot

    def get_candles(self, *a, **k):  # pragma: no cover - unused
        return []

    def get_funding_history(self, *a, **k):  # pragma: no cover - unused
        return []


# --------------------------------------------------------------------------
# result / snapshot invariants
# --------------------------------------------------------------------------


def test_result_snapshot_present_only_when_ok():
    with pytest.raises(ValueError):
        SnapshotResult(
            outcome=SnapshotOutcome.TIMEOUT,
            requested_at=_T0,
            received_at=_T0,
            snapshot=PriceSnapshot("BTC", D(50000), D(50000), _T0, _T0),
        )


def test_price_snapshot_latency():
    snap = PriceSnapshot("BTC", D(50000), D(50000), _T0, _T0 + timedelta(seconds=2))
    assert snap.latency_seconds == D(2)


# --------------------------------------------------------------------------
# PortSnapshotProvider freshness rules (§5.2)
# --------------------------------------------------------------------------


def test_port_provider_valid_snapshot():
    clock = ManualClock(_T0)
    market = _StubMarket(snapshot=_market_snapshot(D("49999")))
    provider = PortSnapshotProvider(market, clock)
    result = provider.fetch("BTC", requested_at=_T0, timeout_seconds=D(5))
    assert result.is_valid
    assert result.snapshot.mid_price == D("49999")
    assert result.snapshot.mark_price == D(50000)


def test_port_provider_missing_mid_is_invalid():
    clock = ManualClock(_T0)
    market = _StubMarket(snapshot=_market_snapshot(None))
    provider = PortSnapshotProvider(market, clock)
    result = provider.fetch("BTC", requested_at=_T0, timeout_seconds=D(5))
    assert result.outcome is SnapshotOutcome.INVALID
    assert result.snapshot is None


def test_port_provider_error_on_raise():
    clock = ManualClock(_T0)
    market = _StubMarket(raise_exc=RuntimeError("boom"))
    provider = PortSnapshotProvider(market, clock)
    result = provider.fetch("BTC", requested_at=_T0, timeout_seconds=D(5))
    assert result.outcome is SnapshotOutcome.ERROR


def test_port_provider_timeout_on_slow_response():
    clock = ManualClock(_T0)
    market = _StubMarket(snapshot=_market_snapshot(D("49999")), delay_s=6)
    market.clock = clock  # advancing the clock during the call simulates latency
    provider = PortSnapshotProvider(market, clock)
    result = provider.fetch("BTC", requested_at=_T0, timeout_seconds=D(5))
    assert result.outcome is SnapshotOutcome.TIMEOUT


# --------------------------------------------------------------------------
# ScriptedSnapshotProvider
# --------------------------------------------------------------------------


def test_scripted_provider_replays_in_order():
    provider = ScriptedSnapshotProvider("BTC", [(D(50000), D("49999")), SnapshotOutcome.TIMEOUT])
    r1 = provider.fetch("BTC", requested_at=_T0, timeout_seconds=D(5))
    r2 = provider.fetch("BTC", requested_at=_T0, timeout_seconds=D(5))
    assert r1.is_valid
    assert r2.outcome is SnapshotOutcome.TIMEOUT


def test_scripted_provider_exhaustion_raises():
    provider = ScriptedSnapshotProvider("BTC", [(D(50000), D("49999"))])
    provider.fetch("BTC", requested_at=_T0, timeout_seconds=D(5))
    with pytest.raises(AssertionError):
        provider.fetch("BTC", requested_at=_T0, timeout_seconds=D(5))
