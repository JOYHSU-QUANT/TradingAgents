"""Tests for the spec §5 acceptance validator."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from contrib.hyperliquid_perp.common.constants import STALE_MARKET_DATA_ERROR
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
from contrib.hyperliquid_perp.paper.no_decision import (
    NO_DECISION_STREAK_THRESHOLD,
    no_decision_shortfall,
    note_cycle_outcome,
    trailing_failure_streaks,
)
from contrib.hyperliquid_perp.paper.scheduler import DecisionInput, PaperScheduler
from contrib.hyperliquid_perp.paper.validation import validate_run
from contrib.hyperliquid_perp.persistence import repository as repo
from contrib.hyperliquid_perp.persistence.db import Database

from ..conftest import insert_decision_attempts

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


def test_report_rejects_nonzero_unrealized_without_as_of():
    from contrib.hyperliquid_perp.paper.validation import ValidationReport

    # A None as_of means the unrealized leg was valued at 0; a non-zero
    # unrealized_pnl beside it would print a self-contradicting summary.
    common = {
        "run_id": "r",
        "cycle_count": 30,
        "api_failed_count": 0,
        "order_count": 0,
        "fill_count": 0,
        "rejected_order_count": 0,
        "orphan_order_count": 0,
        "orphan_fill_count": 0,
        "snapshot_mismatch_count": 0,
        "accounting_replay_mismatch_count": 0,
        "max_exposure_pct": None,
        "max_effective_leverage": None,
        "realized_pnl": D(0),
        "total_pnl": D(0),
        "total_fees": D(0),
        "net_funding_pnl": D(0),
        "failures": (),
    }
    with pytest.raises(ValueError, match="unrealized_pnl must be 0 when unrealized_as_of is None"):
        ValidationReport(**common, unrealized_pnl=D(5), unrealized_as_of=None)
    # 0 unrealized with a None as_of is the legal shape.
    ValidationReport(**common, unrealized_pnl=D(0), unrealized_as_of=None)


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


def test_replay_raise_contained_as_counted_integrity_failure(tmp_path):
    # A store so corrupt the replay itself crashes (a fill cell Decimal() cannot
    # read) must take the counted-failure exit-5 lane with a partial report —
    # classify_replay's "unverifiable books are an outcome" rule — not escape as
    # a generic crash. The ledger-derived metrics print n/a, never a fabricated 0.
    db = _run_one_cycle_with_fill(tmp_path)
    with db.transaction() as conn:
        conn.execute("UPDATE fills SET fill_price = 'garbage' WHERE run_id = 'r'")
    report = validate_run(db, run_id="r")
    assert report.accounting_replay_mismatch_count == 1
    assert any(f.startswith("accounting replay raised:") for f in report.failures)
    assert report.realized_pnl is None
    assert report.total_fees is None
    assert report.net_funding_pnl is None
    assert report.total_pnl is None
    assert not report.phase3_ready
    # Everything derivable before the raise still reports.
    assert report.order_count == 1
    assert report.fill_count == 1
    lines = report.summary_lines()
    assert "realized_pnl: n/a" in lines
    assert "total_pnl: n/a" in lines
    db.close()


def test_unreadable_snapshot_row_is_counted_mismatch_not_crash(tmp_path):
    # Per-row containment: a snapshot cell the checker cannot read is exactly
    # the corruption the identity check exists to catch — counted, not crashed.
    db = _run_one_cycle_with_fill(tmp_path)
    with db.transaction() as conn:
        conn.execute("UPDATE account_snapshots SET wallet_balance = 'xx' WHERE run_id = 'r'")
    report = validate_run(db, run_id="r")
    assert report.snapshot_mismatch_count >= 1
    assert any("identity check raised" in f for f in report.failures)
    assert not report.phase3_ready
    db.close()


def test_unreadable_exposure_cell_flags_row_and_omits_max(tmp_path):
    # The max_exposure/max_leverage scans skip a cell Decimal() rejects instead
    # of crashing; the rows themselves are already counted failures from the
    # identity phase.
    db = _run_one_cycle_with_fill(tmp_path)
    with db.transaction() as conn:
        conn.execute("UPDATE position_snapshots SET exposure_pct = 'bad' WHERE run_id = 'r'")
        conn.execute("UPDATE account_snapshots SET effective_leverage = 'bad' WHERE run_id = 'r'")
    report = validate_run(db, run_id="r")
    assert report.max_exposure_pct is None  # every exposure cell was unreadable
    assert report.max_effective_leverage is None  # same for the leverage scan
    assert report.snapshot_mismatch_count >= 2  # both corrupted rows counted
    assert not report.phase3_ready
    db.close()


def test_unreadable_unrealized_cell_reports_na_leg(tmp_path):
    # The unrealized leg of total_pnl is unavailable (n/a), not zero, when the
    # last snapshot's stored cell is unreadable — 0 would misread as flat.
    db = _run_one_cycle_with_fill(tmp_path)
    with db.transaction() as conn:
        conn.execute("UPDATE account_snapshots SET unrealized_pnl = 'bad' WHERE run_id = 'r'")
    report = validate_run(db, run_id="r")
    assert report.unrealized_pnl is None
    assert report.unrealized_as_of is not None  # a snapshot exists; its value doesn't
    assert report.total_pnl is None
    assert report.snapshot_mismatch_count >= 1
    db.close()


def test_report_rejects_partial_or_uncounted_ledger_absence():
    # New invariants for the replay-raised shape: the ledger trio is absent only
    # together, only as a counted failure, and total_pnl mirrors its components.
    from contrib.hyperliquid_perp.paper.validation import ValidationReport

    common = {
        "run_id": "r",
        "cycle_count": 0,
        "api_failed_count": 0,
        "order_count": 0,
        "fill_count": 0,
        "rejected_order_count": 0,
        "orphan_order_count": 0,
        "orphan_fill_count": 0,
        "snapshot_mismatch_count": 0,
        "max_exposure_pct": None,
        "max_effective_leverage": None,
        "unrealized_pnl": D(0),
        "unrealized_as_of": "2026-07-06T12:00:00+00:00",
    }
    # Partial absence: realized missing but fees present.
    with pytest.raises(ValueError, match="absent together"):
        ValidationReport(
            **common,
            accounting_replay_mismatch_count=1,
            realized_pnl=None,
            total_fees=D(0),
            net_funding_pnl=D(0),
            total_pnl=None,
            failures=("accounting replay raised: x — books unverifiable",),
        )
    # Absent ledger with a zero replay count: an uncounted integrity failure.
    with pytest.raises(ValueError, match="uncounted integrity failure"):
        ValidationReport(
            **common,
            accounting_replay_mismatch_count=0,
            realized_pnl=None,
            total_fees=None,
            net_funding_pnl=None,
            total_pnl=None,
            failures=(),
        )
    # total_pnl present while a component is absent: self-contradicting.
    with pytest.raises(ValueError, match="total_pnl must be absent"):
        ValidationReport(
            **common,
            accounting_replay_mismatch_count=1,
            realized_pnl=None,
            total_fees=None,
            net_funding_pnl=None,
            total_pnl=D(0),
            failures=("accounting replay raised: x — books unverifiable",),
        )
    # The legal replay-raised shape constructs fine.
    ValidationReport(
        **common,
        accounting_replay_mismatch_count=1,
        realized_pnl=None,
        total_fees=None,
        net_funding_pnl=None,
        total_pnl=None,
        failures=("accounting replay raised: x — books unverifiable",),
    )


def test_stale_pending_funding_warns_not_gates(tmp_path):
    # Q2 decision: a funding hour still pending long past settlement is surfaced
    # as a warning (the acceptance reader must see the totals are partial) but
    # never gates phase3 or the exit code — young pending events stay silent.
    from contrib.hyperliquid_perp.paper.reconcile import STALE_PENDING_FUNDING

    db = _run_one_cycle_with_fill(tmp_path)
    accounting.record_funding(
        db,
        run_id="r",
        mode="paper",
        symbol="BTC",
        funding_timestamp=_T0,
        position_size=D("0.001"),
        funding_rate=None,  # no rate yet: stored as a pending event
        mark_price=_MARK,
        recorded_at=_T0,
    )
    stale = validate_run(db, run_id="r", now=_T0 + STALE_PENDING_FUNDING + timedelta(hours=1))
    assert any("funding event(s) still pending" in w for w in stale.warnings)
    assert stale.failures == ()  # surface, never gate

    young = validate_run(db, run_id="r", now=_T0 + timedelta(hours=1))
    assert not any("funding event(s) still pending" in w for w in young.warnings)

    # A pending timestamp the backfill cannot even parse is *corrupt*, not
    # stale (backfill_pending_funding's vocabulary) — its own warning line,
    # regardless of "now".
    with db.transaction() as conn:
        conn.execute("UPDATE funding_events SET funding_timestamp = 'junk' WHERE run_id = 'r'")
    corrupt = validate_run(db, run_id="r", now=_T0 + timedelta(hours=1))
    assert any("unparseable" in w and "corrupt" in w for w in corrupt.warnings)
    assert not any("still pending more than" in w for w in corrupt.warnings)
    db.close()


def test_config_drift_breadcrumb_surfaces_as_warning(tmp_path):
    # Same-concept sibling of the pending-funding warning: the drift breadcrumb
    # is store-persisted but was visible only in the dead process's log; the
    # acceptance report must mention that the aggregate spans two parameter sets.
    db = _run_one_cycle_with_fill(tmp_path)
    with db.transaction() as conn:
        repo.upsert_scheduler_state(
            conn,
            "r",
            last_config_drift_status="drift",
            last_config_drift_error="risk differs",
            last_config_drift_at=_T0,
        )
    report = validate_run(db, run_id="r")
    assert any("config drift" in w and "risk differs" in w for w in report.warnings)
    assert report.failures == ()  # a warning, never a gate

    with db.transaction() as conn:
        repo.upsert_scheduler_state(conn, "r", last_config_drift_status="ok")
    clean = validate_run(db, run_id="r")
    assert not any("config drift" in w for w in clean.warnings)
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


# --------------------------------------------------------------------------
# no-decision / stale-feed streaks (issue #50)
# --------------------------------------------------------------------------

# The §6.2 class engine_bridge's freshness guard writes when the candle feed
# has stopped advancing (or the clocks cannot be compared)...
_STALE = ("api_failed", STALE_MARKET_DATA_ERROR)
# ...one of the other ways a cycle reaches no decision (a network blip is what
# RUNBOOK §7 calls expected; a failure of the l2Book read the freshness guard
# itself depends on lands here too)...
_BLIP = ("api_failed", "connection")
# ...and a cycle that decided.
_OK = "completed"


def _insert_outcomes(db, outcomes, *, start=_T0, run_id="r"):
    """Seed terminal cycles; returns the instant the LAST one was scheduled at.

    Every streak verdict is relative to a clock, so a test has to be able to
    say which one — the recency window turns "the run is blocked" into "the
    run WAS blocked, days ago" (see the archived-run test).
    """
    insert_decision_attempts(db, outcomes, run_id=run_id, start=start)
    return start + timedelta(hours=4 * (len(outcomes) - 1))


def test_streaks_count_trailing_failures_only(tmp_path):
    # The measure is "how long has this run been unable to decide NOW", not a
    # lifetime tally: a failure before a decided cycle does not count.
    db = _bare_run(tmp_path)
    _insert_outcomes(db, [_STALE, _OK, _STALE, _STALE, _STALE])
    streaks = trailing_failure_streaks(db.conn, "r")
    assert streaks.no_decision == 3
    assert streaks.stale_feed == 3
    db.close()


def test_stale_subset_is_the_newest_run_of_the_no_decision_streak(tmp_path):
    # Both numbers count back from the newest cycle, but the stale one stops at
    # the first failure of another class: three cycles with no decision, only
    # the newest two of them refused as stale.
    db = _bare_run(tmp_path)
    _insert_outcomes(db, [_OK, _BLIP, _STALE, _STALE])
    streaks = trailing_failure_streaks(db.conn, "r")
    assert streaks.no_decision == 3
    assert streaks.stale_feed == 2
    db.close()


def test_no_decision_streak_counts_the_l2book_outage_the_stale_one_cannot(tmp_path):
    # The hole the class-blind count exists to close: a failure of the very
    # endpoint the freshness guard now depends on files as ``connection`` /
    # ``malformed_response``, so a stale-only gate would let a run that has not
    # decided for days report clean.
    db = _bare_run(tmp_path)
    last = _insert_outcomes(db, [_OK] * 30 + [_BLIP] * NO_DECISION_STREAK_THRESHOLD)
    streaks = trailing_failure_streaks(db.conn, "r")
    assert streaks.no_decision == NO_DECISION_STREAK_THRESHOLD
    assert streaks.stale_feed == 0
    report = validate_run(db, run_id="r", now=last + timedelta(hours=1))
    assert report.failures == ()
    assert not report.phase3_ready
    assert len(report.shortfalls) == 1
    # ...and it says what it can rather than blaming a feed it has no evidence
    # about.
    assert "0 refused as stale market data, 3 other failures" in report.shortfalls[0]
    assert "all refused as stale" not in report.shortfalls[0]
    db.close()


def test_stale_subset_stops_at_the_first_failure_of_another_class(tmp_path):
    # The latch: `stale_feed` is the NEWEST RUN of stale refusals, not a tally
    # over the whole streak. Without it an older stale cycle behind a
    # `connection` failure would count, `stale_feed` could equal `no_decision`,
    # and the shortfall would say "all refused as stale market data" over a
    # streak that was mostly something else entirely.
    db = _bare_run(tmp_path)
    last = _insert_outcomes(db, [_OK, _STALE, _BLIP, _STALE])
    streaks = trailing_failure_streaks(db.conn, "r")
    assert streaks.no_decision == 3
    assert streaks.stale_feed == 1  # 2 without the latch
    line = no_decision_shortfall(streaks, now=last + timedelta(hours=1))
    assert line is not None
    assert "mixed causes — 1 refused as stale market data, 2 other failures" in line
    assert "all refused as stale" not in line


def test_streak_ignores_a_completed_cycle_carrying_a_stale_class(tmp_path):
    # Defence in depth, not a production shape: both finalize paths write
    # error_type=None when a cycle decides, so a completed row cannot carry a
    # stale class today. Keying on the class alone would start counting decided
    # cycles the day either of those explicit clears is dropped.
    db = _bare_run(tmp_path)
    _insert_outcomes(db, [_STALE, _STALE, ("completed", STALE_MARKET_DATA_ERROR)])
    assert trailing_failure_streaks(db.conn, "r").no_decision == 0
    db.close()


def test_streak_skips_an_in_progress_attempt(tmp_path):
    # The newest row is usually the cycle running right now; it has said
    # nothing yet, so it neither counts nor breaks the streak behind it.
    db = _bare_run(tmp_path)
    _insert_outcomes(db, [_STALE, _STALE, _STALE, "in_progress"])
    assert trailing_failure_streaks(db.conn, "r").no_decision == 3
    db.close()


def test_streak_at_threshold_blocks_phase3_ready_as_a_shortfall(tmp_path):
    # Thirty clean cycles then three stale refusals: every integrity count is
    # clean (so not exit 5), the cycle gate is met — and the run still is not
    # ready, because nothing is reaching a decision (issue #50).
    db = _bare_run(tmp_path)
    last = _insert_outcomes(db, [_OK] * 30 + [_STALE] * NO_DECISION_STREAK_THRESHOLD)
    report = validate_run(db, run_id="r", now=last + timedelta(hours=1))
    assert report.cycle_count == 30
    assert report.failures == ()
    assert report.streaks.no_decision == NO_DECISION_STREAK_THRESHOLD
    assert not report.phase3_ready
    assert len(report.shortfalls) == 1
    assert "no_decision_streak = 3" in report.shortfalls[0]
    assert "~12h" in report.shortfalls[0]  # the count in the operator's units
    # The whole streak is stale, so the wording names the feed and the clock
    # instead of falling back to the class count.
    assert "all refused as stale market data" in report.shortfalls[0]
    lines = report.summary_lines()
    assert "no_decision_streak: 3" in lines
    assert "stale_feed_refusal_streak: 3" in lines
    assert any(line.startswith("shortfall: ") for line in lines)
    db.close()


def test_streak_below_threshold_does_not_gate(tmp_path):
    # Two failures (8h) still looks like a hiccup: the comparison is strict at
    # the threshold, and the streak prints either way.
    db = _bare_run(tmp_path)
    last = _insert_outcomes(db, [_OK] * 30 + [_STALE] * (NO_DECISION_STREAK_THRESHOLD - 1))
    report = validate_run(db, run_id="r", now=last + timedelta(hours=1))
    assert report.phase3_ready
    assert report.shortfalls == ()
    assert "no_decision_streak: 2" in report.summary_lines()
    db.close()


def test_streak_clears_once_a_cycle_decides_again(tmp_path):
    # The misfire exit the issue demands: an exchange maintenance window ends,
    # one cycle decides, and validate stops reporting it — no operator action,
    # no latch to release.
    db = _bare_run(tmp_path)
    last = _insert_outcomes(db, [_OK] * 30 + [_STALE] * 5)
    assert not validate_run(db, run_id="r", now=last + timedelta(hours=1)).phase3_ready
    recovered = _insert_outcomes(db, [_OK], start=_T0 + timedelta(hours=4 * 35))
    report = validate_run(db, run_id="r", now=recovered + timedelta(hours=1))
    assert report.streaks.no_decision == 0
    assert report.phase3_ready
    db.close()


def test_a_stopped_run_is_not_permanently_disqualified_by_its_last_hours(tmp_path):
    # The recency window: an acceptance run that happened to END during an
    # exchange outage would otherwise report exit 4 forever over an otherwise
    # complete 30-cycle dataset, with "run it again" as the only remedy.
    db = _bare_run(tmp_path)
    last = _insert_outcomes(db, [_OK] * 30 + [_STALE] * 5)
    # Judged while the run is still live: blocked.
    assert not validate_run(db, run_id="r", now=last + timedelta(hours=1)).phase3_ready
    # Judged a week later on the archived store: the streak is still reported,
    # but it no longer describes a run that "cannot decide now".
    later = validate_run(db, run_id="r", now=last + timedelta(days=7))
    assert later.streaks.no_decision == 5
    assert later.shortfalls == ()
    assert later.phase3_ready
    db.close()


def test_streak_is_dated_by_when_the_cycle_terminalized_not_by_its_slot(tmp_path):
    # A cycle stranded by a crash is terminalized on RESTART carrying its
    # original slot, hours or days old. Dating the streak by `scheduled_at`
    # would read a run that just came back as "stopped or archived" and
    # suppress the shortfall on exactly the run that has been unable to decide
    # the longest — so the window is measured from `timestamp`, when the row
    # last changed state.
    db = _bare_run(tmp_path)
    _insert_outcomes(db, [_STALE] * NO_DECISION_STREAK_THRESHOLD)
    adopted_at = _T0 + timedelta(days=3)  # the restart that terminalized it
    with db.transaction() as conn:
        conn.execute(
            "UPDATE decision_attempts SET timestamp = ? WHERE run_id = 'r'"
            " AND scheduled_at = (SELECT MAX(scheduled_at) FROM decision_attempts"
            " WHERE run_id = 'r')",
            (adopted_at.isoformat(),),
        )
    streaks = trailing_failure_streaks(db.conn, "r")
    assert streaks.latest_terminal_at == adopted_at
    # Judged just after the restart: still blocked, even though the newest
    # slot is three days old.
    assert no_decision_shortfall(streaks, now=adopted_at + timedelta(hours=1)) is not None
    db.close()


def test_streak_recency_window_boundary_is_exclusive(tmp_path):
    # Pins the comparison direction: exactly at the window the streak still
    # describes the run, one second past it does not.
    from contrib.hyperliquid_perp.paper.no_decision import _STREAK_RECENCY_WINDOW

    db = _bare_run(tmp_path)
    last = _insert_outcomes(db, [_STALE] * NO_DECISION_STREAK_THRESHOLD)
    streaks = trailing_failure_streaks(db.conn, "r")
    assert no_decision_shortfall(streaks, now=last + _STREAK_RECENCY_WINDOW) is not None
    just_past = last + _STREAK_RECENCY_WINDOW + timedelta(seconds=1)
    assert no_decision_shortfall(streaks, now=just_past) is None
    db.close()


def test_streaks_reject_an_impossible_pair():
    # The query cannot build these, but the type is also constructed by hand.
    # A stale subset larger than its superset would make the shortfall claim
    # "all refused as stale market data" over a mixed streak.
    from contrib.hyperliquid_perp.paper.no_decision import TrailingFailureStreaks

    with pytest.raises(ValueError, match="cannot exceed"):
        TrailingFailureStreaks(1, 2, None)
    with pytest.raises(ValueError, match="must be >= 0"):
        TrailingFailureStreaks(-1, 0, None)


def test_shortfall_helper_withholds_a_verdict_it_cannot_date():
    # A run whose newest terminal stamp is unreadable cannot be dated, so the
    # helper reports nothing rather than fabricating a current-state verdict.
    from contrib.hyperliquid_perp.paper.no_decision import TrailingFailureStreaks

    undatable = TrailingFailureStreaks(NO_DECISION_STREAK_THRESHOLD, 0, None)
    assert no_decision_shortfall(undatable, now=_T0) is None


def test_no_decision_shortfall_withholds_when_the_newest_cycle_is_stamped_in_the_future():
    # Issue #94 raised this as "withhold on a negative gap, like the
    # unparseable stamp"; review of the validators' ORDERING said otherwise.
    # live.validate_live_run reads ``now`` BEFORE its store query, so a daemon
    # finalizing the third api_failed cycle between the two calls stamps it a
    # few ms after ``now`` — a genuinely stuck run that must NOT pass the
    # gate. A stamp ahead of ``now`` is trivially recent; only the size of a
    # POSITIVE gap can make a streak stale. Pinned across the whole future
    # side, not just the race-sized one.
    from contrib.hyperliquid_perp.paper.no_decision import TrailingFailureStreaks

    for ahead_by in (timedelta(milliseconds=5), timedelta(seconds=30), timedelta(days=3)):
        ahead = TrailingFailureStreaks(NO_DECISION_STREAK_THRESHOLD, 0, _T0 + ahead_by)
        assert no_decision_shortfall(ahead, now=_T0) is not None, ahead_by


def test_note_cycle_outcome_escalates_to_error_at_the_threshold(caplog):
    # The loops' in-process half: WARNING for the first two, ERROR from the
    # third on (the funding source's 3-strike shape).
    import logging

    streak = 0
    with caplog.at_level(logging.WARNING, logger="contrib.hyperliquid_perp.paper.no_decision"):
        for _ in range(NO_DECISION_STREAK_THRESHOLD + 1):
            streak = note_cycle_outcome(streak, "api_failed", STALE_MARKET_DATA_ERROR, run_id="r")
    assert streak == NO_DECISION_STREAK_THRESHOLD + 1
    levels = [r.levelno for r in caplog.records if "decision cycle for r" in r.getMessage()]
    assert levels == [logging.WARNING] * (NO_DECISION_STREAK_THRESHOLD - 1) + [logging.ERROR] * 2
    # The ERROR names where the operator sees it next, so the log line is
    # actionable on its own.
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert "validate" in errors[0]
    assert "stale market data" in errors[0]


def test_note_cycle_outcome_is_reset_by_a_cycle_that_decided(caplog):
    # The reason both loops feed EVERY terminal outcome and not just the
    # failures: the store query breaks on any decided cycle, so a counter fed
    # only api_failed would log "3 consecutive, ~12h with no decision" over a
    # run that decided in between — and `validate`, which the same ERROR tells
    # the operator to check, would show no shortfall at all.
    import logging

    streak = 0
    with caplog.at_level(logging.WARNING, logger="contrib.hyperliquid_perp.paper.no_decision"):
        streak = note_cycle_outcome(streak, "api_failed", STALE_MARKET_DATA_ERROR, run_id="r")
        streak = note_cycle_outcome(streak, "api_failed", STALE_MARKET_DATA_ERROR, run_id="r")
        assert streak == 2
        streak = note_cycle_outcome(streak, "completed", None, run_id="r")
        assert streak == 0
        streak = note_cycle_outcome(streak, "invalid_output", None, run_id="r")
        assert streak == 0
        streak = note_cycle_outcome(streak, "api_failed", STALE_MARKET_DATA_ERROR, run_id="r")
    assert streak == 1
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_note_cycle_outcome_counts_any_failure_class(caplog):
    # Class-blind by design: an l2Book outage (``connection``) blocks the run
    # exactly as a stalled feed does, and its ERROR names the class rather than
    # blaming a feed it has no evidence about.
    import logging

    streak = 0
    with caplog.at_level(logging.WARNING, logger="contrib.hyperliquid_perp.paper.no_decision"):
        for _ in range(NO_DECISION_STREAK_THRESHOLD):
            streak = note_cycle_outcome(streak, "api_failed", "connection", run_id="r")
    assert streak == NO_DECISION_STREAK_THRESHOLD
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert "connection" in errors[0]
    assert "stale market data" not in errors[0]
