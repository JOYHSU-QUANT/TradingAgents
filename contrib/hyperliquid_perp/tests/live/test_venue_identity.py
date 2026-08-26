"""Tests for the shared §13.5 venue-identity monitor (issue #80).

The per-site verdicts (protection fail-closed, reconcile case rows, kill-switch
disarm blocked) are pinned in their own modules; what is pinned HERE is the
fact the three consumers now share — one streak, one latch, one payload trail,
one escalation helper — and that each consumer's failure text names the fault.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from contrib.hyperliquid_perp.exchanges.hyperliquid.errors import (
    ExchangeRequestError,
    MalformedResponseError,
)
from contrib.hyperliquid_perp.live.config import (
    ExecutionMode,
    KillSwitchConfig,
    LiveProtectionConfig,
)
from contrib.hyperliquid_perp.live.kill_switch import KillSwitchManager
from contrib.hyperliquid_perp.live.order_gate import RealOrderGate
from contrib.hyperliquid_perp.live.protection import ProtectionManager
from contrib.hyperliquid_perp.live.reconcile import LiveReconciler
from contrib.hyperliquid_perp.live.safe_mode import REASON_IDENTITY_FAULT, SafeModeManager
from contrib.hyperliquid_perp.live.venue_identity import (
    UNREADABLE_PROBE_LATCH_THRESHOLD as K,
    VenueIdentityMonitor,
    describe_order_status_failure,
    escalate_identity_fault,
)
from contrib.hyperliquid_perp.paper import accounting
from contrib.hyperliquid_perp.paper.clock import ManualClock
from contrib.hyperliquid_perp.paper.stops import StopConfig
from contrib.hyperliquid_perp.persistence import repository as repo
from contrib.hyperliquid_perp.persistence.db import Database
from contrib.hyperliquid_perp.persistence.schema import SCHEMA_VERSION

_NOW = datetime(2026, 8, 26, 8, 0, tzinfo=timezone.utc)
_OURS = "0x" + "ab" * 16
_OURS_2 = "0x" + "cd" * 16
_STRANGER = "0x" + "ee" * 16


def _misrouted(oid: str = "4242") -> dict:
    """An orderStatus answer about SOMEONE ELSE's order (carries its own cloid)."""
    return {
        "status": "order",
        "order": {"order": {"oid": oid, "cloid": _STRANGER}, "status": "open"},
    }


def _unknown() -> dict:
    return {"status": "unknownOid"}


class _Venue:
    """One scripted orderStatus seam: a list of answers, or per-cloid answers."""

    def __init__(self, script: list | None = None) -> None:
        self.script = list(script or [])
        self.by_cloid: dict[str, object] = {}
        self.asked: list[str] = []

    def query_order_by_cloid(self, cloid_hex: str):
        self.asked.append(cloid_hex)
        answer = self.script.pop(0) if self.script else self.by_cloid.get(cloid_hex, _unknown())
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture
def db():
    db = Database(":memory:")
    accounting.initialize_run(
        db,
        run_id="r",
        mode="live",
        initial_balance_usdc=Decimal(100),
        schema_version=SCHEMA_VERSION,
        created_at=_NOW,
    )
    yield db
    db.close()


def _monitor(db, venue, *, payload_dir=None, clock=None) -> VenueIdentityMonitor:
    return VenueIdentityMonitor(
        query_order_by_cloid=venue.query_order_by_cloid,
        db=db,
        run_id="r",
        symbol="BTC",
        payload_dir=payload_dir,
        clock=clock or ManualClock(_NOW),
    )


def _latch_rows(db) -> list:
    return [
        e
        for e in repo.iter_protection_order_events(db.conn, "r")
        if e["event_type"] == "identity_fault_latched"
    ]


def _probe_expecting_unreadable(monitor, cloid=_OURS, site="test"):
    with pytest.raises(MalformedResponseError) as info:
        monitor.probe(cloid, site=site)
    return info.value


# -- the monitor on its own -----------------------------------------------------


def test_the_kth_consecutive_unreadable_answer_latches_and_the_k_minus_first_does_not(db):
    monitor = _monitor(db, _Venue([_misrouted() for _ in range(K)]))
    for _ in range(K - 1):
        _probe_expecting_unreadable(monitor, site="reconcile absent-order settle")
    assert monitor.unreadable_streak == K - 1
    assert monitor.latched is False
    assert _latch_rows(db) == []

    _probe_expecting_unreadable(monitor, site="kill-switch disarm cross-check")
    assert monitor.latched is True
    (row,) = _latch_rows(db)
    assert row["symbol"] == "BTC"
    assert row["cloid_hex"] == _OURS
    assert f"{K} consecutive" in row["detail"]
    # The row names the SITE that crossed the line and the parser's own
    # verdict, so triage does not have to guess which consumer or which fault.
    assert "kill-switch disarm cross-check" in row["detail"]
    assert "answered with cloid" in row["detail"]


def test_a_transport_failure_neither_counts_nor_resets(db):
    script = [*[_misrouted() for _ in range(K - 1)], ExchangeRequestError("down"), _misrouted()]
    monitor = _monitor(db, _Venue(script))
    for _ in range(K - 1):
        _probe_expecting_unreadable(monitor)
    with pytest.raises(ExchangeRequestError):
        monitor.probe(_OURS, site="test")
    assert monitor.unreadable_streak == K - 1  # untouched either way
    assert monitor.latched is False
    _probe_expecting_unreadable(monitor)
    assert monitor.latched is True


def test_any_readable_answer_ends_the_streak_including_unknown_oid(db):
    script = [
        *[_misrouted() for _ in range(K - 1)],
        _unknown(),
        *[_misrouted() for _ in range(K - 1)],
    ]
    monitor = _monitor(db, _Venue(script))
    for _ in range(K - 1):
        _probe_expecting_unreadable(monitor)
    assert monitor.probe(_OURS, site="test") is None  # the parser's verdict, untouched
    assert monitor.unreadable_streak == 0
    for _ in range(K - 1):
        _probe_expecting_unreadable(monitor)
    assert monitor.latched is False
    assert _latch_rows(db) == []


def test_an_unreadable_answer_carries_its_whole_payload_and_lands_on_disk(db, tmp_path):
    payload_dir = tmp_path / "payloads"
    monitor = _monitor(db, _Venue([_misrouted("777")]), payload_dir=payload_dir)
    exc = _probe_expecting_unreadable(monitor)
    # ``str(exc)`` can name the two cloids; only the payload names the stranger's oid.
    assert exc.payload == _misrouted("777")
    (path,) = sorted(payload_dir.glob("orderStatus-*.json"))
    assert _OURS.lower() in path.name.lower()
    assert json.loads(path.read_text(encoding="utf-8"))["order"]["order"]["oid"] == "777"


def test_without_a_payload_dir_the_payload_is_still_attached_and_nothing_is_written(db, tmp_path):
    monitor = _monitor(db, _Venue([_misrouted()]))
    exc = _probe_expecting_unreadable(monitor)
    assert exc.payload == _misrouted()
    assert list(tmp_path.iterdir()) == []


def test_the_latch_row_is_once_per_episode_and_a_recurrence_writes_a_new_one(db, tmp_path):
    payload_dir = tmp_path / "payloads"
    script = [*[_misrouted() for _ in range(K + 3)], _unknown(), *[_misrouted() for _ in range(K)]]
    # A clock that moves between round-trips, as the wall clock does: the
    # payload file name is stamped from it, and a frozen clock would make
    # every refusal of one cloid overwrite the last.
    clock = ManualClock(_NOW)
    monitor = _monitor(db, _Venue(script), payload_dir=payload_dir, clock=clock)
    for _ in range(K + 3):
        clock.advance(1)
        _probe_expecting_unreadable(monitor)
    assert len(_latch_rows(db)) == 1
    # The evidence is bounded per CLOID, not per answer: under the manual safe
    # mode the latch raises, the §17 sync keeps probing every tick — and a
    # venue that misroutes one cloid while answering another fine never even
    # latches — so one file per answer would fill the payload_dir without
    # bound. The latch row still names the file this cloid's evidence lives in.
    assert len(list(payload_dir.glob("orderStatus-*.json"))) == 1
    assert "payload " in _latch_rows(db)[0]["detail"]

    monitor.probe(_OURS, site="test")  # readable: the episode is over
    assert monitor.latched is False
    for _ in range(K):
        clock.advance(1)
        _probe_expecting_unreadable(monitor)
    assert monitor.latched is True
    assert len(_latch_rows(db)) == 2
    assert len(list(payload_dir.glob("orderStatus-*.json"))) == 1  # same cloid, same file


def test_an_intermittent_misroute_of_one_cloid_writes_one_file_not_one_per_tick(db, tmp_path):
    # The realistic shape: the venue misroutes OUR SL cloid but answers the TP
    # cloid coherently, so the streak flaps 1→0 forever and never latches. The
    # evidence budget must not depend on latching.
    payload_dir = tmp_path / "payloads"
    clock = ManualClock(_NOW)
    script = [_misrouted(), _unknown()] * (K + 2)
    monitor = _monitor(db, _Venue(script), payload_dir=payload_dir, clock=clock)
    for _ in range(K + 2):
        clock.advance(1)
        _probe_expecting_unreadable(monitor, cloid=_OURS)
        clock.advance(1)
        assert monitor.probe(_OURS_2, site="test") is None
    assert monitor.latched is False
    assert len(list(payload_dir.glob("orderStatus-*.json"))) == 1


def test_a_failed_latch_row_write_drops_neither_the_latch_nor_the_raise(db):
    monitor = _monitor(db, _Venue([_misrouted() for _ in range(K)]))

    def _busy(**_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monitor._record_latch = _busy  # type: ignore[method-assign]
    for _ in range(K):
        # Still the parser's error, not the DB's: the consumer's except lane
        # must keep seeing the fault it knows how to handle.
        _probe_expecting_unreadable(monitor)
    assert monitor.latched is True
    assert _latch_rows(db) == []


def test_the_failure_clause_separates_the_two_families():
    assert describe_order_status_failure(MalformedResponseError("x")) == (
        "orderStatus answered unusably (venue identity fault): x"
    )
    assert describe_order_status_failure(ExchangeRequestError("x")) == "orderStatus failed: x"
    assert describe_order_status_failure(RuntimeError("x")) == "orderStatus failed: x"


# -- the escalation helper --------------------------------------------------------


def _gate() -> RealOrderGate:
    return RealOrderGate(
        allow_real_orders=True,
        mode=ExecutionMode.TESTNET_LIVE,
        allowed_symbols=("BTC",),
        agent_authorized=True,
        startup_reconciliation_passed=True,
        kill_switch_active=True,
    )


def test_escalation_enters_manual_only_once_the_latch_is_up_and_is_idempotent(db):
    gate = _gate()
    safe_mode = SafeModeManager(db=db, run_id="r", gate=gate, clock=ManualClock(_NOW))
    monitor = _monitor(db, _Venue([_misrouted() for _ in range(K)]))

    for _ in range(K - 1):
        _probe_expecting_unreadable(monitor)
    assert escalate_identity_fault(monitor, safe_mode, site="test") is False
    assert safe_mode.current() is None
    assert gate.manual_safe_mode is False

    _probe_expecting_unreadable(monitor)
    assert escalate_identity_fault(monitor, safe_mode, site="§18.2 shutdown") is True
    state = safe_mode.current()
    assert state is not None and state.is_manual and state.reason == REASON_IDENTITY_FAULT
    assert gate.manual_safe_mode is True
    # Re-read by the next holder: no second history row, same episode.
    assert escalate_identity_fault(monitor, safe_mode, site="§12 reconciliation") is True
    entered = [
        e for e in repo.iter_safe_mode_events(db.conn, "r") if e["reason"] == REASON_IDENTITY_FAULT
    ]
    assert len(entered) == 1
    assert "§18.2 shutdown" in (entered[0]["detail"] or "")


def test_a_persisted_shutdown_escalation_is_what_the_next_boot_hydrates(db):
    # The kill-switch site's whole point (issue #80): the process that found
    # the fault is exiting, so the bound has to outlive it. A FRESH manager —
    # what the next ``live --run-id`` builds — hydrates the manual state.
    monitor = _monitor(db, _Venue([_misrouted() for _ in range(K)]))
    for _ in range(K):
        _probe_expecting_unreadable(monitor)
    escalate_identity_fault(
        monitor, SafeModeManager(db=db, run_id="r", gate=_gate()), site="§18.2 shutdown"
    )

    next_boot_gate = _gate()
    restored = SafeModeManager(db=db, run_id="r", gate=next_boot_gate).hydrate_gate()
    assert restored is not None and restored.is_manual
    assert restored.reason == REASON_IDENTITY_FAULT
    assert next_boot_gate.manual_safe_mode is True


# -- the three consumers, one streak ----------------------------------------------


def _clearinghouse() -> dict:
    return {
        "marginSummary": {"accountValue": "100", "totalMarginUsed": "0", "totalNtlPos": "0"},
        "withdrawable": "100",
        "crossMaintenanceMarginUsed": "0",
        "assetPositions": [],
    }


class _Client(_Venue):
    """The kill switch's and protection's client surface, over the same seam."""

    def __init__(self, clock: ManualClock) -> None:
        super().__init__()
        self._clock = clock
        self.clear_calls = 0

    def exchange_time(self):
        return self._clock.now()

    def schedule_cancel(self, *, cancel_at):
        pass

    def clear_scheduled_cancel(self):
        self.clear_calls += 1

    def open_orders(self):
        return []

    def cancel_by_cloid(self, *, coin, cloid_hex):  # pragma: no cover — no rows reach it
        raise AssertionError("the sweep has nothing to cancel in these tests")


class _FakeKillSwitch:
    """What protection needs from a switch: firings (row suspicion) + tick."""

    fired_total = 1

    def tick(self) -> None:
        pass


def _live_order(db, *, order_id, cloid_hex):
    with db.transaction() as conn:
        repo.insert_cloid_mapping(
            conn,
            cloid_logical=f"log-{order_id}",
            cloid_hex=cloid_hex,
            run_id="r",
            symbol="BTC",
            order_role="entry",
        )
        repo.insert_order(
            conn,
            order_id=order_id,
            mode="live",
            run_id="r",
            symbol="BTC",
            order_role="entry",
            side="buy",
            order_type="ioc_limit",
            qty=Decimal("0.001"),
            status="open",
            price=Decimal("100"),
            cloid_logical=f"log-{order_id}",
            cloid_hex=cloid_hex,
            exchange_order_id="900",
            is_bot_owned=True,
            timestamp=_NOW,
        )


def _reconciler(db, client, monitor, tmp_path) -> LiveReconciler:
    return LiveReconciler(
        db=db,
        run_id="r",
        coin="BTC",
        fetch_open_orders=client.open_orders,
        fetch_clearinghouse=_clearinghouse,
        fetch_fills=lambda start_ms, end_ms: [],
        payload_dir=tmp_path / "payloads",
        clock=ManualClock(_NOW),
        identity=monitor,  # the monitor owns the orderStatus seam
    )


def test_the_reconciler_takes_the_seam_from_exactly_one_place(db, tmp_path):
    # A ``query_order_by_cloid`` passed beside a monitor would be silently
    # ignored — and a wrapped or recording seam bypassed without a word — so
    # both and neither are refused at construction.
    client = _Client(ManualClock(_NOW))
    common = {
        "db": db,
        "run_id": "r",
        "coin": "BTC",
        "fetch_open_orders": client.open_orders,
        "fetch_clearinghouse": _clearinghouse,
        "clock": ManualClock(_NOW),
    }
    with pytest.raises(ValueError, match="EITHER"):
        LiveReconciler(
            **common,
            query_order_by_cloid=client.query_order_by_cloid,
            identity=_monitor(db, client),
        )
    with pytest.raises(ValueError, match="identity monitor or query_order_by_cloid"):
        LiveReconciler(**common)


def _kill_switch(db, client, monitor, clock, tmp_path) -> KillSwitchManager:
    return KillSwitchManager(
        client=client,
        gate=_gate(),
        db=db,
        run_id="r",
        config=KillSwitchConfig(),
        max_tick_gap_seconds=30.0,
        network_timeout_s=None,
        payload_dir=tmp_path / "payloads",
        clock=clock,
        identity=monitor,
    )


def _protection(db, client, monitor) -> ProtectionManager:
    return ProtectionManager(
        db=db,
        run_id="r",
        coin="BTC",
        client=client,
        gate=_gate(),
        tick_size=Decimal("1"),
        qty_step=Decimal("0.001"),
        stop_config=StopConfig(),
        max_slippage_pct=Decimal("0.005"),
        protection_config=LiveProtectionConfig(),
        owner_prefix="hta",
        clock=ManualClock(_NOW),
        sleep=lambda s: None,
        kill_switch=_FakeKillSwitch(),
        identity=monitor,
    )


def test_the_reconciler_the_kill_switch_and_protection_feed_one_streak(db, tmp_path):
    """The acceptance shape for #80: consumers alternate, the venue is one.

    Two locally-live orders the venue misroutes every time. The reconciler's
    settle probes count two, the shutdown cross-check counts two more (the
    same two rows, asked again), and protection's no-op guard delivers the
    fifth — so the latch rises from a streak no single consumer could have
    reached alone at K=5, and its row names the consumer that crossed the
    line. Each consumer's own audit text names the fault by family.
    """
    assert K == 5, "the choreography below is written for a threshold of five"
    clock = ManualClock(_NOW)
    client = _Client(clock)
    client.by_cloid = {_OURS: _misrouted("1"), _OURS_2: _misrouted("2")}
    monitor = _monitor(db, client, payload_dir=tmp_path / "payloads", clock=clock)
    _live_order(db, order_id="o1", cloid_hex=_OURS)
    _live_order(db, order_id="o2", cloid_hex=_OURS_2)

    report = _reconciler(db, client, monitor, tmp_path).run("heartbeat")
    settle_cases = [c for c in report.cases if c.case_type == "order_missing_on_exchange"]
    assert len(settle_cases) == 2
    assert all("answered unusably (venue identity fault)" in c.detail for c in settle_cases)
    assert monitor.unreadable_streak == 2
    assert monitor.latched is False

    clock.advance(60)  # a later round-trip: its own payload files, not overwrites
    switch = _kill_switch(db, client, monitor, clock, tmp_path)
    switch.arm()
    switch.shutdown()
    assert switch.armed  # unchanged fail-safe: an unconfirmable order blocks the disarm
    completed = [
        e
        for e in repo.iter_kill_switch_events(db.conn, "r")
        if e["event_type"] == "shutdown_cancel_orders_completed"
    ][0]
    failures = json.loads(completed["detail"])["failures"]
    assert len(failures) == 2
    assert all("could not confirm settled" in f for f in failures)
    assert all("answered unusably (venue identity fault)" in f for f in failures)
    assert monitor.unreadable_streak == 4
    assert monitor.latched is False
    assert _latch_rows(db) == []

    clock.advance(60)
    protection = _protection(db, client, monitor)
    assert protection._row_still_rests({"cloid_hex": _OURS}, role="stop_loss") is False
    assert monitor.latched is True
    assert protection.identity.latched is True  # the engine's read: the same monitor
    (row,) = _latch_rows(db)
    assert "protection stop_loss no-op guard" in row["detail"]
    # Two cloids refused, two files: evidence is kept once per cloid, whichever
    # consumer's refusal came first.
    assert len(list((tmp_path / "payloads").glob("orderStatus-*.json"))) == 2


def test_a_reconcile_pass_escalates_the_latch_under_its_own_reason(db, tmp_path):
    """Cross-pass memory, and the reconciler's own escalation.

    Before #80 a persistent misroute re-recorded the same unresolved case
    every pass, forever, and the only escalation was the generic one — three
    unclean passes reach manual as ``repeated_reconciliation_mismatch``
    (safe_mode._REPEATED_MISMATCH_THRESHOLD), which tells the operator
    nothing about WHICH fault. Measured here: that generic latch lands
    before the K-th pass, so the identity escalation arrives on top of an
    already-manual episode — and what it adds is the named reason in the
    §13.6 history (a ``safe_mode_reason_added`` row, once per episode). The
    case ledger stays at one row throughout: the bound is in the streak,
    not in extra rows.
    """
    client = _Client(ManualClock(_NOW))
    client.by_cloid = {_OURS: _misrouted()}
    monitor = _monitor(db, client, payload_dir=tmp_path / "payloads")
    _live_order(db, order_id="o1", cloid_hex=_OURS)
    reconciler = _reconciler(db, client, monitor, tmp_path)
    gate = _gate()
    safe_mode = SafeModeManager(db=db, run_id="r", gate=gate, clock=ManualClock(_NOW))

    def _identity_reason_recorded() -> bool:
        return any(
            e["reason"] == REASON_IDENTITY_FAULT for e in repo.iter_safe_mode_events(db.conn, "r")
        )

    for _ in range(K - 1):
        reconciler.reconcile_and_apply(
            "heartbeat", safe_mode=safe_mode, ws_restored=True, kill_switch_active=True
        )
    state = safe_mode.current()
    assert state is not None and state.is_manual  # the generic latch, already up
    assert state.reason == "repeated_reconciliation_mismatch"
    assert monitor.latched is False
    assert not _identity_reason_recorded()

    reconciler.reconcile_and_apply(
        "heartbeat", safe_mode=safe_mode, ws_restored=True, kill_switch_active=True
    )
    assert monitor.latched is True
    assert _identity_reason_recorded()
    added = [
        e for e in repo.iter_safe_mode_events(db.conn, "r") if e["reason"] == REASON_IDENTITY_FAULT
    ]
    assert [e["event_type"] for e in added] == ["safe_mode_reason_added"]
    assert "§12 reconciliation, heartbeat" in (added[0]["detail"] or "")
    assert gate.manual_safe_mode is True
    rows = repo.iter_exchange_reconciliation_events(
        db.conn, "r", case_type="order_missing_on_exchange"
    )
    assert len(rows) == 1  # deduped across all K passes, as before


def test_a_reconcile_pass_that_latches_first_enters_manual_with_the_identity_reason(db, tmp_path):
    # The other ordering: a streak the loop's other consumers built up is
    # finished by ONE reconcile pass — no unclean-pass history behind it — so
    # the reconciler's escalation is the FIRST manual entry, under its own reason.
    client = _Client(ManualClock(_NOW))
    client.by_cloid = {_OURS: _misrouted()}
    monitor = _monitor(db, client, payload_dir=tmp_path / "payloads")
    for _ in range(K - 1):
        _probe_expecting_unreadable(monitor, site="protection stop_loss no-op guard")
    _live_order(db, order_id="o1", cloid_hex=_OURS)
    gate = _gate()
    safe_mode = SafeModeManager(db=db, run_id="r", gate=gate, clock=ManualClock(_NOW))

    _reconciler(db, client, monitor, tmp_path).reconcile_and_apply(
        "heartbeat", safe_mode=safe_mode, ws_restored=True, kill_switch_active=True
    )
    state = safe_mode.current()
    assert state is not None and state.is_manual and state.reason == REASON_IDENTITY_FAULT
    assert gate.manual_safe_mode is True


def test_a_private_monitor_bounds_a_consumer_built_without_the_shared_one(db, tmp_path):
    # The optional constructor argument must not reopen the treadmill: a
    # reconciler wired the old way (no ``identity``) still latches on its own.
    clock = ManualClock(_NOW)
    client = _Client(clock)
    client.by_cloid = {_OURS: _misrouted()}
    _live_order(db, order_id="o1", cloid_hex=_OURS)
    reconciler = LiveReconciler(
        db=db,
        run_id="r",
        coin="BTC",
        fetch_open_orders=client.open_orders,
        fetch_clearinghouse=_clearinghouse,
        query_order_by_cloid=client.query_order_by_cloid,
        fetch_fills=lambda start_ms, end_ms: [],
        payload_dir=tmp_path / "payloads",
        clock=clock,  # the private monitor inherits it — see LiveReconciler.__init__
    )
    for _ in range(K):
        clock.advance(60)
        reconciler.run("heartbeat")
    (row,) = _latch_rows(db)
    assert "reconcile absent-order settle" in row["detail"]
    assert len(list((tmp_path / "payloads").glob("orderStatus-*.json"))) == 1
