"""Tests for restart reconciliation (phase2-execution §1.2)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
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
from contrib.hyperliquid_perp.paper.engine import AssetSpec, PaperExecutionEngine
from contrib.hyperliquid_perp.paper.market_feed import ScriptedSnapshotProvider
from contrib.hyperliquid_perp.paper.reconcile import (
    ReconciliationError,
    reconcile_on_restart,
)
from contrib.hyperliquid_perp.paper.scheduler import parse_instant
from contrib.hyperliquid_perp.persistence import repository as repo
from contrib.hyperliquid_perp.persistence.db import Database
from contrib.hyperliquid_perp.persistence.models import AccountLedger

D = Decimal
_T0 = datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)
_MARK = D(50000)


def _init(tmp_path):
    db = Database(tmp_path / "rc.db")
    accounting.initialize_run(
        db, run_id="r", mode="paper", initial_balance_usdc=D(1000), schema_version=1
    )
    return db


def _decision(side: str, margin: int) -> ParsedDecision:
    dec = TargetDecision(
        decision_mode=DecisionMode.SET_TARGET,
        target_side=TargetSide(side),
        requested_target_margin_pct=margin,
        confidence=D("0.8"),
        rationale="r",
        key_risks=("k",),
    )
    return ParsedDecision(decision=dec, is_valid=True, invalid_reason=None, raw_response="{}")


def _engine_with_plan(db, *, slices_filled: int):
    """Start a 2-slice TWAP and fill ``slices_filled`` of it, then 'crash'."""
    clock = ManualClock(_T0)
    asset = AssetSpec(
        coin="BTC",
        sz_decimals=3,
        margin_schedule=MarginSchedule(coin="BTC", tiers=(MarginTier(D(0), D(50)),)),
    )
    script = [(_MARK, _MARK)] * (1 + slices_filled)
    engine = PaperExecutionEngine(
        db=db,
        run_id="r",
        asset=asset,
        clock=clock,
        provider=ScriptedSnapshotProvider("BTC", script),
        risk_config=RiskConfig(leverage=D(5), max_target_margin_pct=60),
        decision_config=DecisionConfig(),
        paper_config=PaperTradingConfig.from_dict(None),
    )
    start = engine.start_plan(_decision("long", 2), output_id="out-1")
    assert start.plan_id is not None
    for _ in range(slices_filled):
        clock.advance(30)
        engine.tick()
    return clock, start.plan_id


class _Rates:
    def __init__(self, rate):
        self._rate = rate
        self.calls = 0

    def rate_at(self, coin, ts):
        self.calls += 1
        return self._rate


# --------------------------------------------------------------------------
# steps 1–3 + 8: cancel live plan/orders, record residual, force a new cycle
# --------------------------------------------------------------------------


def test_unfinished_plan_canceled_with_residual_and_immediate_cycle(tmp_path):
    db = _init(tmp_path)
    clock, plan_id = _engine_with_plan(db, slices_filled=1)  # 0.001 of 0.002 filled

    now = clock.now() + timedelta(minutes=5)
    report = reconcile_on_restart(db, run_id="r", now=now)

    assert report.canceled_plan_ids == (plan_id,)
    plan = repo.get_execution_plan(db.conn, plan_id)
    assert plan["status"] == "canceled_restart"
    assert plan["status_reason"] == "restart"
    assert D(plan["residual_qty"]) == D("0.001")

    order = db.conn.execute(
        "SELECT * FROM orders WHERE order_id = ?", (report.canceled_order_ids[0],)
    ).fetchone()
    assert order["status"] == "canceled"
    assert order["status_reason"] == "canceled_restart"

    # Step 8: the abandoned target forces an immediate new cycle.
    assert report.forced_immediate_cycle
    state = repo.get_scheduler_state(db.conn, "r")
    assert parse_instant(state["next_decision_at"]) == now
    db.close()


def test_reconcile_is_idempotent(tmp_path):
    db = _init(tmp_path)
    clock, _plan_id = _engine_with_plan(db, slices_filled=1)
    now = clock.now() + timedelta(minutes=5)
    reconcile_on_restart(db, run_id="r", now=now)
    second = reconcile_on_restart(db, run_id="r", now=now + timedelta(minutes=1))
    assert second.canceled_plan_ids == ()
    assert not second.forced_immediate_cycle
    # next_decision_at keeps the first pass's forced value.
    state = repo.get_scheduler_state(db.conn, "r")
    assert parse_instant(state["next_decision_at"]) == now
    db.close()


def test_no_live_plan_means_no_forced_cycle(tmp_path):
    db = _init(tmp_path)
    report = reconcile_on_restart(db, run_id="r", now=_T0)
    assert report.canceled_plan_ids == ()
    assert not report.forced_immediate_cycle
    assert repo.get_scheduler_state(db.conn, "r") is None  # untouched
    db.close()


# --------------------------------------------------------------------------
# step 5: pending funding backfilled exactly once from the stored basis
# --------------------------------------------------------------------------


def test_pending_funding_backfills_exactly_once(tmp_path):
    db = _init(tmp_path)
    hour = _T0.replace(minute=0)
    res = accounting.record_funding(
        db,
        run_id="r",
        mode="paper",
        symbol="BTC",
        funding_timestamp=hour,
        position_size=D("0.001"),
        funding_rate=None,  # rate unavailable -> pending, basis mark stored
        mark_price=_MARK,
    )
    assert res.status == "pending"

    rates = _Rates(D("0.0001"))
    report = reconcile_on_restart(db, run_id="r", now=_T0, funding_source=rates)
    assert report.funding_posted == 1
    assert report.funding_still_pending == 0
    ledger = repo.get_current_account_state(db.conn, "r")
    # funding_pnl = -(0.001 * 50000) * 0.0001 = -0.005, posted to the wallet once.
    assert ledger.net_funding_pnl == D("-0.005")
    assert ledger.wallet_balance == D("999.995")

    # A second pass finds nothing pending — the wallet must not move again.
    again = reconcile_on_restart(db, run_id="r", now=_T0, funding_source=rates)
    assert again.funding_posted == 0
    assert repo.get_current_account_state(db.conn, "r").wallet_balance == D("999.995")
    db.close()


def test_pending_funding_stays_pending_without_rate(tmp_path):
    db = _init(tmp_path)
    accounting.record_funding(
        db,
        run_id="r",
        mode="paper",
        symbol="BTC",
        funding_timestamp=_T0,
        position_size=D("0.001"),
        funding_rate=None,
        mark_price=_MARK,
    )
    report = reconcile_on_restart(db, run_id="r", now=_T0, funding_source=_Rates(None))
    assert report.funding_posted == 0
    assert report.funding_still_pending == 1
    # Never fabricated: the wallet is untouched.
    assert repo.get_current_account_state(db.conn, "r").wallet_balance == D(1000)
    db.close()


# --------------------------------------------------------------------------
# steps 4 + 7: a store that cannot rebuild itself refuses to trade
# --------------------------------------------------------------------------


def test_replay_mismatch_fails_startup(tmp_path):
    # FLAT run over corrupt books: nothing to protect, so refuse loudly.
    db = _init(tmp_path)
    with db.transaction() as conn:
        repo.upsert_current_account_state(
            conn,
            "r",
            AccountLedger(wallet_balance=D(123)),  # contradicts genesis 1000
        )
    with pytest.raises(ReconciliationError, match="accounting replay"):
        reconcile_on_restart(db, run_id="r", now=_T0)
    db.close()


def test_replay_mismatch_with_open_position_reports_protection_only(tmp_path):
    """Non-flat + corrupt books: report the mismatch, never exit-and-abandon the SL/TP.

    Exiting would leave a live position with nobody watching its stops — worse
    than the corruption. Step 8 (forced cycle) must be skipped: a new decision
    would size against books the store cannot rebuild.
    """
    db = _init(tmp_path)
    clock, plan_id = _engine_with_plan(db, slices_filled=1)  # non-flat position
    with db.transaction() as conn:
        repo.upsert_current_account_state(
            conn,
            "r",
            AccountLedger(wallet_balance=D(123)),  # contradicts the replayed ledger
        )
    now = clock.now() + timedelta(minutes=5)
    report = reconcile_on_restart(db, run_id="r", now=now)

    assert report.replay_mismatch
    assert "accounting replay" in report.replay_error
    # Steps 1-3 still ran: the stale plan is canceled either way.
    assert report.canceled_plan_ids == (plan_id,)
    # Step 8 skipped despite the canceled plan.
    assert not report.forced_immediate_cycle
    state = repo.get_scheduler_state(db.conn, "r")
    assert state is None or state["next_decision_at"] is None
    db.close()


def test_restart_reconciliation_rejects_mismatch_with_forced_cycle():
    from contrib.hyperliquid_perp.paper.reconcile import RestartReconciliation

    with pytest.raises(ValueError, match="replay_error excludes"):
        RestartReconciliation(
            canceled_plan_ids=("p",),
            canceled_order_ids=(),
            funding_posted=0,
            funding_still_pending=0,
            forced_immediate_cycle=True,
            replay_error="boom",
        )


def test_unknown_run_raises(tmp_path):
    db = Database(tmp_path / "empty.db")
    with pytest.raises(ValueError, match="does not exist"):
        reconcile_on_restart(db, run_id="ghost", now=_T0)
    db.close()


# --------------------------------------------------------------------------
# end to end: reconcile-forced cycle actually fires via the scheduler
# --------------------------------------------------------------------------


def test_forced_cycle_fires_immediately_via_scheduler(tmp_path):
    from contrib.hyperliquid_perp.domains.perp.schema import PerpMarketContext
    from contrib.hyperliquid_perp.paper.scheduler import (
        CycleEvent,
        DecisionInput,
        PaperScheduler,
    )

    db = _init(tmp_path)
    clock, _plan_id = _engine_with_plan(db, slices_filled=1)
    # Old rhythm: pretend a prior cycle set next far in the future.
    with db.transaction() as conn:
        repo.upsert_scheduler_state(
            conn, "r", next_decision_at=clock.now() + timedelta(hours=3), updated_at=clock.now()
        )
    now = clock.now() + timedelta(minutes=5)
    clock.set(now)
    report = reconcile_on_restart(db, run_id="r", now=now)
    assert report.forced_immediate_cycle

    class _Provider:
        def build_input(self, *, coin, as_of):
            return DecisionInput(
                context=PerpMarketContext(
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
            )

        def request_decision(self, decision_input):
            return _decision("long", 5)

    from contrib.hyperliquid_perp.paper.engine import AssetSpec, PaperExecutionEngine
    from contrib.hyperliquid_perp.paper.market_feed import ScriptedSnapshotProvider

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
        provider=ScriptedSnapshotProvider("BTC", [(_MARK, _MARK)]),
        risk_config=risk,
        decision_config=DecisionConfig(),
        paper_config=PaperTradingConfig.from_dict(None),
    )
    scheduler = PaperScheduler(
        db=db,
        run_id="r",
        engine=engine,
        clock=clock,
        provider=_Provider(),
        asset=asset,
        risk_config=risk,
        decision_config=DecisionConfig(),
    )
    result = scheduler.poll()  # would have been None until the old 3h boundary
    assert result is not None and result.event is CycleEvent.COMPLETED
    assert result.scheduled_at == now  # the forced stamp, not the old grid
    db.close()


# --------------------------------------------------------------------------
# integration: engine-recorded pending funding backfills via reconcile
# --------------------------------------------------------------------------


class _NoRate:
    def rate_at(self, coin, ts):
        return None


def test_engine_pending_funding_backfills_via_reconcile(tmp_path):
    db = _init(tmp_path)
    clock = ManualClock(_T0)
    asset = AssetSpec(
        coin="BTC",
        sz_decimals=3,
        margin_schedule=MarginSchedule(coin="BTC", tiers=(MarginTier(D(0), D(50)),)),
    )
    engine = PaperExecutionEngine(
        db=db,
        run_id="r",
        asset=asset,
        clock=clock,
        provider=ScriptedSnapshotProvider("BTC", [(_MARK, _MARK)] * 3),
        risk_config=RiskConfig(leverage=D(5), max_target_margin_pct=60),
        decision_config=DecisionConfig(),
        paper_config=PaperTradingConfig.from_dict(None),
        funding_source=_NoRate(),  # rate unavailable -> engine records pending
    )
    engine.start_plan(_decision("long", 1), output_id="o")
    clock.advance(30)
    engine.tick()  # fill; funding baseline established at the first tick's hour
    clock.set(_T0 + timedelta(hours=2))
    engine.tick()  # crosses settlement hours -> pending events with stored basis
    pending = repo.iter_funding_events(db.conn, "r", status="pending")
    assert len(pending) == 2
    assert all(p["mark_price"] == "50000" for p in pending)  # basis captured

    wallet_before = repo.get_current_account_state(db.conn, "r").wallet_balance
    report = reconcile_on_restart(
        db, run_id="r", now=clock.now(), funding_source=_Rates(D("0.0001"))
    )
    assert report.funding_posted == 2
    ledger = repo.get_current_account_state(db.conn, "r")
    # Each hour: -(0.001 * 50000) * 0.0001 = -0.005, exactly once per event.
    assert ledger.wallet_balance == wallet_before + D("-0.005") * 2
    assert repo.iter_funding_events(db.conn, "r", status="pending") == []
    db.close()


def test_in_progress_attempt_suppresses_forced_cycle(tmp_path):
    # Step 8 inverse: an in-progress decision attempt IS the immediate cycle
    # (it resumes on the first poll), so a canceled plan must NOT also force
    # next_decision_at to now — that would double-schedule the boundary.
    db = _init(tmp_path)
    clock, plan_id = _engine_with_plan(db, slices_filled=1)
    future = clock.now() + timedelta(hours=3)
    with db.transaction() as conn:
        repo.upsert_scheduler_state(conn, "r", next_decision_at=future, updated_at=clock.now())
        repo.insert_decision_attempt(
            conn,
            decision_attempt_id="r|live",
            timestamp=clock.now(),
            mode="paper",
            run_id="r",
            scheduled_at=clock.now(),
            attempt_count=1,
            status="in_progress",
        )
    now = clock.now() + timedelta(minutes=5)
    report = reconcile_on_restart(db, run_id="r", now=now)
    assert report.canceled_plan_ids == (plan_id,)  # the plan still dies
    assert not report.forced_immediate_cycle
    state = repo.get_scheduler_state(db.conn, "r")
    assert parse_instant(state["next_decision_at"]) == future  # untouched
    db.close()
