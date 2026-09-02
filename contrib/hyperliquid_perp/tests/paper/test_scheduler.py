"""Tests for the rolling 4h decision scheduler (phase2-spec §3 / §3.1).

Everything runs on a :class:`ManualClock`, a scripted snapshot provider for the
engine, and a scripted decision provider for the AI seam — no test sleeps, no
network. ``start_plan`` consumes one scripted snapshot per gated decision;
``tick`` consumes one per call.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from contrib.hyperliquid_perp.domains.perp.margin import MarginSchedule, MarginTier
from contrib.hyperliquid_perp.domains.perp.risk_gate import DecisionConfig, RiskConfig
from contrib.hyperliquid_perp.domains.perp.schema import PerpMarketContext
from contrib.hyperliquid_perp.domains.perp.target_decision import (
    DecisionMode,
    ParsedDecision,
    TargetDecision,
    TargetSide,
)
from contrib.hyperliquid_perp.paper import accounting
from contrib.hyperliquid_perp.paper.clock import ManualClock
from contrib.hyperliquid_perp.paper.config import PaperTradingConfig
from contrib.hyperliquid_perp.paper.engine import AssetSpec, PaperExecutionEngine
from contrib.hyperliquid_perp.paper.market_feed import ScriptedSnapshotProvider, SnapshotOutcome
from contrib.hyperliquid_perp.paper.scheduler import (
    CYCLE_INTERVAL,
    CycleEvent,
    DecisionInput,
    PaperScheduler,
    RetryableDecisionError,
    parse_instant,
)
from contrib.hyperliquid_perp.persistence import repository as repo
from contrib.hyperliquid_perp.persistence.db import Database

D = Decimal
_T0 = datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)
_MARK = D(50000)


def _ctx(as_of: datetime) -> PerpMarketContext:
    return PerpMarketContext(
        coin="BTC",
        as_of=as_of,
        candle_interval="4h",
        candle_count=200,
        mark_price=_MARK,
        oracle_price=_MARK,
        prev_day_price=_MARK,
        mid_price=_MARK,
        day_change_pct=0.0,  # prev == mark: a reference exists, so 0, not None
        open_interest=D(0),
        day_ntl_volume=D(0),
        funding_rate=D("0.0001"),
        funding_premium=None,
        funding_zscore_30d=None,
        funding_window_days=30,
        funding_sample_count=0,
    )


def _decision(side: str, margin: int, conf: str = "0.8") -> ParsedDecision:
    dec = TargetDecision(
        decision_mode=DecisionMode.SET_TARGET,
        target_side=TargetSide(side),
        requested_target_margin_pct=margin,
        confidence=D(conf),
        rationale="test rationale",
        key_risks=("a risk",),
    )
    return ParsedDecision(decision=dec, is_valid=True, invalid_reason=None, raw_response="{}")


def _invalid_decision(reason: str = "no json block") -> ParsedDecision:
    return ParsedDecision(
        decision=TargetDecision.fail_closed(),
        is_valid=False,
        invalid_reason=reason,
        raw_response="garbage",
    )


class _FakeProvider:
    """Scripted DecisionProvider: each request pops the next outcome."""

    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.build_calls = 0
        self.decide_calls = 0

    def build_input(self, *, coin: str, as_of: datetime) -> DecisionInput:
        self.build_calls += 1
        return DecisionInput(context=_ctx(as_of))

    def request_decision(self, decision_input: DecisionInput) -> ParsedDecision:
        item = self._outcomes[self.decide_calls]  # IndexError = under-scripted test
        self.decide_calls += 1
        if isinstance(item, Exception):
            raise item
        return item


def _setup(tmp_path, outcomes, script):
    db = Database(tmp_path / "s.db")
    accounting.initialize_run(
        db, run_id="r", mode="paper", initial_balance_usdc=D(1000), schema_version=1
    )
    clock = ManualClock(_T0)
    asset = AssetSpec(
        coin="BTC",
        sz_decimals=3,
        margin_schedule=MarginSchedule(coin="BTC", tiers=(MarginTier(D(0), D(50)),)),
    )
    risk = RiskConfig(leverage=D(5), max_target_margin_pct=60)
    engine = PaperExecutionEngine(
        db=db,
        run_id="r",
        asset=asset,
        clock=clock,
        provider=ScriptedSnapshotProvider("BTC", script),
        risk_config=risk,
        decision_config=DecisionConfig(),
        paper_config=PaperTradingConfig.from_dict(None),
    )
    provider = _FakeProvider(outcomes)
    scheduler = PaperScheduler(
        db=db,
        run_id="r",
        engine=engine,
        clock=clock,
        provider=provider,
        asset=asset,
        risk_config=risk,
        decision_config=DecisionConfig(),
    )
    return db, clock, engine, scheduler, provider


def _restart(db, clock, engine, provider):
    """A fresh scheduler over the same store — the "supervisor restarted us" shape.

    Same construction ``_setup`` performs; only the AI seam is re-scripted, so
    a new constructor kwarg lands in one place rather than in every restart
    test.
    """
    return PaperScheduler(
        db=db,
        run_id="r",
        engine=engine,
        clock=clock,
        provider=provider,
        asset=engine._asset,
        risk_config=engine._risk,
        decision_config=DecisionConfig(),
    )


def _snap(mark=_MARK, mid=_MARK):
    return (D(mark), D(mid))


def _attempt_row(db, attempt_id):
    return repo.get_decision_attempt(db.conn, attempt_id)


def _err(kind="timeout"):
    return RetryableDecisionError(kind, "boom")


def test_a_retryable_error_outside_the_vocabulary_fails_at_construction():
    # The §6.2 class is checked where it is PRODUCED, not only at the repository
    # write boundary: a provider's typo used to travel through the retry ladder
    # and fail when the daemon tried to record its own failure (issue #122).
    with pytest.raises(ValueError, match="RetryableDecisionError.error_type"):
        RetryableDecisionError("sever_error", "x")
    # ...while every vocabulary member still constructs.
    from contrib.hyperliquid_perp.common.constants import ERROR_TYPES

    for kind in sorted(ERROR_TYPES):
        assert RetryableDecisionError(kind, "x").error_type == kind


# --------------------------------------------------------------------------
# spec §3: new run runs immediately; next = actual decision_at + 4h
# --------------------------------------------------------------------------


def test_fresh_run_executes_immediately_and_persists_cycle(tmp_path):
    db, clock, engine, scheduler, provider = _setup(tmp_path, [_decision("long", 1)], [_snap()])
    result = scheduler.poll()
    assert result is not None and result.event is CycleEvent.COMPLETED
    assert result.scheduled_at == _T0
    assert result.next_decision_at == _T0 + CYCLE_INTERVAL
    assert provider.decide_calls == 1

    row = _attempt_row(db, result.decision_attempt_id)
    assert row["status"] == "completed"
    assert row["attempt_count"] == 1
    assert row["output_id"] == result.output_id
    assert row["input_id"] is not None

    # ai_inputs / ai_outputs audit rows exist and cross-reference each other.
    out = db.conn.execute(
        "SELECT * FROM ai_outputs WHERE output_id = ?", (result.output_id,)
    ).fetchone()
    assert out["risk_action"] == "approved"
    assert out["order_created"] == 1
    assert out["mark_price"] == "50000"  # the gate's own sizing inputs (§7)
    assert out["account_equity"] == "1000"
    inp = db.conn.execute(
        "SELECT * FROM ai_inputs WHERE input_id = ?", (out["input_id"],)
    ).fetchone()
    assert inp["wallet_balance"] == "1000"
    assert inp["current_position_side"] == "flat"

    # scheduler_state advanced; order row carries the output_id.
    state = repo.get_scheduler_state(db.conn, "r")
    assert parse_instant(state["next_decision_at"]) == _T0 + CYCLE_INTERVAL
    assert state["current_attempt_id"] is None
    order = db.conn.execute("SELECT * FROM orders WHERE run_id = 'r'").fetchone()
    assert order["output_id"] == result.output_id

    # Cycle-end snapshot written even though nothing filled yet (§11.1).
    assert db.conn.execute("SELECT COUNT(*) FROM account_snapshots").fetchone()[0] == 1

    # Not due again until the boundary.
    assert scheduler.poll() is None
    assert scheduler.next_due_at() == _T0 + CYCLE_INTERVAL
    db.close()


def test_finalize_clears_pending_before_snapshot_so_a_raising_snapshot_cant_double_commit(tmp_path):
    # write_cycle_snapshot swallows only (sqlite3.Error, OSError); a non-DB error
    # (mark<=0, halted engine, corrupt Decimal) must not strand self._pending and
    # re-finalize the already-committed cycle into a duplicate plan/output.
    db, clock, engine, scheduler, provider = _setup(tmp_path, [_decision("long", 1)], [_snap()])

    def _boom(_mark):
        raise ValueError("snapshot boom")

    engine.write_cycle_snapshot = _boom  # type: ignore[method-assign]

    with pytest.raises(ValueError, match="snapshot boom"):
        scheduler.poll()

    # The cycle committed exactly once and the in-memory latch is already clear.
    assert scheduler._pending is None
    assert db.conn.execute("SELECT COUNT(*) FROM ai_outputs").fetchone()[0] == 1
    assert db.conn.execute("SELECT COUNT(*) FROM decision_attempts").fetchone()[0] == 1
    assert db.conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1

    # A later poll never re-finalizes the same decision: still one of each row.
    assert scheduler.poll() is None
    assert db.conn.execute("SELECT COUNT(*) FROM ai_outputs").fetchone()[0] == 1
    assert db.conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1
    db.close()


def test_delayed_cycle_runs_under_original_schedule_then_rolls_from_actual(tmp_path):
    # Spec §3's example: due 14:15-style boundary missed; recovery at 16:00-style
    # instant runs once and the next boundary keys off the actual decision time.
    db, clock, engine, scheduler, provider = _setup(
        tmp_path, [_decision("long", 1), _decision("long", 1)], [_snap(), _snap()]
    )
    scheduler.poll()  # cycle 1 at T0 -> next due T0+4h
    late = _T0 + timedelta(hours=5, minutes=45)
    clock.set(late)
    result = scheduler.poll()
    assert result.event is CycleEvent.COMPLETED
    assert result.scheduled_at == _T0 + CYCLE_INTERVAL  # original stamp, not "now"
    assert result.next_decision_at == late + CYCLE_INTERVAL  # rolls from actual
    # Exactly one cycle ran for the missed window — no catch-up cycles.
    count = db.conn.execute("SELECT COUNT(*) FROM decision_attempts").fetchone()[0]
    assert count == 2
    db.close()


def test_not_due_polls_return_none(tmp_path):
    db, clock, engine, scheduler, provider = _setup(tmp_path, [_decision("long", 1)], [_snap()])
    scheduler.poll()
    clock.advance(3600)  # only 1h of 4h elapsed
    assert scheduler.poll() is None
    assert provider.decide_calls == 1
    db.close()


# --------------------------------------------------------------------------
# spec §3.1: retry ladder 10s / 30s, then api_failed
# --------------------------------------------------------------------------


def test_retry_ladder_then_api_failed(tmp_path):
    # The terminal api_failed tries a best-effort §11.1 snapshot; market data
    # is down in the same outage here, so it must skip — never fabricate.
    db, clock, engine, scheduler, provider = _setup(
        tmp_path,
        [_err("timeout"), _err("connection"), _err("server_error")],
        [SnapshotOutcome.ERROR],
    )
    r1 = scheduler.poll()
    assert r1.event is CycleEvent.RETRY_SCHEDULED
    assert r1.retry_at == _T0 + timedelta(seconds=10)
    assert scheduler.poll() is None  # not yet retry time

    clock.advance(10)
    r2 = scheduler.poll()
    assert r2.event is CycleEvent.RETRY_SCHEDULED
    assert r2.retry_at == clock.now() + timedelta(seconds=30)
    clock.advance(5)
    assert scheduler.poll() is None  # 30s backoff not elapsed

    clock.advance(25)
    r3 = scheduler.poll()
    assert r3.event is CycleEvent.API_FAILED
    assert r3.next_decision_at == _T0 + CYCLE_INTERVAL  # scheduled_at + 4h, not now + 4h

    row = _attempt_row(db, r3.decision_attempt_id)
    assert row["status"] == "api_failed"
    assert row["attempt_count"] == 3
    assert row["error_type"] == "server_error"
    # The result carries the same §6.2 class the row was written with, so the
    # loop can escalate on it (issue #50) without reading back the row it just
    # wrote. Compared to the row, not to a literal: the point is that they agree.
    assert r3.error_type == row["error_type"]
    assert row["output_id"] is None
    # No target, no order, no AI output — position untouched (spec §3.1).
    assert db.conn.execute("SELECT COUNT(*) FROM ai_outputs").fetchone()[0] == 0
    assert db.conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
    # And no cycle-end snapshot: market data failed too, and a snapshot would
    # need a fabricated mark (execution §6.5).
    assert db.conn.execute("SELECT COUNT(*) FROM account_snapshots").fetchone()[0] == 0
    assert provider.decide_calls == 3
    db.close()


def test_stale_api_failed_cycle_anchors_next_on_terminal_instant(tmp_path):
    # A cycle that itself ran LATE (outage across schedule points) and then
    # api_fails must not schedule its next cycle into the past: the literal
    # scheduled_at + 4h would be immediately due, chaining one full paid retry
    # ladder per missed interval (§3's non-backfill principle, extended to
    # failed cycles — the completed path already anchors on completion).
    db, clock, engine, scheduler, provider = _setup(
        tmp_path,
        [_decision("long", 1), _err("timeout"), _err("connection"), _err("server_error")],
        [_snap(), SnapshotOutcome.ERROR],
    )
    scheduler.poll()  # cycle 1 completes at T0 -> next due T0+4h
    late = _T0 + timedelta(hours=28)  # outage across six schedule points
    clock.set(late)
    r1 = scheduler.poll()
    assert r1.event is CycleEvent.RETRY_SCHEDULED
    assert r1.scheduled_at == _T0 + CYCLE_INTERVAL  # original stamp (§3)
    clock.advance(10)
    scheduler.poll()
    clock.advance(30)
    r3 = scheduler.poll()
    assert r3.event is CycleEvent.API_FAILED
    # Anchored on the terminal instant, not the stale stamp...
    assert r3.next_decision_at == clock.now() + CYCLE_INTERVAL
    # ...so the run is NOT immediately due again: no catch-up ladder chain.
    assert scheduler.poll() is None
    assert scheduler.next_due_at() == clock.now() + CYCLE_INTERVAL
    assert db.conn.execute("SELECT COUNT(*) FROM decision_attempts").fetchone()[0] == 2
    db.close()


class _SlowFailingProvider(_FakeProvider):
    """The scripted call itself consumes clock time (a real timeout does)."""

    def __init__(self, outcomes, clock, seconds):
        super().__init__(outcomes)
        self._clock = clock
        self._seconds = seconds

    def request_decision(self, decision_input: DecisionInput) -> ParsedDecision:
        self._clock.advance(self._seconds)
        return super().request_decision(decision_input)


def test_retry_backoff_counts_from_failure_instant(tmp_path):
    # §3.1's ladder counts from when the try FAILED, not when it started: a
    # 45s in-flight timeout must still be followed by a full 10s wait.
    db, clock, engine, _scheduler, _ = _setup(tmp_path, [], [_snap()])
    provider = _SlowFailingProvider([_err("timeout"), _decision("long", 1)], clock, 45)
    scheduler = _restart(db, clock, engine, provider)
    r1 = scheduler.poll()
    assert r1.event is CycleEvent.RETRY_SCHEDULED
    assert clock.now() == _T0 + timedelta(seconds=45)  # the call burned 45s
    assert r1.retry_at == _T0 + timedelta(seconds=45 + 10)  # failure instant + 10s
    # last_attempt_at was re-stamped to the failure instant, so a restart
    # computes the same basis.
    row = _attempt_row(db, r1.decision_attempt_id)
    assert parse_instant(row["last_attempt_at"]) == _T0 + timedelta(seconds=45)

    assert scheduler.poll() is None  # immediately after the failure: not due
    clock.advance(9)
    assert scheduler.poll() is None  # one second before retry_at: still waiting
    clock.advance(1)
    r2 = scheduler.poll()  # at retry_at: re-attempts
    assert r2.event is CycleEvent.COMPLETED
    assert provider.decide_calls == 2
    db.close()


def test_retry_counter_survives_restart(tmp_path):
    db, clock, engine, scheduler, provider = _setup(tmp_path, [_err()], [])
    r1 = scheduler.poll()
    assert r1.event is CycleEvent.RETRY_SCHEDULED

    # "Restart": a new scheduler over the same store continues the SAME attempt
    # with the counter intact (spec §3.1 — never reset, never double-decide).
    provider2 = _FakeProvider([_decision("long", 1)])
    engine._provider = ScriptedSnapshotProvider("BTC", [_snap()])
    scheduler2 = _restart(db, clock, engine, provider2)
    assert scheduler2.poll() is None  # 10s backoff still pending across restart
    clock.advance(10)
    r2 = scheduler2.poll()
    assert r2.event is CycleEvent.COMPLETED
    assert r2.decision_attempt_id == r1.decision_attempt_id
    assert _attempt_row(db, r2.decision_attempt_id)["attempt_count"] == 2
    db.close()


def test_interrupted_final_attempt_terminalizes_without_new_call(tmp_path):
    # Market data IS available here — the api_failed terminal still writes the
    # best-effort §11.1 cycle-end snapshot at the fresh mark.
    db, clock, engine, scheduler, provider = _setup(tmp_path, [], [_snap()])
    # Simulate a crash after try 3's counter was persisted but before its outcome.
    with db.transaction() as conn:
        repo.insert_decision_attempt(
            conn,
            decision_attempt_id="r|crash",
            timestamp=_T0,
            mode="paper",
            run_id="r",
            scheduled_at=_T0,
            attempt_count=3,
            status="in_progress",
            first_attempt_at=_T0,
            last_attempt_at=_T0,
        )
    result = scheduler.poll()
    assert result.event is CycleEvent.API_FAILED
    assert provider.build_calls == 0  # the 3-try budget is spent; no fourth call
    row = _attempt_row(db, "r|crash")
    assert row["status"] == "api_failed"
    assert row["error_type"] == "interrupted"
    # Best-effort cycle-end snapshot written at the available fresh mark (§11.1).
    assert db.conn.execute("SELECT COUNT(*) FROM account_snapshots").fetchone()[0] == 1
    db.close()


# --------------------------------------------------------------------------
# invalid output: fail-closed, cycle completes, no re-ask (spec §3.1)
# --------------------------------------------------------------------------


def test_invalid_output_completes_cycle_fail_closed(tmp_path):
    db, clock, engine, scheduler, provider = _setup(tmp_path, [_invalid_decision()], [_snap()])
    result = scheduler.poll()
    assert result.event is CycleEvent.INVALID_OUTPUT
    assert result.next_decision_at == _T0 + CYCLE_INTERVAL
    assert provider.decide_calls == 1  # never re-asked

    row = _attempt_row(db, result.decision_attempt_id)
    assert row["status"] == "invalid_output"
    out = db.conn.execute(
        "SELECT * FROM ai_outputs WHERE output_id = ?", (result.output_id,)
    ).fetchone()
    assert out["risk_action"] == "invalid_fail_closed"
    assert out["decision_mode"] == "maintain_current"
    assert out["order_created"] == 0
    assert out["decision_reason"]  # §7: never empty — falls back to the reason
    assert db.conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
    db.close()


# --------------------------------------------------------------------------
# pending market data at plan start: hold the decision, never re-ask
# --------------------------------------------------------------------------


def test_pending_market_data_retries_gate_without_second_ai_call(tmp_path):
    db, clock, engine, scheduler, provider = _setup(
        tmp_path,
        [_decision("long", 1)],
        [SnapshotOutcome.TIMEOUT, _snap()],
    )
    r1 = scheduler.poll()
    assert r1.event is CycleEvent.PENDING_MARKET_DATA
    assert _attempt_row(db, r1.decision_attempt_id)["status"] == "in_progress"
    assert scheduler.next_due_at() is None  # gate retry is due immediately

    clock.advance(30)
    r2 = scheduler.poll()
    assert r2.event is CycleEvent.COMPLETED
    assert r2.decision_attempt_id == r1.decision_attempt_id
    assert provider.decide_calls == 1  # one AI call for the whole cycle
    # next keys off gate completion — the instant the cycle actually finished —
    # so two consecutive AI calls stay >= 4h apart even after a long outage.
    assert r2.next_decision_at == clock.now() + CYCLE_INTERVAL
    db.close()


# --------------------------------------------------------------------------
# result-shape guards
# --------------------------------------------------------------------------


def test_decision_input_rejects_half_candle_window():
    ctx = _ctx(_T0)
    # The candle boundaries are one §5 audit pair — one without the other is
    # malformed, not narrower.
    with pytest.raises(ValueError, match="candle_start"):
        DecisionInput(context=ctx, candle_start=_T0)
    with pytest.raises(ValueError, match="candle_start"):
        DecisionInput(context=ctx, candle_end=_T0)
    # Both supplied (or both omitted) is fine.
    DecisionInput(context=ctx, candle_start=_T0, candle_end=_T0 + timedelta(hours=4))
    DecisionInput(context=ctx)


# The three statements ``read_books`` issues. The prologue's SL/TP read off
# ``current_positions`` (``SELECT stop_loss_price, take_profit_price ...``) is
# a different fact and stays where it is.
_BOOK_READS = (
    "SELECT * FROM current_account_state",
    "SELECT * FROM current_positions",
    "MAX(timestamp) FROM fills",
)


def _trace_audit_prologue(db, scheduler):
    """Record every SQL statement ``_insert_ai_input`` issues (and nothing else's)."""
    statements: list[str] = []
    original = scheduler._insert_ai_input

    def traced(*args, **kwargs):
        db.conn.set_trace_callback(statements.append)
        try:
            return original(*args, **kwargs)
        finally:
            db.conn.set_trace_callback(None)

    scheduler._insert_ai_input = traced
    return statements


def test_the_audit_row_is_written_from_the_books_the_provider_carried(tmp_path):
    # Issue #134: the provider reads the books once for the prompt's position
    # section and carries them on the DecisionInput; the ai_inputs prologue
    # writes THOSE, making none of its own three reads. Measured on the SQL
    # the prologue issues, so a re-read sneaking back in fails here rather
    # than only showing up as a second scan in a profile.
    from contrib.hyperliquid_perp.paper.position_facts import read_books

    db, clock, engine, scheduler, provider = _setup(tmp_path, [_decision("long", 1)], [_snap()])
    carried = {}

    def build_input(*, coin, as_of):
        carried["books"] = read_books(db, "r", coin)
        return DecisionInput(context=_ctx(as_of), books=carried["books"])

    provider.build_input = build_input
    statements = _trace_audit_prologue(db, scheduler)
    result = scheduler.poll()
    assert result is not None and result.event is CycleEvent.COMPLETED
    assert statements, "the prologue ran"
    assert not [s for s in statements if any(read in s for read in _BOOK_READS)]
    row = db.conn.execute("SELECT wallet_balance, last_fill_time FROM ai_inputs").fetchone()
    assert (row["wallet_balance"], row["last_fill_time"]) == (
        str(carried["books"].ledger.wallet_balance),
        carried["books"].last_fill_time,
    )
    db.close()


def test_the_audit_row_falls_back_to_its_own_read_for_a_provider_without_books(tmp_path):
    # The retained path: a provider that carries no books (every scripted
    # double here, a replay harness) still gets a correct row — from the one
    # read the prologue makes itself, the way it did before #134.
    db, clock, engine, scheduler, provider = _setup(tmp_path, [_decision("long", 1)], [_snap()])
    statements = _trace_audit_prologue(db, scheduler)
    result = scheduler.poll()
    assert result is not None and result.event is CycleEvent.COMPLETED
    for read in _BOOK_READS:
        assert sum(read in s for s in statements) == 1, read
    row = db.conn.execute("SELECT wallet_balance, last_fill_time FROM ai_inputs").fetchone()
    assert (row["wallet_balance"], row["last_fill_time"]) == ("1000", None)
    db.close()


# --------------------------------------------------------------------------
# non-retryable errors fail the cycle closed, never the daemon (issue #134)
# --------------------------------------------------------------------------


def test_a_bug_in_build_input_fails_the_cycle_closed_without_a_ladder(tmp_path, caplog):
    import logging

    # Snapshots: the failed cycle's best-effort cycle-end snapshot, then the
    # recovered cycle's gate and ITS cycle-end snapshot.
    db, clock, engine, scheduler, provider = _setup(
        tmp_path, [_decision("long", 1)], [_snap(), _snap(), _snap()]
    )

    def broken(*, coin, as_of):
        raise RuntimeError("a DTO guard tripped")

    provider.build_input = broken
    with caplog.at_level(logging.ERROR):
        result = scheduler.poll()
    # Terminal at once — no 10s/30s ladder, the try counter at 1 — the row
    # carrying no §6.2 class and the cause under the live lane's prefix.
    assert result is not None and result.event is CycleEvent.API_FAILED
    assert result.error_type is None and result.attempt_count == 1
    row = _attempt_row(db, result.decision_attempt_id)
    assert row["status"] == "api_failed"
    assert row["error_type"] is None
    assert row["error_message"] == "non-retryable: RuntimeError('a DTO guard tripped')"
    assert provider.decide_calls == 0
    # The traceback reaches the log at ERROR — the daemon no longer exits, so
    # this is where the bug is now found.
    record = next(r for r in caplog.records if "non-retryable" in r.getMessage())
    assert record.levelno == logging.ERROR and record.exc_info is not None
    # The next cycle is on schedule (scheduled + 4h) and starts normally.
    assert result.next_decision_at == _T0 + CYCLE_INTERVAL
    assert scheduler.poll() is None
    clock.set(_T0 + CYCLE_INTERVAL)
    provider.build_input = _FakeProvider.build_input.__get__(provider)
    fresh = scheduler.poll()
    assert fresh is not None and fresh.event is CycleEvent.COMPLETED
    db.close()


def test_a_bug_in_the_engine_call_fails_the_cycle_closed_after_the_input_row(tmp_path):
    # The same guard past the audit write: the ai_inputs row for the try
    # stays (it describes what the AI was about to see), the attempt is
    # api_failed with no class, and nothing is re-asked.
    db, clock, engine, scheduler, provider = _setup(
        tmp_path, [RuntimeError("engine bug")], [_snap()]
    )
    result = scheduler.poll()
    assert result is not None and result.event is CycleEvent.API_FAILED
    assert result.error_type is None
    row = _attempt_row(db, result.decision_attempt_id)
    assert (row["status"], row["error_type"]) == ("api_failed", None)
    assert row["error_message"].startswith("non-retryable: RuntimeError(")
    assert row["input_id"] == f"{result.decision_attempt_id}#in1"
    assert db.conn.execute("SELECT COUNT(*) FROM ai_inputs").fetchone()[0] == 1
    assert provider.decide_calls == 1
    db.close()


def test_decision_input_rejects_a_partial_segmentation_key_set():
    ctx = _ctx(_T0)
    # prompt_version, context_shape and format_fingerprint are the three
    # segmentation keys of one ai_inputs row (issues #97, #129): a row carrying
    # some but not all would be filed with pre-v10 / pre-v11 history, which the
    # review reads as "unknown". Every proper subset is rejected, not just the
    # historical version-without-shape pair.
    keys = {
        "prompt_version": "v1",
        "context_shape": "price|market|funding|indicators()",
        "format_fingerprint": "97aa0feaa4496d6f",
    }
    for missing in keys:
        partial = {name: value for name, value in keys.items() if name != missing}
        with pytest.raises(ValueError, match="format_fingerprint"):
            DecisionInput(context=ctx, **partial)
    for only in keys:
        with pytest.raises(ValueError, match="format_fingerprint"):
            DecisionInput(context=ctx, **{only: keys[only]})
    DecisionInput(context=ctx, **keys)


def test_decision_input_rejects_inverted_candle_window():
    ctx = _ctx(_T0)
    # An inverted [start, end] is malformed the same way a half-present pair is.
    with pytest.raises(ValueError, match="after candle_end"):
        DecisionInput(
            context=ctx,
            candle_start=_T0 + timedelta(hours=4),
            candle_end=_T0,
        )
    # Equal boundaries (degenerate but not inverted) are allowed through.
    DecisionInput(context=ctx, candle_start=_T0, candle_end=_T0)


def test_poll_result_shape_guards():
    from contrib.hyperliquid_perp.paper.scheduler import PollResult

    with pytest.raises(ValueError, match="retry_at"):
        PollResult(
            event=CycleEvent.COMPLETED,
            decision_attempt_id="a",
            scheduled_at=_T0,
            attempt_count=1,
            output_id="o",
            plan=None,
            retry_at=_T0,
            next_decision_at=_T0,
        )
    with pytest.raises(ValueError, match="next_decision_at"):
        PollResult(
            event=CycleEvent.RETRY_SCHEDULED,
            decision_attempt_id="a",
            scheduled_at=_T0,
            attempt_count=1,
            retry_at=_T0,
            next_decision_at=_T0,
        )
    # A naive instant on any carried datetime is rejected (breadcrumb/export
    # consumers compare against UTC-aware instants).
    with pytest.raises(ValueError, match="timezone-aware"):
        PollResult(
            event=CycleEvent.API_FAILED,
            decision_attempt_id="a",
            scheduled_at=datetime(2026, 7, 6, 12, 0),  # noqa: DTZ001 — naive on purpose
            attempt_count=1,
            next_decision_at=_T0,
            error_type="server_error",
        )
    # ``error_type`` is present only on an api_failed result — the loop's
    # streak counter keys on the EVENT and reads the class for wording alone
    # (issue #50), so a decided cycle carrying a class is the malformed one.
    # An api_failed result MAY carry none: a non-retryable bug fails the cycle
    # closed with no §6.2 class (issue #134), and the counter names that
    # "unclassified" rather than resetting.
    PollResult(
        event=CycleEvent.API_FAILED,
        decision_attempt_id="a",
        scheduled_at=_T0,
        attempt_count=1,
        next_decision_at=_T0,
    )
    with pytest.raises(ValueError, match="error_type"):
        PollResult(
            event=CycleEvent.COMPLETED,
            decision_attempt_id="a",
            scheduled_at=_T0,
            attempt_count=1,
            output_id="o",
            plan=None,
            next_decision_at=None,
            error_type="server_error",
        )


# --------------------------------------------------------------------------
# persisted decision: restart resumes the gate, never re-asks (spec §3.1)
# --------------------------------------------------------------------------

_RAW_LONG_1 = (
    '{"decision_mode": "set_target", "target_side": "long", '
    '"requested_target_margin_pct": 1, "confidence": 0.8, '
    '"rationale": "resume test", "key_risks": ["a risk"]}'
)


def test_restart_resumes_persisted_decision_without_reask(tmp_path):
    from contrib.hyperliquid_perp.domains.perp.target_decision import parse_target_decision

    parsed = parse_target_decision(_RAW_LONG_1, DecisionConfig())
    assert parsed.is_valid
    db, clock, engine, scheduler, provider = _setup(
        tmp_path, [parsed], [SnapshotOutcome.TIMEOUT, _snap()]
    )
    r1 = scheduler.poll()
    assert r1.event is CycleEvent.PENDING_MARKET_DATA
    row = repo.find_in_progress_attempt(db.conn, "r")
    assert row["pending_raw_response"] == _RAW_LONG_1  # persisted before the gate

    # "Restart": a fresh scheduler with a provider that would blow up if asked.
    provider2 = _FakeProvider([])
    scheduler2 = _restart(db, clock, engine, provider2)
    clock.advance(30)
    r2 = scheduler2.poll()
    assert r2.event is CycleEvent.COMPLETED
    assert provider2.build_calls == 0 and provider2.decide_calls == 0  # no re-ask
    assert r2.output_id.endswith("#out1")  # same try's id — rows line up
    done = repo.get_decision_attempt(db.conn, r2.decision_attempt_id)
    assert done["status"] == "completed"
    assert done["attempt_count"] == 1  # resume spent no extra budget
    assert done["pending_raw_response"] is None  # consumed on completion
    db.close()


def test_completed_cycle_clears_stale_error_fields(tmp_path):
    db, clock, engine, scheduler, provider = _setup(
        tmp_path, [_err("timeout"), _decision("long", 1)], [_snap()]
    )
    scheduler.poll()  # try 1 fails, error fields recorded
    clock.advance(10)
    r2 = scheduler.poll()
    assert r2.event is CycleEvent.COMPLETED
    assert r2.output_id.endswith("#out2")  # per-try id
    row = repo.get_decision_attempt(db.conn, r2.decision_attempt_id)
    # A completed row carrying "timeout" breadcrumbs would misread as a failed
    # cycle in the exported decision_attempts.csv.
    assert row["status"] == "completed"
    assert row["error_type"] is None
    assert row["error_message"] is None
    db.close()


# --------------------------------------------------------------------------
# post-answer persist failures: retry the persist, never the AI (issue #163)
# --------------------------------------------------------------------------


def _arm_lock_fault(monkeypatch, target, name, *, shots=1, when=lambda *a, **k: True):
    """Make ``target.name`` raise "database is locked" for its first ``shots`` hits.

    ``when`` filters which calls count (the store lane patches the shared
    ``repository`` module object, so the filter is what keeps the fault on the
    raw-response update rather than every attempt write in the poll). Returns
    the mutable state so a test can assert how many faults actually fired.
    """
    real = getattr(target, name)
    state = {"fired": 0}

    def flaky(*args, **kwargs):
        if state["fired"] < shots and when(*args, **kwargs):
            state["fired"] += 1
            raise sqlite3.OperationalError("database is locked")
        return real(*args, **kwargs)

    monkeypatch.setattr(target, name, flaky)
    return state


def _arm_flaky_response_store(monkeypatch, *, shots=1):
    """Fail the §3.1 response store for its first ``shots`` attempts.

    ``sched_mod.repo`` is the shared ``persistence.repository`` module object,
    so the ``pending_raw_response`` filter — not the patch site — is what keeps
    the pre-call counter update and ``_finalize``'s clearing pass (``None``)
    going through untouched.
    """
    from contrib.hyperliquid_perp.paper import scheduler as sched_mod

    return _arm_lock_fault(
        monkeypatch,
        sched_mod.repo,
        "update_decision_attempt",
        shots=shots,
        when=lambda conn, attempt_id, **kwargs: kwargs.get("pending_raw_response") is not None,
    )


def test_response_store_failure_retries_the_store_never_the_ai(tmp_path, monkeypatch):
    db, clock, engine, scheduler, provider = _setup(tmp_path, [_decision("long", 1)], [_snap()])
    _arm_flaky_response_store(monkeypatch)
    # The paid-for decision lives only in memory: the poll reports "nothing
    # terminal yet" instead of letting the exception exit the daemon.
    assert scheduler.poll() is None
    row = repo.find_in_progress_attempt(db.conn, "r")
    assert row["pending_raw_response"] is None  # the store never landed
    assert scheduler.next_due_at() is None  # the store retry is due immediately
    r2 = scheduler.poll()  # the one-shot fault has cleared: store → gate → done
    assert r2.event is CycleEvent.COMPLETED
    assert provider.decide_calls == 1  # the held decision was reused, never re-asked
    assert repo.get_decision_attempt(db.conn, r2.decision_attempt_id)["status"] == "completed"
    db.close()


def test_crash_during_store_retry_never_resumes_the_unstored_decision(tmp_path, monkeypatch):
    db, clock, engine, scheduler, provider = _setup(tmp_path, [_decision("long", 1)], [_snap()])
    _arm_flaky_response_store(monkeypatch)
    assert scheduler.poll() is None  # decision held in memory, store pending
    # "Crash + restart": the in-memory decision is gone and was never durable,
    # so §3.1 fails closed — the new process re-enters the retry ladder (a
    # fresh AI call within budget); it must NOT gate a decision whose raw
    # response never landed.
    provider2 = _FakeProvider([_decision("long", 1)])
    scheduler2 = _restart(db, clock, engine, provider2)
    assert scheduler2.poll() is None  # 10s backoff from the spent try-1 counter
    clock.advance(10)
    r2 = scheduler2.poll()
    assert r2.event is CycleEvent.COMPLETED
    assert provider2.decide_calls == 1  # a NEW call — the unstored decision is dead
    assert repo.get_decision_attempt(db.conn, r2.decision_attempt_id)["attempt_count"] == 2
    db.close()


def test_a_persist_fault_that_outlives_the_budget_escalates_to_the_supervisor(
    tmp_path, monkeypatch
):
    from contrib.hyperliquid_perp.paper.scheduler import _MAX_PERSIST_FAILURES

    db, clock, engine, scheduler, provider = _setup(tmp_path, [_decision("long", 1)], [_snap()])
    # A fault that never heals is not the transient lock the retry lane exists
    # for: containing it forever would leave the attempt row in_progress —
    # invisible to the §3.1 streak and to validate's exit 4 — while the lease
    # heartbeat keeps reporting a healthy daemon.
    faults = _arm_flaky_response_store(monkeypatch, shots=_MAX_PERSIST_FAILURES + 5)
    for _ in range(_MAX_PERSIST_FAILURES - 1):
        assert scheduler.poll() is None  # contained: decision held, store retried
    with pytest.raises(sqlite3.OperationalError):
        scheduler.poll()  # the budget is spent — propagate to the supervisor
    assert faults["fired"] == _MAX_PERSIST_FAILURES
    assert provider.decide_calls == 1  # never re-asked across the whole streak
    db.close()


def test_a_persist_that_lands_clears_the_failure_streak(tmp_path, monkeypatch):
    from contrib.hyperliquid_perp.paper.scheduler import _MAX_PERSIST_FAILURES

    db, clock, engine, scheduler, provider = _setup(tmp_path, [_decision("long", 1)], [_snap()])
    # The bound measures an unbroken streak, not the cycle's lifetime total:
    # a store that lands after near-fatal contention must hand the audit
    # commit a full budget, and the escalation log must not claim a run of
    # failures that never happened.
    _arm_flaky_response_store(monkeypatch, shots=_MAX_PERSIST_FAILURES - 1)
    for _ in range(_MAX_PERSIST_FAILURES - 1):
        assert scheduler.poll() is None
    _arm_lock_fault(monkeypatch, scheduler, "_insert_ai_output")  # one audit miss
    assert scheduler.poll() is None  # store lands, gate runs, audit misses once
    r = scheduler.poll()
    assert r.event is CycleEvent.COMPLETED  # no escalation: the streak was reset
    assert provider.decide_calls == 1
    db.close()


def test_resume_with_a_poisoned_stored_response_fails_the_cycle_closed(
    tmp_path, monkeypatch, caplog
):
    from contrib.hyperliquid_perp.domains.perp.target_decision import parse_target_decision

    parsed = parse_target_decision(_RAW_LONG_1, DecisionConfig())
    db, clock, engine, scheduler, provider = _setup(
        tmp_path, [parsed], [SnapshotOutcome.TIMEOUT, _snap()]
    )
    r1 = scheduler.poll()
    assert r1.event is CycleEvent.PENDING_MARKET_DATA  # response persisted, gate blocked

    # "Restart" into a deterministic parse bug: propagating it would exit the
    # daemon, and every supervised restart would resume into the same parse —
    # an unbounded crash-loop. The cycle fails closed instead, like any other
    # non-retryable error, and the poisoned response is cleared.
    from contrib.hyperliquid_perp.paper import scheduler as sched_mod

    def boom(raw, cfg):
        raise ValueError("corrupt stored response")

    monkeypatch.setattr(sched_mod, "parse_target_decision", boom)
    scheduler2 = _restart(db, clock, engine, _FakeProvider([]))
    with caplog.at_level(logging.ERROR, logger=sched_mod.__name__):
        r2 = scheduler2.poll()
    assert r2.event is CycleEvent.API_FAILED
    assert r2.error_type is None
    row = repo.get_decision_attempt(db.conn, r2.decision_attempt_id)
    assert row["status"] == "api_failed"
    assert row["error_type"] is None
    assert row["error_message"].startswith("non-retryable:")
    assert row["pending_raw_response"] is None  # never again presented as resumable
    # The row was the only durable copy (ai_outputs never stores raw text), so
    # clearing it without preserving the text would destroy the evidence the
    # post-mortem needs to tell a parser bug from a corrupted store.
    assert _RAW_LONG_1 in caplog.text
    db.close()


def test_a_start_plan_bug_still_exits_the_daemon(tmp_path, monkeypatch):
    db, clock, engine, scheduler, provider = _setup(tmp_path, [_decision("long", 1)], [_snap()])

    def blown(parsed, *, output_id):
        raise RuntimeError("engine bug")

    monkeypatch.setattr(engine, "start_plan", blown)
    # Deliberately NOT contained (the issue #163 split's one remaining exit):
    # the engine fail-stops on any escaped exception, so keeping the daemon up
    # would leave the position inside a live-looking process no engine watches.
    # The supervisor's restart rebuilds the engine from the DB.
    with pytest.raises(RuntimeError, match="engine bug"):
        scheduler.poll()
    db.close()


def test_audit_persist_failure_retries_the_persist_never_the_gate(tmp_path, monkeypatch):
    db, clock, engine, scheduler, provider = _setup(tmp_path, [_decision("long", 1)], [_snap()])
    real_start = engine.start_plan
    calls = {"n": 0}

    def counting(parsed, *, output_id):
        calls["n"] += 1
        return real_start(parsed, output_id=output_id)

    monkeypatch.setattr(engine, "start_plan", counting)
    _arm_lock_fault(monkeypatch, scheduler, "_insert_ai_output")
    # The engine committed a plan before the audit txn; failing the cycle
    # closed now would write "no action" into the audit trail while that plan
    # keeps filling — so the poll holds the decision AND its registration.
    assert scheduler.poll() is None
    assert calls["n"] == 1
    assert db.conn.execute("SELECT COUNT(*) FROM execution_plans").fetchone()[0] == 1
    r2 = scheduler.poll()
    assert r2.event is CycleEvent.COMPLETED
    assert calls["n"] == 1  # retried ONLY the persist — the gate never re-ran
    assert provider.decide_calls == 1
    assert repo.get_decision_attempt(db.conn, r2.decision_attempt_id)["status"] == "completed"
    assert db.conn.execute("SELECT COUNT(*) FROM ai_outputs").fetchone()[0] == 1
    db.close()


# --------------------------------------------------------------------------
# ai_inputs with a real (non-flat) position and an active plan
# --------------------------------------------------------------------------


def test_ai_input_reports_open_position_and_active_plan(tmp_path):
    from contrib.hyperliquid_perp.persistence.models import PositionState

    db = Database(tmp_path / "s2.db")
    accounting.initialize_run(
        db,
        run_id="r",
        mode="paper",
        initial_balance_usdc=D(1000),
        schema_version=1,
        initial_positions=[PositionState(coin="BTC", size=D("0.002"), entry_price=D(49000))],
    )
    clock = ManualClock(_T0)
    asset = AssetSpec(
        coin="BTC",
        sz_decimals=3,
        margin_schedule=MarginSchedule(coin="BTC", tiers=(MarginTier(D(0), D(50)),)),
    )
    risk = RiskConfig(leverage=D(5), max_target_margin_pct=60)
    engine = PaperExecutionEngine(
        db=db,
        run_id="r",
        asset=asset,
        clock=clock,
        provider=ScriptedSnapshotProvider("BTC", [_snap()]),
        risk_config=risk,
        decision_config=DecisionConfig(),
        paper_config=PaperTradingConfig.from_dict(None),
    )
    with db.transaction() as conn:
        repo.insert_execution_plan(
            conn,
            plan_id="seed-plan",  # not engine-shaped, so it can't shift the id sequence
            run_id="r",
            symbol="BTC",
            status="active",
            created_at=_T0,
            remaining_qty=D("0.003"),
        )
    scheduler = PaperScheduler(
        db=db,
        run_id="r",
        engine=engine,
        clock=clock,
        provider=_FakeProvider([_decision("long", 5)]),
        asset=asset,
        risk_config=risk,
        decision_config=DecisionConfig(),
    )
    result = scheduler.poll()
    assert result is not None and result.event is CycleEvent.COMPLETED
    inp = db.conn.execute("SELECT * FROM ai_inputs WHERE run_id = 'r'").fetchone()
    assert inp["current_position_side"] == "long"
    assert D(inp["current_position_size"]) == D("0.002")
    assert D(inp["entry_price"]) == D(49000)
    assert D(inp["position_notional"]) == D("0.002") * _MARK
    assert inp["current_margin_pct"] is not None
    # unrealized 0.002*(50000-49000)=2 folds into equity: wallet 1000 + 2.
    assert D(inp["account_equity"]) == D(1002)
    assert D(inp["unrealized_pnl"]) == D(2)
    assert inp["active_twap"] == 1
    assert D(inp["remaining_twap_qty"]) == D("0.003")
    assert D(inp["configured_leverage"]) == D(5)
    assert D(inp["max_target_margin_pct"]) == D(60)
    db.close()
