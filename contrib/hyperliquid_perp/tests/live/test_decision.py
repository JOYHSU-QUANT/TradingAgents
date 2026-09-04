"""Tests for the §18.2 background AI decision worker + driver (PR 5, Option A)."""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from contrib.hyperliquid_perp.domains.perp.margin import MarginSchedule, MarginTier
from contrib.hyperliquid_perp.domains.perp.risk_gate import (
    CurrentPositionState,
    RiskConfig,
    evaluate,
)
from contrib.hyperliquid_perp.domains.perp.target_decision import (
    DecisionConfig,
    DecisionMode,
    ParsedDecision,
    TargetDecision,
    TargetSide,
)
from contrib.hyperliquid_perp.live import decision as decision_mod
from contrib.hyperliquid_perp.live.decision import LiveDecisionDriver, LiveDecisionWorker
from contrib.hyperliquid_perp.paper import accounting
from contrib.hyperliquid_perp.paper.clock import ManualClock
from contrib.hyperliquid_perp.paper.engine import AssetSpec
from contrib.hyperliquid_perp.paper.scheduler import (
    CYCLE_INTERVAL,
    DecisionInput,
    RetryableDecisionError,
    parse_instant,
)
from contrib.hyperliquid_perp.persistence import repository as repo
from contrib.hyperliquid_perp.persistence.db import Database

from ..conftest import arm_lock_fault, poison_stored_parse


def _decision() -> ParsedDecision:
    return ParsedDecision(
        decision=TargetDecision(
            decision_mode=DecisionMode.SET_TARGET,
            target_side=TargetSide.LONG,
            requested_target_margin_pct=5,
            confidence=Decimal("0.8"),
            rationale="r",
            key_risks=("k",),
        ),
        is_valid=True,
        invalid_reason=None,
        raw_response="{}",
    )


class _Provider:
    def __init__(self, *, result=None, error=None, gate: threading.Event | None = None) -> None:
        self._result = result
        self._error = error
        self._gate = gate
        self.calls = 0

    def build_input(self, *, coin, as_of):  # not used by the worker
        raise NotImplementedError

    def request_decision(self, decision_input):
        self.calls += 1
        if self._gate is not None:
            self._gate.wait(2.0)  # hold until the test releases it
        if self._error is not None:
            raise self._error
        return self._result


def _await(worker, timeout=2.0):
    worker.join(timeout)
    deadline = time.time() + timeout
    while worker.busy and time.time() < deadline:
        time.sleep(0.01)


def test_result_is_returned_on_the_main_thread():
    parsed = _decision()
    worker = LiveDecisionWorker(provider=_Provider(result=parsed))
    worker.submit(decision_input=object())
    _await(worker)
    assert worker.has_result is True
    assert worker.poll() is parsed
    # Consuming clears it.
    assert worker.has_result is False
    assert worker.poll() is None


def test_busy_while_running_and_poll_returns_none():
    gate = threading.Event()
    worker = LiveDecisionWorker(provider=_Provider(result=_decision(), gate=gate))
    worker.submit(decision_input=object())
    assert worker.busy is True
    assert worker.poll() is None  # still computing
    gate.set()
    _await(worker)
    assert worker.poll() is not None


def test_cannot_submit_while_busy():
    gate = threading.Event()
    worker = LiveDecisionWorker(provider=_Provider(result=_decision(), gate=gate))
    worker.submit(decision_input=object())
    with pytest.raises(RuntimeError, match="already in flight"):
        worker.submit(decision_input=object())
    gate.set()
    _await(worker)


def test_retryable_error_reraised_on_poll():
    err = RetryableDecisionError("timeout", "the model timed out")
    worker = LiveDecisionWorker(provider=_Provider(error=err))
    worker.submit(decision_input=object())
    _await(worker)
    with pytest.raises(RetryableDecisionError, match="timed out"):
        worker.poll()
    worker2 = LiveDecisionWorker(provider=_Provider(result=_decision()))
    worker2.submit(decision_input=object())
    _await(worker2)
    assert worker2.poll() is not None


# -- LiveDecisionDriver -----------------------------------------------------

_T0 = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)


def _schedule() -> MarginSchedule:
    return MarginSchedule(coin="BTC", tiers=(MarginTier(Decimal(0), Decimal(50)),))


def _gate_result():
    return evaluate(
        _decision(),
        account_equity=Decimal(4000),
        current=CurrentPositionState.flat(),
        risk=RiskConfig(leverage=Decimal(1), max_target_margin_pct=60),
        decision_cfg=DecisionConfig(),
    )


class _DriverProvider:
    def __init__(
        self, decision_input, *, parsed, build_error=None, request_error=None, request_gate=None
    ) -> None:
        self._di = decision_input
        self._parsed = parsed
        self._build_error = build_error
        self._request_error = request_error
        self._request_gate = request_gate  # hold the LLM call until the test releases it
        self.builds = 0
        self.requests = 0

    def build_input(self, *, coin, as_of):
        self.builds += 1
        if self._build_error is not None:
            raise self._build_error
        return self._di

    def request_decision(self, decision_input):
        self.requests += 1
        if self._request_gate is not None:
            self._request_gate.wait(30.0)
        if self._request_error is not None:
            raise self._request_error
        return self._parsed


class _DriverEngine:
    def __init__(self, reg, *, start_error=None, first_regs=()) -> None:
        self._reg = reg
        self._first = list(first_regs)  # served in order before the steady reg
        self._start_error = start_error
        self.plans: list = []

    def start_plan(self, parsed, *, output_id):
        if self._start_error is not None:
            raise self._start_error
        self.plans.append(output_id)
        if self._first:
            return self._first.pop(0)
        return self._reg

    def liquidation_price(self, position, mark):
        return None


def _decision_input():
    ctx = SimpleNamespace(
        mark_price=Decimal(50000), mid_price=Decimal(50000), funding_rate=Decimal("0.0001")
    )
    return DecisionInput(
        context=ctx,
        candle_start=None,
        candle_end=None,
        input_payload_path=None,
        input_payload_hash=None,
        prompt_version="v1",
        context_shape="price|market|funding|indicators()",
        format_fingerprint="97aa0feaa4496d6f",
        model="m",
    )


def _driver(
    tmp_path,
    *,
    build_error=None,
    request_error=None,
    start_error=None,
    first_regs=(),
    request_gate=None,
    parsed=None,
):
    from contrib.hyperliquid_perp.live.engine import PlanRegistration

    db = Database(tmp_path / "d.db")
    accounting.initialize_run(
        db, run_id="r", mode="live", initial_balance_usdc=Decimal(4000), schema_version=7
    )
    clock = ManualClock(_T0)
    reg = PlanRegistration(
        gate=_gate_result(),
        plan_id="p1",
        disposition=None,
        reason=None,
        mark_price=Decimal(50000),
        account_equity=Decimal(4000),
    )
    engine = _DriverEngine(reg, start_error=start_error, first_regs=first_regs)
    provider = _DriverProvider(
        _decision_input(),
        parsed=_decision() if parsed is None else parsed,
        build_error=build_error,
        request_error=request_error,
        request_gate=request_gate,
    )
    worker = LiveDecisionWorker(provider=provider)
    driver = LiveDecisionDriver(
        db=db,
        run_id="r",
        coin="BTC",
        asset=AssetSpec(coin="BTC", sz_decimals=3, margin_schedule=_schedule()),
        risk_config=RiskConfig(leverage=Decimal(1), max_target_margin_pct=60),
        decision_config=DecisionConfig(),
        engine=engine,
        worker=worker,
        provider=provider,
        clock=clock,
    )
    return db, clock, driver, engine, worker, provider


def test_driver_runs_a_full_cycle(tmp_path):
    db, clock, driver, engine, worker, provider = _driver(tmp_path)
    # Tick 1: a fresh run is due -> build input, persist ai_input, submit to worker.
    assert driver.pump() == "cycle_started"
    assert provider.builds == 1
    _await(worker)  # the instant fake may already be done — settle it deterministically
    # Tick 2: worker done -> collect, gate, persist ai_output + terminal records.
    assert driver.pump() == "completed"
    assert engine.plans == ["r|" + _T0.isoformat() + "#out1"]
    row = db.conn.execute("SELECT * FROM decision_attempts WHERE run_id='r'").fetchone()
    assert row["status"] == "completed"
    out = db.conn.execute("SELECT * FROM ai_outputs WHERE run_id='r'").fetchone()
    assert out is not None and out["order_created"] in (0, 1)
    # The live writer carries all three segmentation keys off the DecisionInput
    # (issues #97, #129) — the same audit_rows path the paper scheduler uses.
    ai_in = db.conn.execute(
        "SELECT prompt_version, context_shape, format_fingerprint FROM ai_inputs WHERE run_id='r'"
    ).fetchone()
    assert tuple(ai_in) == ("v1", "price|market|funding|indicators()", "97aa0feaa4496d6f")
    state = repo.get_scheduler_state(db.conn, "r")
    assert state["next_decision_at"] is not None
    # Not due again until the next 4h boundary.
    assert driver.pump() is None


def test_driver_writes_the_audit_row_from_the_books_the_provider_carried(tmp_path):
    # Issue #134 on the live lane: the same one-read contract the paper
    # scheduler keeps — books carried on the input are written as-is, and the
    # prologue makes none of the three reads; a bookless input still gets the
    # lane's own read (and its own "no ledger" refusal, unchanged).
    from contrib.hyperliquid_perp.paper.position_facts import read_books

    # read_books' three statements (the prologue's SL/TP read is another fact).
    reads = (
        "SELECT * FROM current_account_state",
        "SELECT * FROM current_positions",
        "MAX(timestamp) FROM fills",
    )
    db, clock, driver, engine, worker, provider = _driver(tmp_path)
    books = read_books(db, "r", "BTC")
    provider._di = DecisionInput(context=_decision_input().context, books=books)
    statements: list[str] = []
    original = driver._persist_ai_input

    def traced(*args, **kwargs):
        db.conn.set_trace_callback(statements.append)
        try:
            return original(*args, **kwargs)
        finally:
            db.conn.set_trace_callback(None)

    driver._persist_ai_input = traced
    assert driver.pump() == "cycle_started"
    assert statements and not [s for s in statements if any(r in s for r in reads)]
    row = db.conn.execute("SELECT wallet_balance FROM ai_inputs WHERE run_id='r'").fetchone()
    assert row["wallet_balance"] == str(books.ledger.wallet_balance) == "4000"


def test_driver_build_failure_is_api_failed(tmp_path):
    err = RetryableDecisionError("connection", "market data unreachable")
    db, clock, driver, engine, worker, provider = _driver(tmp_path, build_error=err)
    assert driver.pump() == "api_failed"
    row = db.conn.execute("SELECT * FROM decision_attempts WHERE run_id='r'").fetchone()
    assert row["status"] == "api_failed"
    assert row["error_type"] == "connection"
    assert engine.plans == []  # no order on a failed cycle (§10.2 fail closed)
    state = repo.get_scheduler_state(db.conn, "r")
    assert state["next_decision_at"] is not None  # re-anchored to the next cycle


def test_driver_build_nonretryable_error_fails_closed_and_recovers(tmp_path):
    """A NON-retryable exception in build_input is a bug, not a §6.2 API failure. The
    driver must still fail the cycle CLOSED — never leave an unresolved in_progress
    row with next_decision_at in the past, which crash-loops every tick on the
    duplicate attempt_id (C2)."""
    db, clock, driver, engine, worker, provider = _driver(tmp_path, build_error=ValueError("boom"))
    assert driver.pump() == "api_failed"
    row = db.conn.execute("SELECT * FROM decision_attempts WHERE run_id='r'").fetchone()
    assert row["status"] == "api_failed"
    assert row["error_type"] is None  # not a §6.2 vocabulary word
    assert "boom" in (row["error_message"] or "")
    state = repo.get_scheduler_state(db.conn, "r")
    assert state["next_decision_at"] is not None  # re-anchored, not stuck in the past
    assert driver.pump() is None  # not due -> no per-tick crash-loop
    # The next boundary starts a FRESH attempt id (re-anchored), so it fails cleanly
    # again rather than raising IntegrityError on the old duplicate row.
    clock.set(parse_instant(state["next_decision_at"]))
    assert driver.pump() == "api_failed"


def test_driver_worker_nonretryable_error_fails_closed_and_recovers(tmp_path):
    """A NON-retryable exception on the worker thread must not wedge the driver:
    _inflight has to be cleared so the loop keeps deciding, rather than silently
    stalling forever on the empty one-shot poll slot (C1)."""
    db, clock, driver, engine, worker, provider = _driver(
        tmp_path, request_error=RuntimeError("kaboom")
    )
    assert driver.pump() == "cycle_started"  # submitted to the worker
    _await(worker)  # the worker finishes with the error
    assert driver.pump() == "api_failed"  # collected, failed closed (not wedged)
    assert driver._inflight is None  # the wedge is gone
    row = db.conn.execute("SELECT * FROM decision_attempts WHERE run_id='r'").fetchone()
    assert row["status"] == "api_failed"
    assert row["error_type"] is None
    assert engine.plans == []  # no order off a failed decision (§10.2)
    state = repo.get_scheduler_state(db.conn, "r")
    assert state["next_decision_at"] is not None  # re-anchored
    assert driver.pump() is None  # not the old silent "not ready" forever
    # A later boundary starts a brand-new cycle — the driver recovered.
    clock.set(parse_instant(state["next_decision_at"]))
    assert driver.pump() == "cycle_started"


def _stale_refusal() -> RetryableDecisionError:
    """What build_input raises when engine_bridge refuses a stalled feed."""
    from contrib.hyperliquid_perp.common.constants import STALE_MARKET_DATA_ERROR

    return RetryableDecisionError(
        STALE_MARKET_DATA_ERROR, "... past the 12h 0m 0s freshness limit (3 x 4h) ..."
    )


def test_driver_escalates_consecutive_stale_feed_refusals(tmp_path, caplog):
    # Issue #50: a live run whose every cycle is refused as stale holds its
    # position on SL/TP alone with only a per-cycle WARNING to show for it.
    # From the third consecutive refusal the log says so at ERROR. This is the
    # WIRING pin — that the driver hands the refusal's §6.2 class to the shared
    # counter at all; the escalation shape itself (and its reset on an ordinary
    # api_failed) is pinned once, on the shared function in
    # tests/paper/test_validation.py.
    import logging

    from contrib.hyperliquid_perp.common.no_decision import NO_DECISION_STREAK_THRESHOLD

    db, clock, driver, engine, worker, provider = _driver(tmp_path, build_error=_stale_refusal())
    with caplog.at_level(logging.WARNING, logger="contrib.hyperliquid_perp.common.no_decision"):
        for _ in range(NO_DECISION_STREAK_THRESHOLD):
            assert driver.pump() == "api_failed"
            state = repo.get_scheduler_state(db.conn, "r")
            clock.set(parse_instant(state["next_decision_at"]))
    levels = [r.levelno for r in caplog.records if "decision cycle for r" in r.getMessage()]
    assert levels == [logging.WARNING] * (NO_DECISION_STREAK_THRESHOLD - 1) + [logging.ERROR]
    db.close()


def test_driver_streak_is_reset_by_a_cycle_that_decided(tmp_path, caplog):
    # The counter must be fed on the SUCCESS path too, not only from
    # `_fail_closed`: the store query it mirrors breaks on any decided cycle,
    # so a driver that only counted failures would log "3 consecutive, ~12h
    # with no decision" over a run that decided in between — and `validate`,
    # which that same ERROR tells the operator to check, would show nothing.
    import logging

    db, clock, driver, engine, worker, provider = _driver(tmp_path, build_error=_stale_refusal())

    def _advance():
        state = repo.get_scheduler_state(db.conn, "r")
        clock.set(parse_instant(state["next_decision_at"]))

    with caplog.at_level(logging.WARNING, logger="contrib.hyperliquid_perp.common.no_decision"):
        assert driver.pump() == "api_failed"  # streak 1
        _advance()
        assert driver.pump() == "api_failed"  # streak 2
        _advance()
        provider._build_error = None
        assert driver.pump() == "cycle_started"
        _await(worker)
        assert driver.pump() == "completed"  # resets
        _advance()
        provider._build_error = _stale_refusal()
        assert driver.pump() == "api_failed"  # streak 1 again, not 3
    notes = [r for r in caplog.records if "decision cycle for r" in r.getMessage()]
    assert [r.levelno for r in notes] == [logging.WARNING] * 3
    assert "1 consecutive" in notes[-1].getMessage()
    db.close()


def test_driver_counts_a_refused_cycle_once_even_when_its_write_keeps_failing(tmp_path, caplog):
    # The counter is advanced where the fail record is ARMED, not where it is
    # written: ``_flush_pending_fail`` re-enters ``_fail_cycle`` on every pump
    # while a failing write keeps it armed, so counting there would let ONE
    # refused cycle reach the ERROR threshold within three ~10s ticks and claim
    # "3 consecutive (~12h with no decision)".
    import logging

    db, clock, driver, engine, worker, provider = _driver(tmp_path, build_error=_stale_refusal())

    calls = {"n": 0}
    real_fail_cycle = driver._fail_cycle

    def _flaky_fail_cycle(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            raise sqlite3.OperationalError("database is locked")
        return real_fail_cycle(*args, **kwargs)

    driver._fail_cycle = _flaky_fail_cycle
    with caplog.at_level(logging.WARNING, logger="contrib.hyperliquid_perp.common.no_decision"):
        for _ in range(2):
            with pytest.raises(sqlite3.OperationalError):
                driver.pump()
        assert driver.pump() == "api_failed"
    assert calls["n"] == 3  # three write attempts...
    notes = [r for r in caplog.records if "decision cycle for r" in r.getMessage()]
    assert len(notes) == 1  # ...one counted cycle
    assert notes[0].levelno == logging.WARNING
    db.close()


def test_driver_worker_retryable_error_is_api_failed(tmp_path):
    """A RETRYABLE worker error surfaced through poll() is handled by pump's in-flight
    guard with its §6.2 error_type preserved (not misrecorded as an internal bug)."""
    db, clock, driver, engine, worker, provider = _driver(
        tmp_path, request_error=RetryableDecisionError("timeout", "model timed out")
    )
    assert driver.pump() == "cycle_started"
    _await(worker)
    assert driver.pump() == "api_failed"
    assert driver._inflight is None
    row = db.conn.execute("SELECT * FROM decision_attempts WHERE run_id='r'").fetchone()
    assert row["status"] == "api_failed"
    assert row["error_type"] == "timeout"  # retryable type preserved


def test_fail_cycle_past_its_own_boundary_reanchors_from_now(tmp_path):
    """A cycle that fails AFTER its own next boundary already passed (the failure
    landed 2.5 cycles late) must re-anchor next_decision_at from NOW —
    scheduled_at + interval would be in the past, leaving the driver immediately
    due again every tick."""
    db, clock, driver, engine, worker, provider = _driver(
        tmp_path, request_error=RetryableDecisionError("timeout", "model timed out")
    )
    assert driver.pump() == "cycle_started"  # scheduled_at = _T0
    _await(worker)
    late = _T0 + 2.5 * CYCLE_INTERVAL  # now > scheduled_at + interval
    clock.set(late)
    assert driver.pump() == "api_failed"
    state = repo.get_scheduler_state(db.conn, "r")
    assert parse_instant(state["next_decision_at"]) == late + CYCLE_INTERVAL
    assert driver.pump() is None  # not due — the re-anchor is in the future


def test_driver_gate_nonretryable_error_fails_closed_and_recovers(tmp_path):
    """A non-retryable error in the GATE step (start_plan / persist) must also fail
    the cycle closed via pump's guard, not wedge the driver — _gate is reached from
    pump (after _collect polls the worker), inside pump's try guard."""
    db, clock, driver, engine, worker, provider = _driver(
        tmp_path, start_error=RuntimeError("gate boom")
    )
    assert driver.pump() == "cycle_started"
    _await(worker)  # the worker returns a valid decision; the gate is what fails
    assert driver.pump() == "api_failed"  # _collect -> _gate raises -> pump guard
    assert driver._inflight is None
    row = db.conn.execute("SELECT * FROM decision_attempts WHERE run_id='r'").fetchone()
    assert row["status"] == "api_failed"
    assert row["error_type"] is None
    state = repo.get_scheduler_state(db.conn, "r")
    assert state["next_decision_at"] is not None
    clock.set(parse_instant(state["next_decision_at"]))
    assert driver.pump() == "cycle_started"  # recovered, not wedged on the gate


# -- resume_startup (§3.1 restart adoption) ----------------------------------

_RAW_LONG = (
    '{"decision_mode": "set_target", "target_side": "long", '
    '"requested_target_margin_pct": 5, "confidence": 0.8, '
    '"rationale": "resume", "key_risks": ["a risk"]}'
)


def _strand_attempt(db, *, raw):
    """Persist the in_progress attempt row a crashed process would leave behind."""
    from contrib.hyperliquid_perp.persistence import ids

    attempt_id = ids.decision_attempt_id("r", _T0)
    with db.transaction() as conn:
        repo.insert_decision_attempt(
            conn,
            decision_attempt_id=attempt_id,
            timestamp=_T0,
            mode="live",
            run_id="r",
            scheduled_at=_T0,
            attempt_count=1,
            status="in_progress",
            first_attempt_at=_T0,
            last_attempt_at=_T0,
        )
        if raw is not None:
            # Through the one writer, as the crashed process's own store did.
            repo.store_pending_response(conn, attempt_id, raw, timestamp=_T0)
    return attempt_id


def test_resume_startup_resumes_persisted_response_without_reask(tmp_path):
    """§3.1: a stranded in_progress attempt WITH a stored response resumes at the
    gate — the parsed decision is reconstructed from the persisted text and the
    AI is never asked a second time."""
    db, clock, driver, engine, worker, provider = _driver(tmp_path)
    attempt_id = _strand_attempt(db, raw=_RAW_LONG)
    assert driver.resume_startup() == "resumed"
    assert driver._inflight is not None
    assert driver._inflight.parsed is not None and driver._inflight.parsed.is_valid
    # The next pump completes the cycle from the STORED text.
    assert driver.pump() == "completed"
    assert provider.requests == 0  # never a second AI call
    assert engine.plans == [f"{attempt_id}#out1"]
    row = db.conn.execute("SELECT * FROM decision_attempts WHERE run_id='r'").fetchone()
    assert row["status"] == "completed"


def test_resume_startup_without_response_fails_closed(tmp_path):
    """A stranded attempt the AI never answered fails closed: the row goes
    api_failed and next_decision_at advances, so the driver no longer wedges on
    the duplicate attempt id every tick."""
    db, clock, driver, engine, worker, provider = _driver(tmp_path)
    _strand_attempt(db, raw=None)
    assert driver.resume_startup() == "api_failed"
    assert driver._inflight is None
    row = db.conn.execute("SELECT * FROM decision_attempts WHERE run_id='r'").fetchone()
    assert row["status"] == "api_failed"
    state = repo.get_scheduler_state(db.conn, "r")
    assert state["next_decision_at"] is not None
    assert parse_instant(state["next_decision_at"]) > _T0  # re-anchored
    assert driver.pump() is None  # not due -> no per-tick crash-loop
    assert engine.plans == []  # no order off the dead cycle (§10.2)


def test_resume_startup_with_a_poisoned_stored_response_fails_closed(
    tmp_path, monkeypatch, caplog
):
    """issue #180: resume_startup runs BEFORE the loop's tick guard exists, so a
    re-parse that raises would exit the daemon at startup and every supervised
    restart would resume into the same deterministic parse — a crash-loop with
    the real position and its SL/TP unwatched in between. The cycle fails
    closed instead, exactly as the paper lane's _resume_pending does."""
    db, clock, driver, engine, worker, provider = _driver(tmp_path)
    attempt_id = _strand_attempt(db, raw=_RAW_LONG)
    calls = poison_stored_parse(monkeypatch, decision_mod)
    with caplog.at_level(logging.ERROR, logger=decision_mod.__name__):
        assert driver.resume_startup() == "api_failed"  # returned, never raised
    assert calls["n"] == 1
    assert driver._inflight is None
    row = repo.get_decision_attempt(db.conn, attempt_id)
    assert row["status"] == "api_failed"
    assert row["error_type"] is None
    assert row["error_message"].startswith("non-retryable:")
    assert row["pending_raw_response"] is None  # never again presented as resumable
    # The row was the only durable copy (ai_outputs never stores raw text), so
    # clearing it without preserving the text would destroy the evidence the
    # post-mortem needs to tell a parser bug from a corrupted store. The line
    # logs the text with %r, so its repr is what the log carries.
    assert repr(_RAW_LONG) in caplog.text
    assert provider.requests == 0  # never a second AI call
    assert engine.plans == []  # no order off the dead cycle (§10.2)


def test_a_poisoned_resume_is_not_retried_by_the_following_pumps(tmp_path, monkeypatch):
    """The failed cycle re-anchored next_decision_at: later pumps neither
    re-parse the (now cleared) text nor collide on the duplicate attempt id, so
    there is no per-tick crash-loop either."""
    db, clock, driver, engine, worker, provider = _driver(tmp_path)
    _strand_attempt(db, raw=_RAW_LONG)
    calls = poison_stored_parse(monkeypatch, decision_mod)
    assert driver.resume_startup() == "api_failed"
    state = repo.get_scheduler_state(db.conn, "r")
    assert parse_instant(state["next_decision_at"]) > _T0  # re-anchored
    assert driver.pump() is None  # not due
    assert driver.pump() is None
    assert calls["n"] == 1  # the poisoned parse ran exactly once, at startup
    assert provider.requests == 0
    assert repo.find_in_progress_attempt(db.conn, "r") is None


def test_a_startup_adoption_whose_write_missed_is_retried_by_pump(tmp_path, monkeypatch):
    """The containment in the loop (issue #180 review) must not itself become
    the wedge. Adoption's OWN fail-closed write can miss — an operator's
    export/validate holding the lock at boot — and the caller now contains that
    raise instead of exiting. But the stranded attempt still owns
    next_decision_at, so a pump that fell through to _start would re-derive its
    deterministic id and collide on the primary key every tick, forever: the
    C2 wedge, reached through the containment. pump retries the ADOPTION until
    it lands instead."""
    db, clock, driver, engine, worker, provider = _driver(tmp_path)
    _strand_attempt(db, raw=None)  # the AI never answered — no pending_fail lane
    state = arm_lock_fault(monkeypatch, repo, "update_decision_attempt")
    with pytest.raises(sqlite3.OperationalError):
        driver.resume_startup()  # the caller contains this and starts the loop
    assert state["fired"] == 1
    assert driver._adopted is False  # the step is owed, so pump must not skip it
    assert repo.find_in_progress_attempt(db.conn, "r") is not None  # still stranded
    # The lock has cleared. The next pump must adopt, not start a fresh cycle.
    assert driver.pump() == "api_failed"
    assert driver._adopted is True  # ... and the step is not owed twice
    row = db.conn.execute("SELECT * FROM decision_attempts WHERE run_id='r'").fetchone()
    assert row["status"] == "api_failed"
    assert repo.find_in_progress_attempt(db.conn, "r") is None
    # ... and once adopted, the driver goes back to ordinary scheduling.
    assert driver.pump() is None  # re-anchored, not due
    assert provider.requests == 0
    assert engine.plans == []


def test_resume_startup_with_no_stranded_attempt_is_none(tmp_path):
    db, clock, driver, engine, worker, provider = _driver(tmp_path)
    assert driver.resume_startup() is None
    assert driver._inflight is None
    # The step is done even though there was nothing to adopt: an unlatched
    # flag would leave every pump of the run's life re-reading the row.
    assert driver._adopted is True


def test_a_cycle_failing_closed_after_its_store_clears_the_stored_response(tmp_path):
    """The live fail-closed path reaches the repository's terminal clear (issue #181).

    A cycle whose §3.1 store landed and then failed closed — here the gate
    blows up, so pump's in-flight guard records api_failed — passes nothing
    about the response to its terminal write; the repository lands the
    column NULL anyway (pinned at the repository level in
    tests/persistence), so no api_failed row ever presents the consumed
    text as resumable state.
    """
    db, clock, driver, engine, worker, provider = _driver(
        tmp_path, start_error=RuntimeError("gate blew up")
    )
    _strand_attempt(db, raw=_RAW_LONG)
    assert driver.resume_startup() == "resumed"
    assert driver.pump() == "api_failed"
    row = db.conn.execute("SELECT * FROM decision_attempts WHERE run_id='r'").fetchone()
    assert row["status"] == "api_failed"
    assert row["pending_raw_response"] is None


# -- S2: post-registration persist failure retries the persist only ----------


def test_registered_plan_persist_failure_retries_persist_only(tmp_path, monkeypatch):
    """S2: once start_plan REGISTERED a plan, a failing audit persist raises
    _PlanRegisteredPersistError out of pump (never 'api_failed' — that would
    falsify the audit trail while real slices go out); _inflight is retained and
    the next pump retries ONLY the persist — start_plan never runs twice."""
    from contrib.hyperliquid_perp.live.decision import _PlanRegisteredPersistError

    db, clock, driver, engine, worker, provider = _driver(tmp_path)
    real_insert = repo.insert_ai_output
    boom = {"left": 1}

    def flaky(*args, **kwargs):
        if boom["left"]:
            boom["left"] -= 1
            raise RuntimeError("ai_outputs store down")
        return real_insert(*args, **kwargs)

    monkeypatch.setattr(repo, "insert_ai_output", flaky)
    assert driver.pump() == "cycle_started"
    _await(worker)
    with pytest.raises(_PlanRegisteredPersistError):
        driver.pump()  # the gate ran, the plan registered, the persist failed
    assert len(engine.plans) == 1  # start_plan ran exactly once
    assert driver._inflight is not None  # retained, registration cached
    assert driver._inflight.registration is not None
    row = db.conn.execute("SELECT * FROM decision_attempts WHERE run_id='r'").fetchone()
    assert row["status"] == "in_progress"  # the failed transaction rolled back
    # The next pump retries the persist alone — never a second start_plan.
    assert driver.pump() == "completed"
    assert len(engine.plans) == 1
    row = db.conn.execute("SELECT * FROM decision_attempts WHERE run_id='r'").fetchone()
    assert row["status"] == "completed"


# -- 2026-07-23: collected-decision store failure retries the persist only ----


def test_collected_decision_store_failure_retries_persist_only(tmp_path, monkeypatch):
    """poll() is one-shot: the §3.1 store failing AFTER the worker slot was
    consumed must not discard the paid-for decision as api_failed. pump raises
    _PendingResponsePersistError (tick guard -> recoverable safe mode), the
    parsed decision survives on _inflight, and the next pump retries ONLY the
    store — never re-polling the worker, never re-asking the AI, and never
    gating an unstored decision (§3.1: resumability before action)."""

    from contrib.hyperliquid_perp.live.decision import _PendingResponsePersistError

    db, clock, driver, engine, worker, provider = _driver(tmp_path)
    assert driver.pump() == "cycle_started"
    _await(worker)
    arm_lock_fault(monkeypatch, repo, "store_pending_response")
    with pytest.raises(_PendingResponsePersistError):
        driver.pump()  # collected, but the store missed
    inflight = driver._inflight
    assert inflight is not None and inflight.parsed is not None  # decision survived
    assert inflight.raw_stored is False
    assert engine.plans == []  # an unstored decision never gates
    row = db.conn.execute("SELECT * FROM decision_attempts WHERE run_id='r'").fetchone()
    assert row["status"] == "in_progress"  # never falsified to api_failed
    assert driver.pump() == "completed"  # the store retried alone, then gated
    assert provider.requests == 1  # one paid AI call, total
    assert len(engine.plans) == 1
    row = db.conn.execute("SELECT * FROM decision_attempts WHERE run_id='r'").fetchone()
    assert row["status"] == "completed"


# -- T4: gate=None (pending market data) holds the parsed decision -----------


def test_gate_pending_market_data_holds_parsed_and_retries(tmp_path):
    """start_plan returning gate=None (no fresh snapshot) means the gate never
    ran: pump reports pending_market_data, the parsed decision survives, and a
    later pump retries the GATE — never the AI."""
    from contrib.hyperliquid_perp.live.engine import PlanRegistration

    pending = PlanRegistration(
        gate=None, plan_id=None, disposition=None, reason="pending_market_data"
    )
    db, clock, driver, engine, worker, provider = _driver(tmp_path, first_regs=[pending])
    assert driver.pump() == "cycle_started"
    _await(worker)
    assert driver.pump() == "pending_market_data"
    assert driver._inflight is not None and driver._inflight.parsed is not None
    assert provider.requests == 1  # the AI was asked exactly once
    assert driver.pump() == "completed"  # the gate retried with fresh data
    assert provider.requests == 1  # ... and the AI still only once
    assert len(engine.plans) == 2  # start_plan re-ran; the AI did not


# -- an invalid answer is never stored as resumable ---------------------------

# An engine answering with BYTES rather than text: parse_target_decision keeps
# a non-str answer as its repr for the audit trail and fails it closed BEFORE
# extraction, "its repr may embed a JSON-looking span that must never be parsed
# as a live decision". That repr IS a str on resume — hence this fixture.
_BYTES_ANSWER = (
    b'{"decision_mode": "set_target", "target_side": "long", '
    b'"requested_target_margin_pct": 5, "confidence": 0.8, '
    b'"rationale": "drift", "key_risks": ["a risk"]}'
)


def test_an_invalid_answer_is_never_stored_as_resumable(tmp_path):
    """PR #204 review: a parse that failed closed is no decision to resume, and
    its preserved text is not guaranteed to re-parse to the same verdict — so
    the §3.1 store is skipped for it. Nothing is lost: with no stored response
    a crash in that window fails the cycle closed, the same held position and
    no order the gate records for an invalid answer."""
    from contrib.hyperliquid_perp.domains.perp.target_decision import parse_target_decision
    from contrib.hyperliquid_perp.live.engine import PlanRegistration

    invalid = parse_target_decision(_BYTES_ANSWER, DecisionConfig())
    assert invalid.is_valid is False  # this process refuses it outright
    # ... and THIS is why storing it would be unsafe: the preserved repr is a
    # str on resume, and re-parsing lifts out the live target just refused.
    assert parse_target_decision(invalid.raw_response, DecisionConfig()).is_valid is True

    # Hold the cycle at the gate so the post-store, pre-gate row is observable.
    pending = PlanRegistration(
        gate=None, plan_id=None, disposition=None, reason="pending_market_data"
    )
    db, clock, driver, engine, worker, provider = _driver(
        tmp_path, parsed=invalid, first_regs=[pending]
    )
    assert driver.pump() == "cycle_started"
    _await(worker)
    assert driver.pump() == "pending_market_data"
    row = repo.find_in_progress_attempt(db.conn, "r")
    assert row["pending_raw_response"] is None  # nothing a restart could resume
    # A valid answer in the same spot DOES land — the skip is about validity,
    # not about the gate being blocked.
    other = tmp_path / "valid"
    other.mkdir()
    db2, _c, driver2, _e, worker2, _p = _driver(other, first_regs=[pending])
    assert driver2.pump() == "cycle_started"
    _await(worker2)
    assert driver2.pump() == "pending_market_data"
    assert repo.find_in_progress_attempt(db2.conn, "r")["pending_raw_response"] == "{}"


def test_a_restart_over_an_unstored_invalid_answer_fails_closed(tmp_path):
    """The other half: with nothing stored, the restart adopts the stranded row
    through the "AI never answered" branch — api_failed, no second AI call, no
    order off the answer this run had already refused."""
    db, clock, driver, engine, worker, provider = _driver(tmp_path)
    _strand_attempt(db, raw=None)  # what the skipped store leaves behind
    assert driver.resume_startup() == "api_failed"
    row = db.conn.execute("SELECT * FROM decision_attempts WHERE run_id='r'").fetchone()
    assert row["status"] == "api_failed"
    assert provider.requests == 0
    assert engine.plans == []


# -- R4 loop: fail-record double fault (pending-fail retry) -------------------


def test_fail_record_double_fault_retries_write_only(tmp_path, monkeypatch):
    """The fail-closed record itself failing (a double fault) must not lose the
    cycle: pump keeps _inflight with pending_fail armed, retries ONLY the
    api_failed write next pump -- never re-polling the one-shot worker slot (the
    C1 wedge: an emptied slot reads as "not ready" forever) and never re-asking
    the AI."""
    import sqlite3

    db, clock, driver, engine, worker, provider = _driver(
        tmp_path, request_error=RetryableDecisionError("timeout", "model timed out")
    )
    assert driver.pump() == "cycle_started"
    _await(worker)
    real = repo.update_decision_attempt

    def _locked(conn, attempt_id, **kw):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(repo, "update_decision_attempt", _locked)
    with pytest.raises(sqlite3.OperationalError):
        driver.pump()  # the retryable failure's OWN record write fails
    assert driver._inflight is not None
    assert driver._inflight.pending_fail is not None  # verdict survived the fault
    # The wedge this guards against: pump must NOT read the emptied poll slot.
    monkeypatch.setattr(repo, "update_decision_attempt", real)
    assert driver.pump() == "api_failed"  # retried ONLY the write
    assert driver._inflight is None
    assert provider.requests == 1  # the AI was never re-asked
    row = db.conn.execute("SELECT * FROM decision_attempts WHERE run_id='r'").fetchone()
    assert row["status"] == "api_failed"
    assert row["error_type"] == "timeout"  # the original verdict, not a new one
    state = repo.get_scheduler_state(db.conn, "r")
    assert state["next_decision_at"] is not None  # re-anchored
    # And the driver is alive: the next boundary starts a fresh cycle.
    clock.set(parse_instant(state["next_decision_at"]))
    assert driver.pump() == "cycle_started"


def test_start_fail_record_double_fault_adopts_and_retries(tmp_path, monkeypatch):
    """_start's failure path shares the guard: the attempt row is already
    in_progress, so losing the fail record would duplicate-crash-loop on the
    deterministic attempt id every tick (C2). The driver adopts the failed
    cycle as in-flight (pending_fail armed) and retries only the write."""
    import sqlite3

    err = RetryableDecisionError("connection", "market data unreachable")
    db, clock, driver, engine, worker, provider = _driver(tmp_path, build_error=err)
    real = repo.update_decision_attempt

    def _locked(conn, attempt_id, **kw):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(repo, "update_decision_attempt", _locked)
    with pytest.raises(sqlite3.OperationalError):
        driver.pump()  # build fails, then the fail record's write also fails
    assert driver._inflight is not None
    assert driver._inflight.pending_fail is not None
    monkeypatch.setattr(repo, "update_decision_attempt", real)
    assert driver.pump() == "api_failed"  # no duplicate INSERT, just the write
    assert driver._inflight is None
    row = db.conn.execute("SELECT * FROM decision_attempts WHERE run_id='r'").fetchone()
    assert row["status"] == "api_failed"
    assert row["error_type"] == "connection"
    state = repo.get_scheduler_state(db.conn, "r")
    clock.set(parse_instant(state["next_decision_at"]))
    assert driver.pump() == "api_failed"  # fresh cycle, clean failure — no crash-loop


def test_worker_submit_failure_fails_cycle_closed_and_recovers(tmp_path, monkeypatch):
    """r13 fix: worker.submit raising (Thread.start under resource pressure) is
    the one wedge the earlier guards could not catch — the attempt row is
    in_progress, no thread ever ran, and poll() would answer None forever. The
    wrapped submit fails the cycle CLOSED like any other start failure, and the
    next due cycle submits a fresh worker thread."""
    db, clock, driver, engine, worker, provider = _driver(tmp_path)
    real_submit = worker.submit
    boom = {"left": 1}

    def _flaky_submit(decision_input):
        if boom["left"]:
            boom["left"] -= 1
            raise RuntimeError("can't start new thread")
        return real_submit(decision_input)

    monkeypatch.setattr(worker, "submit", _flaky_submit)
    assert driver.pump() == "api_failed"  # failed closed, never wedged
    assert driver._inflight is None
    row = db.conn.execute("SELECT * FROM decision_attempts WHERE run_id='r'").fetchone()
    assert row["status"] == "api_failed"
    assert row["error_type"] is None  # a bug, not a §6.2 vocabulary word
    assert "non-retryable" in (row["error_message"] or "")
    assert "can't start new thread" in (row["error_message"] or "")
    state = repo.get_scheduler_state(db.conn, "r")
    assert state["next_decision_at"] is not None  # re-anchored
    assert driver.pump() is None  # not due — no per-tick crash-loop
    # A later due cycle starts FRESH (a new worker thread), not wedged on the
    # never-started one.
    clock.set(parse_instant(state["next_decision_at"]))
    assert driver.pump() == "cycle_started"
    _await(worker)
    assert driver.pump() == "completed"


# -- R4 loop: shutdown salvage of a completed-but-unpolled decision -----------


def test_salvage_shutdown_persists_completed_unpolled_decision(tmp_path):
    db, clock, driver, engine, worker, provider = _driver(tmp_path)
    assert driver.pump() == "cycle_started"
    _await(worker)  # the decision finished, but shutdown lands before the poll
    assert driver.salvage_shutdown() is True
    row = db.conn.execute("SELECT * FROM decision_attempts WHERE run_id='r'").fetchone()
    assert row["status"] == "in_progress"
    assert row["pending_raw_response"]  # stored for resume
    # A restarted driver resumes from the stored text — never a second AI call.
    driver2 = LiveDecisionDriver(
        db=db,
        run_id="r",
        coin="BTC",
        asset=AssetSpec(coin="BTC", sz_decimals=3, margin_schedule=_schedule()),
        risk_config=RiskConfig(leverage=Decimal(1), max_target_margin_pct=60),
        decision_config=DecisionConfig(),
        engine=engine,
        worker=LiveDecisionWorker(provider=provider),
        provider=provider,
        clock=clock,
    )
    assert driver2.resume_startup() == "resumed"
    assert provider.requests == 1  # salvage + resume never re-asked the AI


def test_salvage_shutdown_stores_a_collected_but_unstored_decision(tmp_path, monkeypatch):
    """Salvage covers the OTHER undurable shape too: _collect polled the worker
    but the §3.1 store missed (parsed set, raw_stored False). Shutdown gets one
    last store attempt so restart resumes instead of re-asking the AI."""

    from contrib.hyperliquid_perp.live.decision import _PendingResponsePersistError

    db, clock, driver, engine, worker, provider = _driver(tmp_path)
    assert driver.pump() == "cycle_started"
    _await(worker)
    arm_lock_fault(monkeypatch, repo, "store_pending_response")
    with pytest.raises(_PendingResponsePersistError):
        driver.pump()  # collected, store missed — the retry lane is armed
    assert driver.salvage_shutdown() is True  # the DB healed: one last try lands
    assert driver._inflight is not None and driver._inflight.raw_stored is True
    row = db.conn.execute("SELECT * FROM decision_attempts WHERE run_id='r'").fetchone()
    assert row["status"] == "in_progress"
    assert row["pending_raw_response"]  # restart resumes from the store
    assert provider.requests == 1  # the AI was never re-asked


def test_salvage_shutdown_noops_when_nothing_is_pending(tmp_path):
    db, clock, driver, engine, worker, provider = _driver(tmp_path)
    assert driver.salvage_shutdown() is False  # no in-flight cycle at all
    assert driver.pump() == "cycle_started"
    _await(worker)
    assert driver.pump() == "completed"  # normally collected
    assert driver.salvage_shutdown() is False  # nothing left to salvage


# -- 2026-07-22 decisions: stall visibility + manual-latch pause --------------


def _set_safe_mode(db, type_, reason):
    """Write (or clear, with ``None, None``) the stored safe-mode triple."""
    with db.transaction() as conn:
        repo.upsert_scheduler_state(
            conn,
            "r",
            safe_mode_type=type_,
            safe_mode_reason=reason,
            safe_mode_entered_at=None if type_ is None else _T0,
            updated_at=_T0,
        )


def test_stall_warning_fires_on_a_cadence_while_the_llm_call_hangs(tmp_path, caplog):
    # Decided 2026-07-22 ("warn, don't force"): after 30 minutes of worker-busy
    # pump() warns, re-warns every 5 minutes, and never forces the cycle — no
    # api_failed, no second worker; the attempt stays honestly in_progress and
    # the hung call still completes normally when it finally returns.
    import logging

    hang = threading.Event()
    db, clock, driver, engine, worker, provider = _driver(tmp_path, request_gate=hang)
    try:
        caplog.set_level(logging.WARNING, logger="contrib.hyperliquid_perp.live.decision")
        assert driver.pump() == "cycle_started"

        def stalls():
            return [r for r in caplog.records if "may be hung" in r.getMessage()]

        clock.advance(29 * 60)
        assert driver.pump() is None  # under the 30-minute threshold: quiet
        assert stalls() == []
        clock.advance(2 * 60)  # 31 minutes in flight
        assert driver.pump() is None
        assert len(stalls()) == 1
        clock.advance(2 * 60)  # 33 minutes — inside the 5-minute re-warn window
        assert driver.pump() is None
        assert len(stalls()) == 1  # no spam
        clock.advance(4 * 60)  # 37 minutes — past the cadence
        assert driver.pump() is None
        assert len(stalls()) == 2
        # Never forced: the attempt is still in_progress, the AI asked once.
        row = db.conn.execute("SELECT status FROM decision_attempts WHERE run_id='r'").fetchone()
        assert row["status"] == "in_progress"
        assert provider.requests == 1
    finally:
        hang.set()
    _await(worker)
    assert driver.pump() == "completed"  # the late answer still lands normally


def test_manual_latch_skips_the_llm_spend_until_release(tmp_path):
    # Decided 2026-07-22: a standing §13.5 manual latch means every target would
    # be refused at the manual_safe_mode gate line — pump() skips _start entirely
    # (no attempt row, no build_input, no LLM spend), announces the pause ONCE,
    # and a release starts a FRESH cycle on the very next pump (next_decision_at
    # never advanced while paused).
    db, clock, driver, engine, worker, provider = _driver(tmp_path)
    _set_safe_mode(db, "manual", "emergency_close")
    assert driver.pump() == "decision_paused"  # announced once
    assert driver.pump() is None  # then quiet while the latch stands
    assert provider.builds == 0 and provider.requests == 0
    assert db.conn.execute("SELECT COUNT(*) AS c FROM decision_attempts").fetchone()["c"] == 0
    # A human releases the latch (§13.6): the very next pump decides afresh.
    _set_safe_mode(db, None, None)
    assert driver.pump() == "cycle_started"
    assert provider.builds == 1


def test_recoverable_safe_mode_does_not_pause_decisions(tmp_path):
    # Only the MANUAL latch pauses the spend (decided 2026-07-22): recoverable
    # safe mode self-heals within minutes, so pausing it could burn a whole 4h
    # cycle for a transient containment. The gate still refuses the target if
    # the mode is standing at admission — that refusal is the audited path.
    db, clock, driver, engine, worker, provider = _driver(tmp_path)
    _set_safe_mode(db, "recoverable", "reconcile_error")
    assert driver.pump() == "cycle_started"
    assert provider.builds == 1
