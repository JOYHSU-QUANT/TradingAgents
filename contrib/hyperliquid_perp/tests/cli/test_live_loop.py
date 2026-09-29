"""Tests for ``_run_live_loop`` and the lease checks around it."""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from contrib.hyperliquid_perp.cli import _live_heartbeat, _still_owns_run
from contrib.hyperliquid_perp.domains.perp.risk_gate import DecisionConfig, RiskConfig
from contrib.hyperliquid_perp.domains.perp.schema import TopOfBook
from contrib.hyperliquid_perp.integration import decision_provider as decision_provider_mod
from contrib.hyperliquid_perp.persistence import repository as repo
from contrib.hyperliquid_perp.persistence.db import Database
from contrib.hyperliquid_perp.persistence.models import PositionState
from contrib.hyperliquid_perp.persistence.schema import SCHEMA_VERSION
from contrib.hyperliquid_perp.runtime import accounting

from ..fakes.gates import order_gate
from ..fakes.market import margin_schedule
from ..fakes.payloads import clearinghouse
from .conftest import BAD_ENGINE_ENV_KNOBS, StopBeforeTheLoop, assert_position_source_binds

D = Decimal
_T0 = datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# _live_heartbeat: the §18.2 live-loop lease heartbeat's containment contract
# --------------------------------------------------------------------------


class _RecordingSafeMode:
    """Records enter() calls; optionally raises (the safe-mode write can fail too)."""

    def __init__(self, raises: bool = False) -> None:
        self.raises = raises
        self.entered: list[tuple[tuple, dict]] = []

    def enter(self, *args, **kwargs):
        self.entered.append((args, kwargs))
        if self.raises:
            raise RuntimeError("safe-mode write failed")
        return True


def test_live_heartbeat_run_lock_error_stays_fatal(monkeypatch):
    # The pid fence (RunLockError: this process was superseded by a newer one)
    # must PROPAGATE — two writers must never flip-flop the lease — and must
    # not be softened into a safe-mode entry.
    from contrib.hyperliquid_perp.runtime import run_lock as run_lock_mod

    def fenced(db_, run_id, *, pid, now):
        raise run_lock_mod.RunLockError("superseded by a newer process")

    monkeypatch.setattr(run_lock_mod, "heartbeat_run_lock", fenced)
    safe_mode = _RecordingSafeMode()
    with pytest.raises(run_lock_mod.RunLockError):
        _live_heartbeat(object(), "r", pid=123, now=_T0, safe_mode=safe_mode)
    assert safe_mode.entered == []


def test_live_heartbeat_transient_failure_is_contained_in_safe_mode(monkeypatch):
    # Any OTHER heartbeat failure (a busy SQLite store, say) is contained: no
    # raise — tearing the loop down would run the §18.2 shutdown sweep and
    # strip the resting SL/TP — and exactly one recoverable safe-mode entry
    # with the live-tick-error reason.
    from contrib.hyperliquid_perp.live.safe_mode import REASON_LIVE_TICK_ERROR
    from contrib.hyperliquid_perp.runtime import run_lock as run_lock_mod

    def busy(db_, run_id, *, pid, now):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(run_lock_mod, "heartbeat_run_lock", busy)
    safe_mode = _RecordingSafeMode()
    _live_heartbeat(object(), "r", pid=123, now=_T0, safe_mode=safe_mode)  # no raise
    assert len(safe_mode.entered) == 1
    args, kwargs = safe_mode.entered[0]
    assert args == ("recoverable", REASON_LIVE_TICK_ERROR)
    assert kwargs.get("detail")


def test_live_heartbeat_contains_a_failing_safe_mode_write(monkeypatch):
    # The containment must not depend on the safe-mode write succeeding: a
    # store busy enough to fail the heartbeat can fail that write too, and a
    # raise from EITHER must not end the loop.
    from contrib.hyperliquid_perp.runtime import run_lock as run_lock_mod

    def busy(db_, run_id, *, pid, now):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(run_lock_mod, "heartbeat_run_lock", busy)
    safe_mode = _RecordingSafeMode(raises=True)
    _live_heartbeat(object(), "r", pid=123, now=_T0, safe_mode=safe_mode)  # still no raise
    assert len(safe_mode.entered) == 1


# --------------------------------------------------------------------------
# _still_owns_run: the positive re-check guarding the §18.2 shutdown sweep
# --------------------------------------------------------------------------


def test_still_owns_run_false_once_a_successor_holds_the_lease(tmp_path):
    # The shutdown sweep's ``superseded`` flag is absence-of-evidence: it is set
    # only when the loop's heartbeat actually RAISED, and the Ctrl-C / SIGTERM
    # lane leaves the loop with no heartbeat at all. So after a tick that blocked
    # past LOCK_STALE_SECONDS a successor can already own the run while this
    # process still reads ``superseded is False`` — and the §18.2 sweep would
    # then cancel the SUCCESSOR's SL/TP and clear the wallet's dead-man switch.
    # This is the re-ASK that catches it.
    from contrib.hyperliquid_perp.runtime.run_lock import LOCK_STALE_SECONDS, acquire_run_lock

    db = Database(tmp_path / "own.db")
    acquire_run_lock(db, "r", pid=101, now=_T0)
    takeover_at = _T0 + timedelta(seconds=LOCK_STALE_SECONDS)
    acquire_run_lock(db, "r", pid=202, now=takeover_at)  # legitimate takeover
    assert _still_owns_run(db, "r", pid=101, now=takeover_at + timedelta(seconds=30)) is False
    row = repo.get_scheduler_state(db.conn, "r")
    assert row["lock_pid"] == 202  # and the loser must not stamp itself back on
    assert row["lock_heartbeat_at"] == takeover_at.isoformat()
    db.close()


def test_still_owns_run_true_for_the_holder_and_refreshes_the_lease(tmp_path):
    # The ordinary shutdown: the lease is ours, the sweep must run. The refresh
    # is not incidental — the check goes through heartbeat_run_lock, so the
    # lease stays warm for however long the sweep's cancels take on the wire.
    from contrib.hyperliquid_perp.runtime.run_lock import acquire_run_lock

    db = Database(tmp_path / "own.db")
    acquire_run_lock(db, "r", pid=101, now=_T0)
    beat = _T0 + timedelta(seconds=60)
    assert _still_owns_run(db, "r", pid=101, now=beat) is True
    assert repo.get_scheduler_state(db.conn, "r")["lock_heartbeat_at"] == beat.isoformat()
    db.close()


def test_still_owns_run_fails_open_when_the_store_blips(tmp_path, caplog, monkeypatch):
    # Deliberately fail-OPEN, the opposite of _live_heartbeat's containment: a
    # busy/locked SQLite store is not evidence of supersession, and treating it
    # as one would skip the §18.2 sweep and strand this run's own resting orders
    # on the wallet with nothing left to cancel them. Only RunLockError — a
    # positive answer that someone else holds the lease — gives the run away.
    from contrib.hyperliquid_perp.runtime import run_lock as run_lock_mod

    def busy(db_, run_id, *, pid, now):
        raise sqlite3.OperationalError("database is locked")

    db = Database(tmp_path / "own.db")
    run_lock_mod.acquire_run_lock(db, "r", pid=101, now=_T0)
    monkeypatch.setattr(run_lock_mod, "heartbeat_run_lock", busy)
    with caplog.at_level("WARNING"):
        assert _still_owns_run(db, "r", pid=101, now=_T0 + timedelta(seconds=60)) is True
    assert "could not re-verify the run lease" in caplog.text  # never silent
    db.close()


# --------------------------------------------------------------------------
# _run_live_loop
# --------------------------------------------------------------------------


def test_a_locked_store_at_startup_adoption_still_reaches_the_loop(tmp_path, monkeypatch):
    """issue #180: a raising ``resume_startup`` must not stop the loop starting.

    ``resume_startup`` runs BEFORE the loop, so its own fail-closed write
    meeting a locked store (an operator's export/validate) used to exit the
    daemon — and the supervisor's restart can meet the same lock, with the real
    position and its resting SL/TP unwatched in between while systemd's
    StartLimitBurst counts down.

    Behavioural, not structural (issue #206). The AST pin this replaces proved
    the call sat in a broad try that reached the containment idiom, and was
    blind to the mutation that matters most: a ``return`` after that call
    passes every one of those assertions and reinstates exactly the #180
    failure — contained, safe mode entered, and no loop. So this drives the
    real function until the loop BODY runs, and asserts both halves: the run is
    in safe mode (containment happened) AND the engine's first tick was reached
    (the loop started anyway).
    """
    from contrib.hyperliquid_perp.live.safe_mode import REASON_LIVE_TICK_ERROR, SafeModeManager

    built = _drive_live_loop_construction(
        tmp_path,
        monkeypatch,
        fetch_clearinghouse=lambda: clearinghouse(),
        adoption_raises=sqlite3.OperationalError("database is locked"),
    )
    assert built.ticks == 1, "the loop body never ran — the daemon stopped at adoption"
    db = Database(built.db_path)
    try:
        state = SafeModeManager(db=db, run_id="r1", gate=None).current()
        assert state is not None, "a raising startup adoption paused no new risk"
        # A LOCKED store heals by itself, so the latch must be the recoverable
        # one: the next clean reconciliation releases it with no human.
        assert not state.is_manual
        assert state.reason == REASON_LIVE_TICK_ERROR
    finally:
        db.close()


def test_an_unhealable_startup_adoption_latches_manual_safe_mode(tmp_path, monkeypatch):
    """issue #205: the wedge lane latches MANUAL and keeps watching the position.

    ``find_in_progress_attempt`` raises ``ValueError`` when one run has two
    ``in_progress`` attempts — the repository's deliberate fail-loud, meaning
    the decision state machine broke. ``_adopt`` re-reads that same row every
    tick, so the raise repeats identically forever: containing it as a
    RECOVERABLE safe mode (the #180 lane) left a run that looked alive, could
    never decide again, and auto-released its own latch on the next clean
    reconciliation pass.

    Both halves are asserted here too: MANUAL (no clean pass releases it, and
    §13.1 blocks risk-adding orders until a human does) AND the loop body still
    running, because exiting would hand the supervisor a restart that meets the
    same deterministic raise until StartLimitBurst gives up — leaving the
    position with resting SL/TP and nothing refreshing the kill switch.
    """
    from contrib.hyperliquid_perp.live.safe_mode import REASON_ADOPTION_WEDGED, SafeModeManager

    built = _drive_live_loop_construction(
        tmp_path,
        monkeypatch,
        fetch_clearinghouse=lambda: clearinghouse(),
        adoption_raises=ValueError("run 'r1' has 2 in-progress attempts (a, b)"),
    )
    assert built.ticks == 1, "the loop body never ran — the position stopped being watched"
    db = Database(built.db_path)
    try:
        state = SafeModeManager(db=db, run_id="r1", gate=None).current()
        assert state is not None and state.is_manual, (
            "an adoption failure that cannot heal left a latch a clean reconcile releases"
        )
        assert state.reason == REASON_ADOPTION_WEDGED
    finally:
        db.close()


def test_an_unclassified_startup_adoption_raise_still_reaches_the_loop(tmp_path, monkeypatch):
    """The boot handler must stay as broad as ``Exception`` (issue #206).

    The AST pin these tests replaced asserted the handler's BREADTH, and the
    two lanes above cannot: one drives ``sqlite3.OperationalError`` and the
    other drives a ``ValueError`` that ``resume_startup`` converts, so it is
    caught by the dedicated ``AdoptionWedgedError`` branch. Narrowing the
    generic handler to ``except sqlite3.OperationalError`` would pass both.

    That narrowing is not hypothetical. ``resume_startup`` deliberately
    re-raises the ORIGINAL, unclassified exception when ``_adopt`` had already
    armed a fail record, and that exception comes from a store write — it can
    be any ``sqlite3`` error or a plain ``RuntimeError``. Under a narrowed
    handler that lane would exit the daemon at boot: issue #180 exactly, with a
    real position on resting SL/TP and StartLimitBurst counting down.
    """
    from contrib.hyperliquid_perp.live.safe_mode import SafeModeManager

    built = _drive_live_loop_construction(
        tmp_path,
        monkeypatch,
        fetch_clearinghouse=lambda: clearinghouse(),
        adoption_raises=RuntimeError("the fail record's own write blew up"),
        arm_pending_fail=True,
    )
    assert built.ticks == 1, "an unclassified adoption raise ended the daemon before the loop"
    db = Database(built.db_path)
    try:
        state = SafeModeManager(db=db, run_id="r1", gate=None).current()
        # Recoverable, not manual: an armed fail record has its own retry lane,
        # so this is not a wedge however unusual the exception type is.
        assert state is not None and not state.is_manual
    finally:
        db.close()


def _contained_details(db_path) -> list[str]:
    """The durable ``detail`` of every safe-mode event a drive left behind."""
    db = Database(db_path)
    try:
        with db.transaction() as conn:
            return [row["detail"] for row in repo.iter_safe_mode_events(conn, "r1")]
    finally:
        db.close()


def test_a_raising_pump_is_contained_as_the_pump_and_not_as_the_tick(tmp_path, monkeypatch):
    """issue #238 review: the durable containment record names the failing half.

    Both halves share one containment, and its ``detail`` said "live tick
    raised" whatever had failed — so a pump failure was filed against the tick.
    That string outlives the journal: safe mode stores it, and `safe-mode
    --status` and validate read it back with no traceback beside it, which is
    the one moment an operator has to tell a tick fault from a decision-driver
    one. The reason stays REASON_LIVE_TICK_ERROR — the lane and its recovery are
    the same, only the record was wrong.
    """
    from contrib.hyperliquid_perp.live.engine import LiveTickResult, TickStatus
    from contrib.hyperliquid_perp.live.safe_mode import REASON_LIVE_TICK_ERROR, SafeModeManager

    did_something = LiveTickResult(
        at=_T0, status=TickStatus.OK, fills_ingested=2, slices_submitted=1
    )
    built = _drive_live_loop_construction(
        tmp_path,
        monkeypatch,
        fetch_clearinghouse=lambda: clearinghouse(),
        tick_results=(did_something,),
        pump_raises=sqlite3.OperationalError("database is locked"),
    )
    assert built.ticks == 2, "the contained pump raise ended the loop instead of ticking again"
    assert _contained_details(built.db_path) == ["live decision pump raised (see log)"]

    db = Database(built.db_path)
    try:
        state = SafeModeManager(db=db, run_id="r1", gate=None).current()
        assert state is not None, "the raising pump paused no new risk"
        assert not state.is_manual and state.reason == REASON_LIVE_TICK_ERROR
    finally:
        db.close()


def test_a_raising_tick_is_still_contained_as_the_tick(tmp_path, monkeypatch):
    """The other half of the phase marker: it must not be set too early.

    Moving ``phase = "decision pump"`` above ``engine.tick()`` would file every
    tick fault against the driver and pass the sibling test above, so this lane
    pins the original wording — which the RUNBOOK's journald evidence quotes.
    """
    built = _drive_live_loop_construction(
        tmp_path,
        monkeypatch,
        fetch_clearinghouse=lambda: clearinghouse(),
        tick_results=(sqlite3.OperationalError("database is locked"),),
    )
    assert built.ticks == 2, "the contained tick raise ended the loop instead of ticking again"
    assert _contained_details(built.db_path) == ["live tick raised (see log)"]


def test_the_live_loop_refreshes_the_switch_between_the_tick_and_the_pump(tmp_path, monkeypatch):
    """§18.2: one refresh of THIS run's switch sits between the two blocking halves.

    ``engine.tick()`` and ``driver.pump()`` each run a chain of REST calls on
    the loop's thread. Adjacent, the unrefreshed run is their sum, not the max
    ``_MAX_UNREFRESHED_REST_CALLS`` records.
    """
    from contrib.hyperliquid_perp.live.engine import LiveTickResult, TickStatus

    built = _drive_live_loop_construction(
        tmp_path,
        monkeypatch,
        fetch_clearinghouse=lambda: clearinghouse(),
        tick_results=(LiveTickResult(at=_T0, status=TickStatus.OK),),
        pump_raises=sqlite3.OperationalError("database is locked"),
    )
    (at_pump,) = built.refreshes_at_pump
    assert at_pump == built.refreshes_at_tick[0] + 1


class _StopTheLoop(BaseException):
    """Sentinel raised from the first ``engine.tick()`` to end a loop-body drive.

    A ``BaseException``: the loop's tick guard catches ``Exception`` and keeps
    ticking (that is the behaviour under test elsewhere), so an ordinary
    exception here would spin forever instead of ending the drive — and would
    enter safe mode itself, contaminating the very state the adoption tests
    read. ``KeyboardInterrupt`` is a BaseException for the same reason, which
    is why the loop's Ctrl-C handler sits outside that guard; this sentinel is
    NOT that class, so it passes straight out rather than running the §18.2
    shutdown path on its way.
    """


def _drive_live_loop_construction(
    tmp_path,
    monkeypatch,
    *,
    fetch_clearinghouse,
    adoption_raises=None,
    arm_pending_fail=False,
    pump_raises=None,
    tick_results=(),
    provider_raises=None,
    initial_positions=(),
    expect_return=False,
):
    """Build ``_run_live_loop``'s components and stop; return what it built with.

    ``provider_raises`` makes the provider's construction raise it (the
    bridge's startup gate refusing an env knob, issue #268) instead of
    stopping the drive; ``initial_positions`` seeds the run's books so the
    engine has live work. Together they drive the protection-only lane —
    ``expect_return`` then says the loop is expected to RETURN (its
    settle-exit) rather than end on a sentinel, and ``built.returned`` holds
    what it returned. A refusing provider over a FLAT book propagates out of
    the loop, so that drive ends on the refusal itself.

    With ``adoption_raises`` or ``pump_raises`` the drive goes FURTHER, into the
    loop body: the provider stops being the stopping point and becomes a stub,
    the run lease is seeded for this pid so the loop's heartbeat does not fatally
    disown it, and ``engine.tick()`` raises :class:`_StopTheLoop` to end the
    drive. ``built.ticks`` then counts the tick calls the loop actually reached
    and ``built.db_path`` locates the store for the durable assertions.
    ``built.refreshes_at_tick`` holds the switch's refresh count as each
    ``engine.tick()`` began; under ``pump_raises``, ``built.refreshes_at_pump``
    holds it as each ``driver.pump()`` began.

    ``adoption_raises`` makes the driver's ``_adopt`` raise it, so the real
    ``resume_startup`` classification and the real containment both run.
    ``pump_raises`` makes ``driver.pump()`` raise instead, leaving adoption on
    its ordinary clean path (no in-progress row, so it reads the store and
    returns). ``tick_results`` feeds the leading ticks one entry each before the
    sentinel ends the drive — a :class:`LiveTickResult` is returned, an exception
    instance is raised — so an iteration can be driven past its first tick and
    into the pump (issue #238 review). A drive that survives its first tick
    reaches the cadence sleep at the bottom of the body, so ``_LIVE_TICK_SECONDS``
    is zeroed rather than waiting the real 10s.

    The construction block is straight-line, it is where the three safety kwargs
    below are decided, and it can simply be driven. The drive stops at
    ``_EngineDecisionProvider``, the last of the three. It is not pure
    construction by then: the block reads the store from the ledger lookup
    onwards, runs ``ensure_settlement_anchor``, and builds the real
    ``LiveExecutionEngine`` (whose own ``__init__`` reads the position) before
    running its ``settle_offline_flat()`` pass — which is why the drive needs a
    real store rather than a double. Only the worker, the driver and the
    driver's ``resume_startup()`` come after; none of them is modelled here.

    The recorders subclass the real classes and construct THROUGH them wherever
    the real constructor runs offline, so a call site that drifts from a
    signature dies here instead of being recorded as fine. The exception is
    ``_EngineDecisionProvider``, whose ``__init__`` builds a whole engine
    config: its arguments are bound against the real signature instead.
    """
    import inspect

    from contrib.hyperliquid_perp import cli as cli_mod
    from contrib.hyperliquid_perp.exchanges.hyperliquid import market_data as md_mod
    from contrib.hyperliquid_perp.live import loss_guards as lg_mod, protection as prot_mod
    from contrib.hyperliquid_perp.live.config import LiveConfig
    from contrib.hyperliquid_perp.live.safe_mode import SafeModeManager
    from contrib.hyperliquid_perp.live.venue_identity import VenueIdentityMonitor

    reach_loop_body = (
        adoption_raises is not None
        or pump_raises is not None
        or bool(tick_results)
        or (provider_raises is not None and bool(initial_positions))
    )

    class _FakeMarket:
        def __init__(self, _client):
            pass

        def get_top_of_book(self, coin):
            # The maker slice's quote seam (§9.2.1), wired even under the
            # default taker style; never read by these loop tests.
            from datetime import datetime, timezone

            return TopOfBook(
                coin=coin,
                best_bid=D(49990),
                best_ask=D(50010),
                time=datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc),
            )

        def get_asset_meta(self, coin):
            return 3, margin_schedule(coin)

    class _FakeSwitch:
        """The refresh surface ``refresh_across_blocking_work`` reaches for."""

        def __init__(self):
            self.ticks = 0

        def tick(self):
            self.ticks += 1

    built = SimpleNamespace(
        protection=None,
        guards=None,
        provider=None,
        kill_switch=_FakeSwitch(),
        identity=None,
        ticks=0,
        refreshes_at_tick=[],
        refreshes_at_pump=[],
        db_path=None,
        returned=None,
    )

    def _recorder(module, name, field):
        real = getattr(module, name)

        class _Recording(real):  # type: ignore[misc, valid-type]
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                setattr(built, field, kwargs)

        monkeypatch.setattr(module, name, _Recording)

    real_provider = cli_mod._EngineDecisionProvider

    class _RecordingProvider:
        def __init__(self, *args, **kwargs):
            # Bound, not constructed: the real __init__ builds a whole engine
            # config. Binding still fails a call site that drifts from the
            # signature, which is the failure the other recorders get from
            # constructing through the real class.
            inspect.signature(real_provider).bind(*args, **kwargs)
            built.provider = kwargs
            if provider_raises is not None:
                raise provider_raises
            if not reach_loop_body:
                raise StopBeforeTheLoop
            # Loop-body drive: stand in for the provider instead of stopping.
            # Nothing calls it — the tick sentinel fires before the first pump.

    monkeypatch.setattr(md_mod, "HyperliquidMarketData", _FakeMarket)
    monkeypatch.setattr(decision_provider_mod, "EngineDecisionProvider", _RecordingProvider)
    _recorder(prot_mod, "ProtectionManager", "protection")
    _recorder(lg_mod, "LossGuards", "guards")

    dbp = tmp_path / "live.db"
    built.db_path = dbp
    db = Database(dbp)
    accounting.initialize_run(
        db,
        run_id="r1",
        mode="live",
        initial_balance_usdc=D(200),
        schema_version=SCHEMA_VERSION,
        initial_positions=list(initial_positions),
    )
    if reach_loop_body:
        from contrib.hyperliquid_perp.live.decision import LiveDecisionDriver
        from contrib.hyperliquid_perp.live.engine import LiveExecutionEngine

        def _raising_adopt(self):
            if arm_pending_fail:
                # The poisoned-response lane: _adopt armed the fail record and
                # then its WRITE raised, so resume_startup re-raises the
                # original, unclassified exception. Built through the real
                # in-flight object so the shape is the production one.
                from contrib.hyperliquid_perp.live.decision import _InFlight

                self._inflight = _InFlight.for_try("a-1", datetime(2026, 7, 20, tzinfo=timezone.utc), 1)
                self._inflight.arm_fail(None, "non-retryable: boom")
            raise adoption_raises

        def _stop(self, *args, **kwargs):
            built.ticks += 1
            built.refreshes_at_tick.append(built.kill_switch.ticks)
            if built.ticks <= len(tick_results):
                queued = tick_results[built.ticks - 1]
                if isinstance(queued, BaseException):
                    raise queued
                return queued
            raise _StopTheLoop

        def _raising_pump(self, *args, **kwargs):
            built.refreshes_at_pump.append(built.kill_switch.ticks)
            raise pump_raises

        # _adopt, not resume_startup: the classification that decides
        # recoverable-vs-manual containment lives IN resume_startup, so
        # patching that away would leave the tests asserting the CLI's
        # dispatch over a verdict the test made up.
        if adoption_raises is not None:
            monkeypatch.setattr(LiveDecisionDriver, "_adopt", _raising_adopt)
        if pump_raises is not None:
            monkeypatch.setattr(LiveDecisionDriver, "pump", _raising_pump)
        monkeypatch.setattr(LiveExecutionEngine, "tick", _stop)
        # The body sleeps out the rest of the cadence after each contained tick,
        # so a drive that survives its first tick would wait the real 10s.
        monkeypatch.setattr(cli_mod.live_loop, "_LIVE_TICK_SECONDS", 0.0)
        # The loop heartbeats BEFORE its first tick and treats a lost lease as
        # fatal, so an unseeded lock_pid would end the drive for an unrelated
        # reason and read as "the loop body never ran".
        with db.transaction() as conn:
            repo.upsert_scheduler_state(conn, "r1", lock_pid=os.getpid())
    live_cfg = LiveConfig.from_dict(
        {
            "mode": "testnet_live",
            "network": "testnet",
            "safety": {
                "allowed_symbols": ["BTC"],
                "leverage": 1,
                "max_target_margin_pct": 60,
                "max_notional_usdc": "500",
                "absolute_notional_ceiling": "1000",
            },
        }
    )
    gate = order_gate()
    # The caller's shared §13.5 monitor (issue #80): the loop must hand THIS
    # instance to the protection manager it builds, not build its own.
    built.identity = VenueIdentityMonitor(
        query_order_by_cloid=lambda cloid_hex: {"status": "unknownOid"},
        db=db,
        run_id="r1",
        symbol="BTC",
    )
    if provider_raises is not None and not reach_loop_body:
        stop_at = type(provider_raises)  # flat: the refusal propagates out
    else:
        stop_at = _StopTheLoop if reach_loop_body else StopBeforeTheLoop

    def _drive():
        return cli_mod._run_live_loop(
            cfgs=(RiskConfig(leverage=D(1), max_target_margin_pct=60), DecisionConfig()),
            db=db,
            run_id="r1",
            coin="BTC",
            config={},
            live_cfg=live_cfg,
            client=SimpleNamespace(),
            signed=SimpleNamespace(open_orders=lambda: []),
            gate=gate,
            kill_switch=built.kill_switch,
            safe_mode=SafeModeManager(db=db, run_id="r1", gate=gate),
            reconciler=SimpleNamespace(),
            processor=SimpleNamespace(),
            payload_dir=tmp_path / "payloads",
            fetch_clearinghouse=fetch_clearinghouse,
            identity=built.identity,
        )

    try:
        if expect_return:
            built.returned = _drive()
        else:
            with pytest.raises(stop_at):
                _drive()
    finally:
        db.close()
    for field in ("protection", "guards", "provider"):
        assert getattr(built, field) is not None, f"no {field} built — the pin proves nothing"
    return built


def test_the_live_loop_hands_protection_the_kill_switch(tmp_path, monkeypatch):
    """§18.2: SL/TP repair refreshes the switch across its own retry delays.

    ``ProtectionManager.kill_switch`` defaults to None, and None turns all six
    ``refresh_across_blocking_work`` calls in the manager into no-ops — the four
    on the repair ladder plus the orderStatus confirmation read and the
    protection cancel — and disables the firing latch with them. Every one of
    those episodes can then let the dead man's switch self-trip; the repair
    ladder is the place where it does so onto the very SL it is repairing, with
    synchronous delays between attempts holding the tick open while it happens.
    Only this call site wires it, and nothing observed this call site
    (2026-08-17 issue #45).
    """
    built = _drive_live_loop_construction(
        tmp_path, monkeypatch, fetch_clearinghouse=lambda: clearinghouse()
    )
    assert built.protection.get("kill_switch") is built.kill_switch


def test_the_live_loop_hands_protection_the_shared_identity_monitor(tmp_path, monkeypatch):
    """§13.5 (issue #80): protection probes through the CALLER's monitor.

    ``ProtectionManager.identity`` defaults to None, and None builds a private
    monitor that still bounds protection's own probes — so nothing fails if
    this kwarg is dropped. What is lost is the whole point of the shared
    instance: the streak the reconciler and the kill switch feed would no
    longer be the one protection reads, and a fault alternating between
    consumers would stay below every threshold forever. Same wiring-pin shape
    as the kill-switch pin above, for the same reason.
    """
    built = _drive_live_loop_construction(
        tmp_path, monkeypatch, fetch_clearinghouse=lambda: clearinghouse()
    )
    assert built.protection.get("identity") is built.identity


def test_the_live_loop_takes_the_day_baseline_from_the_clearinghouse(tmp_path, monkeypatch):
    """§10.3 rule 1: the UTC-day baseline is READ, not taken from the ledger.

    ``LossGuards.day_baseline_source`` defaults to None, which silently swaps the
    baseline for the LOCAL ledger's equity — no error, no log — and every
    drawdown and daily-loss judgement for that whole day is anchored to the wrong
    number. The comment at the call site records the last time this path died
    quietly (an arity mismatch its own except-Exception ate, 2026-08-01); what it
    could not record is that the wiring was still unpinned.

    Asserted by CALLING it, for the reason that history gives: a source-level or
    not-None check passes on a source that returns the ledger. The pin covers the
    hop it can see — the source is bound to the ``fetch_clearinghouse`` this
    function was handed; that THAT callable reads the exchange is a fact about
    the caller, pinned where the caller is.
    """
    reads: list[str] = []

    def _fetch():
        reads.append("clearinghouse")
        return clearinghouse(account_value="4242")

    built = _drive_live_loop_construction(tmp_path, monkeypatch, fetch_clearinghouse=_fetch)
    source = built.guards.get("day_baseline_source")
    assert source is not None, "the day baseline silently fell back to the local ledger"
    # 4242 is what the handed-in read returns; the seeded ledger is 200, so a
    # source bound to the local ledger cannot produce it.
    before = built.kill_switch.ticks
    assert source() == D("4242")
    assert reads == ["clearinghouse"]
    # The same call is a bare full-timeout REST read on the single-threaded tick,
    # so it refreshes across itself. A delta rather than a floor: the drive is
    # free to grow refreshes of its own without quietly retiring this one.
    assert built.kill_switch.ticks == before + 1


def _forbid_the_decision_driver(monkeypatch):
    """Protection-only never builds the decision stack; make building it fail."""
    from contrib.hyperliquid_perp.live import decision as decision_mod

    def _forbidden(self, *args, **kwargs):
        raise AssertionError("protection-only must not build the decision worker/driver")

    monkeypatch.setattr(decision_mod.LiveDecisionWorker, "__init__", _forbidden)
    monkeypatch.setattr(decision_mod.LiveDecisionDriver, "__init__", _forbidden)


def _refused_knob(key: str, env: str):
    """The bridge's refusal for a bad env knob, as ``_build_engine_config`` raises it."""
    from contrib.hyperliquid_perp.engine_bridge import EngineConfigError

    return EngineConfigError(f"config key '{key}' ({env}) must be a number, got 'abc'")


@pytest.mark.parametrize("key,bad,env", BAD_ENGINE_ENV_KNOBS)
def test_the_live_loop_enters_protection_only_when_the_engine_cannot_be_built_over_a_position(
    tmp_path, monkeypatch, capsys, key, bad, env
):
    """Issue #268: a rejected engine env knob over a live position must NOT
    leave the loop.

    The bridge gates the env knobs at startup (#177, #266, #269) and the
    ``paper`` command catches the refusal around provider construction; the
    live loop did not. The refusal then crossed ``_run_live_loop`` into the
    caller's ``except Exception`` with a PASSING verdict recorded, so the
    §18.2 shutdown sweep ran with ``keep_protective=False`` — cancelling the
    bot's own SL/TP over the live position, then a crash-loop under
    ``Restart=`` into the same refusal. This pins the containment: the loop
    starts, ticks (the kill-switch refresh and SL/TP repair live in the
    tick), never pumps, never builds the decision stack, and does not enter
    safe mode (nothing failed; the environment is wrong).
    """
    from contrib.hyperliquid_perp.live.engine import LiveTickResult, TickStatus
    from contrib.hyperliquid_perp.live.safe_mode import SafeModeManager

    _forbid_the_decision_driver(monkeypatch)
    quiet = LiveTickResult(at=_T0, status=TickStatus.OK)
    built = _drive_live_loop_construction(
        tmp_path,
        monkeypatch,
        fetch_clearinghouse=lambda: clearinghouse(),
        provider_raises=_refused_knob(key, env),
        initial_positions=[PositionState(coin="BTC", size=D("0.01"), entry_price=D(50000))],
        # Two clean ticks, then the sentinel: the loop must survive an
        # iteration with no driver (the pump seam is where it used to reach
        # for one) and come back for the next tick.
        tick_results=(quiet, quiet),
    )
    assert built.ticks == 3, "the loop did not keep ticking without a decision driver"
    assert _contained_details(built.db_path) == []
    err = capsys.readouterr().err
    assert env in err  # the fixable cause is named
    assert "protection-only" in err
    assert "in protection-only mode" in err  # the startup line says so too
    db = Database(built.db_path)
    try:
        assert SafeModeManager(db=db, run_id="r1", gate=None).current() is None
    finally:
        db.close()


def test_the_live_loop_raises_the_engine_refusal_over_a_flat_book(tmp_path, monkeypatch, capsys):
    """Flat, there is nothing to guard: the refusal propagates OUT of the loop
    by name (the caller's ``except EngineConfigError`` makes it exit 1), and
    protection-only is never entered — the paper lane's empty-book rule.
    """
    _forbid_the_decision_driver(monkeypatch)
    built = _drive_live_loop_construction(
        tmp_path,
        monkeypatch,
        fetch_clearinghouse=lambda: clearinghouse(),
        provider_raises=_refused_knob("llm_max_retries", "TRADINGAGENTS_LLM_MAX_RETRIES"),
    )
    # The harness pinned the raise's type (the bridge's own, unwrapped).
    assert built.ticks == 0, "a flat refusal must not start the loop"
    assert "protection-only" not in capsys.readouterr().err


def _scripted_active_work(monkeypatch, answers):
    """``LiveExecutionEngine.has_active_work`` answering from ``answers`` in
    order — a bool is returned, an exception instance is raised."""
    from contrib.hyperliquid_perp.live.engine import LiveExecutionEngine

    queue = iter(answers)

    def _answer(self):
        item = next(queue)
        if isinstance(item, BaseException):
            raise item
        return item

    monkeypatch.setattr(LiveExecutionEngine, "has_active_work", _answer)


def test_the_live_loop_protection_only_ends_itself_once_the_position_closes(
    tmp_path, monkeypatch, capsys
):
    """Protection-only exists for a live position; once it is closed, no later
    tick can do anything, and a zombie would hold the lease and refresh the
    switch for days. The loop must return its settle-exit record instead —
    the caller then exits 1, the paper loop's settle-exit code — with the
    cause the operator has to fix.
    """
    from contrib.hyperliquid_perp.cli.live_loop import ProtectionOnlyExit
    from contrib.hyperliquid_perp.live.engine import LiveTickResult, TickStatus

    _forbid_the_decision_driver(monkeypatch)
    # The engine's own answer, scripted: live work at the provider refusal,
    # flat after the first tick (an SL filled).
    _scripted_active_work(monkeypatch, [True, False])
    quiet = LiveTickResult(at=_T0, status=TickStatus.OK)
    built = _drive_live_loop_construction(
        tmp_path,
        monkeypatch,
        fetch_clearinghouse=lambda: clearinghouse(),
        provider_raises=_refused_knob("temperature", "TRADINGAGENTS_TEMPERATURE"),
        initial_positions=[PositionState(coin="BTC", size=D("0.01"), entry_price=D(50000))],
        tick_results=(quiet,),
        expect_return=True,
    )
    assert built.ticks == 1
    assert built.returned == ProtectionOnlyExit(
        cause=str(_refused_knob("temperature", "TRADINGAGENTS_TEMPERATURE")), settled=True
    )
    assert "protection-only" in capsys.readouterr().err


def test_the_live_loop_treats_an_unreadable_book_as_live_work(tmp_path, monkeypatch, capsys):
    """Unknown ≠ flat (#268 review): the "anything to guard?" read inside the
    refusal handler is a store read, and a raise there (an operator's
    export/validate holding the lock) used to leave the loop through the
    generic handler — passing verdict on record, sweep strips SL/TP. It must
    fail toward guarding: protection-only, not an exit.
    """
    from contrib.hyperliquid_perp.live.engine import LiveTickResult, TickStatus

    _forbid_the_decision_driver(monkeypatch)
    _scripted_active_work(monkeypatch, [sqlite3.OperationalError("database is locked"), True])
    quiet = LiveTickResult(at=_T0, status=TickStatus.OK)
    built = _drive_live_loop_construction(
        tmp_path,
        monkeypatch,
        fetch_clearinghouse=lambda: clearinghouse(),
        provider_raises=_refused_knob("temperature", "TRADINGAGENTS_TEMPERATURE"),
        initial_positions=[PositionState(coin="BTC", size=D("0.01"), entry_price=D(50000))],
        tick_results=(quiet,),
    )
    assert built.ticks == 2, "the unreadable book ended the loop instead of guarding"
    assert "protection-only" in capsys.readouterr().err


def test_the_live_loop_protection_only_survives_a_broken_stranded_attempt_lookup(
    tmp_path, monkeypatch, capsys
):
    """The stranded-attempt note is a courtesy; its lookup is fail-loud on a
    store holding two in-progress rows (the wedge the healthy lane escalates
    by name). Raised from inside the refusal handler it used to leave the
    loop over the live position (#268 review) — now logged, the note simply
    not printed, and the loop starts.
    """
    from contrib.hyperliquid_perp.cli import _common as common_mod

    _forbid_the_decision_driver(monkeypatch)

    def _wedged(conn, run_id):
        raise ValueError(f"run {run_id!r} has 2 in-progress attempts")

    monkeypatch.setattr(common_mod.repo, "find_in_progress_attempt", _wedged)
    built = _drive_live_loop_construction(
        tmp_path,
        monkeypatch,
        fetch_clearinghouse=lambda: clearinghouse(),
        provider_raises=_refused_knob("max_tokens", "TRADINGAGENTS_MAX_TOKENS"),
        initial_positions=[PositionState(coin="BTC", size=D("0.01"), entry_price=D(50000))],
    )
    assert built.ticks == 1, "the broken lookup ended the loop before its first tick"
    err = capsys.readouterr().err
    assert "protection-only" in err
    assert "note: decision attempt" not in err


def test_a_raising_settle_check_is_contained_under_its_own_phase(tmp_path, monkeypatch):
    # The settle check is a store read AFTER engine.tick() has returned. A
    # raise from it (a locked store) is contained like any loop-body fault —
    # the loop keeps ticking, recoverable safe mode latched so a lock that
    # PERSISTS is visible to `safe-mode --status`/`validate` rather than only
    # as a ~10s ERROR storm (#270 review, round 2) — and it is recorded under
    # its own phase, not as "live tick raised" (#238). The STARTUP read is
    # the one that never latches (test_the_live_loop_treats_an_unreadable_book_as_live_work).
    from contrib.hyperliquid_perp.live.engine import LiveTickResult, TickStatus
    from contrib.hyperliquid_perp.live.safe_mode import REASON_LIVE_TICK_ERROR, SafeModeManager

    _forbid_the_decision_driver(monkeypatch)
    _scripted_active_work(monkeypatch, [True, sqlite3.OperationalError("database is locked")])
    quiet = LiveTickResult(at=_T0, status=TickStatus.OK)
    built = _drive_live_loop_construction(
        tmp_path,
        monkeypatch,
        fetch_clearinghouse=lambda: clearinghouse(),
        provider_raises=_refused_knob("llm_max_retries", "TRADINGAGENTS_LLM_MAX_RETRIES"),
        initial_positions=[PositionState(coin="BTC", size=D("0.01"), entry_price=D(50000))],
        tick_results=(quiet,),
    )
    assert built.ticks == 2
    assert _contained_details(built.db_path) == [
        "live protection-only settle check raised (see log)"
    ]
    db = Database(built.db_path)
    try:
        state = SafeModeManager(db=db, run_id="r1", gate=None).current()
        assert state is not None and not state.is_manual
        assert state.reason == REASON_LIVE_TICK_ERROR
    finally:
        db.close()


def test_the_live_loop_protection_only_reports_an_operator_stop(tmp_path, monkeypatch):
    """Ctrl-C / SIGTERM in protection-only returns the record with
    ``settled=False`` (the caller exits 4, "executed, not clean" — never 0
    for a run that was not trading) and skips the decision-stack teardown
    it never built.
    """
    from contrib.hyperliquid_perp.cli.live_loop import ProtectionOnlyExit

    _forbid_the_decision_driver(monkeypatch)
    built = _drive_live_loop_construction(
        tmp_path,
        monkeypatch,
        fetch_clearinghouse=lambda: clearinghouse(),
        provider_raises=_refused_knob("max_tokens", "TRADINGAGENTS_MAX_TOKENS"),
        initial_positions=[PositionState(coin="BTC", size=D("0.01"), entry_price=D(50000))],
        tick_results=(KeyboardInterrupt(),),
        expect_return=True,
    )
    assert built.returned == ProtectionOnlyExit(
        cause=str(_refused_knob("max_tokens", "TRADINGAGENTS_MAX_TOKENS")), settled=False
    )


def test_the_live_loop_refreshes_across_the_decision_cycles_market_reads(tmp_path, monkeypatch):
    """§18.2: the longest REST chain in the system refreshes between its reads.

    ``_EngineDecisionProvider.on_blocking_read`` defaults to None, and
    ``_build_context``'s ``_between_reads`` simply returns when it is — so the
    four back-to-back full-timeout reads of a decision cycle run entirely
    unrefreshed. That chain is what ``_MAX_UNREFRESHED_REST_CALLS`` is reasoned
    about against; unwired, the switch's real exposure is the chain's length
    while the operator advisory is still computed from the submit chain's 3.

    Same shape as the four kwargs issue #45 lists, and found by scanning for that
    shape — the issue does not name this site. ``_build_context``'s own refresh
    behaviour is driven in test_kill_switch.py; pinned here is that the live loop
    hands it a hook at all, and that the hook drives THIS run's switch.
    """
    built = _drive_live_loop_construction(
        tmp_path, monkeypatch, fetch_clearinghouse=lambda: clearinghouse()
    )
    hook = built.provider.get("on_blocking_read")
    assert hook is not None, "a whole decision cycle of market reads refreshes nothing (§18.2)"
    before = built.kill_switch.ticks
    hook()
    assert built.kill_switch.ticks == before + 1


def test_the_live_loop_wires_the_books_as_the_provider_position_source(tmp_path, monkeypatch):
    # Prompt v4 on the live lane: build_decision_provider binds read_books
    # over THIS run's store, as it does for paper — dropped, None, or bound
    # to the wrong run/coin would leave the live prompt silently position-blind.
    built = _drive_live_loop_construction(
        tmp_path, monkeypatch, fetch_clearinghouse=lambda: clearinghouse()
    )
    source = built.provider.get("position_source")
    assert source is not None, "the live prompt would be position-blind"
    # The binding only: the drive closes the store on its way out, so the
    # read itself is exercised by test_position_facts over a live handle.
    assert_position_source_binds(source, run_id="r1", coin="BTC")
