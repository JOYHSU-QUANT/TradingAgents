"""Tests for market-data snapshot freshness accounting (execution §1.1 / §5.2)."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from contrib.hyperliquid_perp.domains.perp.schema import MarketSnapshot
from contrib.hyperliquid_perp.exchanges.hyperliquid.errors import ExchangeRequestError
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
    # A VENUE failure — the port's declared contract (``ports.ExchangeMarketData``)
    # — is what becomes ERROR. The fake raises that family, not a bare
    # ``RuntimeError``: driving this path with an arbitrary exception kept
    # passing under the broad ``except Exception`` this replaced, which is how
    # a drifted call signature came to read as an outage.
    clock = ManualClock(_T0)
    market = _StubMarket(raise_exc=ExchangeRequestError("venue refused"))
    provider = PortSnapshotProvider(market, clock)
    result = provider.fetch("BTC", requested_at=_T0, timeout_seconds=D(5))
    assert result.outcome is SnapshotOutcome.ERROR


def test_port_provider_sorts_a_non_venue_failure_into_its_own_outcome(caplog):
    """OUR defect and the venue's outage stop being the same outcome (issue #157).

    A call site drifted from the reader's signature raises ``TypeError``.
    Collapsed into ``ERROR`` it read as an exchange outage and left market data
    paused forever, one WARNING per tick, about an exchange that was answering.
    It is now ``DEFECT``, logged at ERROR with a traceback.

    Still RETURNED, never raised: ``fetch`` is called bare by
    ``engine.try_write_cycle_snapshot``, which is not fail-stop and sits in no
    broad handler, so a raise would end the daemon after the terminal row had
    committed — trading a silent stall for a crash-loop.
    """
    clock = ManualClock(_T0)
    market = _StubMarket(raise_exc=TypeError("get_market_snapshot() takes 1 argument"))
    provider = PortSnapshotProvider(market, clock)
    with caplog.at_level(logging.ERROR):
        result = provider.fetch("BTC", requested_at=_T0, timeout_seconds=D(5))
    assert result.outcome is SnapshotOutcome.DEFECT
    assert result.snapshot is None
    assert not result.is_valid
    message = "\n".join(r.getMessage() for r in caplog.records)
    assert "defect on our side, not an exchange outage" in message
    # The two verdicts must stay distinguishable, which is the entire point.
    assert result.outcome is not SnapshotOutcome.ERROR


def test_port_provider_contains_a_backwards_clock_step_on_the_way_out(caplog):
    """The containment covers the snapshot BUILD, not just the reader call.

    ``PriceSnapshot`` enforces ``received_at >= requested_at``, and a host
    clock stepped backwards mid-request (NTP correction, VM resume — the
    RUNBOOK documents this happening) makes it refuse instants nobody passed
    in. The venue answered perfectly well; leaving that construction outside
    the guard would let a bare ``ValueError`` reach
    ``engine.try_write_cycle_snapshot`` — not fail-stop, inside no broad
    handler, running after the terminal trade — which is the exact crash-loop
    shape this PR exists to remove.
    """
    clock = ManualClock(_T0)
    market = _StubMarket(snapshot=_market_snapshot(D("49999")))
    provider = PortSnapshotProvider(market, clock)
    # The request was stamped a minute AFTER the clock now reads: time moved
    # backwards between the two reads.
    requested_at = _T0 + timedelta(minutes=1)
    with caplog.at_level(logging.ERROR):
        result = provider.fetch("BTC", requested_at=requested_at, timeout_seconds=D(5))
    assert result.outcome is SnapshotOutcome.DEFECT  # ours, and it did not escape
    assert result.snapshot is None
    assert "defect on our side, not an exchange outage" in "\n".join(
        r.getMessage() for r in caplog.records
    )


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
