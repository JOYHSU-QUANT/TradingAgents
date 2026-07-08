"""Tests for the rolling 4h decision scheduler (phase2-spec §3 / §3.1).

Everything runs on a :class:`ManualClock`, a scripted snapshot provider for the
engine, and a scripted decision provider for the AI seam — no test sleeps, no
network. ``start_plan`` consumes one scripted snapshot per gated decision;
``tick`` consumes one per call.
"""

from __future__ import annotations

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
        day_change_pct=None,
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


def _snap(mark=_MARK, mid=_MARK):
    return (D(mark), D(mid))


def _attempt_row(db, attempt_id):
    return repo.get_decision_attempt(db.conn, attempt_id)


def _err(kind="timeout"):
    return RetryableDecisionError(kind, "boom")


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
    db, clock, engine, scheduler, provider = _setup(
        tmp_path, [_err("timeout"), _err("connection"), _err("server_error")], []
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
    assert row["output_id"] is None
    # No target, no order, no AI output — position untouched (spec §3.1).
    assert db.conn.execute("SELECT COUNT(*) FROM ai_outputs").fetchone()[0] == 0
    assert db.conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
    assert provider.decide_calls == 3
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
    scheduler = PaperScheduler(
        db=db,
        run_id="r",
        engine=engine,
        clock=clock,
        provider=provider,
        asset=engine._asset,
        risk_config=engine._risk,
        decision_config=DecisionConfig(),
    )
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
    scheduler2 = PaperScheduler(
        db=db,
        run_id="r",
        engine=engine,
        clock=clock,
        provider=provider2,
        asset=engine._asset,
        risk_config=engine._risk,
        decision_config=DecisionConfig(),
    )
    assert scheduler2.poll() is None  # 10s backoff still pending across restart
    clock.advance(10)
    r2 = scheduler2.poll()
    assert r2.event is CycleEvent.COMPLETED
    assert r2.decision_attempt_id == r1.decision_attempt_id
    assert _attempt_row(db, r2.decision_attempt_id)["attempt_count"] == 2
    db.close()


def test_interrupted_final_attempt_terminalizes_without_new_call(tmp_path):
    db, clock, engine, scheduler, provider = _setup(tmp_path, [], [])
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
    scheduler2 = PaperScheduler(
        db=db,
        run_id="r",
        engine=engine,
        clock=clock,
        provider=provider2,
        asset=engine._asset,
        risk_config=engine._risk,
        decision_config=DecisionConfig(),
    )
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
