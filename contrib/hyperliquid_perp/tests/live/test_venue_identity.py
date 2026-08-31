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
    EscalationHolder,
    ProbeSite,
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

from ..conftest import doc_text, identity_latch_rows, misrouted_order_status

_NOW = datetime(2026, 8, 26, 8, 0, tzinfo=timezone.utc)
_OURS = "0x" + "ab" * 16
_OURS_2 = "0x" + "cd" * 16


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


def _probe_expecting_unreadable(
    monitor, cloid=_OURS, site=ProbeSite.RECONCILE_ABSENT_SETTLE, role=None
):
    with pytest.raises(MalformedResponseError) as info:
        monitor.probe(cloid, site=site, role=role)
    return info.value


# -- the monitor on its own -----------------------------------------------------


def test_the_kth_consecutive_unreadable_answer_latches_and_the_k_minus_first_does_not(db):
    monitor = _monitor(db, _Venue([misrouted_order_status() for _ in range(K)]))
    for _ in range(K - 1):
        _probe_expecting_unreadable(monitor, site=ProbeSite.RECONCILE_ABSENT_SETTLE)
    assert monitor.unreadable_streak == K - 1
    assert monitor.latched is False
    assert identity_latch_rows(db) == []

    _probe_expecting_unreadable(monitor, site=ProbeSite.KILL_SWITCH_DISARM_CROSS_CHECK)
    assert monitor.latched is True
    (row,) = identity_latch_rows(db)
    assert row["symbol"] == "BTC"
    assert row["cloid_hex"] == _OURS
    assert f"{K} consecutive" in row["detail"]
    # The row names the SITE that crossed the line and the parser's own
    # verdict, so triage does not have to guess which consumer or which fault.
    assert "kill-switch disarm cross-check" in row["detail"]
    assert "answered with cloid" in row["detail"]


def test_a_transport_failure_neither_counts_nor_resets(db):
    script = [
        *[misrouted_order_status() for _ in range(K - 1)],
        ExchangeRequestError("down"),
        misrouted_order_status(),
    ]
    monitor = _monitor(db, _Venue(script))
    for _ in range(K - 1):
        _probe_expecting_unreadable(monitor)
    with pytest.raises(ExchangeRequestError):
        monitor.probe(_OURS, site=ProbeSite.RECONCILE_ABSENT_SETTLE)
    assert monitor.unreadable_streak == K - 1  # untouched either way
    assert monitor.latched is False
    _probe_expecting_unreadable(monitor)
    assert monitor.latched is True


def test_any_readable_answer_ends_the_streak_including_unknown_oid(db):
    script = [
        *[misrouted_order_status() for _ in range(K - 1)],
        _unknown(),
        *[misrouted_order_status() for _ in range(K - 1)],
    ]
    monitor = _monitor(db, _Venue(script))
    for _ in range(K - 1):
        _probe_expecting_unreadable(monitor)
    # The parser's verdict, untouched.
    assert monitor.probe(_OURS, site=ProbeSite.RECONCILE_ABSENT_SETTLE) is None
    assert monitor.unreadable_streak == 0
    for _ in range(K - 1):
        _probe_expecting_unreadable(monitor)
    assert monitor.latched is False
    assert identity_latch_rows(db) == []


def test_an_unreadable_answer_carries_its_whole_payload_and_lands_on_disk(db, tmp_path):
    payload_dir = tmp_path / "payloads"
    monitor = _monitor(db, _Venue([misrouted_order_status("777")]), payload_dir=payload_dir)
    exc = _probe_expecting_unreadable(monitor)
    # ``str(exc)`` can name the two cloids; only the payload names the stranger's oid.
    assert exc.payload == misrouted_order_status("777")
    (path,) = sorted(payload_dir.glob("orderStatus-*.json"))
    assert _OURS.lower() in path.name.lower()
    assert json.loads(path.read_text(encoding="utf-8"))["order"]["order"]["oid"] == "777"


def test_without_a_payload_dir_the_payload_is_still_attached_and_nothing_is_written(db, tmp_path):
    monitor = _monitor(db, _Venue([misrouted_order_status()]))
    exc = _probe_expecting_unreadable(monitor)
    assert exc.payload == misrouted_order_status()
    assert list(tmp_path.iterdir()) == []


def test_the_latch_row_is_once_per_episode_and_a_recurrence_writes_a_new_one(db, tmp_path):
    payload_dir = tmp_path / "payloads"
    script = [
        *[misrouted_order_status() for _ in range(K + 3)],
        _unknown(),
        *[misrouted_order_status() for _ in range(K)],
    ]
    # A clock that moves between round-trips, as the wall clock does: the
    # payload file name is stamped from it, and a frozen clock would make
    # every refusal of one cloid overwrite the last.
    clock = ManualClock(_NOW)
    monitor = _monitor(db, _Venue(script), payload_dir=payload_dir, clock=clock)
    for _ in range(K + 3):
        clock.advance(1)
        _probe_expecting_unreadable(monitor)
    assert len(identity_latch_rows(db)) == 1
    # The evidence is bounded per CLOID, not per answer or per episode: under
    # the manual safe mode the latch raises, the §17 sync keeps probing every
    # tick, and a cloid that flaps readable/unreadable starts a new episode on
    # every flap — either budget would fill the payload_dir without bound.
    # The latch row still names the file this cloid's evidence lives in.
    assert len(list(payload_dir.glob("orderStatus-*.json"))) == 1
    assert "payload " in identity_latch_rows(db)[0]["detail"]

    monitor.probe(_OURS, site=ProbeSite.RECONCILE_ABSENT_SETTLE)  # readable: the episode is over
    assert monitor.latched is False
    for _ in range(K):
        clock.advance(1)
        _probe_expecting_unreadable(monitor)
    assert monitor.latched is True
    assert len(identity_latch_rows(db)) == 2
    assert len(list(payload_dir.glob("orderStatus-*.json"))) == 1  # same cloid, same file


def test_a_misroute_of_one_cloid_latches_despite_coherent_answers_about_others(db, tmp_path):
    # The realistic shape: the venue misroutes OUR SL cloid but answers the TP
    # cloid coherently. Streaks are per cloid (issue #80 round-1 decision), so
    # the coherent answers about the OTHER order neither reset the misrouted
    # one's count nor stop the latch — under a process-wide streak this fault
    # flapped 1→0 forever and the treadmill ran unbounded. Evidence stays one
    # file per cloid regardless.
    payload_dir = tmp_path / "payloads"
    clock = ManualClock(_NOW)
    script = [misrouted_order_status(), _unknown()] * K
    monitor = _monitor(db, _Venue(script), payload_dir=payload_dir, clock=clock)
    for i in range(K):
        assert monitor.latched is False
        clock.advance(1)
        _probe_expecting_unreadable(monitor, cloid=_OURS)
        clock.advance(1)
        assert monitor.probe(_OURS_2, site=ProbeSite.RECONCILE_ABSENT_SETTLE) is None
        assert monitor.unreadable_streak == i + 1  # untouched by the other cloid
    assert monitor.latched is True
    (row,) = identity_latch_rows(db)
    assert row["cloid_hex"] == _OURS
    assert len(list(payload_dir.glob("orderStatus-*.json"))) == 1


def test_two_cloids_below_threshold_do_not_latch_in_aggregate(db):
    # ``latched`` is the WORST cloid's streak, never a sum across cloids: two
    # orders each three answers deep must not read as one fault of six — that
    # would let ordinary multi-order flakiness impersonate the identity fault.
    venue = _Venue()
    venue.by_cloid = {_OURS: misrouted_order_status("1"), _OURS_2: misrouted_order_status("2")}
    monitor = _monitor(db, venue)
    for _ in range(K - 2):
        _probe_expecting_unreadable(monitor, cloid=_OURS)
        _probe_expecting_unreadable(monitor, cloid=_OURS_2)
    assert monitor.unreadable_streak == K - 2
    assert monitor.latched is False
    assert identity_latch_rows(db) == []


def test_a_readable_answer_resets_only_its_own_cloids_streak(db):
    venue = _Venue()
    venue.by_cloid = {_OURS: misrouted_order_status("1")}
    monitor = _monitor(db, venue)
    for _ in range(K - 1):
        _probe_expecting_unreadable(monitor, cloid=_OURS)
    assert (
        monitor.probe(_OURS_2, site=ProbeSite.RECONCILE_ABSENT_SETTLE) is None
    )  # coherent, other cloid
    assert monitor.unreadable_streak == K - 1  # _OURS keeps its count
    _probe_expecting_unreadable(monitor, cloid=_OURS)
    assert monitor.latched is True


def test_a_failed_latch_row_write_drops_neither_the_latch_nor_the_raise(db):
    monitor = _monitor(db, _Venue([misrouted_order_status() for _ in range(K)]))

    def _busy(**_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monitor._record_latch = _busy  # type: ignore[method-assign]
    for _ in range(K):
        # Still the parser's error, not the DB's: the consumer's except lane
        # must keep seeing the fault it knows how to handle.
        _probe_expecting_unreadable(monitor)
    assert monitor.latched is True
    assert identity_latch_rows(db) == []


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
    monitor = _monitor(db, _Venue([misrouted_order_status() for _ in range(K)]))

    for _ in range(K - 1):
        _probe_expecting_unreadable(monitor)
    assert (
        escalate_identity_fault(monitor, safe_mode, site=EscalationHolder.PROTECTION_SYNC) is False
    )
    assert safe_mode.current() is None
    assert gate.manual_safe_mode is False

    _probe_expecting_unreadable(monitor)
    assert escalate_identity_fault(monitor, safe_mode, site=EscalationHolder.SHUTDOWN) is True
    state = safe_mode.current()
    assert state is not None and state.is_manual and state.reason == REASON_IDENTITY_FAULT
    assert gate.manual_safe_mode is True
    # Re-read by the next holder: no second history row, same episode.
    assert (
        escalate_identity_fault(
            monitor, safe_mode, site=EscalationHolder.RECONCILIATION, trigger="heartbeat"
        )
        is True
    )
    entered = [
        e for e in repo.iter_safe_mode_events(db.conn, "r") if e["reason"] == REASON_IDENTITY_FAULT
    ]
    assert len(entered) == 1
    assert "§18.2 shutdown" in (entered[0]["detail"] or "")


def test_a_persisted_shutdown_escalation_is_what_the_next_boot_hydrates(db):
    # The kill-switch site's whole point (issue #80): the process that found
    # the fault is exiting, so the bound has to outlive it. A FRESH manager —
    # what the next ``live --run-id`` builds — hydrates the manual state.
    monitor = _monitor(db, _Venue([misrouted_order_status() for _ in range(K)]))
    for _ in range(K):
        _probe_expecting_unreadable(monitor)
    escalate_identity_fault(
        monitor, SafeModeManager(db=db, run_id="r", gate=_gate()), site=EscalationHolder.SHUTDOWN
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
    """The acceptance shape for #80: consumers alternate, the order is one.

    One locally-live order the venue misroutes every time. The reconciler's
    settle probe counts one per pass (two passes), the shutdown cross-check
    the third, and protection's no-op guard the fourth and fifth — the latch
    rises from a per-cloid streak no single consumer reached alone at K=5,
    and its row names the consumer that crossed the line. Each consumer's own
    audit text names the fault by family.
    """
    assert K == 5, "the choreography below is written for a threshold of five"
    clock = ManualClock(_NOW)
    client = _Client(clock)
    client.by_cloid = {_OURS: misrouted_order_status("1")}
    monitor = _monitor(db, client, payload_dir=tmp_path / "payloads", clock=clock)
    _live_order(db, order_id="o1", cloid_hex=_OURS)

    reconciler = _reconciler(db, client, monitor, tmp_path)
    for expected in (1, 2):  # two passes: the streak carries across passes
        report = reconciler.run("heartbeat")
        (settle_case,) = [c for c in report.cases if c.case_type == "order_missing_on_exchange"]
        assert "answered unusably (venue identity fault)" in settle_case.detail
        assert monitor.unreadable_streak == expected
        clock.advance(60)
    assert monitor.latched is False

    switch = _kill_switch(db, client, monitor, clock, tmp_path)
    switch.arm()
    switch.shutdown()
    assert switch.armed  # unchanged fail-safe: an unconfirmable order blocks the disarm
    completed = [
        e
        for e in repo.iter_kill_switch_events(db.conn, "r")
        if e["event_type"] == "shutdown_cancel_orders_completed"
    ][0]
    (failure,) = json.loads(completed["detail"])["failures"]
    assert "could not confirm settled" in failure
    assert "answered unusably (venue identity fault)" in failure
    assert monitor.unreadable_streak == 3
    assert monitor.latched is False
    assert identity_latch_rows(db) == []

    clock.advance(60)
    protection = _protection(db, client, monitor)
    assert protection._row_still_rests({"cloid_hex": _OURS}, role="stop_loss") is False
    assert monitor.unreadable_streak == 4
    assert protection._row_still_rests({"cloid_hex": _OURS}, role="stop_loss") is False
    assert monitor.latched is True
    assert protection.identity.latched is True  # the engine's read: the same monitor
    (row,) = identity_latch_rows(db)
    assert "protection stop_loss no-op guard" in row["detail"]
    assert monitor.latched_site == "protection stop_loss no-op guard"
    # One cloid refused, one file — whichever consumer's refusal came first.
    assert len(list((tmp_path / "payloads").glob("orderStatus-*.json"))) == 1


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
    client.by_cloid = {_OURS: misrouted_order_status()}
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


def test_the_reconciler_escalates_after_its_own_safe_mode_bookkeeping(db, tmp_path):
    # Pins the "run last" ordering: the pass's own recoverable entry must land
    # before the identity escalation, so an ``enter`` that dies on a busy DB
    # cannot cost the pass its mismatch bookkeeping. Moving the escalation
    # ahead of the verdict branch flips this recorded order.
    client = _Client(ManualClock(_NOW))
    client.by_cloid = {_OURS: misrouted_order_status()}
    monitor = _monitor(db, client, payload_dir=tmp_path / "payloads")
    for _ in range(K):
        _probe_expecting_unreadable(monitor, site=ProbeSite.PROTECTION_NOOP_GUARD, role="stop_loss")
    assert monitor.latched is True
    _live_order(db, order_id="o1", cloid_hex=_OURS)

    entered: list[tuple[str, str]] = []

    class _Recording(SafeModeManager):
        def enter(self, safe_mode_type, reason, *, detail=None):
            entered.append((safe_mode_type, reason))
            return super().enter(safe_mode_type, reason, detail=detail)

    safe_mode = _Recording(db=db, run_id="r", gate=_gate(), clock=ManualClock(_NOW))
    _reconciler(db, client, monitor, tmp_path).reconcile_and_apply(
        "heartbeat", safe_mode=safe_mode, ws_restored=True, kill_switch_active=True
    )
    assert entered[0][0] == "recoverable"  # the pass's own verdict, first
    assert entered[-1] == ("manual", REASON_IDENTITY_FAULT)  # the escalation, last


def test_a_failed_evidence_write_is_retried_and_the_latch_row_admits_the_gap(
    db, tmp_path, monkeypatch
):
    # A payload_dir that stops accepting writes (disk full, permissions) is
    # likeliest DURING an incident: the write must be retried on the next
    # refusal instead of silently given up, and a latch row whose file never
    # landed must say so rather than reading as "capture working, nothing
    # kept" (2026-08-27 round-1 review).
    import contrib.hyperliquid_perp.live.venue_identity as vi_mod

    calls: list[str] = []

    def _flaky_write(*, payload_dir, kind, key, payload, now, once=False):
        calls.append(key)
        return None  # the writer's own contract: None = could not write

    monkeypatch.setattr(vi_mod, "write_raw_payload", _flaky_write)
    monitor = _monitor(
        db, _Venue([misrouted_order_status() for _ in range(K)]), payload_dir=tmp_path / "p"
    )
    for _ in range(K):
        _probe_expecting_unreadable(monitor)
    assert len(calls) == K  # every refusal retried the write
    (row,) = identity_latch_rows(db)
    assert "payload capture FAILED" in row["detail"]

    # ...and once a write finally lands, the row's clause names the file.
    monkeypatch.setattr(vi_mod, "write_raw_payload", lambda **kw: "payloads/orderStatus-x.json")
    monitor2 = _monitor(
        db, _Venue([misrouted_order_status() for _ in range(K)]), payload_dir=tmp_path / "p"
    )
    for _ in range(K):
        _probe_expecting_unreadable(monitor2)
    assert any(
        "payloads/orderStatus-x.json" in (r["detail"] or "") for r in identity_latch_rows(db)
    )


def test_a_reconcile_pass_that_latches_first_enters_manual_with_the_identity_reason(db, tmp_path):
    # The other ordering: a streak the loop's other consumers built up is
    # finished by ONE reconcile pass — no unclean-pass history behind it — so
    # the reconciler's escalation is the FIRST manual entry, under its own reason.
    client = _Client(ManualClock(_NOW))
    client.by_cloid = {_OURS: misrouted_order_status()}
    monitor = _monitor(db, client, payload_dir=tmp_path / "payloads")
    for _ in range(K - 1):
        _probe_expecting_unreadable(monitor, site=ProbeSite.PROTECTION_NOOP_GUARD, role="stop_loss")
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
    client.by_cloid = {_OURS: misrouted_order_status()}
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
    (row,) = identity_latch_rows(db)
    assert "reconcile absent-order settle" in row["detail"]
    assert len(list((tmp_path / "payloads").glob("orderStatus-*.json"))) == 1


# -- the closed site vocabulary (issue #132) -------------------------------------
#
# The site is the operator's only handle on WHERE the fault was seen — it is
# what the latch row, the safe-mode detail and the RUNBOOK's triage steps key
# on — so it is a closed set, checked at the call, not a free string that the
# next consumer spells its own way.


def test_an_unregistered_probe_site_is_refused_before_any_round_trip(db):
    venue = _Venue([misrouted_order_status()])
    monitor = _monitor(db, venue)
    with pytest.raises(ValueError):
        monitor.probe(_OURS, site="kill-switch disarm crosscheck")  # a typo, not a member
    with pytest.raises(ValueError):
        # A RENDERED label is not a member either: the role is a separate field.
        monitor.probe(_OURS, site="protection stop_loss no-op guard")
    assert venue.asked == []  # refused at the call, whatever the venue would have said
    assert monitor.unreadable_streak == 0


def test_a_sites_dynamic_field_must_pair_with_its_member(db):
    venue = _Venue()
    monitor = _monitor(db, venue)
    with pytest.raises(ValueError, match="needs role"):
        monitor.probe(_OURS, site=ProbeSite.PROTECTION_NOOP_GUARD)
    with pytest.raises(ValueError, match="takes no role"):
        monitor.probe(_OURS, site=ProbeSite.KILL_SWITCH_DISARM_CROSS_CHECK, role="stop_loss")
    with pytest.raises(ValueError, match="role must be one of"):
        monitor.probe(_OURS, site=ProbeSite.PROTECTION_RECOVERY_PROBE, role="entry")
    # (The rendered labels themselves are pinned where the rows are written —
    # the consumer tests above assert the exact pre-#132 strings.)


def test_an_unregistered_escalation_holder_is_refused_even_with_nothing_latched(db):
    safe_mode = SafeModeManager(db=db, run_id="r", gate=_gate(), clock=ManualClock(_NOW))
    monitor = _monitor(db, _Venue())
    with pytest.raises(ValueError):
        escalate_identity_fault(monitor, safe_mode, site="§18.2 shutdown")  # not a member
    with pytest.raises(ValueError, match="needs trigger"):
        escalate_identity_fault(monitor, safe_mode, site=EscalationHolder.RECONCILIATION)
    with pytest.raises(ValueError, match="trigger must be one of"):
        escalate_identity_fault(
            monitor, safe_mode, site=EscalationHolder.RECONCILIATION, trigger="hearbeat"
        )
    assert safe_mode.current() is None


def test_the_runbook_site_table_is_the_vocabulary():
    # The RUNBOOK's ``venue_identity_fault`` table and the two enums are ONE
    # list: a member added without its row, or a row without its member, is red.
    import re

    rows = re.findall(
        r"^\| (probe|holder) \| `([^`]+)` \|$", doc_text("RUNBOOK-live.md"), flags=re.MULTILINE
    )
    assert {label for family, label in rows if family == "probe"} == {m.value for m in ProbeSite}
    assert {label for family, label in rows if family == "holder"} == {
        m.value for m in EscalationHolder
    }


def test_the_seam_must_be_callable_at_construction(db):
    with pytest.raises(TypeError, match="orderStatus seam"):
        VenueIdentityMonitor(query_order_by_cloid=_Venue(), db=db, run_id="r", symbol="BTC")
