"""Tests for the paper execution engine (execution §1–§5).

Everything runs on an injected :class:`ManualClock` + :class:`ScriptedSnapshotProvider`
so no test sleeps and the schedule is deterministic. ``start_plan`` consumes the
first scripted snapshot (its plan-build fetch); each ``tick`` consumes the next.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from contrib.hyperliquid_perp.domains.perp.margin import MarginSchedule, MarginTier
from contrib.hyperliquid_perp.domains.perp.risk_gate import DecisionConfig, RiskConfig
from contrib.hyperliquid_perp.domains.perp.target_decision import (
    DecisionMode,
    ParsedDecision,
    TargetDecision,
    TargetSide,
)
from contrib.hyperliquid_perp.paper import accounting
from contrib.hyperliquid_perp.paper.clock import ManualClock
from contrib.hyperliquid_perp.paper.config import PaperTradingConfig
from contrib.hyperliquid_perp.paper.engine import AssetSpec, PaperExecutionEngine, TickEvent
from contrib.hyperliquid_perp.paper.market_feed import ScriptedSnapshotProvider, SnapshotOutcome
from contrib.hyperliquid_perp.persistence import repository as repo
from contrib.hyperliquid_perp.persistence.db import Database
from contrib.hyperliquid_perp.persistence.models import PositionState

D = Decimal
_T0 = datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)
_MARK = D(50000)


def _schedule() -> MarginSchedule:
    return MarginSchedule(coin="BTC", tiers=(MarginTier(D(0), D(50)),))


def _decision(side: str | None, margin: int | None, conf: str = "0.8") -> ParsedDecision:
    dec = TargetDecision(
        decision_mode=DecisionMode.SET_TARGET,
        target_side=None if side is None else TargetSide(side),
        requested_target_margin_pct=margin,
        confidence=D(conf),
        rationale="test rationale",
        key_risks=("a risk",),
    )
    return ParsedDecision(decision=dec, is_valid=True, invalid_reason=None, raw_response="{}")


def _engine(
    tmp_path,
    *,
    leverage: str = "5",
    min_notional: str | None = None,
    funding=None,
    stop_config=None,
    seed=(),
):
    db = Database(tmp_path / "e.db")
    accounting.initialize_run(
        db,
        run_id="r",
        mode="paper",
        initial_balance_usdc=D(1000),
        schema_version=1,
        initial_positions=seed,
    )
    clock = ManualClock(_T0)
    asset = AssetSpec(coin="BTC", sz_decimals=3, margin_schedule=_schedule())
    paper = PaperTradingConfig.from_dict(
        {"execution": {"min_notional_usdc": min_notional}} if min_notional else None
    )
    engine = PaperExecutionEngine(
        db=db,
        run_id="r",
        asset=asset,
        clock=clock,
        provider=None,  # set per test after building the script
        risk_config=RiskConfig(leverage=D(leverage), max_target_margin_pct=60),
        decision_config=DecisionConfig(),
        paper_config=paper,
        stop_config=stop_config,
        funding_source=funding,
    )
    return db, clock, engine, asset


def _provider(engine, script):
    engine._provider = ScriptedSnapshotProvider("BTC", script)


def _snap(mark=_MARK, mid=_MARK):
    return (D(mark), D(mid))


def _size(db) -> Decimal:
    pos = repo.get_current_position(db.conn, "r", "BTC")
    return D(0) if pos is None else pos.size


# --------------------------------------------------------------------------
# paper_market single fill
# --------------------------------------------------------------------------


def test_paper_market_fill_opens_position(tmp_path):
    db, clock, engine, _ = _engine(tmp_path)
    # 1% margin @ 5x, equity 1000 -> notional 50 -> size 0.001 = one min_order_qty
    _provider(engine, [_snap(), _snap()])
    start = engine.start_plan(_decision("long", 1))
    assert start.plan_id is not None
    clock.advance(30)
    result = engine.tick()
    assert result.has(TickEvent.PAPER_MARKET_FILL)
    position = repo.get_current_position(db.conn, "r", "BTC")
    assert position.size == D("0.001")
    # buy fill = 50000 * (1 + 5/10000) = 50025
    assert position.entry_price == D("50025")
    db.close()


# --------------------------------------------------------------------------
# TWAP cadence + integer-step allocation
# --------------------------------------------------------------------------


def test_twap_two_slices_execute_on_cadence(tmp_path):
    db, clock, engine, _ = _engine(tmp_path)
    # 2% margin -> notional 100 -> size 0.002 -> 2 slices of 0.001
    _provider(engine, [_snap(), _snap(), _snap()])
    start = engine.start_plan(_decision("long", 2))
    assert start.disposition.value == "twap"
    clock.advance(30)
    r1 = engine.tick()
    assert r1.has(TickEvent.SLICE_FILL)
    assert repo.get_current_position(db.conn, "r", "BTC").size == D("0.001")
    clock.advance(30)
    r2 = engine.tick()
    assert r2.has(TickEvent.SLICE_FILL)
    assert repo.get_current_position(db.conn, "r", "BTC").size == D("0.002")
    assert r2.has(TickEvent.PLAN_TERMINAL)
    db.close()


def test_slice_not_due_before_thirty_seconds(tmp_path):
    db, clock, engine, _ = _engine(tmp_path)
    _provider(engine, [_snap(), _snap()])
    engine.start_plan(_decision("long", 2))
    clock.advance(10)  # only 10s -> no slice due yet
    r = engine.tick()
    assert not r.has(TickEvent.SLICE_FILL)
    assert r.has(TickEvent.IDLE)
    assert _size(db) == D(0)
    db.close()


# --------------------------------------------------------------------------
# stop loss: recompute after fill, trigger before slice, cancel remainder
# --------------------------------------------------------------------------


def test_stop_loss_triggers_before_slice_and_cancels_plan(tmp_path):
    db, clock, engine, _ = _engine(tmp_path)
    # 2-slice TWAP; after slice 1 an SL is set (~ entry * 0.925). Next tick's mark
    # dives below the SL: SL (event step 4) fires before the slice (step 6).
    _provider(engine, [_snap(), _snap(), _snap(mark=40000, mid=40000)])
    engine.start_plan(_decision("long", 2))
    clock.advance(30)
    engine.tick()  # slice 0 fills, SL set
    assert engine._protection.stop_loss is not None
    clock.advance(30)
    r2 = engine.tick()
    assert r2.has(TickEvent.STOP_LOSS_FILL)
    assert not r2.has(TickEvent.SLICE_FILL)  # SL closed before the slice ran
    assert repo.get_current_position(db.conn, "r", "BTC").size == D(0)
    db.close()


# --------------------------------------------------------------------------
# market-data freshness (§1.1)
# --------------------------------------------------------------------------


def test_timeout_skips_slice_without_filling(tmp_path):
    db, clock, engine, _ = _engine(tmp_path)
    _provider(engine, [_snap(), SnapshotOutcome.TIMEOUT])
    engine.start_plan(_decision("long", 2))
    clock.advance(30)
    r = engine.tick()
    assert r.outcome is SnapshotOutcome.TIMEOUT
    assert r.has(TickEvent.PENDING_MARKET_DATA)
    assert r.has(TickEvent.SLICE_MISSED)
    assert _size(db) == D(0)
    db.close()


def test_three_failures_pause_then_resume_no_catchup(tmp_path):
    db, clock, engine, _ = _engine(tmp_path)
    # size 0.005 -> 5 slices; three consecutive failures pause the plan, then a
    # valid snapshot resumes without re-running the missed slices.
    script = [_snap()] + [SnapshotOutcome.TIMEOUT] * 3 + [_snap()]
    _provider(engine, script)
    engine.start_plan(_decision("long", 5))
    for _ in range(3):
        clock.advance(30)
        engine.tick()
    assert engine._paused
    clock.advance(30)
    r = engine.tick()
    assert r.has(TickEvent.RESUMED)
    # exactly one slice executes on resume — the missed three are not re-run.
    assert repo.get_current_position(db.conn, "r", "BTC").size == D("0.001")
    db.close()


def test_gap_stop_fill_on_resume_when_mark_crossed_sl(tmp_path):
    db, clock, engine, _ = _engine(tmp_path)
    # Open a standing long (paper_market), then market-data drops out; on resume the
    # mark has crossed the SL -> immediate gap_stop_fill (execution §1.1).
    _provider(
        engine,
        [_snap(), _snap()] + [SnapshotOutcome.TIMEOUT] * 3 + [_snap(mark=40000, mid=40000)],
    )
    engine.start_plan(_decision("long", 1))
    clock.advance(30)
    engine.tick()  # paper_market fill, SL set
    assert engine._protection.stop_loss is not None
    for _ in range(3):
        clock.advance(30)
        engine.tick()
    assert engine._paused
    clock.advance(30)
    r = engine.tick()
    assert r.has(TickEvent.GAP_STOP_FILL)
    assert repo.get_current_position(db.conn, "r", "BTC").size == D(0)
    db.close()


# --------------------------------------------------------------------------
# no-order / pending distinctions
# --------------------------------------------------------------------------


def test_start_plan_pending_when_no_snapshot(tmp_path):
    db, clock, engine, _ = _engine(tmp_path)
    _provider(engine, [SnapshotOutcome.TIMEOUT])
    start = engine.start_plan(_decision("long", 2))
    assert start.plan_id is None
    assert start.reason == "pending_market_data"
    db.close()


def test_start_plan_no_order_on_maintain_current(tmp_path):
    db, clock, engine, _ = _engine(tmp_path)
    _provider(engine, [_snap()])
    maintain = ParsedDecision(
        decision=TargetDecision(
            decision_mode=DecisionMode.MAINTAIN_CURRENT,
            target_side=None,
            requested_target_margin_pct=None,
            confidence=D("0.8"),
            rationale="hold",
            key_risks=("r",),
        ),
        is_valid=True,
        invalid_reason=None,
        raw_response="{}",
    )
    start = engine.start_plan(maintain)
    assert start.plan_id is None
    assert not start.gate.order_created
    db.close()


# --------------------------------------------------------------------------
# funding posts in event order
# --------------------------------------------------------------------------


def test_funding_posted_within_tick(tmp_path):
    class _Funding:
        def rate_at(self, coin, ts):
            return D("0.0001")

    db, clock, engine, _ = _engine(tmp_path, funding=_Funding())
    _provider(engine, [_snap(), _snap(), _snap()])
    engine.start_plan(_decision("long", 1))
    clock.advance(30)
    engine.tick()  # opens position; establishes funding baseline hour
    clock.advance(3600)  # cross one funding hour
    r = engine.tick()
    assert r.has(TickEvent.FUNDING_POSTED)
    events = repo.iter_funding_events(db.conn, "r", status="posted")
    assert len(events) == 1
    db.close()


# --------------------------------------------------------------------------
# flip (execution §1.3)
# --------------------------------------------------------------------------


def test_flip_closes_then_opens_reverse(tmp_path):
    seed = (PositionState(coin="BTC", size=D("0.001"), entry_price=D(50000)),)
    db, clock, engine, _ = _engine(tmp_path, seed=seed)
    # Long 0.001 -> short 5% target: close leg (sell to flat, one min slice) then a
    # larger open leg (short ~0.005). The open target is comfortably above one step
    # so fee-driven equity shrink can't floor it below a legal slice.
    _provider(engine, [_snap()] * 10)
    start = engine.start_plan(_decision("short", 5))
    assert start.plan_id is not None
    went_short = False
    for _ in range(8):
        clock.advance(30)
        engine.tick()
        if _size(db) < 0:
            went_short = True
            break
    assert went_short
    db.close()


def test_flip_incomplete_when_open_leg_has_no_legal_slice(tmp_path):
    # Long 0.001 -> short 1%: the open target (~0.001) floors below one legal slice
    # after fees, so the reverse position never opens (execution §1.3).
    seed = (PositionState(coin="BTC", size=D("0.001"), entry_price=D(50000)),)
    db, clock, engine, _ = _engine(tmp_path, seed=seed)
    _provider(engine, [_snap()] * 4)
    engine.start_plan(_decision("short", 1))
    clock.advance(30)
    engine.tick()  # close leg fills to flat; open leg cannot fund a slice
    assert engine._flip is None
    assert _size(db) == D(0)  # never opened the reverse
    plans = db.conn.execute(
        "SELECT status FROM execution_plans WHERE status = 'flip_incomplete'"
    ).fetchall()
    assert len(plans) == 1
    db.close()


# --------------------------------------------------------------------------
# rejected plan (execution §1.2 — no legal slice, never rounded up)
# --------------------------------------------------------------------------


def test_plan_rejected_when_qty_below_min_slice(tmp_path):
    # leverage 1, 1% margin -> notional 10 -> size 0.0002 < min_order_qty 0.001.
    db, clock, engine, _ = _engine(tmp_path, leverage="1")
    _provider(engine, [_snap()])
    start = engine.start_plan(_decision("long", 1))
    assert start.disposition.value == "reject"
    assert start.reason == "no_legal_slice"
    assert _size(db) == D(0)
    db.close()


# --------------------------------------------------------------------------
# take profit lifecycle (execution §4.1)
# --------------------------------------------------------------------------


def test_take_profit_created_at_terminal_then_triggers(tmp_path):
    db, clock, engine, _ = _engine(tmp_path)
    # paper_market opens long -> plan terminal -> TP created (entry * 1.2 ~ 60030).
    _provider(engine, [_snap(), _snap(), _snap(mark=61000, mid=61000)])
    engine.start_plan(_decision("long", 1))
    clock.advance(30)
    engine.tick()  # open + terminal
    assert engine._protection.take_profit is not None
    clock.advance(30)
    r = engine.tick()  # mark 61000 >= TP -> take profit fires
    assert r.has(TickEvent.TAKE_PROFIT_FILL)
    assert _size(db) == D(0)
    db.close()


def test_stop_loss_close_is_atomic_clears_protection_and_cancels_plan(tmp_path):
    db, clock, engine, _ = _engine(tmp_path)
    _provider(engine, [_snap(), _snap(), _snap(mark=40000, mid=40000)])
    engine.start_plan(_decision("long", 2))  # 2-slice TWAP
    clock.advance(30)
    engine.tick()  # slice 0 fills, SL set
    plan_id = engine._leg.plan_id
    clock.advance(30)
    engine.tick()  # SL fires and closes before slice 1
    # The flattening fill's protection-clear and plan-cancel are committed together.
    pos_row = db.conn.execute(
        "SELECT stop_loss_price, take_profit_price FROM current_positions WHERE run_id='r'"
    ).fetchone()
    assert pos_row["stop_loss_price"] is None
    assert pos_row["take_profit_price"] is None
    plan_row = db.conn.execute(
        "SELECT status FROM execution_plans WHERE plan_id = ?", (plan_id,)
    ).fetchone()
    assert plan_row["status"] == "canceled"
    db.close()


def test_gap_stop_flag_does_not_stick_to_later_normal_stop(tmp_path):
    db, clock, engine, _ = _engine(tmp_path)
    # First position: outage -> resume with mark past SL -> gap_stop_fill.
    # Then a fresh position whose SL triggers under normal conditions must be
    # labeled a plain stop_loss, not gap_stop_fill (the one-shot flag must reset).
    _provider(
        engine,
        [_snap(), _snap()]  # plan 1: start_plan + open fill (paper_market)
        + [SnapshotOutcome.TIMEOUT] * 3
        + [_snap(mark=40000, mid=40000)]  # resume -> gap stop
        + [_snap(), _snap(), _snap(mark=40000, mid=40000)],  # plan 2: start + slice0 + SL
    )
    engine.start_plan(_decision("long", 1))
    clock.advance(30)
    engine.tick()  # open, SL set
    for _ in range(3):
        clock.advance(30)
        engine.tick()  # 3 failures -> pause
    clock.advance(30)
    r_gap = engine.tick()  # resume -> gap stop
    assert r_gap.has(TickEvent.GAP_STOP_FILL)
    # Second position (2% -> multi-slice; fee-shrunk equity still funds >1 slice),
    # no outage this time.
    engine.start_plan(_decision("long", 2))
    clock.advance(30)
    engine.tick()  # slice 0 fills, SL set
    clock.advance(30)
    r_norm = engine.tick()  # SL fires normally, before the next slice
    assert r_norm.has(TickEvent.STOP_LOSS_FILL)
    assert not r_norm.has(TickEvent.GAP_STOP_FILL)
    db.close()


def test_has_active_work_reflects_state(tmp_path):
    db, clock, engine, _ = _engine(tmp_path)
    assert not engine.has_active_work()
    _provider(engine, [_snap(), _snap()])
    engine.start_plan(_decision("long", 1))
    assert engine.has_active_work()  # active plan
    clock.advance(30)
    engine.tick()
    assert engine.has_active_work()  # open position
    db.close()
