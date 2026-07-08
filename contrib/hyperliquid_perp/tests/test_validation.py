"""Tests for the spec §5 acceptance validator."""

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
from contrib.hyperliquid_perp.paper.market_feed import ScriptedSnapshotProvider
from contrib.hyperliquid_perp.paper.scheduler import DecisionInput, PaperScheduler
from contrib.hyperliquid_perp.paper.validation import validate_run
from contrib.hyperliquid_perp.persistence import repository as repo
from contrib.hyperliquid_perp.persistence.db import Database

from .conftest import insert_decision_attempts

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


class _OneShotProvider:
    def __init__(self, parsed):
        self._parsed = parsed

    def build_input(self, *, coin, as_of):
        return DecisionInput(context=_ctx(as_of))

    def request_decision(self, decision_input):
        return self._parsed


def _run_one_cycle_with_fill(tmp_path):
    """One completed cycle: decision -> paper_market order -> fill -> snapshots."""
    db = Database(tmp_path / "v.db")
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
        provider=ScriptedSnapshotProvider("BTC", [(_MARK, _MARK), (_MARK, _MARK)]),
        risk_config=risk,
        decision_config=DecisionConfig(),
        paper_config=PaperTradingConfig.from_dict(None),
    )
    scheduler = PaperScheduler(
        db=db,
        run_id="r",
        engine=engine,
        clock=clock,
        provider=_OneShotProvider(_decision("long", 1)),
        asset=asset,
        risk_config=risk,
        decision_config=DecisionConfig(),
    )
    result = scheduler.poll()
    assert result is not None and result.output_id is not None
    clock.advance(30)
    tick = engine.tick()
    assert tick.position_size == D("0.001")
    return db


def test_clean_single_cycle_run_validates_consistently(tmp_path):
    db = _run_one_cycle_with_fill(tmp_path)
    report = validate_run(db, run_id="r")
    assert report.cycle_count == 1
    assert report.order_count == 1
    assert report.fill_count == 1
    assert report.rejected_order_count == 0
    assert report.orphan_order_count == 0
    assert report.orphan_fill_count == 0
    assert report.snapshot_mismatch_count == 0
    assert report.accounting_replay_mismatch_count == 0
    assert report.failures == ()
    # fee = 0.001 * 50025 * 0.00045 (buy fill at mid + 5bps slippage)
    assert report.total_fees == D("0.001") * D("50025") * D("0.00045")
    assert report.net_funding_pnl == D(0)
    assert report.max_exposure_pct is not None and report.max_exposure_pct > 0
    # 1 cycle < 30 — consistent, but not yet Phase-3 ready.
    assert not report.phase3_ready
    db.close()


def test_report_splits_realized_and_unrealized_pnl(tmp_path):
    # total_pnl folds an unrealized leg valued at the last snapshot's mark; the
    # split fields surface that leg and its (possibly stale) valuation instant so
    # a run ended holding a position can't silently misread as flat (Q2 decision).
    db = _run_one_cycle_with_fill(tmp_path)
    report = validate_run(db, run_id="r")
    assert report.unrealized_as_of is not None  # a snapshot exists to value against
    assert report.total_pnl == (
        report.realized_pnl - report.total_fees + report.net_funding_pnl + report.unrealized_pnl
    )
    lines = report.summary_lines()
    assert any(line.startswith("realized_pnl: ") for line in lines)
    assert any(line.startswith("unrealized_pnl: ") for line in lines)
    assert any(line.startswith("unrealized_as_of: ") for line in lines)
    db.close()


def test_report_unrealized_as_of_none_without_snapshot(tmp_path):
    # No account snapshot at all: the unrealized leg is 0 and the valuation
    # instant is explicitly n/a rather than a fabricated 0-valued position.
    db = Database(tmp_path / "v.db")
    accounting.initialize_run(
        db, run_id="r", mode="paper", initial_balance_usdc=D(1000), schema_version=1
    )
    report = validate_run(db, run_id="r")
    assert report.unrealized_as_of is None
    assert report.unrealized_pnl == D(0)
    assert "unrealized_as_of: n/a (no account snapshot; valued at 0)" in report.summary_lines()
    db.close()


def test_orphan_fill_detected(tmp_path):
    db = _run_one_cycle_with_fill(tmp_path)
    # post_fill keeps the ledger consistent but references a non-existent order.
    accounting.post_fill(
        db,
        run_id="r",
        mode="paper",
        fill_id="r|ghost|0",
        order_id="ghost-order",
        symbol="BTC",
        side="buy",
        qty=D("0.001"),
        price=D(50000),
        fee_rate=D(0),
        timestamp=_T0,
    )
    report = validate_run(db, run_id="r")
    assert report.orphan_fill_count == 1
    assert report.accounting_replay_mismatch_count == 0  # ledger itself is coherent
    assert not report.phase3_ready
    assert any("orphan fill" in f for f in report.failures)
    db.close()


def test_orphan_orders_detected(tmp_path):
    db = _run_one_cycle_with_fill(tmp_path)
    with db.transaction() as conn:
        # An opening order with no output_id: only the AI path may open exposure.
        repo.insert_order(
            conn,
            order_id="rogue-open",
            mode="paper",
            run_id="r",
            symbol="BTC",
            order_role="entry",
            side="buy",
            order_type="paper_market",
            qty=D("0.001"),
            status="filled",
            timestamp=_T0,
        )
        # An order whose output_id resolves to no persisted ai_outputs row.
        repo.insert_order(
            conn,
            order_id="dangling-ref",
            mode="paper",
            run_id="r",
            symbol="BTC",
            order_role="rebalance",
            side="buy",
            order_type="paper_market",
            qty=D("0.001"),
            status="filled",
            output_id="no-such-output",
            timestamp=_T0,
        )
        # A reduce-only system order over a symbol with prior fills: NOT orphan.
        repo.insert_order(
            conn,
            order_id="sl-close",
            mode="paper",
            run_id="r",
            symbol="BTC",
            order_role="stop_loss",
            side="sell",
            order_type="stop_market",
            qty=D("0.001"),
            status="filled",
            reduce_only=True,
            timestamp=_T0 + timedelta(seconds=60),
        )
    report = validate_run(db, run_id="r")
    assert report.orphan_order_count == 2
    assert not report.phase3_ready
    db.close()


def test_snapshot_identity_violation_detected(tmp_path):
    db = _run_one_cycle_with_fill(tmp_path)
    with db.transaction() as conn:
        repo.insert_account_snapshot(
            conn,
            timestamp=_T0,
            mode="paper",
            run_id="r",
            wallet_balance=D(1000),
            account_equity=D(999),  # violates equity == wallet + unrealized
            available_balance=D(999),
            realized_pnl=D(0),
            unrealized_pnl=D(0),
            total_pnl=D(0),
            total_fees=D(0),
            net_funding_pnl=D(0),
            total_position_notional=D(0),
            effective_leverage=D(0),
            used_initial_margin=D(0),
            total_maintenance_margin=D(0),
            margin_ratio=None,
        )
    report = validate_run(db, run_id="r")
    assert report.snapshot_mismatch_count == 1
    assert not report.phase3_ready
    db.close()


def test_cycle_count_ignores_in_progress_attempts(tmp_path):
    db = _run_one_cycle_with_fill(tmp_path)
    with db.transaction() as conn:
        repo.insert_decision_attempt(
            conn,
            decision_attempt_id="r|open",
            timestamp=_T0,
            mode="paper",
            run_id="r",
            scheduled_at=_T0 + timedelta(hours=4),
            attempt_count=1,
            status="in_progress",
        )
    report = validate_run(db, run_id="r")
    assert report.cycle_count == 1  # the live attempt is not a finished cycle
    db.close()


def test_unknown_run_raises(tmp_path):
    db = Database(tmp_path / "empty.db")
    with pytest.raises(ValueError, match="does not exist"):
        validate_run(db, run_id="ghost")
    db.close()


def _bare_run(tmp_path):
    """A run with genesis only — no cycles, orders, fills, or snapshots."""
    db = Database(tmp_path / "v.db")
    accounting.initialize_run(
        db, run_id="r", mode="paper", initial_balance_usdc=D(1000), schema_version=1
    )
    return db


def test_reduce_only_order_without_prior_fill_or_seed_is_orphan(tmp_path):
    # Positive case for the system-order rule: a stop_loss with NO earlier
    # same-symbol fill and NO seed position had no position to reduce — orphan.
    db = _bare_run(tmp_path)
    with db.transaction() as conn:
        repo.insert_order(
            conn,
            order_id="sl-orphan",
            mode="paper",
            run_id="r",
            symbol="BTC",
            order_role="stop_loss",
            side="sell",
            order_type="stop_market",
            qty=D("0.001"),
            status="filled",
            reduce_only=True,
            timestamp=_T0,
        )
    report = validate_run(db, run_id="r")
    assert report.orphan_order_count == 1
    assert "orphan order sl-orphan" in report.failures
    assert not report.phase3_ready
    db.close()


_ACCOUNT_SNAPSHOT_COLUMNS = (
    "timestamp, mode, run_id, wallet_balance, account_equity, available_balance,"
    " realized_pnl, unrealized_pnl, total_pnl, total_fees, net_funding_pnl,"
    " total_position_notional, effective_leverage, used_initial_margin,"
    " total_maintenance_margin, margin_ratio"
)


def test_duplicate_timestamp_account_snapshots_warn_not_fail(tmp_path):
    # Two account snapshots sharing one timestamp make the same-instant
    # exposure_pct companion ambiguous: reported as a warning (coverage loss is
    # loud) but NOT a gating failure — the identical duplicate still passes its
    # own arithmetic identities.
    db = _run_one_cycle_with_fill(tmp_path)
    with db.transaction() as conn:
        ts = conn.execute(
            "SELECT timestamp FROM position_snapshots"
            " WHERE run_id = 'r' AND exposure_pct IS NOT NULL"
        ).fetchone()[0]
        conn.execute(
            f"INSERT INTO account_snapshots ({_ACCOUNT_SNAPSHOT_COLUMNS})"
            f" SELECT {_ACCOUNT_SNAPSHOT_COLUMNS} FROM account_snapshots"
            " WHERE run_id = 'r' AND timestamp = ?",
            (ts,),
        )
    report = validate_run(db, run_id="r")
    assert any("exposure_pct unverifiable" in w for w in report.warnings)
    assert report.snapshot_mismatch_count == 0
    assert report.failures == ()
    db.close()


def test_thirty_completed_cycles_make_phase3_ready(tmp_path):
    # invalid_output counts as a completed cycle (spec §3.1); api_failed never
    # does — 28 + 1 + 1 = 30 despite three api_failed rows in between.
    db = _bare_run(tmp_path)
    insert_decision_attempts(
        db, ["completed"] * 28 + ["invalid_output"] + ["api_failed"] * 3 + ["completed"], start=_T0
    )
    report = validate_run(db, run_id="r")
    assert report.cycle_count == 30
    assert report.api_failed_count == 3
    assert report.failures == ()
    assert report.phase3_ready
    db.close()


def test_twenty_nine_cycles_not_phase3_ready(tmp_path):
    # The >=30 boundary: a consistent store one cycle short stays gated.
    db = _bare_run(tmp_path)
    insert_decision_attempts(db, ["completed"] * 29, start=_T0)
    report = validate_run(db, run_id="r")
    assert report.cycle_count == 29
    assert report.failures == ()
    assert not report.phase3_ready
    db.close()


def test_corrupted_account_state_reports_replay_mismatch(tmp_path):
    # Corrupt the materialized books after real fills: replay recomputes from
    # events and must disagree — a gating failure (the CLI's exit-5 lane keys
    # off failures being non-empty).
    db = _run_one_cycle_with_fill(tmp_path)
    with db.transaction() as conn:
        conn.execute(
            "UPDATE current_account_state SET wallet_balance = '123456' WHERE run_id = 'r'"
        )
    report = validate_run(db, run_id="r")
    assert report.accounting_replay_mismatch_count == 1
    assert "replay ledger mismatch: current_account_state disagrees" in report.failures
    assert not report.phase3_ready
    db.close()
