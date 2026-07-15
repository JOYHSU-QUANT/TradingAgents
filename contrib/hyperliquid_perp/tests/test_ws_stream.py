"""Tests for the WS ingestion stream, reconnect supervisor, and REST backfill (§11).

Uses fakes for the socket and REST endpoints — no real network. Covers the
queue-only callback (§11.4 rule 1), the disconnect timing / staleness signal
(§11.2 rule 7), the reconnect + backfill-on-reconnect handshake (§11.2 rule 5),
and the trailing-window REST backfill converging with the WS path (§14).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
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
_NAIVE = datetime(2026, 7, 14, 8, 0)  # no tzinfo — rejected at the backfill boundary


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
    assert stream.mark_backfill_done(stream.backfill_epoch()) is True
    assert not stream.needs_backfill
    stream.mark_disconnected(clock.now())
    stream.mark_connected()  # reconnect
    assert stream.needs_backfill


def test_backfill_done_cannot_clear_a_newer_request():
    """A reconnect during an in-flight backfill must not have its request swallowed."""
    clock = ManualClock(_NOW)
    stream = LiveWsStream(clock=clock)
    stream.mark_connected()
    in_flight = stream.backfill_epoch()  # the pass the caller is about to run

    # ... the socket drops and comes back while that pass is still fetching. The new
    # gap is NOT covered by the in-flight window, which closed before it existed.
    stream.mark_disconnected(clock.now())
    stream.mark_connected()

    assert stream.mark_backfill_done(in_flight) is False  # superseded — owe another pass
    assert stream.needs_backfill
    assert stream.mark_backfill_done(stream.backfill_epoch()) is True
    assert not stream.needs_backfill


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


def test_supervisor_never_leaks_a_second_connection():
    # If two ensure_connected calls ever overlap (out of contract — only the tick
    # thread calls it — but the backoff is 0 here), the loser's socket must be
    # closed, never silently overwritten: an overwritten handle is a live
    # connection nobody holds a reference to any more.
    clock = ManualClock(_NOW)
    stream = LiveWsStream(clock=clock)
    opened = []
    holder = {}

    def connect():
        handle = _FakeHandle()
        opened.append(handle)
        if len(opened) == 1:
            # Re-enter while the first connect is still in flight.
            holder["sup"].ensure_connected()
        return handle

    sup = WsConnectionSupervisor(
        connect=connect, stream=stream, clock=clock, reconnect_min_interval_seconds=0
    )
    holder["sup"] = sup
    sup.ensure_connected()
    assert len(opened) == 2  # both attempts really opened a socket
    # Exactly one is installed; the other was closed, not leaked.
    assert sum(1 for h in opened if not h.closed) == 1
    assert sup.connected


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
    stream.mark_backfill_done(stream.backfill_epoch())
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


def _rest_fill(tid, time_ms=_TIME_MS):
    return {
        "coin": "BTC",
        "side": "B",
        "px": "100",
        "sz": "1",
        "closedPnl": "0",
        "crossed": True,
        "oid": 777,
        "time": time_ms,
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
        fetch=lambda s, e: [_rest_fill(1), _rest_fill(2)],
        processor=proc,
        clock=clock,
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


# ---------------------------------------------------------------------------
# §11.2 rule 7: a socket also fails by going SILENT, not only by closing
# ---------------------------------------------------------------------------


def test_half_open_socket_goes_stale():
    """A half-open TCP connection never calls note_closed — silence is the only signal.

    Without this, `_connected` stays True forever and a dead feed reads as healthy,
    while the bot keeps opening entries against a position model nothing is updating.
    """
    clock = ManualClock(_NOW)
    stream = LiveWsStream(silent_after_seconds=120, clock=clock)
    stream.mark_connected()
    stream.enqueue({"channel": "userFills"})

    clock.advance(119)
    assert not stream.is_stale()

    clock.advance(2)  # 121s with nothing delivered, on a socket that claims to be up
    assert stream.connected  # it still believes it is connected...
    assert stream.is_stale()  # ...and it is still stale


def test_a_connected_but_never_delivering_socket_goes_stale():
    """No event has ever arrived, so silence is measured from the connect itself."""
    clock = ManualClock(_NOW)
    stream = LiveWsStream(silent_after_seconds=120, clock=clock)
    stream.mark_connected()
    clock.advance(121)
    assert stream.is_stale()


def test_a_reconnect_does_not_inherit_the_previous_connections_silence_clock():
    """The silence baseline is the LATER of last-event and connect — and stale is sticky.

    An event from the previous connection must not seed the new connection's
    SILENCE clock (a stale baseline there would misfire the 120s rule on every
    reconnect). But after an outage past ``stale_after``, the reconnect alone does
    not read healthy either: the proof-of-life clock ran out during the outage,
    and only an actual event clears it — reconnecting is a claim, delivering is
    proof.
    """
    clock = ManualClock(_NOW)
    stream = LiveWsStream(silent_after_seconds=120, clock=clock)
    stream.mark_connected()
    stream.enqueue({"channel": "webData2"})  # the OLD connection's last event

    clock.advance(60)
    stream.mark_disconnected(clock.now())
    clock.advance(600)  # down far longer than stale_after — the old event is ancient
    stream.mark_connected()  # a healthy reconnect

    assert stream.silent_for() == 0  # measured from the reconnect, not the old event
    assert stream.is_stale()  # sticky: no event since before the outage

    stream.enqueue({"channel": "webData2"})  # proof of life
    assert not stream.is_stale()

    # The new baseline still ages: silence past the threshold with no event is stale.
    clock.advance(121)
    assert stream.is_stale()


def test_a_flapping_stream_that_never_delivers_goes_stale():
    """Reconnect cycles shorter than every per-connection threshold still run out.

    A stream that once delivered and then degrades into flapping — every
    reconnect succeeds, no connection ever delivers again — keeps resetting both
    per-connection clocks (``disconnected_for`` zeroes on connect, ``silent_for``
    re-baselines). The proof-of-life clock is the one no reconnect resets: past
    ``stale_after`` without a single event, the feed is stale even though each
    individual connection looks fresh.
    """
    clock = ManualClock(_NOW)
    stream = LiveWsStream(silent_after_seconds=120, stale_after_seconds=300, clock=clock)
    stream.mark_connected()
    stream.enqueue({"channel": "webData2"})  # it was alive once

    for _ in range(10):  # 10 cycles x 60s = 600s of flapping, each cycle under both thresholds
        clock.advance(30)
        stream.mark_disconnected(clock.now())
        clock.advance(30)
        stream.mark_connected()

    assert stream.silent_for() <= 30  # this connection's own clocks never fired...
    assert stream.disconnected_for() == 0
    assert stream.is_stale()  # ...but 600s without one event is not a healthy feed


def test_events_keep_a_connected_stream_fresh():
    clock = ManualClock(_NOW)
    stream = LiveWsStream(silent_after_seconds=120, clock=clock)
    stream.mark_connected()
    for _ in range(5):
        clock.advance(100)
        stream.enqueue({"channel": "webData2"})
        assert not stream.is_stale()


# ---------------------------------------------------------------------------
# Supervisor/stream state must move as ONE unit (both directions)
# ---------------------------------------------------------------------------


def test_connect_publishes_stream_state_under_the_supervisor_lock():
    """Installing the handle and marking the stream connected must be one atomic step.

    Published after the lock is released, a note_closed() landing in the gap would be
    overwritten: the stream would report a healthy socket the supervisor no longer
    holds, with the disconnect clock reset so rule 7's stale trip never accumulates.
    """
    clock = ManualClock(_NOW)
    stream = LiveWsStream(clock=clock)
    sup = WsConnectionSupervisor(connect=_FakeHandle, stream=stream, clock=clock)
    observed = {}
    original = stream.mark_connected

    def spy(now=None):
        observed["lock_held"] = sup._lock.locked()
        original(now)

    stream.mark_connected = spy  # type: ignore[method-assign]
    assert sup.ensure_connected() is True
    assert observed["lock_held"] is True


def test_close_publishes_stream_state_under_the_supervisor_lock():
    """The mirror case, and the worse one: a late mark_disconnected fails CLOSED.

    Published outside the lock, a connect starting AFTER the generation bump is not
    "superseded", installs a live handle and marks the stream connected — then the
    late disconnect lands on top. Every later tick short-circuits on `_handle is not
    None`, so the stream stays permanently disconnected on a working socket and the
    run sits in safe mode forever.
    """
    clock = ManualClock(_NOW)
    stream = LiveWsStream(clock=clock)
    sup = WsConnectionSupervisor(connect=_FakeHandle, stream=stream, clock=clock)
    sup.ensure_connected()
    observed = {}
    original = stream.mark_disconnected

    def spy(now=None):
        observed["lock_held"] = sup._lock.locked()
        original(now)

    stream.mark_disconnected = spy  # type: ignore[method-assign]
    sup.note_closed()
    assert observed["lock_held"] is True
    assert not stream.connected
    assert not sup.connected


# ---------------------------------------------------------------------------
# FillBackfiller — the gap start and the page budget (the gap must really close)
# ---------------------------------------------------------------------------


def test_window_start_falls_back_to_the_lookback_with_no_gap():
    clock = ManualClock(_NOW)
    seen = []

    bf = FillBackfiller(
        fetch=lambda s, e: seen.append((s, e)) or [],
        processor=None,  # never reached: the fetch returns no fills
        clock=clock,
        lookback_seconds=3600,
    )
    bf.backfill()  # a routine heartbeat pass: no known gap

    start_ms, end_ms = seen[0]
    assert end_ms - start_ms == 3600 * 1000


def test_since_pulls_the_window_back_past_a_long_outage():
    """Down for longer than the lookback: those fills are ingested by NO other path.

    A fixed trailing window would silently skip them — no error, no gap reported.
    """
    clock = ManualClock(_NOW)
    gap_start = _NOW - timedelta(hours=30)  # we were down far longer than the lookback
    seen = []

    bf = FillBackfiller(
        fetch=lambda s, e: seen.append((s, e)) or [],
        processor=None,
        clock=clock,
        lookback_seconds=6 * 3600,
    )
    bf.backfill(since=gap_start)

    start_ms, _ = seen[0]
    assert start_ms == int(gap_start.timestamp() * 1000)  # the REAL gap, not the window


def test_a_recent_since_does_not_shrink_the_overlap():
    """The lookback is a floor: a fresh gap start must not narrow the window below it."""
    clock = ManualClock(_NOW)
    seen = []

    bf = FillBackfiller(
        fetch=lambda s, e: seen.append((s, e)) or [],
        processor=None,
        clock=clock,
        lookback_seconds=3600,
    )
    bf.backfill(since=_NOW - timedelta(minutes=1))

    start_ms, end_ms = seen[0]
    assert end_ms - start_ms == 3600 * 1000  # still the full trailing hour


def test_a_capped_response_is_paged_not_truncated(db, tmp_path):
    """A full page is a truncated view of its window — paging is how the gap closes."""
    clock = ManualClock(_NOW)
    _live_run_with_order(db)
    proc = LiveFillProcessor(db=db, run_id="r", payload_dir=tmp_path, clock=clock)

    # Cap of 2: page 1 comes back FULL (so there must be more), page 2 short (so there
    # is not). Every fill is distinct and later than the last, as the exchange sends them.
    page1 = [_rest_fill(1, time_ms=_TIME_MS + 1), _rest_fill(2, time_ms=_TIME_MS + 2)]
    page2 = [_rest_fill(3, time_ms=_TIME_MS + 3)]
    pages = [page1, page2]

    bf = FillBackfiller(
        fetch=lambda s, e: pages.pop(0) if pages else [],
        processor=proc,
        clock=clock,
        response_fill_cap=2,
    )
    summary = bf.backfill()

    assert summary.fetched == 3
    assert summary.applied == 3  # the fills BEHIND the cap were not lost
    assert summary.complete is True  # the short page proved the window was covered


def test_an_exhausted_page_budget_reports_the_window_uncovered(db, tmp_path):
    """Never report a gap closed when it is not — the caller must run another pass."""
    clock = ManualClock(_NOW)
    _live_run_with_order(db)
    proc = LiveFillProcessor(db=db, run_id="r", payload_dir=tmp_path, clock=clock)

    calls = {"n": 0}

    def always_capped(start_ms, end_ms):
        calls["n"] += 1
        base = start_ms + 1
        return [_rest_fill(base + i, time_ms=base + i) for i in range(2)]

    bf = FillBackfiller(
        fetch=always_capped,
        processor=proc,
        clock=clock,
        max_pages=3,
        response_fill_cap=2,
    )
    summary = bf.backfill()

    assert calls["n"] == 3  # it kept paging...
    assert summary.complete is False  # ...and still could not prove the window covered


def test_a_full_page_with_no_readable_cursor_stops_and_reports_uncovered(db, tmp_path):
    """A capped page whose times are all unreadable cannot advance the cursor.

    Paging again would re-fetch the same page forever — the pass must stop after ONE
    fetch and report the window uncovered, never page on a guess or spin.
    """
    clock = ManualClock(_NOW)
    _live_run_with_order(db)
    proc = LiveFillProcessor(db=db, run_id="r", payload_dir=tmp_path, clock=clock)
    calls = {"n": 0}

    def fetch(start_ms, end_ms):
        calls["n"] += 1
        no_time = _rest_fill(1)
        del no_time["time"]
        garbage_time = _rest_fill(2)
        garbage_time["time"] = "garbage"
        return [no_time, garbage_time]  # a FULL page (cap 2) with no usable cursor

    bf = FillBackfiller(fetch=fetch, processor=proc, clock=clock, max_pages=5, response_fill_cap=2)
    summary = bf.backfill()

    assert calls["n"] == 1  # it stopped, it did not spin its whole page budget
    assert summary.complete is False  # the gap stays open — do not clear needs_backfill
    assert summary.malformed == 2  # both fills were recorded and skipped (§11.3)


def test_a_page_whose_cursor_moves_backwards_reports_the_window_uncovered(db, tmp_path):
    """A capped page whose newest time predates the window start cannot advance either.

    Resuming from it would move the cursor BACKWARDS and re-cover ground already read;
    the pass stops and reports the window uncovered instead.
    """
    clock = ManualClock(_NOW)
    _live_run_with_order(db)
    proc = LiveFillProcessor(db=db, run_id="r", payload_dir=tmp_path, clock=clock)
    calls = {"n": 0}

    def fetch(start_ms, end_ms):
        calls["n"] += 1
        # A full page whose fills all predate the window it was asked for.
        return [_rest_fill(1, time_ms=start_ms - 2), _rest_fill(2, time_ms=start_ms - 1)]

    bf = FillBackfiller(fetch=fetch, processor=proc, clock=clock, max_pages=5, response_fill_cap=2)
    summary = bf.backfill()

    assert calls["n"] == 1  # stopped rather than paging backwards forever
    assert summary.complete is False


@pytest.mark.parametrize("kwargs", [{"now": _NAIVE}, {"since": _NAIVE}], ids=["now", "since"])
def test_a_naive_instant_is_rejected_at_the_boundary(kwargs):
    """A naive `now` is the dangerous one: it does not raise, it silently shifts the window.

    `.timestamp()` reads a naive datetime as LOCAL time, so on the Tokyo box the whole
    window slides 9 hours into the past — the fetch returns nothing, the pass reports
    complete, and the caller declares a gap closed that it never looked at.
    """
    bf = FillBackfiller(
        fetch=lambda s, e: [], processor=None, clock=ManualClock(_NOW), lookback_seconds=3600
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        bf.backfill(**kwargs)


def test_last_live_fill_time_is_the_startup_backfill_floor(db, tmp_path):
    """PR5's startup floor: the newest booked fill is where the gap begins."""
    clock = ManualClock(_NOW)
    _live_run_with_order(db)
    assert repo.last_live_fill_time(db.conn, "r") is None  # cold start: no floor

    proc = LiveFillProcessor(db=db, run_id="r", payload_dir=tmp_path, clock=clock)
    proc.ingest(_rest_fill(1, time_ms=_TIME_MS))
    proc.ingest(_rest_fill(2, time_ms=_TIME_MS + 60_000))

    newest = repo.last_live_fill_time(db.conn, "r")
    assert newest == datetime.fromtimestamp((_TIME_MS + 60_000) / 1000, tz=timezone.utc)


def test_the_gap_anchor_survives_the_reconnect_that_raised_it():
    """The drop instant IS the backfill's `since` — it must outlive the reconnect.

    `disconnected_for` cannot serve: it is the liveness clock and reads 0 the moment the
    socket is back. Without a surviving anchor, PR5 has nothing to pass as `since`, falls
    back to the trailing lookback, and an outage longer than it loses its fills silently.
    """
    clock = ManualClock(_NOW)
    stream = LiveWsStream(clock=clock)
    stream.mark_connected()
    assert stream.backfill_since() is None  # startup: no outage created this request

    drop_at = clock.now()
    stream.mark_disconnected(drop_at)
    clock.advance(10 * 3600)  # a ten-hour outage, far beyond any trailing window
    stream.mark_connected()

    assert stream.connected
    assert stream.disconnected_for() == 0  # the liveness clock has reset...
    assert stream.backfill_since() == drop_at  # ...but the gap still knows where it began


def test_a_failed_first_connect_anchors_no_gap():
    """Never-connected is not a gap: `None` must keep meaning "use the startup floor".

    The supervisor marks the stream disconnected when a connect ATTEMPT fails, so the
    liveness clock runs and a boot that cannot connect still goes stale — but nothing
    was ever subscribed, so there is no drop instant to anchor. Anchoring one at boot
    would hand PR5 a "gap" starting ≈now; trusting it, the caller skips the
    `repo.last_live_fill_time` startup floor, the window snaps back to the trailing
    lookback, and every older fill is fetched by nothing while the pass reports success.
    """
    clock = ManualClock(_NOW)
    stream = LiveWsStream(clock=clock)

    stream.mark_disconnected(clock.now())  # a failed first connect attempt
    clock.advance(60)
    assert stream.disconnected_for() == 60  # the liveness clock DOES run...
    assert stream.backfill_since() is None  # ...but no gap: startup floor territory

    stream.mark_connected()
    assert stream.backfill_since() is None  # still startup — nothing was ever missed

    stream.mark_disconnected(clock.now())  # the FIRST real drop...
    assert stream.backfill_since() == clock.now()  # ...is the first anchor


def test_a_post_boot_drop_cannot_shadow_the_startup_floor():
    """The two obligations fold into one `since`; covering the anchor alone is not enough.

    Dispatched caller-side ("floor at startup, anchor afterwards"), a drop seconds after
    a successful first connect anchors a post-boot gap, the caller reads the non-None
    anchor as the whole story, and the pass that covers those few minutes clears the
    request — while the floor's day-long gap was never fetched by anything. The stream
    holds both, returns the earlier, and retires them only together.
    """
    clock = ManualClock(_NOW)
    stream = LiveWsStream(clock=clock)
    floor = _NOW - timedelta(days=1)  # newest booked fill: the process was down a day
    stream.set_startup_floor(floor)
    assert stream.backfill_since() == floor  # the floor IS an obligation pre-connect

    stream.mark_connected()
    clock.advance(30)
    stream.mark_disconnected(clock.now())  # flapping right after boot
    clock.advance(30)
    stream.mark_connected()

    # The anchor exists, but the floor is EARLIER and still uncovered — it wins.
    assert stream.backfill_since() == floor

    # A stale-epoch acknowledgement retires nothing.
    stale = stream.backfill_epoch() - 1
    assert not stream.mark_backfill_done(stale)
    assert stream.backfill_since() == floor

    # The epoch-matched acknowledgement covered min(floor, anchor) == floor, so BOTH
    # obligations retire together.
    assert stream.mark_backfill_done(stream.backfill_epoch())
    assert stream.backfill_since() is None


def test_startup_floor_rejects_a_naive_instant_and_a_second_registration():
    stream = LiveWsStream(clock=ManualClock(_NOW))
    with pytest.raises(ValueError, match="timezone-aware"):
        stream.set_startup_floor(_NAIVE)
    stream.set_startup_floor(None)  # "no obligation" stays expressible
    assert stream.backfill_since() is None
    # Set-ONCE is enforced: re-registering (fills booked since boot make the floor
    # read ~now) could move it forward past an uncovered obligation — the exact
    # shadowing hole the stream-held fold closes.
    with pytest.raises(ValueError, match="once per stream"):
        stream.set_startup_floor(_NOW)


def test_an_ack_while_the_socket_is_down_reanchors_the_ongoing_outage():
    """Only mark_connected bumps the epoch — a drop mid-pass does not stale the ack.

    The pass's window closed before the drop's fills stopped flowing, so its outage is
    still open at the acknowledgement: clearing the anchor to None would let the
    reconnect raise a request with backfill_since() None, snapping the window back to
    the trailing lookback and silently losing an outage longer than it.
    """
    clock = ManualClock(_NOW)
    stream = LiveWsStream(clock=clock)
    stream.mark_connected()
    epoch = stream.backfill_epoch()  # the pass reads its epoch, then runs...

    clock.advance(5)
    drop_at = clock.now()
    stream.mark_disconnected(drop_at)  # ...and the socket drops mid-flight

    assert stream.mark_backfill_done(epoch)  # epoch unchanged: the ack is accepted
    # But the outage is ongoing — the gap re-anchors at its start instead of vanishing.
    assert stream.backfill_since() == drop_at

    clock.advance(10 * 3600)  # far beyond any trailing lookback
    stream.mark_connected()
    assert stream.backfill_since() == drop_at  # the reconnect's pass still knows

    assert stream.mark_backfill_done(stream.backfill_epoch())
    assert stream.backfill_since() is None  # connected at THIS ack: nothing re-anchors


def test_the_gap_anchor_is_retired_only_by_the_backfill_that_covered_it():
    """A pass that never ran, or could not prove it covered its window, leaves it standing."""
    clock = ManualClock(_NOW)
    stream = LiveWsStream(clock=clock)
    stream.mark_connected()
    first_drop = clock.now()
    stream.mark_disconnected(first_drop)
    clock.advance(60)
    stream.mark_connected()

    # A second outage before any backfill ran must NOT move the anchor forward, or the
    # first outage's fills fall outside the window and are fetched by nothing.
    clock.advance(60)
    stream.mark_disconnected(clock.now())
    clock.advance(60)
    stream.mark_connected()
    assert stream.backfill_since() == first_drop

    stream.mark_backfill_done(stream.backfill_epoch())
    assert stream.backfill_since() is None  # covered — it stops being a gap
