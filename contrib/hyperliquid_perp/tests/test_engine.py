"""Tests for the paper execution engine (execution §1–§5).

Everything runs on an injected :class:`ManualClock` + :class:`ScriptedSnapshotProvider`
so no test sleeps and the schedule is deterministic. ``start_plan`` consumes the
first scripted snapshot (its plan-build fetch); each ``tick`` consumes the next.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

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
from contrib.hyperliquid_perp.paper.engine import (
    AssetSpec,
    EngineHaltedError,
    PaperExecutionEngine,
    TickEvent,
    _FlipState,
    _Leg,
    _Protection,
)
from contrib.hyperliquid_perp.paper.market_feed import ScriptedSnapshotProvider, SnapshotOutcome
from contrib.hyperliquid_perp.paper.stops import StopConfig
from contrib.hyperliquid_perp.paper.twap import PlanDisposition
from contrib.hyperliquid_perp.persistence import repository as repo
from contrib.hyperliquid_perp.persistence.db import Database
from contrib.hyperliquid_perp.persistence.models import PositionState, Side

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


def _plan_status(db, plan_id) -> tuple[str, str | None]:
    row = db.conn.execute(
        "SELECT status, status_reason FROM execution_plans WHERE plan_id = ?", (plan_id,)
    ).fetchone()
    return row["status"], row["status_reason"]


def _order_status(db, order_id) -> tuple[str, str | None]:
    row = db.conn.execute(
        "SELECT status, status_reason FROM orders WHERE order_id = ?", (order_id,)
    ).fetchone()
    return row["status"], row["status_reason"]


class _ConstFunding:
    def __init__(self, rate: Decimal):
        self._rate = rate

    def rate_at(self, coin, ts):
        return self._rate


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
    # No snapshot -> no gate ran at all: nothing is priced at a fabricated mark
    # and nothing is persisted; the caller retries when data returns.
    assert start.gate is None
    assert db.conn.execute("SELECT COUNT(*) FROM execution_plans").fetchone()[0] == 0
    assert db.conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
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
    db, clock, engine, _ = _engine(tmp_path, funding=_ConstFunding(D("0.0001")))
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


# --------------------------------------------------------------------------
# liquidation / emergency close during an active plan (execution §5.3 step 3, §3.6)
# --------------------------------------------------------------------------

_BIG_SEED = (PositionState(coin="BTC", size=D("0.1"), entry_price=D(50000)),)


def test_liquidation_close_during_active_plan(tmp_path):
    # Seeded 0.1 BTC on a 1000 wallet: liquidatable near mark 40400. A reduce
    # plan is mid-flight when the mark dives to 40000 — liquidation (step 3)
    # must close everything before the SL (step 4) or the due slice (step 6).
    db, clock, engine, _ = _engine(tmp_path, seed=_BIG_SEED)
    _provider(engine, [_snap(), _snap(), _snap(mark=40000, mid=40000)])
    start = engine.start_plan(_decision("long", 10))  # reduce toward 10% margin
    assert start.plan_id is not None
    plan_id = engine._leg.plan_id
    order_id = engine._leg.order_id
    clock.advance(30)
    engine.tick()  # slice 0 fills, SL set
    clock.advance(30)
    r = engine.tick()
    assert r.has(TickEvent.LIQUIDATION_CLOSE)
    assert not r.has(TickEvent.STOP_LOSS_FILL)
    assert r.has(TickEvent.PLAN_TERMINAL)
    assert _size(db) == D(0)
    assert _plan_status(db, plan_id) == ("canceled", "risk_exit")
    # The leg's own order row reaches a terminal status in the same transaction.
    assert _order_status(db, order_id) == ("canceled", "risk_exit")
    pos_row = db.conn.execute(
        "SELECT stop_loss_price, take_profit_price FROM current_positions WHERE run_id='r'"
    ).fetchone()
    assert pos_row["stop_loss_price"] is None and pos_row["take_profit_price"] is None
    db.close()


def test_emergency_close_when_no_safe_sl(tmp_path):
    # A liq_buffer wide enough that the post-fill SL recompute finds no legal
    # band above liq*(1+buffer) -> CLOSE_NOW: the same tick's fill is followed by
    # a synchronous emergency close (§3.6), cancelling the plan.
    db, clock, engine, _ = _engine(
        tmp_path, seed=_BIG_SEED, stop_config=StopConfig(liq_buffer=D("0.2"))
    )
    _provider(engine, [_snap(), _snap()])
    engine.start_plan(_decision("long", 10))
    plan_id = engine._leg.plan_id
    clock.advance(30)
    r = engine.tick()  # slice 0 fills -> recompute -> no safe SL -> emergency close
    assert r.has(TickEvent.SLICE_FILL)
    assert r.has(TickEvent.LIQUIDATION_CLOSE)
    assert r.has(TickEvent.PLAN_TERMINAL)
    assert _size(db) == D(0)
    assert _plan_status(db, plan_id) == ("canceled", "no_safe_sl")
    # The emergency order records no trigger price — its just-invalidated SL
    # never triggered; fills.fill_reason carries the why.
    order_row = db.conn.execute(
        "SELECT trigger_price, order_role, type FROM orders WHERE order_id IN "
        "(SELECT order_id FROM fills WHERE fill_reason = 'emergency_close')"
    ).fetchone()
    assert order_row is not None
    assert order_row["trigger_price"] is None
    assert (order_row["order_role"], order_row["type"]) == ("stop_loss", "stop_market")
    db.close()


# --------------------------------------------------------------------------
# plan deadline expiry (execution §1.2 / §4.1: expired is a TP-reconciled terminal)
# --------------------------------------------------------------------------


def test_plan_expires_at_deadline_with_tp_and_order_canceled(tmp_path):
    db, clock, engine, _ = _engine(tmp_path)
    _provider(engine, [_snap(), _snap(), _snap()])
    engine.start_plan(_decision("long", 5))  # 5 slices
    plan_id = engine._leg.plan_id
    order_id = engine._leg.order_id
    clock.advance(30)
    engine.tick()  # slice 0 fills
    clock.set(_T0.replace(hour=13, second=1))  # past the 1h deadline
    r = engine.tick()  # one more slice fills, then the plan expires
    assert r.has(TickEvent.PLAN_TERMINAL)
    assert _plan_status(db, plan_id) == ("expired", "deadline")
    residual = db.conn.execute(
        "SELECT residual_qty FROM execution_plans WHERE plan_id = ?", (plan_id,)
    ).fetchone()[0]
    assert D(residual) > 0
    assert _order_status(db, order_id) == ("canceled", "deadline")
    # §4.1 lists expired among the TP-reconciled terminals.
    assert engine._protection.take_profit is not None
    db.close()


def test_plan_expiry_during_outage_still_creates_tp(tmp_path):
    # The deadline tick itself has NO market data: the expiry must still create
    # the TP (its math needs only entry price + tick size), or the surviving
    # position would run without one for the rest of the process's life.
    db, clock, engine, _ = _engine(tmp_path)
    _provider(engine, [_snap(), _snap(), SnapshotOutcome.TIMEOUT])
    engine.start_plan(_decision("long", 5))
    plan_id = engine._leg.plan_id
    clock.advance(30)
    engine.tick()  # slice 0 fills -> non-flat position
    clock.set(_T0.replace(hour=13, second=1))
    r = engine.tick()  # no-data tick at the deadline
    assert r.has(TickEvent.PENDING_MARKET_DATA)
    assert r.has(TickEvent.PLAN_TERMINAL)
    assert _plan_status(db, plan_id) == ("expired", "deadline")
    assert engine._protection.take_profit is not None
    db.close()


# --------------------------------------------------------------------------
# flip abnormal terminals get their TP (execution §4.1: flip_incomplete included)
# --------------------------------------------------------------------------


def test_flip_incomplete_close_leg_leaves_position_with_tp(tmp_path):
    # Flip close leg = 2 slices; slice 0 is missed in an outage, slice 1 fills.
    # The close leg terminates without reaching flat -> flip_incomplete — and the
    # surviving 0.001 position must regain a TP (it was cancelled at plan start).
    seed = (PositionState(coin="BTC", size=D("0.002"), entry_price=D(50000)),)
    db, clock, engine, _ = _engine(tmp_path, seed=seed)
    _provider(engine, [_snap(), SnapshotOutcome.TIMEOUT, _snap()])
    engine.start_plan(_decision("short", 5))
    clock.advance(30)
    engine.tick()  # slice 0 missed
    clock.advance(30)
    r = engine.tick()  # slice 1 fills; close leg terminal but position non-flat
    assert r.has(TickEvent.SLICE_FILL)
    assert engine._flip is None
    assert _size(db) == D("0.001")
    row = db.conn.execute(
        "SELECT status_reason FROM execution_plans WHERE status = 'flip_incomplete'"
    ).fetchone()
    assert row["status_reason"] == "close_leg_incomplete"
    assert engine._protection.take_profit is not None
    db.close()


def test_flip_incomplete_when_open_leg_gate_rejected(tmp_path):
    # The open leg re-runs the deterministic RiskGate; if it declines (here:
    # confidence mutated below min_confidence between legs), the reverse position
    # must never open and the flip records open_leg_gate_rejected.
    seed = (PositionState(coin="BTC", size=D("0.001"), entry_price=D(50000)),)
    db, clock, engine, _ = _engine(tmp_path, seed=seed)
    _provider(engine, [_snap(), _snap()])
    engine.start_plan(_decision("short", 5))
    assert engine._flip is not None
    engine._flip.parsed = _decision("short", 5, conf="0.1")  # below min_confidence
    clock.advance(30)
    engine.tick()  # close leg fills to flat; open-leg gate declines
    assert _size(db) == D(0)
    assert engine._flip is None
    row = db.conn.execute(
        "SELECT status_reason FROM execution_plans WHERE status = 'flip_incomplete'"
    ).fetchone()
    assert row["status_reason"] == "open_leg_gate_rejected"
    db.close()


def test_flip_open_leg_inherits_close_leg_deadline(tmp_path):
    # §1.3: both legs share one envelope — the open leg must NOT restart its own
    # hour at close-leg terminal.
    seed = (PositionState(coin="BTC", size=D("0.001"), entry_price=D(50000)),)
    db, clock, engine, _ = _engine(tmp_path, seed=seed)
    _provider(engine, [_snap()] * 4)
    engine.start_plan(_decision("short", 5))
    clock.advance(30)
    engine.tick()  # close leg fills to flat; open leg registers the same tick
    rows = db.conn.execute(
        "SELECT flip_leg, deadline_at FROM execution_plans"
        " WHERE flip_leg IN ('close', 'open') ORDER BY flip_leg"
    ).fetchall()
    assert [r["flip_leg"] for r in rows] == ["close", "open"]
    assert rows[0]["deadline_at"] == rows[1]["deadline_at"]
    db.close()


# --------------------------------------------------------------------------
# funding: multi-hour catch-up + funding-driven SL refresh (§6.5 / §6.6.1)
# --------------------------------------------------------------------------


def test_multi_hour_funding_catchup_posts_each_hour(tmp_path):
    db, clock, engine, _ = _engine(tmp_path, funding=_ConstFunding(D("0.0001")))
    _provider(engine, [_snap(), _snap(), _snap()])
    engine.start_plan(_decision("long", 1))
    clock.advance(30)
    engine.tick()  # opens position; establishes the funding baseline hour
    clock.advance(3 * 3600)  # jump three settlement hours in one gap
    r = engine.tick()
    assert r.has(TickEvent.FUNDING_POSTED)
    events = repo.iter_funding_events(db.conn, "r", status="posted")
    assert len(events) == 3  # exactly one per crossed hour — no double post
    db.close()


def test_funding_post_refreshes_stop_loss(tmp_path):
    # Funding erodes the wallet hourly, moving the liquidation price toward
    # entry; with a liq-bound SL, the post-funding recompute must move the SL
    # (§6.6.1 lists funding posting among the must-recompute events).
    db, clock, engine, _ = _engine(
        tmp_path,
        seed=_BIG_SEED,
        funding=_ConstFunding(D("0.01")),  # long pays 1%/h -> ~50 USDC on 5000 notional
        stop_config=StopConfig(liq_buffer=D("0.15")),
    )
    _provider(engine, [_snap(), _snap(), _snap()])
    engine.start_plan(_decision("long", 10))  # reduce plan, many slices
    clock.advance(30)
    engine.tick()  # slice 0 fills -> liq-bound SL placed
    sl_before = engine._protection.stop_loss
    assert sl_before is not None
    clock.advance(3600)
    r = engine.tick()
    assert r.has(TickEvent.FUNDING_POSTED)
    assert r.has(TickEvent.PROTECTION_UPDATED)
    assert engine._protection.stop_loss > sl_before
    db.close()


# --------------------------------------------------------------------------
# snapshots are written on material events only (§11.1 / §12.1)
# --------------------------------------------------------------------------


def test_snapshots_written_on_fills_not_idle_ticks(tmp_path):
    db, clock, engine, _ = _engine(tmp_path)
    _provider(engine, [_snap(), _snap(), _snap()])
    engine.start_plan(_decision("long", 2))  # 2-slice TWAP: nothing due before 30s
    clock.advance(10)
    r_idle = engine.tick()
    assert r_idle.has(TickEvent.IDLE)
    counts = db.conn.execute(
        "SELECT (SELECT COUNT(*) FROM account_snapshots), (SELECT COUNT(*) FROM position_snapshots)"
    ).fetchone()
    assert tuple(counts) == (0, 0)
    clock.advance(20)
    r_fill = engine.tick()
    assert r_fill.has(TickEvent.SLICE_FILL)
    counts = db.conn.execute(
        "SELECT (SELECT COUNT(*) FROM account_snapshots), (SELECT COUNT(*) FROM position_snapshots)"
    ).fetchone()
    assert tuple(counts) == (1, 1)
    db.close()


# --------------------------------------------------------------------------
# a new decision supersedes a live plan (one live plan max)
# --------------------------------------------------------------------------


def test_new_plan_supersedes_active_plan(tmp_path):
    db, clock, engine, _ = _engine(tmp_path)
    _provider(engine, [_snap(), _snap(), _snap()])
    engine.start_plan(_decision("long", 5))  # 5-slice plan A
    plan_a = engine._leg.plan_id
    order_a = engine._leg.order_id
    clock.advance(30)
    engine.tick()  # slice 0 fills
    start_b = engine.start_plan(_decision("long", 30))
    assert start_b.plan_id is not None and start_b.plan_id != plan_a
    assert _plan_status(db, plan_a) == ("canceled", "superseded")
    residual = db.conn.execute(
        "SELECT residual_qty FROM execution_plans WHERE plan_id = ?", (plan_a,)
    ).fetchone()[0]
    assert D(residual) > 0
    assert _order_status(db, order_a) == ("canceled", "superseded")
    assert engine._leg.plan_id == start_b.plan_id
    db.close()


def test_supersede_by_rejected_plan_restores_tp(tmp_path):
    # Plan B cancelled the TP for its duration; a new decision whose plan is
    # REJECT supersedes B with nothing replacing it — the surviving position
    # must regain its TP (§4.1: canceled is a TP-reconciled terminal).
    db, clock, engine, _ = _engine(tmp_path, leverage="1")
    _provider(engine, [_snap()] * 4)
    engine.start_plan(_decision("long", 5))  # notional 50 -> one min slice
    clock.advance(30)
    engine.tick()  # paper_market open -> terminal -> TP created
    assert engine._protection.take_profit is not None
    engine.start_plan(_decision("long", 30))  # plan B: TP cancelled for the plan
    plan_b = engine._leg.plan_id
    assert engine._protection.take_profit is None
    # 7% vs current ~5%: outside the 1-point deadband, but the delta (~0.0004)
    # floors below one 0.001 step -> REJECT, superseding B.
    start_c = engine.start_plan(_decision("long", 7))
    assert start_c.disposition is not None and start_c.disposition.value == "reject"
    assert _plan_status(db, plan_b) == ("canceled", "superseded")
    assert engine._protection.take_profit is not None
    db.close()


def test_non_flip_decision_clears_stale_flip(tmp_path):
    # A flip's close leg is live when a plain same-side rebalance arrives: the
    # old leg is superseded AND the flip bookkeeping is dropped, so
    # has_active_work can go quiet once everything later closes.
    seed = (PositionState(coin="BTC", size=D("0.001"), entry_price=D(50000)),)
    db, clock, engine, _ = _engine(tmp_path, seed=seed)
    _provider(engine, [_snap(), _snap()])
    engine.start_plan(_decision("short", 5))  # flip: close leg active
    assert engine._flip is not None
    old_plan = engine._leg.plan_id
    engine.start_plan(_decision("long", 30))  # same-side vs current long -> not a flip
    assert engine._flip is None
    assert _plan_status(db, old_plan) == ("canceled", "superseded")
    db.close()


# --------------------------------------------------------------------------
# restart guards: id continuity + protection hydration (execution §2)
# --------------------------------------------------------------------------


def test_engine_rebuild_hydrates_protection_and_continues_ids(tmp_path):
    db, clock, engine, asset = _engine(tmp_path)
    _provider(engine, [_snap(), _snap()])
    engine.start_plan(_decision("long", 1))
    clock.advance(30)
    engine.tick()  # open + terminal -> SL and TP live on current_positions
    sl, tp = engine._protection.stop_loss, engine._protection.take_profit
    assert sl is not None and tp is not None
    # Rebuild over the same run: protection must hydrate and the id sequence
    # must resume above the persisted maximum (no PK collision on the first plan).
    engine2 = PaperExecutionEngine(
        db=db,
        run_id="r",
        asset=asset,
        clock=ManualClock(clock.now()),
        provider=ScriptedSnapshotProvider("BTC", [_snap()]),
        risk_config=RiskConfig(leverage=D("5"), max_target_margin_pct=60),
        decision_config=DecisionConfig(),
        paper_config=PaperTradingConfig.from_dict(None),
    )
    assert engine2._protection.stop_loss == sl
    assert engine2._protection.take_profit == tp
    assert engine2.has_active_work()
    start = engine2.start_plan(_decision("long", 30))  # would IntegrityError on seq reset
    assert start.plan_id is not None
    db.close()


# --------------------------------------------------------------------------
# fail-stop contract, miss exhaustion, gap-flag tick scope, rejected order row
# --------------------------------------------------------------------------


class _RaisingProvider:
    def fetch(self, coin, requested_at, timeout_seconds):
        raise RuntimeError("boom")


def test_engine_fail_stops_after_escaped_exception(tmp_path):
    # A mid-tick exception may leave in-memory state ahead of the rolled-back
    # DB: every later public call must refuse to run until a rebuild.
    db, clock, engine, _ = _engine(tmp_path)
    engine._provider = _RaisingProvider()
    with pytest.raises(RuntimeError, match="boom"):
        engine.tick()
    with pytest.raises(EngineHaltedError):
        engine.tick()
    with pytest.raises(EngineHaltedError):
        engine.start_plan(_decision("long", 1))
    with pytest.raises(EngineHaltedError):
        engine.has_active_work()
    db.close()


def test_all_slices_missed_terminates_plan_as_residual(tmp_path):
    # A 1-slice paper_market plan whose only scheduled fill is missed can never
    # fill again: it terminates as residual immediately instead of sitting
    # "active" until the 1h deadline.
    db, clock, engine, _ = _engine(tmp_path)
    _provider(engine, [_snap(), SnapshotOutcome.TIMEOUT])
    start = engine.start_plan(_decision("long", 1))
    plan_id = start.plan_id
    order_id = engine._leg.order_id
    clock.advance(30)
    r = engine.tick()  # the slice's window passes with no data
    assert r.has(TickEvent.SLICE_MISSED)
    assert r.has(TickEvent.PLAN_TERMINAL)
    assert _plan_status(db, plan_id) == ("residual", None)
    assert _order_status(db, order_id) == ("canceled", "residual")
    residual = db.conn.execute(
        "SELECT residual_qty, total_qty FROM execution_plans WHERE plan_id = ?", (plan_id,)
    ).fetchone()
    assert D(residual["residual_qty"]) == D(residual["total_qty"])
    db.close()


def test_gap_flag_cleared_when_liquidation_preempts_stop_check(tmp_path):
    # On the resume tick, liquidation (step 3) finishes the tick before the SL
    # check (step 4, the gap flag's normal consumer): the one-shot flag must
    # not survive the tick, or a later unrelated SL fill would be mislabeled
    # gap_stop_fill.
    db, clock, engine, _ = _engine(tmp_path, seed=_BIG_SEED)
    _provider(
        engine,
        [_snap(), _snap()]
        + [SnapshotOutcome.TIMEOUT] * 3
        + [_snap(mark=40000, mid=40000)],  # resume: liquidatable near 40400
    )
    engine.start_plan(_decision("long", 10))  # reduce plan, slices to spare
    clock.advance(30)
    engine.tick()  # slice 0 fills, SL set
    for _ in range(3):
        clock.advance(30)
        engine.tick()  # 3 failures -> pause
    clock.advance(30)
    r = engine.tick()  # resume; liquidation fires before _maybe_stop_loss runs
    assert r.has(TickEvent.RESUMED)
    assert r.has(TickEvent.LIQUIDATION_CLOSE)
    assert not r.has(TickEvent.GAP_STOP_FILL)
    assert engine._resume_pending_gap_check is False
    db.close()


def test_rejected_plan_writes_rejected_order_row(tmp_path):
    # §5.2: a validation failure before the fill flow still writes an orders
    # row (status="rejected") — orders is the exported audit trail.
    db, clock, engine, _ = _engine(tmp_path, leverage="1")
    _provider(engine, [_snap()])
    start = engine.start_plan(_decision("long", 1))
    assert start.disposition.value == "reject"
    row = db.conn.execute(
        "SELECT status, status_reason, side, type, active_from FROM orders WHERE run_id='r'"
    ).fetchone()
    assert row is not None
    assert (row["status"], row["status_reason"]) == ("rejected", "no_legal_slice")
    assert row["side"] == "buy"
    assert row["type"] == "paper_market"
    assert row["active_from"] is None  # never enters the fill flow
    db.close()


def test_reject_after_missed_out_flip_close_leg_still_reconciles_tp(tmp_path):
    # Compound case: a flip's close leg is miss-exhausted (terminal residual,
    # flip still pending), then a NEW decision REJECTs (no_legal_slice) before
    # any valid tick can advance the flip. The stale flip dies at registration,
    # so the REJECT path must restore the surviving position's TP even though
    # there was no live leg to supersede.
    db, clock, engine, _ = _engine(
        tmp_path,
        min_notional="60",
        seed=(PositionState(coin="BTC", size=D("-0.002"), entry_price=_MARK),),
    )
    _provider(engine, [_snap(), SnapshotOutcome.TIMEOUT, _snap()])
    start = engine.start_plan(_decision("long", 1))  # flip; 1-slice close leg
    assert start.plan_id is not None
    assert engine._flip is not None
    clock.advance(30)
    r = engine.tick()  # the close leg's only slice is missed -> terminal residual
    assert r.has(TickEvent.PLAN_TERMINAL)
    assert engine._flip is not None  # flip still pending, close leg terminal
    start2 = engine.start_plan(_decision("short", 1))  # delta 50 < min_notional 60
    assert start2.reason == "no_legal_slice"
    assert engine._flip is None  # stale flip dropped at registration
    assert engine._protection.take_profit is not None  # steady state restored
    row = db.conn.execute(
        "SELECT take_profit_price FROM current_positions WHERE run_id='r'"
    ).fetchone()
    assert row["take_profit_price"] is not None
    db.close()


def test_close_position_rejects_reason_without_status(tmp_path):
    db, clock, engine, _ = _engine(tmp_path)
    with pytest.raises(ValueError, match="plan_terminal_reason requires"):
        engine._close_position(
            _T0,
            None,
            PositionState(coin="BTC", size=D("0.001"), entry_price=_MARK),
            fill_reason="stop_loss",
            order_role="stop_loss",
            plan_terminal_reason="oops",
        )
    db.close()


def test_internal_state_guards_reject_invalid_construction():
    with pytest.raises(ValueError, match="counters out of order"):
        _Leg(
            plan_id="p",
            order_id="o",
            output_id=None,
            side=Side.BUY,
            disposition=PlanDisposition.PAPER_MARKET,
            slice_sizes=(D("0.001"),),
            reduce_only=False,
            order_role="entry",
            flip_plan_id=None,
            flip_leg=None,
            active_from=_T0,
            deadline=_T0,
            executed=1,
        )
    with pytest.raises(ValueError, match="stop_loss"):
        _Protection(stop_loss=D(0))
    with pytest.raises(ValueError, match="take_profit"):
        _Protection(take_profit=D(-5))
    with pytest.raises(ValueError, match="open_budget"):
        _FlipState(
            flip_plan_id="f",
            output_id=None,
            parsed=_decision("long", 1),
            open_budget=0,
            deadline=_T0,
        )
    with pytest.raises(ValueError, match="flip_plan_id"):
        _FlipState(
            flip_plan_id="",
            output_id=None,
            parsed=_decision("long", 1),
            open_budget=1,
            deadline=_T0,
        )


# --------------------------------------------------------------------------
# PR4 seams: restart gap-SL arming + cycle-end snapshot content
# --------------------------------------------------------------------------


def test_flag_restart_gap_labels_first_tick_stop_as_gap(tmp_path):
    # Process 1 opens a position; its SL persists on current_positions.
    db, clock, engine1, asset = _engine(tmp_path)
    _provider(engine1, [_snap(), _snap()])
    engine1.start_plan(_decision("long", 1))
    clock.advance(30)
    engine1.tick()  # fill + SL persisted
    sl = engine1._protection.stop_loss
    assert sl is not None

    # "Restart": a fresh engine hydrates protection from the store; the blind
    # window is armed explicitly (execution §1.2 step 6). The mark has crossed
    # the SL during the gap -> the first tick fills a gap stop, not a normal SL.
    engine2 = PaperExecutionEngine(
        db=db,
        run_id="r",
        asset=asset,
        clock=clock,
        provider=ScriptedSnapshotProvider("BTC", [_snap(mark=40000, mid=40000)]),
        risk_config=RiskConfig(leverage=D(5), max_target_margin_pct=60),
        decision_config=DecisionConfig(),
        paper_config=PaperTradingConfig.from_dict(None),
    )
    assert engine2._protection.stop_loss == sl  # hydrated, not forgotten
    engine2.flag_restart_gap()
    clock.advance(30)
    result = engine2.tick()
    assert result.has(TickEvent.GAP_STOP_FILL)
    assert not result.has(TickEvent.STOP_LOSS_FILL)
    reason = db.conn.execute(
        "SELECT fill_reason FROM fills WHERE run_id='r' ORDER BY rowid DESC LIMIT 1"
    ).fetchone()[0]
    assert reason == "gap_stop_fill"
    assert _size(db) == D(0)
    db.close()


def test_write_cycle_snapshot_records_position_row(tmp_path):
    db, clock, engine, _ = _engine(tmp_path)
    _provider(engine, [_snap(), _snap()])
    engine.start_plan(_decision("long", 1))
    clock.advance(30)
    engine.tick()  # fill -> position 0.001 @ 50025
    before = db.conn.execute("SELECT COUNT(*) FROM position_snapshots").fetchone()[0]
    engine.write_cycle_snapshot(D(51000))
    row = db.conn.execute("SELECT * FROM position_snapshots ORDER BY rowid DESC LIMIT 1").fetchone()
    after = db.conn.execute("SELECT COUNT(*) FROM position_snapshots").fetchone()[0]
    assert after == before + 1
    assert row["side"] == "long"
    assert D(row["position_size"]) == D("0.001")
    assert D(row["mark_price"]) == D(51000)
    assert D(row["position_notional"]) == D("0.001") * D(51000)
    # unrealized = size * (mark - entry) under the pinned context
    assert D(row["unrealized_pnl"]) == D("0.001") * (D(51000) - D("50025"))
    assert row["stop_loss_price"] is not None  # live protection captured
    db.close()
