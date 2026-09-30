"""Tests for ``_paper_loop``, driven with stub engines and schedulers."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from contrib.hyperliquid_perp.cli import paper as paper_mod, paper_export as paper_export_mod

from .conftest import seed_db

_T0 = datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)


class _HoldingEngine:
    """Live work that no tick closes."""

    def has_active_work(self):
        return True

    def tick(self):
        pass


class _ClosingEngine:
    """Live work that the first tick closes."""

    def __init__(self):
        self.work = True

    def has_active_work(self):
        return self.work

    def tick(self):
        self.work = False  # this tick's SL/TP closes the position


class _RepeatingScheduler:
    """Every poll returns ``result``."""

    def __init__(self, result):
        self.result = result

    def poll(self):
        return self.result


class _IdleScheduler(_RepeatingScheduler):
    """...and nothing further is due."""

    def next_due_at(self):
        return None


# --------------------------------------------------------------------------
# _paper_loop wiring: tick-before-poll, per-iteration heartbeat, halt latch
# --------------------------------------------------------------------------


def test_paper_loop_wiring_and_halt_latch(tmp_path, monkeypatch):
    """Drive the production loop for two iterations with recording stubs.

    Pins the wiring that only exists in ``_paper_loop`` itself: the heartbeat
    fires every iteration, tick precedes poll, a cycle-terminal poll triggers
    the funding backfill and the replay-verify export, and a failed
    verification latches ``trading_halted`` AND cancels the in-flight plans
    (no further ``poll()`` calls, no fills on unverifiable books).
    """
    from datetime import timedelta

    from contrib.hyperliquid_perp.paper import reconcile as reconcile_mod
    from contrib.hyperliquid_perp.paper.scheduler import CycleEvent, PollResult
    from contrib.hyperliquid_perp.runtime import run_lock as run_lock_mod
    from contrib.hyperliquid_perp.runtime.clock import ManualClock

    path, db = seed_db(tmp_path)
    clock = ManualClock(_T0)
    calls: list[str] = []

    monkeypatch.setattr(
        run_lock_mod,
        "heartbeat_run_lock",
        lambda db_, run_id, *, pid, now: calls.append("heartbeat"),
    )
    monkeypatch.setattr(
        reconcile_mod,
        "backfill_pending_funding",
        lambda db_, *, run_id, now, funding_source: calls.append("backfill"),
    )
    monkeypatch.setattr(
        paper_export_mod,
        "_post_cycle_export",
        lambda db_, run_id, export_dir: (calls.append("export"), False)[1],
    )

    terminal = PollResult(
        event=CycleEvent.API_FAILED,
        decision_attempt_id="r#001",
        scheduled_at=_T0,
        attempt_count=3,
        next_decision_at=_T0 + timedelta(hours=4),
        error_type="server_error",
    )

    class _Engine:
        def has_active_work(self):
            return True

        def tick(self):
            calls.append("tick")

        def cancel_active_plans(self):
            calls.append("cancel")
            return True

    class _Scheduler:
        def poll(self):
            calls.append("poll")
            return terminal

        def next_due_at(self):  # pragma: no cover — halted branch skips it
            raise AssertionError("next_due_at must not be consulted while halted")

    sleeps: list[float] = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        clock.advance(seconds)  # the tick throttle keys off elapsed clock time
        if len(sleeps) >= 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(paper_mod.time, "sleep", fake_sleep)

    with pytest.raises(KeyboardInterrupt):
        paper_mod._paper_loop(
            db,
            "r",
            _Engine(),
            _Scheduler(),
            clock,
            30,
            tmp_path / "exports",
            funding_source=None,
            trading_halted=False,
        )

    # Iteration 1: heartbeat -> tick -> poll -> cycle-terminal work; the failed
    # verification latches the halt and cancels the in-flight plans, so
    # iteration 2 ticks but never polls.
    assert calls == [
        "heartbeat",
        "tick",
        "poll",
        "backfill",
        "export",
        "cancel",
        "heartbeat",
        "tick",
    ]
    # Sleep stays inside the lease-freshness cap.
    assert sleeps and all(s <= 60.0 for s in sleeps)
    db.close()


def test_paper_loop_escalates_consecutive_stale_feed_refusals(tmp_path, monkeypatch, caplog):
    """Issue #50: the loop counts cycles that reach no decision, and escalates.

    The wiring pin for the paper half — that the loop feeds every terminal
    result's status and §6.2 class to the shared counter. The escalation shape
    itself (and its reset on a decided cycle) is pinned once, on that function,
    in tests/paper/test_validation.py.
    """
    import logging
    from datetime import timedelta

    from contrib.hyperliquid_perp.common.constants import STALE_MARKET_DATA_ERROR
    from contrib.hyperliquid_perp.paper import reconcile as reconcile_mod
    from contrib.hyperliquid_perp.paper.scheduler import CycleEvent, PollResult
    from contrib.hyperliquid_perp.runtime import run_lock as run_lock_mod
    from contrib.hyperliquid_perp.runtime.clock import ManualClock
    from contrib.hyperliquid_perp.runtime.no_decision import NO_DECISION_STREAK_THRESHOLD

    path, db = seed_db(tmp_path)
    clock = ManualClock(_T0)

    monkeypatch.setattr(run_lock_mod, "heartbeat_run_lock", lambda db_, run_id, *, pid, now: None)
    monkeypatch.setattr(
        reconcile_mod, "backfill_pending_funding", lambda db_, *, run_id, now, funding_source: None
    )
    # Verification passes, so the loop keeps polling instead of latching halt.
    monkeypatch.setattr(
        paper_export_mod, "_post_cycle_export", lambda db_, run_id, export_dir: True
    )

    terminal = PollResult(
        event=CycleEvent.API_FAILED,
        decision_attempt_id="r#stale",
        scheduled_at=_T0,
        attempt_count=3,
        next_decision_at=_T0 + timedelta(hours=4),
        error_type=STALE_MARKET_DATA_ERROR,
    )

    iterations = 0

    def fake_sleep(seconds):
        nonlocal iterations
        iterations += 1
        clock.advance(seconds)
        if iterations >= NO_DECISION_STREAK_THRESHOLD:
            raise KeyboardInterrupt

    monkeypatch.setattr(paper_mod.time, "sleep", fake_sleep)

    with (
        caplog.at_level(logging.WARNING, logger="contrib.hyperliquid_perp.runtime.no_decision"),
        pytest.raises(KeyboardInterrupt),
    ):
        paper_mod._paper_loop(
            db,
            "r",
            _HoldingEngine(),
            _IdleScheduler(terminal),
            clock,
            30,
            tmp_path / "exports",
            funding_source=None,
            trading_halted=False,
        )

    levels = [r.levelno for r in caplog.records if "decision cycle for r" in r.getMessage()]
    assert levels == [logging.WARNING] * (NO_DECISION_STREAK_THRESHOLD - 1) + [logging.ERROR]
    db.close()


def test_paper_loop_streak_is_reset_by_a_cycle_that_decided(tmp_path, monkeypatch, caplog):
    """The loop feeds EVERY terminal outcome to the counter, not just failures.

    Discriminating by construction: two failures, a COMPLETED cycle, then two
    more failures. With the call inside the api_failed branch (where it started)
    the streak reaches 3 and logs ERROR over a run that decided in between —
    and `validate`, which that ERROR points the operator at, would show nothing.
    """
    import logging
    from datetime import timedelta

    from contrib.hyperliquid_perp.common.constants import STALE_MARKET_DATA_ERROR
    from contrib.hyperliquid_perp.paper import reconcile as reconcile_mod
    from contrib.hyperliquid_perp.paper.scheduler import CycleEvent, PollResult
    from contrib.hyperliquid_perp.runtime import run_lock as run_lock_mod
    from contrib.hyperliquid_perp.runtime.clock import ManualClock

    path, db = seed_db(tmp_path)
    clock = ManualClock(_T0)

    monkeypatch.setattr(run_lock_mod, "heartbeat_run_lock", lambda db_, run_id, *, pid, now: None)
    monkeypatch.setattr(
        reconcile_mod, "backfill_pending_funding", lambda db_, *, run_id, now, funding_source: None
    )
    monkeypatch.setattr(
        paper_export_mod, "_post_cycle_export", lambda db_, run_id, export_dir: True
    )

    def _failed():
        return PollResult(
            event=CycleEvent.API_FAILED,
            decision_attempt_id="r#f",
            scheduled_at=_T0,
            attempt_count=3,
            next_decision_at=_T0 + timedelta(hours=4),
            error_type=STALE_MARKET_DATA_ERROR,
        )

    def _decided():
        return PollResult(
            event=CycleEvent.COMPLETED,
            decision_attempt_id="r#c",
            scheduled_at=_T0,
            attempt_count=1,
            output_id="o",
            plan=object(),
            next_decision_at=_T0 + timedelta(hours=4),
        )

    outcomes = [_failed(), _failed(), _decided(), _failed(), _failed()]

    class _Scheduler:
        def poll(self):
            return outcomes.pop(0) if outcomes else None

        def next_due_at(self):
            return None

    def fake_sleep(seconds):
        clock.advance(seconds)
        if not outcomes:
            raise KeyboardInterrupt

    monkeypatch.setattr(paper_mod.time, "sleep", fake_sleep)

    with (
        caplog.at_level(logging.WARNING, logger="contrib.hyperliquid_perp.runtime.no_decision"),
        pytest.raises(KeyboardInterrupt),
    ):
        paper_mod._paper_loop(
            db,
            "r",
            _HoldingEngine(),
            _Scheduler(),
            clock,
            30,
            tmp_path / "exports",
            funding_source=None,
            trading_halted=False,
        )

    notes = [r for r in caplog.records if "decision cycle for r" in r.getMessage()]
    # Four failures observed, never three in a row: all WARNING, none ERROR.
    assert [r.levelno for r in notes] == [logging.WARNING] * 4
    # The one right after the decided cycle; anchored on the dash (issue #290).
    assert "— 1 consecutive" in notes[2].getMessage()
    db.close()


def test_paper_loop_tick_throttled_to_interval_above_heartbeat_cap(tmp_path, monkeypatch):
    """The wake cadence and the tick cadence must stay decoupled: the loop
    wakes every <=60s for the lease heartbeat, but the tick (the market-data
    fetch) fires only once the configured interval has elapsed — not on every
    wake. Config rejects intervals above the 30s TWAP slice cadence, so this
    pins the loop's defensive invariant with a direct call (interval=120),
    not an operator-reachable configuration."""
    from contrib.hyperliquid_perp.runtime import run_lock as run_lock_mod
    from contrib.hyperliquid_perp.runtime.clock import ManualClock

    path, db = seed_db(tmp_path)
    clock = ManualClock(_T0)
    calls: list[str] = []
    monkeypatch.setattr(
        run_lock_mod,
        "heartbeat_run_lock",
        lambda db_, run_id, *, pid, now: calls.append("heartbeat"),
    )

    class _Engine:
        def has_active_work(self):
            return True

        def tick(self):
            calls.append(f"tick@{int((clock.now() - _T0).total_seconds())}")

    sleeps: list[float] = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        clock.advance(seconds)
        if len(sleeps) >= 4:
            raise KeyboardInterrupt

    monkeypatch.setattr(paper_mod.time, "sleep", fake_sleep)

    with pytest.raises(KeyboardInterrupt):
        paper_mod._paper_loop(
            db,
            "r",
            _Engine(),
            None,  # halted: poll never runs, isolating the tick cadence
            clock,
            120,
            tmp_path / "exports",
            funding_source=None,
            trading_halted=True,
        )

    # Wakes at 0/60/120/180s; ticks only at 0 and 120 (the 120s interval),
    # while every sleep stays inside the 60s lease-freshness cap.
    assert [c for c in calls if c.startswith("tick")] == ["tick@0", "tick@120"]
    assert calls.count("heartbeat") == 4
    assert sleeps == [60.0, 60.0, 60.0, 60.0]
    db.close()


def test_paper_loop_halted_with_nothing_to_protect_exits_1(tmp_path, monkeypatch, capsys):
    """Protection-only exists for the live position: once SL/TP closes it (no
    active work left), the loop must export the final state (the closing fill
    reaches the CSVs) and exit 1 — not idle as a zombie holding the lease.

    ``scheduler=None`` pins the protection-only construction contract: a halted
    start never builds the scheduler/decision provider, so the loop must never
    touch it."""
    from contrib.hyperliquid_perp.runtime import run_lock as run_lock_mod
    from contrib.hyperliquid_perp.runtime.clock import ManualClock

    path, db = seed_db(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(run_lock_mod, "heartbeat_run_lock", lambda db_, run_id, *, pid, now: None)

    def record_export(db_, run_id, export_dir):
        calls.append("export")
        return True

    def forbid_sleep(seconds):
        raise AssertionError("must exit before sleeping")

    monkeypatch.setattr(paper_export_mod, "_post_cycle_export", record_export)
    monkeypatch.setattr(paper_mod.time, "sleep", forbid_sleep)

    rc = paper_mod._paper_loop(
        db,
        "r",
        _ClosingEngine(),
        None,  # protection-only never builds the scheduler
        ManualClock(_T0),
        30,
        tmp_path / "exports",
        funding_source=None,
        trading_halted=True,
    )
    assert rc == 1
    assert calls == ["export"]  # the final state (closing fill) was published
    err = capsys.readouterr().err
    assert "nothing left to protect" in err
    assert "books never re-verified" in err  # default halt reason: replay
    db.close()


def test_paper_loop_missing_key_settle_exit_names_the_key(tmp_path, monkeypatch, capsys):
    # Same settle-exit lane, but a keyless-healthy halt must tell the operator
    # to set the key — not to investigate a store that verified fine.
    from contrib.hyperliquid_perp.runtime import run_lock as run_lock_mod
    from contrib.hyperliquid_perp.runtime.clock import ManualClock

    path, db = seed_db(tmp_path)
    monkeypatch.setattr(run_lock_mod, "heartbeat_run_lock", lambda db_, run_id, *, pid, now: None)
    monkeypatch.setattr(
        paper_export_mod, "_post_cycle_export", lambda db_, run_id, export_dir: True
    )
    monkeypatch.setattr(
        paper_mod.time,
        "sleep",
        lambda s: (_ for _ in ()).throw(AssertionError("must exit first")),
    )

    rc = paper_mod._paper_loop(
        db,
        "r",
        _ClosingEngine(),
        None,
        ManualClock(_T0),
        30,
        tmp_path / "exports",
        funding_source=None,
        trading_halted=True,
        halt_reason="missing-key",
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "OPENROUTER_API_KEY" in err
    assert "books never re-verified" not in err
    db.close()


def test_paper_loop_engine_config_error_settle_exit_names_the_cause(tmp_path, monkeypatch, capsys):
    # Third settle-exit wording: an engine-config-error halt (a failed import
    # or a rejected config value) has healthy books, so the
    # exit message must point at the environment fix — not at investigating a
    # store that verified fine, and not at the API key.
    from contrib.hyperliquid_perp.runtime import run_lock as run_lock_mod
    from contrib.hyperliquid_perp.runtime.clock import ManualClock

    path, db = seed_db(tmp_path)
    monkeypatch.setattr(run_lock_mod, "heartbeat_run_lock", lambda db_, run_id, *, pid, now: None)
    monkeypatch.setattr(
        paper_export_mod, "_post_cycle_export", lambda db_, run_id, export_dir: True
    )
    monkeypatch.setattr(
        paper_mod.time,
        "sleep",
        lambda s: (_ for _ in ()).throw(AssertionError("must exit first")),
    )

    rc = paper_mod._paper_loop(
        db,
        "r",
        _ClosingEngine(),
        None,
        ManualClock(_T0),
        30,
        tmp_path / "exports",
        funding_source=None,
        trading_halted=True,
        halt_reason="engine-config-error",
        halt_cause="config key 'temperature' (TRADINGAGENTS_TEMPERATURE) must be a number",
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "the engine could not be built" in err
    assert "(TRADINGAGENTS_TEMPERATURE)" in err  # the cause itself, not a pointer to the log
    assert "books never re-verified" not in err
    assert "OPENROUTER_API_KEY" not in err
    db.close()


def test_paper_loop_halted_retries_pending_funding_hourly(tmp_path, monkeypatch):
    """Protection-only never polls the scheduler, so it never reaches the
    cycle-terminal funding retry — the loop must retry pending funding on its
    own wall-clock cadence instead, or a transiently unresolvable hour stays
    pending (its P&L uncounted) for the run's whole halted lifetime. The first
    retry waits a full period: every entry into halted mode has just run a
    backfill."""
    from contrib.hyperliquid_perp.paper import reconcile as reconcile_mod
    from contrib.hyperliquid_perp.runtime import run_lock as run_lock_mod
    from contrib.hyperliquid_perp.runtime.clock import ManualClock

    path, db = seed_db(tmp_path)
    clock = ManualClock(_T0)
    monkeypatch.setattr(run_lock_mod, "heartbeat_run_lock", lambda db_, run_id, *, pid, now: None)
    retries: list[int] = []
    monkeypatch.setattr(
        reconcile_mod,
        "backfill_pending_funding",
        lambda db_, *, run_id, now, funding_source: retries.append(
            int((now - _T0).total_seconds())
        ),
    )

    sleeps: list[float] = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        clock.advance(seconds)
        if len(sleeps) >= 250:  # ~2h05m of 30s (interval) wakes — two retry periods
            raise KeyboardInterrupt

    monkeypatch.setattr(paper_mod.time, "sleep", fake_sleep)

    with pytest.raises(KeyboardInterrupt):
        paper_mod._paper_loop(
            db,
            "r",
            _HoldingEngine(),
            None,  # protection-only never builds the scheduler
            clock,
            30,
            tmp_path / "exports",
            funding_source=None,
            trading_halted=True,
        )
    # Fires once per hour on the wall clock — not on every 30s wake, and not
    # immediately on entry (a backfill just ran on every path into halted mode).
    assert retries == [3600, 7200]
    db.close()


def test_paper_loop_mid_run_halt_arms_hourly_funding_retry(tmp_path, monkeypatch):
    # The other entry into halted mode: a mid-run replay failure. The halt must
    # arm the hourly retry timer too (one full period out — the cycle-terminal
    # backfill just ran), or a mid-run-halted loop would never retry again.
    from datetime import timedelta

    from contrib.hyperliquid_perp.paper import reconcile as reconcile_mod
    from contrib.hyperliquid_perp.paper.scheduler import CycleEvent, PollResult
    from contrib.hyperliquid_perp.runtime import run_lock as run_lock_mod
    from contrib.hyperliquid_perp.runtime.clock import ManualClock

    path, db = seed_db(tmp_path)
    clock = ManualClock(_T0)
    monkeypatch.setattr(run_lock_mod, "heartbeat_run_lock", lambda db_, run_id, *, pid, now: None)
    retries: list[int] = []
    monkeypatch.setattr(
        reconcile_mod,
        "backfill_pending_funding",
        lambda db_, *, run_id, now, funding_source: retries.append(
            int((now - _T0).total_seconds())
        ),
    )
    # The failing verification flips the loop into halted mode on iteration 1.
    monkeypatch.setattr(
        paper_export_mod, "_post_cycle_export", lambda db_, run_id, export_dir: False
    )

    terminal = PollResult(
        event=CycleEvent.API_FAILED,
        decision_attempt_id="r#001",
        scheduled_at=_T0,
        attempt_count=3,
        next_decision_at=_T0 + timedelta(hours=4),
        error_type="server_error",
    )

    class _Engine:
        def has_active_work(self):
            return True

        def tick(self):
            pass

        def cancel_active_plans(self):
            return False

    sleeps: list[float] = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        clock.advance(seconds)
        if len(sleeps) >= 125:  # ~1h02m of 30s (interval) wakes — one retry period
            raise KeyboardInterrupt

    monkeypatch.setattr(paper_mod.time, "sleep", fake_sleep)

    with pytest.raises(KeyboardInterrupt):
        paper_mod._paper_loop(
            db,
            "r",
            _Engine(),
            _RepeatingScheduler(terminal),
            clock,
            30,
            tmp_path / "exports",
            funding_source=None,
            trading_halted=False,
        )
    # t=0: the cycle-terminal lane's direct backfill (then the halt); t=3600:
    # the halted-mode timer's first fire, one full period after the halt.
    assert retries == [0, 3600]
    db.close()


def test_paper_loop_settle_exit_retries_pending_funding_before_final_export(tmp_path, monkeypatch):
    # The settle-exit CSVs are the run's last word — pending funding that can
    # resolve now must be posted before that final export, not left uncounted
    # forever because the process exits.
    from contrib.hyperliquid_perp.paper import reconcile as reconcile_mod
    from contrib.hyperliquid_perp.runtime import run_lock as run_lock_mod
    from contrib.hyperliquid_perp.runtime.clock import ManualClock

    path, db = seed_db(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(run_lock_mod, "heartbeat_run_lock", lambda db_, run_id, *, pid, now: None)
    monkeypatch.setattr(
        reconcile_mod,
        "backfill_pending_funding",
        lambda db_, *, run_id, now, funding_source: calls.append("backfill"),
    )
    monkeypatch.setattr(
        paper_export_mod,
        "_post_cycle_export",
        lambda db_, run_id, export_dir: (calls.append("export"), True)[1],
    )
    monkeypatch.setattr(
        paper_mod.time,
        "sleep",
        lambda s: (_ for _ in ()).throw(AssertionError("must exit first")),
    )

    rc = paper_mod._paper_loop(
        db,
        "r",
        _ClosingEngine(),
        None,
        ManualClock(_T0),
        30,
        tmp_path / "exports",
        funding_source=None,
        trading_halted=True,
    )
    assert rc == 1
    assert calls == ["backfill", "export"]
    db.close()


def test_paper_loop_shutdown_funding_retry_is_best_effort(tmp_path, monkeypatch):
    # Contrast with the fail-loud cycle-terminal lane: in the settle-exit lane
    # the retry exists to complete the final CSVs — a raising retry must not
    # cost us the export itself (or, in the halted timer, kill the loop that
    # keeps SL/TP alive).
    from contrib.hyperliquid_perp.paper import reconcile as reconcile_mod
    from contrib.hyperliquid_perp.runtime import run_lock as run_lock_mod
    from contrib.hyperliquid_perp.runtime.clock import ManualClock

    path, db = seed_db(tmp_path)
    monkeypatch.setattr(run_lock_mod, "heartbeat_run_lock", lambda db_, run_id, *, pid, now: None)

    def raising_backfill(db_, *, run_id, now, funding_source):
        raise RuntimeError("funding source broke mid-backfill")

    monkeypatch.setattr(reconcile_mod, "backfill_pending_funding", raising_backfill)
    exports: list[str] = []
    monkeypatch.setattr(
        paper_export_mod,
        "_post_cycle_export",
        lambda db_, run_id, export_dir: exports.append(run_id) or True,
    )
    monkeypatch.setattr(
        paper_mod.time,
        "sleep",
        lambda s: (_ for _ in ()).throw(AssertionError("must exit first")),
    )

    rc = paper_mod._paper_loop(
        db,
        "r",
        _ClosingEngine(),
        None,
        ManualClock(_T0),
        30,
        tmp_path / "exports",
        funding_source=None,
        trading_halted=True,
    )
    assert rc == 1
    assert exports == ["r"]  # the final export still happened
    db.close()


def test_paper_loop_names_an_untyped_failure_instead_of_counting_api_tries(
    tmp_path, monkeypatch, capsys
):
    # Issue #134: a non-retryable bug fails the cycle closed with no §6.2
    # class and no ladder — the AI was never asked, so "decision API failed 1
    # times" would file a bug as an outage for anyone scraping the log.
    from datetime import timedelta

    from contrib.hyperliquid_perp.paper import reconcile as reconcile_mod
    from contrib.hyperliquid_perp.paper.scheduler import CycleEvent, PollResult
    from contrib.hyperliquid_perp.runtime import run_lock as run_lock_mod
    from contrib.hyperliquid_perp.runtime.clock import ManualClock

    path, db = seed_db(tmp_path)
    monkeypatch.setattr(run_lock_mod, "heartbeat_run_lock", lambda db_, run_id, *, pid, now: None)
    monkeypatch.setattr(
        reconcile_mod, "backfill_pending_funding", lambda db_, *, run_id, now, funding_source: None
    )
    monkeypatch.setattr(paper_export_mod, "_post_cycle_export", lambda db_, run_id, d: True)
    untyped = PollResult(
        event=CycleEvent.API_FAILED,
        decision_attempt_id="r#001",
        scheduled_at=_T0,
        attempt_count=1,
        next_decision_at=_T0 + timedelta(hours=4),
        error_type=None,
    )

    class _Engine:
        def has_active_work(self):
            return False

    def stop_after_one(seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(paper_mod.time, "sleep", stop_after_one)
    with pytest.raises(KeyboardInterrupt):
        paper_mod._paper_loop(
            db,
            "r",
            _Engine(),
            _IdleScheduler(untyped),
            ManualClock(_T0),
            30,
            tmp_path / "exports",
            funding_source=None,
            trading_halted=False,
        )
    err = capsys.readouterr().err
    assert "non-retryable error" in err
    assert "decision API failed" not in err
    db.close()


def test_paper_loop_does_not_swallow_backfill_runtime_error(tmp_path, monkeypatch):
    # RuntimeError out of backfill_pending_funding is fail-loud by design:
    # nothing in the loop may contain it (main() maps it to exit 2, already
    # pinned by test_cli_main_wrapper_maps_interrupt_and_unexpected_error).
    from datetime import timedelta

    from contrib.hyperliquid_perp.paper import reconcile as reconcile_mod
    from contrib.hyperliquid_perp.paper.scheduler import CycleEvent, PollResult
    from contrib.hyperliquid_perp.runtime import run_lock as run_lock_mod
    from contrib.hyperliquid_perp.runtime.clock import ManualClock

    path, db = seed_db(tmp_path)
    monkeypatch.setattr(run_lock_mod, "heartbeat_run_lock", lambda db_, run_id, *, pid, now: None)

    def raising_backfill(db_, *, run_id, now, funding_source):
        raise RuntimeError("funding source broke mid-backfill")

    monkeypatch.setattr(reconcile_mod, "backfill_pending_funding", raising_backfill)
    exports: list[str] = []
    monkeypatch.setattr(
        paper_export_mod,
        "_post_cycle_export",
        lambda db_, run_id, export_dir: exports.append(run_id) or True,
    )

    terminal = PollResult(
        event=CycleEvent.API_FAILED,
        decision_attempt_id="r#001",
        scheduled_at=_T0,
        attempt_count=3,
        next_decision_at=_T0 + timedelta(hours=4),
        error_type="server_error",
    )

    with pytest.raises(RuntimeError, match="mid-backfill"):
        paper_mod._paper_loop(
            db,
            "r",
            _HoldingEngine(),
            _RepeatingScheduler(terminal),
            ManualClock(_T0),
            30,
            tmp_path / "exports",
            funding_source=None,
            trading_halted=False,
        )
    assert exports == []  # backfill precedes the export in the terminal branch
    db.close()
