"""Tests for the §18.2 background AI decision worker + driver (PR 5, Option A)."""

from __future__ import annotations

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
from contrib.hyperliquid_perp.live.decision import LiveDecisionDriver, LiveDecisionWorker
from contrib.hyperliquid_perp.paper import accounting
from contrib.hyperliquid_perp.paper.clock import ManualClock
from contrib.hyperliquid_perp.paper.engine import AssetSpec
from contrib.hyperliquid_perp.paper.scheduler import (
    DecisionInput,
    RetryableDecisionError,
    parse_instant,
)
from contrib.hyperliquid_perp.persistence import repository as repo
from contrib.hyperliquid_perp.persistence.db import Database


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
    def __init__(self, decision_input, *, parsed, build_error=None, request_error=None) -> None:
        self._di = decision_input
        self._parsed = parsed
        self._build_error = build_error
        self._request_error = request_error
        self.builds = 0
        self.requests = 0

    def build_input(self, *, coin, as_of):
        self.builds += 1
        if self._build_error is not None:
            raise self._build_error
        return self._di

    def request_decision(self, decision_input):
        self.requests += 1
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
        model="m",
    )


def _driver(tmp_path, *, build_error=None, request_error=None, start_error=None, first_regs=()):
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
        parsed=_decision(),
        build_error=build_error,
        request_error=request_error,
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
    state = repo.get_scheduler_state(db.conn, "r")
    assert state["next_decision_at"] is not None
    # Not due again until the next 4h boundary.
    assert driver.pump() is None


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


def test_driver_gate_nonretryable_error_fails_closed_and_recovers(tmp_path):
    """A non-retryable error in the GATE step (start_plan / persist) must also fail
    the cycle closed via pump's guard, not wedge the driver — _gate is reachable both
    from pump directly and from _collect's tail, outside _collect's own body."""
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
            pending_raw_response=raw,
        )
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


def test_resume_startup_with_no_stranded_attempt_is_none(tmp_path):
    db, clock, driver, engine, worker, provider = _driver(tmp_path)
    assert driver.resume_startup() is None
    assert driver._inflight is None


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
