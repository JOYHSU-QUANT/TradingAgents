"""Tests for the WS ingestion stream, reconnect supervisor, and REST backfill (§11).

Uses fakes for the socket and REST endpoints — no real network. Covers the
queue-only callback (§11.4 rule 1), the disconnect timing / staleness signal
(§11.2 rule 7), the reconnect + backfill-on-reconnect handshake (§11.2 rule 5),
and the trailing-window REST backfill converging with the WS path (§14).
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from contrib.hyperliquid_perp.exchanges.hyperliquid.errors import MalformedResponseError
from contrib.hyperliquid_perp.live.fill_backfill import FillBackfiller
from contrib.hyperliquid_perp.live.fills import IngestOutcome, LiveFillProcessor
from contrib.hyperliquid_perp.live.ws_stream import (
    CLEARINGHOUSE_CHANNEL,
    ORDER_UPDATES_CHANNEL,
    USER_FILLS_CHANNEL,
    LiveWsStream,
    WsConnectionSupervisor,
    bind_user_subscriptions,
)
from contrib.hyperliquid_perp.paper import accounting
from contrib.hyperliquid_perp.paper.clock import ManualClock
from contrib.hyperliquid_perp.persistence import repository as repo
from contrib.hyperliquid_perp.persistence.db import Database
from contrib.hyperliquid_perp.persistence.schema import SCHEMA_VERSION

_NOW = datetime(2026, 7, 14, 8, 0, tzinfo=timezone.utc)
_TIME_MS = int(_NOW.timestamp() * 1000)


# ---------------------------------------------------------------------------
# LiveWsStream — queue + connection lifecycle
# ---------------------------------------------------------------------------


def test_enqueue_and_drain_preserve_order():
    stream = LiveWsStream(clock=ManualClock(_NOW))
    stream.enqueue({"channel": "userFills", "n": 1})
    stream.enqueue({"channel": "orderUpdates", "n": 2})
    assert stream.pending() == 2
    drained = stream.drain()
    assert [e["n"] for e in drained] == [1, 2]
    assert stream.drain() == []  # drained once


def test_disconnect_clock_and_staleness():
    clock = ManualClock(_NOW)
    stream = LiveWsStream(stale_after_seconds=300, clock=clock)
    stream.mark_connected()
    assert not stream.is_stale()
    stream.mark_disconnected(clock.now())
    clock.advance(200)
    assert stream.disconnected_for(clock.now()) == 200
    assert not stream.is_stale(clock.now())
    clock.advance(200)  # now 400s down > 300 threshold
    assert stream.is_stale(clock.now())


def test_disconnect_clock_anchors_at_first_drop():
    # A flapping socket (repeated disconnect calls) must not keep resetting the
    # clock, or the §11.2-rule-7 stale threshold could never be reached.
    clock = ManualClock(_NOW)
    stream = LiveWsStream(clock=clock)
    stream.mark_disconnected(clock.now())
    clock.advance(100)
    stream.mark_disconnected(clock.now())  # second drop, same outage
    clock.advance(100)
    assert stream.disconnected_for(clock.now()) == 200


def test_reconnect_requests_backfill():
    clock = ManualClock(_NOW)
    stream = LiveWsStream(clock=clock)
    assert not stream.needs_backfill
    stream.mark_connected()  # first connect (startup) also backfills
    assert stream.needs_backfill
    stream.mark_backfill_done()
    assert not stream.needs_backfill
    stream.mark_disconnected(clock.now())
    stream.mark_connected()  # reconnect
    assert stream.needs_backfill


def test_never_connected_stream_is_not_stale():
    clock = ManualClock(_NOW)
    stream = LiveWsStream(clock=clock)
    clock.advance(10_000)
    # No prior connection dropped, so there is no "disconnected for" basis.
    assert stream.disconnected_for(clock.now()) == 0
    assert not stream.is_stale(clock.now())


# ---------------------------------------------------------------------------
# bind_user_subscriptions
# ---------------------------------------------------------------------------


class _FakeSubscriber:
    """Records subscriptions and lets a test drive their callbacks."""

    def __init__(self):
        self.subscriptions = []
        self._next_id = 0

    def subscribe(self, subscription, callback):
        self._next_id += 1
        self.subscriptions.append((subscription, callback))
        return self._next_id

    def deliver(self, channel, message):
        for sub, cb in self.subscriptions:
            if sub["type"] == channel:
                cb(message)


def test_bind_subscribes_all_three_streams_and_callbacks_enqueue():
    stream = LiveWsStream(clock=ManualClock(_NOW))
    sub = _FakeSubscriber()
    ids = bind_user_subscriptions(sub, "0xWALLET", stream)
    assert len(ids) == 3
    types = {s["type"] for s, _ in sub.subscriptions}
    assert types == {USER_FILLS_CHANNEL, ORDER_UPDATES_CHANNEL, CLEARINGHOUSE_CHANNEL}
    # A delivered event lands in the queue (and nowhere else — no parse, no DB).
    sub.deliver(USER_FILLS_CHANNEL, {"channel": USER_FILLS_CHANNEL, "data": {"fills": []}})
    assert stream.pending() == 1


# ---------------------------------------------------------------------------
# WsConnectionSupervisor — reconnect policy
# ---------------------------------------------------------------------------


class _FakeHandle:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def test_supervisor_connects_and_marks_stream():
    clock = ManualClock(_NOW)
    stream = LiveWsStream(clock=clock)
    handle = _FakeHandle()
    sup = WsConnectionSupervisor(connect=lambda: handle, stream=stream, clock=clock)
    assert sup.ensure_connected() is True
    assert sup.connected
    assert stream.connected
    assert stream.needs_backfill  # first connect requests a backfill
    sup.close()
    assert handle.closed
    assert not sup.connected


def test_supervisor_connect_failure_marks_disconnected_and_backs_off():
    clock = ManualClock(_NOW)
    stream = LiveWsStream(clock=clock)
    calls = []

    def failing_connect():
        calls.append(1)
        raise ConnectionError("down")

    sup = WsConnectionSupervisor(
        connect=failing_connect, stream=stream, clock=clock, reconnect_min_interval_seconds=5
    )
    assert sup.ensure_connected() is False
    assert not stream.connected
    assert len(calls) == 1
    # Within the backoff window, no second attempt.
    clock.advance(2)
    assert sup.ensure_connected() is False
    assert len(calls) == 1
    # Past the backoff, it retries.
    clock.advance(5)
    assert sup.ensure_connected() is False
    assert len(calls) == 2


def test_supervisor_does_not_reconnect_while_already_connected():
    # The early-return guard: a tick that finds a live handle must not open a
    # second socket (which would leak a connection and double every event).
    clock = ManualClock(_NOW)
    stream = LiveWsStream(clock=clock)
    calls = []

    def connect():
        calls.append(1)
        return _FakeHandle()

    sup = WsConnectionSupervisor(connect=connect, stream=stream, clock=clock)
    assert sup.ensure_connected() is True
    assert sup.ensure_connected() is True
    assert len(calls) == 1


def test_supervisor_note_closed_is_idempotent():
    clock = ManualClock(_NOW)
    stream = LiveWsStream(clock=clock)
    sup = WsConnectionSupervisor(connect=_FakeHandle, stream=stream, clock=clock)
    sup.ensure_connected()
    sup.note_closed()
    clock.advance(100)
    sup.note_closed()  # a second close for the same outage
    assert not sup.connected
    assert not stream.connected
    # The disconnect clock still anchors at the FIRST drop.
    clock.advance(100)
    assert stream.disconnected_for(clock.now()) == 200


def test_supervisor_discards_a_connect_superseded_by_a_concurrent_close():
    # The socket thread's on_close can land while the tick thread is inside
    # connect(). The connection it opened is already dead, so it must be dropped
    # — not installed, and NOT allowed to mark the stream healthy (which would
    # hide the disconnect from §11.2 rules 6/7).
    clock = ManualClock(_NOW)
    stream = LiveWsStream(clock=clock)
    handle = _FakeHandle()
    holder = {}

    def connect():
        # Simulate the race: the close fires mid-connect.
        holder["sup"].note_closed()
        return handle

    sup = WsConnectionSupervisor(
        connect=connect, stream=stream, clock=clock, reconnect_min_interval_seconds=0
    )
    holder["sup"] = sup
    assert sup.ensure_connected() is False
    assert not sup.connected
    assert handle.closed  # the stale connection was cleaned up
    assert not stream.connected  # the disconnect stands


def test_supervisor_note_closed_then_reconnect_requests_backfill():
    clock = ManualClock(_NOW)
    stream = LiveWsStream(clock=clock)
    sup = WsConnectionSupervisor(
        connect=_FakeHandle, stream=stream, clock=clock, reconnect_min_interval_seconds=0
    )
    sup.ensure_connected()
    stream.mark_backfill_done()
    sup.note_closed()
    assert not sup.connected
    assert not stream.connected
    assert sup.ensure_connected() is True
    assert stream.needs_backfill  # the reconnect asks for a backfill


# ---------------------------------------------------------------------------
# FillBackfiller — REST trailing window, converging with WS
# ---------------------------------------------------------------------------


@pytest.fixture
def db():
    with Database(":memory:") as database:
        yield database


def _live_run_with_order(db):
    accounting.initialize_run(
        db,
        run_id="r",
        mode="live",
        initial_balance_usdc=Decimal("1000"),
        schema_version=SCHEMA_VERSION,
        created_at=_NOW,
    )
    with db.transaction() as conn:
        repo.insert_order(
            conn,
            order_id="o1",
            mode="live",
            run_id="r",
            symbol="BTC",
            order_role="entry",
            side="buy",
            order_type="ioc_limit",
            qty=Decimal("1"),
            status="filled",
            price=Decimal("100"),
            cloid_logical="log-1",
            cloid_hex="0x" + "ab" * 16,
            exchange_order_id="777",
            is_bot_owned=True,
            timestamp=_NOW,
        )


def _rest_fill(tid):
    return {
        "coin": "BTC",
        "side": "B",
        "px": "100",
        "sz": "1",
        "closedPnl": "0",
        "crossed": True,
        "oid": 777,
        "time": _TIME_MS,
        "tid": tid,
        "fee": "0.05",
        "feeToken": "USDC",
    }


def test_backfill_applies_new_fills(db, tmp_path):
    _live_run_with_order(db)
    clock = ManualClock(_NOW)
    proc = LiveFillProcessor(db=db, run_id="r", payload_dir=tmp_path, clock=clock)
    captured = {}

    def fetch(start_ms, end_ms):
        captured["window"] = (start_ms, end_ms)
        return [_rest_fill(1)]

    bf = FillBackfiller(fetch=fetch, processor=proc, clock=clock, lookback_seconds=3600)
    summary = bf.backfill()
    assert (summary.fetched, summary.applied) == (1, 1)
    assert captured["window"] == (_TIME_MS - 3600 * 1000, _TIME_MS)
    assert db.conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 1


def test_backfill_dedupes_against_ws_applied_fill(db, tmp_path):
    _live_run_with_order(db)
    clock = ManualClock(_NOW)
    proc = LiveFillProcessor(db=db, run_id="r", payload_dir=tmp_path, clock=clock)
    # WS already applied tid=1; REST re-fetches it plus a new tid=2.
    proc.ingest(_rest_fill(1))
    bf = FillBackfiller(
        fetch=lambda s, e: [_rest_fill(1), _rest_fill(2)], processor=proc, clock=clock
    )
    summary = bf.backfill()
    assert (summary.applied, summary.duplicate) == (1, 1)
    assert db.conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 2


def test_backfill_records_malformed_and_counts_it(db, tmp_path):
    _live_run_with_order(db)
    clock = ManualClock(_NOW)
    proc = LiveFillProcessor(db=db, run_id="r", payload_dir=tmp_path, clock=clock)
    bad = _rest_fill(2)
    del bad["closedPnl"]
    bf = FillBackfiller(fetch=lambda s, e: [_rest_fill(1), bad], processor=proc, clock=clock)
    summary = bf.backfill()
    assert (summary.applied, summary.malformed) == (1, 1)
    assert list(tmp_path.glob("fill_parse_error-*.json"))


@pytest.mark.parametrize("bad", [{"err": "boom"}, None, "oops"])
def test_backfill_non_list_response_fails_loud(db, tmp_path, bad):
    # Including None: coercing a null response to [] would report "0 fills
    # fetched", the caller would clear needs_backfill, and the reconnect gap
    # would be silently declared closed — the exact failure this module exists to
    # prevent. Same null-vs-empty stance as map_candles / map_funding_history.
    _live_run_with_order(db)
    clock = ManualClock(_NOW)
    proc = LiveFillProcessor(db=db, run_id="r", payload_dir=tmp_path, clock=clock)
    bf = FillBackfiller(fetch=lambda s, e: bad, processor=proc, clock=clock)
    with pytest.raises(MalformedResponseError):
        bf.backfill()


def test_backfill_empty_list_is_a_legitimate_no_fills(db, tmp_path):
    # The counterpart: an actual empty list DOES mean "no fills in the window".
    _live_run_with_order(db)
    clock = ManualClock(_NOW)
    proc = LiveFillProcessor(db=db, run_id="r", payload_dir=tmp_path, clock=clock)
    bf = FillBackfiller(fetch=lambda s, e: [], processor=proc, clock=clock)
    assert bf.backfill().fetched == 0


def test_backfill_transport_failure_propagates(db, tmp_path):
    _live_run_with_order(db)
    clock = ManualClock(_NOW)
    proc = LiveFillProcessor(db=db, run_id="r", payload_dir=tmp_path, clock=clock)

    def fetch(s, e):
        raise ConnectionError("rest down")

    bf = FillBackfiller(fetch=fetch, processor=proc, clock=clock)
    with pytest.raises(ConnectionError):
        bf.backfill()


def test_backfill_and_ws_converge_on_exactly_once(db, tmp_path):
    # The invariant that matters: WS and REST both see the same fill, and it is
    # applied exactly once regardless of order.
    _live_run_with_order(db)
    clock = ManualClock(_NOW)
    proc = LiveFillProcessor(db=db, run_id="r", payload_dir=tmp_path, clock=clock)
    fill = _rest_fill(1)
    # REST first...
    FillBackfiller(fetch=lambda s, e: [fill], processor=proc, clock=clock).backfill()
    # ...then the WS delivers the same fill.
    assert proc.ingest(fill).outcome is IngestOutcome.DUPLICATE
    assert db.conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 1
