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
from contrib.hyperliquid_perp.paper.scheduler import DecisionInput, RetryableDecisionError
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
    def __init__(self, decision_input, *, parsed, build_error=None) -> None:
        self._di = decision_input
        self._parsed = parsed
        self._build_error = build_error
        self.builds = 0

    def build_input(self, *, coin, as_of):
        self.builds += 1
        if self._build_error is not None:
            raise self._build_error
        return self._di

    def request_decision(self, decision_input):
        return self._parsed


class _DriverEngine:
    def __init__(self, reg) -> None:
        self._reg = reg
        self.plans: list = []

    def start_plan(self, parsed, *, output_id):
        self.plans.append(output_id)
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


def _driver(tmp_path, *, build_error=None):
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
    engine = _DriverEngine(reg)
    provider = _DriverProvider(_decision_input(), parsed=_decision(), build_error=build_error)
    worker = LiveDecisionWorker(provider=provider)
    driver = LiveDecisionDriver(
        db=db,
        run_id="r",
        coin="BTC",
        asset=AssetSpec(coin="BTC", sz_decimals=3, margin_schedule=_schedule()),
        risk_config=RiskConfig(leverage=Decimal(1), max_target_margin_pct=60),
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
