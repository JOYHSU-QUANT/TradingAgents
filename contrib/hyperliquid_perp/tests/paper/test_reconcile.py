"""Tests for restart reconciliation (phase2-execution §1.2)."""

from __future__ import annotations

import logging
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
from contrib.hyperliquid_perp.paper import accounting, reconcile as reconcile_module
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

    def rate_at(self, coin, ts):
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


def test_force_cycle_survives_crash_before_replay(tmp_path, monkeypatch):
    """The step-8 force commits with the cancels, not a later transaction.

    A crash during the funding/replay work after the cancels must not strand the
    store with canceled plans but the *old* next_decision_at — that would idle the
    position until the stale clock (up to 4h) while a re-run, finding the plans
    already canceled_restart, rebuilds an empty canceled list and never re-forces.

    The crash is injected at the backfill CALL, not inside it through a raising
    ``rate_at``: every lane within that pass is contained on purpose (the pass
    may never abort — issue #193), so driving this through the reader would be
    asserting the containment is absent. What this pins is the commit
    boundary, whatever it is that fails after the cancels.
    """
    db = _init(tmp_path)
    clock, plan_id = _engine_with_plan(db, slices_filled=1)  # non-flat, live plan
    accounting.record_funding(  # a pending event so backfill has work to do
        db,
        run_id="r",
        mode="paper",
        symbol="BTC",
        funding_timestamp=_T0,
        position_size=D("0.001"),
        funding_rate=None,
        mark_price=_MARK,
    )

    def boom(*args, **kwargs):
        raise RuntimeError("network down mid-backfill")

    monkeypatch.setattr(reconcile_module, "backfill_pending_funding", boom)
    now = clock.now() + timedelta(minutes=5)
    with pytest.raises(RuntimeError, match="network down"):
        reconcile_on_restart(db, run_id="r", now=now, funding_source=_Rates(D("0.0001")))

    # The cancel + force committed before the crash — the force signal is durable.
    assert repo.get_execution_plan(db.conn, plan_id)["status"] == "canceled_restart"
    state = repo.get_scheduler_state(db.conn, "r")
    assert parse_instant(state["next_decision_at"]) == now
    db.close()


def test_stale_pending_funding_escalates_to_error(tmp_path, caplog):
    """A pending event older than the stale threshold logs ERROR, not the ordinary
    WARNING — its rate for a settled hour will likely never resolve, and only this
    age check distinguishes it from a rate that is merely un-published yet (the
    fetch source resets its own failure counter on every successful fetch)."""
    from contrib.hyperliquid_perp.paper.reconcile import (
        STALE_PENDING_FUNDING,
        backfill_pending_funding,
    )

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
    now = _T0 + STALE_PENDING_FUNDING + timedelta(minutes=1)  # past the threshold
    with caplog.at_level(logging.WARNING):
        posted, still_pending = backfill_pending_funding(
            db, run_id="r", now=now, funding_source=_Rates(None)
        )
    assert (posted, still_pending) == (0, 1)
    assert any(
        r.levelno == logging.ERROR and "will never resolve" in r.getMessage()
        for r in caplog.records
    )
    # The line no longer ASSERTS a cause it cannot know: the same count also
    # covers an event the reader or a defect has been failing on every pass,
    # so it points at the per-event ERRORs rather than blaming a delisted coin.
    assert any(
        "check the per-event ERROR lines above" in r.getMessage() for r in caplog.records
    )
    db.close()


def test_young_pending_funding_warns_not_errors(tmp_path, caplog):
    """A recently-elapsed pending event stays a WARNING — it may just be unsettled."""
    from contrib.hyperliquid_perp.paper.reconcile import backfill_pending_funding

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
    now = _T0 + timedelta(hours=1)  # well under the stale threshold
    with caplog.at_level(logging.WARNING):
        backfill_pending_funding(db, run_id="r", now=now, funding_source=_Rates(None))
    assert not any(r.levelno == logging.ERROR for r in caplog.records)
    assert any(r.levelno == logging.WARNING and "pending" in r.getMessage() for r in caplog.records)
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
    than the corruption. The reported ``forced_immediate_cycle`` stays False: a new
    decision must never size against books the store cannot rebuild. The store-side
    ``next_decision_at=now`` written with the cancels (so a crash can't strand the
    force signal) is inert here — protection-only halts the scheduler, so it never
    polls that clock; only a later healthy restart would act on it.
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
    # The report never claims a forced cycle over mismatched books...
    assert not report.forced_immediate_cycle
    # ...but the store-side force stamp was committed with the cancels; it is inert
    # under protection-only (the scheduler is halted and never polls it).
    state = repo.get_scheduler_state(db.conn, "r")
    assert parse_instant(state["next_decision_at"]) == now
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
                    day_change_pct=0.0,  # prev == mark: a reference exists, so 0, not None
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


# --------------------------------------------------------------------------
# protection-first containment: corrupt store data must not abort the restart
# --------------------------------------------------------------------------


def test_backfill_contains_corrupt_stored_row(tmp_path, caplog):
    """One corrupt pending row must not abort the whole backfill pass.

    ``record_funding`` fail-louds on a corrupt stored basis (mark missing) —
    correct in isolation, but uncontained it would abort the restart BEFORE the
    protection-only fork (live position unwatched, crash-loop on the same row
    every retry) and kill the live loop at a cycle boundary. The event is
    logged at ERROR, left pending, and the batch's other events still post.
    """
    from contrib.hyperliquid_perp.paper.reconcile import backfill_pending_funding

    good_hour = _T0.replace(minute=0)
    bad_hour = good_hour - timedelta(hours=1)
    db = _init(tmp_path)
    for hour in (bad_hour, good_hour):
        accounting.record_funding(
            db,
            run_id="r",
            mode="paper",
            symbol="BTC",
            funding_timestamp=hour,
            position_size=D("0.001"),
            funding_rate=None,  # pending, basis mark stored
            mark_price=_MARK,
        )
    with db.transaction() as conn:  # simulate a legacy/corrupt stored basis
        row = conn.execute(
            "SELECT funding_event_id FROM funding_events ORDER BY funding_timestamp LIMIT 1"
        ).fetchone()
        conn.execute(
            "UPDATE funding_events SET mark_price = NULL WHERE funding_event_id = ?",
            (row["funding_event_id"],),
        )

    with caplog.at_level(logging.ERROR):
        posted, still_pending = backfill_pending_funding(
            db, run_id="r", now=good_hour, funding_source=_Rates(D("0.0001"))
        )

    assert (posted, still_pending) == (1, 1)
    assert any("could not post" in r.getMessage() for r in caplog.records)
    # The corrupt event got its own ERROR — not the "rate still unavailable"
    # young-pending WARNING, which would misdirect the operator.
    assert not any("still unavailable" in r.getMessage() for r in caplog.records)
    # The good event moved the wallet exactly once; the corrupt one never did.
    assert repo.get_current_account_state(db.conn, "r").wallet_balance == D("999.995")
    db.close()


def test_backfill_reads_a_reader_failure_apart_from_a_corrupt_row(tmp_path, caplog):
    """A failing funding READER is not a corrupt stored row (issue #193).

    Both are contained per event — the pass may never abort — but they send an
    operator to opposite places. ``rate_at`` answers a venue failure with
    ``None`` and lets everything else through on purpose (issue #157), so what
    lands here is a defect of ours: a drifted signature, a naive clock. Filed
    under "corrupt stored row; fix it in the store", it sent that operator to
    SQLite to hunt a fault that was in the code.
    """
    db = _init(tmp_path)
    accounting.record_funding(
        db,
        run_id="r",
        mode="paper",
        symbol="BTC",
        funding_timestamp=_T0,
        position_size=D("0.001"),
        funding_rate=None,  # pending, so the backfill calls the reader
        mark_price=_MARK,
    )

    class _BrokenReader:
        def rate_at(self, coin, ts):
            raise ValueError("funding history window end must be timezone-aware (UTC)")

    with caplog.at_level(logging.ERROR):
        posted, still_pending = reconcile_module.backfill_pending_funding(
            db, run_id="r", now=_T0, funding_source=_BrokenReader()
        )

    # The pass completed and the event stays pending — it did not post, so it
    # still counts, whatever the reason.
    assert (posted, still_pending) == (0, 1)
    messages = [r.getMessage() for r in caplog.records]
    assert any("the funding reader failed" in m for m in messages)
    # The verdict an operator acts on: NOT the store.
    assert not any("fix it in the store" in m for m in messages)
    assert repo.get_current_account_state(db.conn, "r").wallet_balance == D("1000")
    db.close()


def test_backfill_still_reads_a_corrupt_stored_timestamp_as_a_corrupt_row(tmp_path, caplog):
    # The stored timestamp is now parsed in its own ``try``, ahead of the
    # reader, so its verdict had to be carried across the split deliberately:
    # a naive/garbled ``funding_timestamp`` IS a bad row, and must keep saying
    # so. Both ends of the split are pinned — this one and the reader lane
    # above — because the whole point is that they differ.
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
    with db.transaction() as conn:
        conn.execute("UPDATE funding_events SET funding_timestamp = ?", ("not-a-timestamp",))

    with caplog.at_level(logging.ERROR):
        posted, still_pending = reconcile_module.backfill_pending_funding(
            db, run_id="r", now=_T0, funding_source=_Rates(D("0.0001"))
        )

    assert (posted, still_pending) == (0, 1)
    messages = [r.getMessage() for r in caplog.records]
    assert any("fix it in the store" in m for m in messages)
    assert not any("the funding reader failed" in m for m in messages)
    db.close()


@pytest.mark.parametrize("column", ["funding_timestamp", "position_size"])
def test_backfill_reads_a_wrong_typed_stored_cell_as_a_corrupt_row(tmp_path, caplog, column):
    """A cell of the wrong TYPE is the store's fault, and must be said to be.

    Both columns reach a parser that answers a non-string with ``TypeError``,
    not ``ValueError`` — ``datetime.fromisoformat`` and ``Decimal``. Catching
    only ``ValueError`` sent those rows to the outer lane, which tells the
    operator to read a traceback and stop suspecting the store, for a row that
    is exactly the store's problem — issue #193's misdirection, reversed.

    Driven with a BLOB rather than a NULL: both columns are ``TEXT NOT NULL``,
    so NULL is unreachable, but SQLite's TEXT affinity does NOT convert a
    BLOB — it stores and returns ``bytes``. That is what makes this lane
    reachable, and therefore worth having.
    """
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
    with db.transaction() as conn:
        conn.execute(f"UPDATE funding_events SET {column} = X'0102'")  # noqa: S608 — literal

    with caplog.at_level(logging.ERROR):
        posted, still_pending = reconcile_module.backfill_pending_funding(
            db, run_id="r", now=_T0, funding_source=_Rates(D("0.0001"))
        )

    assert (posted, still_pending) == (0, 1)
    messages = [r.getMessage() for r in caplog.records]
    assert any("fix it in the store" in m for m in messages)
    assert not any("no lane claimed this one" in m for m in messages)
    db.close()


def test_backfill_contains_a_failure_no_lane_claims(tmp_path, caplog, monkeypatch):
    """"Never allowed to abort" must not depend on the handler lists.

    Those lists are exactly what issue #191 got through — an ``OverflowError``
    no lane named. A ``TypeError`` out of ``record_funding`` is the same shape
    and still reaches nothing enumerated, so the guarantee is structural: an
    outer per-event handler gives the event a verdict whatever was raised, and
    says in its own words that this one is a defect to read the traceback for.
    """
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

    def drifted(*args, **kwargs):
        # Deliberately NOT a TypeError/ValueError: those are claimed by the
        # corrupt-row lane (a wrong-typed column reaches
        # Decimal/fromisoformat as one). This has to be a type no inner lane lists, which is the whole
        # category the outer lane exists for.
        raise RuntimeError("the reconciler asked accounting for something it no longer does")

    monkeypatch.setattr(accounting, "record_funding", drifted)
    with caplog.at_level(logging.ERROR):
        posted, still_pending = reconcile_module.backfill_pending_funding(
            db, run_id="r", now=_T0, funding_source=_Rates(D("0.0001"))
        )

    assert (posted, still_pending) == (0, 1)  # the pass finished; the event stays pending
    messages = [r.getMessage() for r in caplog.records]
    assert any("no lane claimed this one" in m for m in messages)
    # Not misfiled as either of the verdicts that send an operator somewhere.
    assert not any("fix it in the store" in m for m in messages)
    assert not any("the funding reader failed" in m for m in messages)
    db.close()


def test_restart_replay_raise_with_open_position_reports_failed(tmp_path, monkeypatch):
    """A replay that *raises* is unverifiable books one notch worse than a
    mismatch — same protection-only fork, under the "failed" breadcrumb lane
    (mirroring the mid-run verify in ``_post_cycle_export``)."""
    db = _init(tmp_path)
    clock, plan_id = _engine_with_plan(db, slices_filled=1)  # non-flat position

    def boom(db_, *, run_id):
        raise RuntimeError("corrupt decimal text")

    monkeypatch.setattr(accounting, "replay", boom)
    now = clock.now() + timedelta(minutes=5)
    report = reconcile_on_restart(db, run_id="r", now=now)

    assert report.replay_status == "failed"
    assert "raised" in report.replay_error
    # Steps 1-3 still ran, and a forced cycle is never claimed over books
    # that could not verify.
    assert report.canceled_plan_ids == (plan_id,)
    assert not report.forced_immediate_cycle
    db.close()


def test_restart_replay_raise_flat_fails_startup_with_failed_status(tmp_path, monkeypatch):
    # FLAT run + replay raise: nothing to protect, so refuse loudly — but under
    # the "failed" lane so the CLI's breadcrumb names the right refusal.
    db = _init(tmp_path)

    def boom(db_, *, run_id):
        raise RuntimeError("corrupt decimal text")

    monkeypatch.setattr(accounting, "replay", boom)
    with pytest.raises(ReconciliationError, match="replay raised") as excinfo:
        reconcile_on_restart(db, run_id="r", now=_T0)
    assert excinfo.value.replay_status == "failed"
    db.close()


def test_corrupt_next_decision_at_is_overwritten_not_fatal(tmp_path):
    """A corrupt stored clock must not abort the restart (that abort would skip
    the protection-only fork below it) — the step-8 force overwrite it receives
    is also the repair."""
    db = _init(tmp_path)
    clock, plan_id = _engine_with_plan(db, slices_filled=1)  # live plan -> step 8
    with db.transaction() as conn:
        repo.upsert_scheduler_state(conn, "r", next_decision_at=_T0, updated_at=_T0)
        conn.execute(
            "UPDATE scheduler_state SET next_decision_at = 'garbage' WHERE run_id = ?",
            ("r",),
        )
    now = clock.now() + timedelta(minutes=5)
    report = reconcile_on_restart(db, run_id="r", now=now)
    assert report.canceled_plan_ids == (plan_id,)
    assert report.forced_immediate_cycle
    state = repo.get_scheduler_state(db.conn, "r")
    assert parse_instant(state["next_decision_at"]) == now  # repaired in place
    db.close()


def test_backfill_contains_a_store_error(tmp_path, caplog, monkeypatch):
    """A transient store failure must take the same per-event lane as a corrupt row.

    ``record_funding`` opens its OWN transaction, so "database is locked" is reachable
    inside the loop. Escaping, it aborts the pass — and at restart that abort fires
    BEFORE the protection-only fork, leaving a live position unwatched and crash-looping
    on the same event.
    """
    import sqlite3

    from contrib.hyperliquid_perp.paper import reconcile as reconcile_mod
    from contrib.hyperliquid_perp.paper.reconcile import backfill_pending_funding

    hour = _T0.replace(minute=0)
    db = _init(tmp_path)
    accounting.record_funding(
        db,
        run_id="r",
        mode="paper",
        symbol="BTC",
        funding_timestamp=hour,
        position_size=D("0.001"),
        funding_rate=None,  # pending
        mark_price=_MARK,
    )

    def _locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(reconcile_mod.accounting, "record_funding", _locked)

    with caplog.at_level(logging.ERROR):
        posted, still_pending = backfill_pending_funding(
            db, run_id="r", now=hour, funding_source=_Rates(D("0.0001"))
        )

    assert posted == 0
    assert still_pending == 1  # left pending for the next pass, not lost
    assert "store error" in caplog.text
