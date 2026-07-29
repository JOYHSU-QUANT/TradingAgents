"""Tests for the §9 live sliced-TWAP execution engine (PR 5)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from contrib.hyperliquid_perp.domains.perp.margin import MarginSchedule, MarginTier
from contrib.hyperliquid_perp.domains.perp.risk_gate import RiskConfig
from contrib.hyperliquid_perp.domains.perp.target_decision import (
    DecisionConfig,
    DecisionMode,
    ParsedDecision,
    TargetDecision,
    TargetSide,
)
from contrib.hyperliquid_perp.live.config import ExecutionMode, LiveConfig
from contrib.hyperliquid_perp.live.engine import LiveExecutionEngine
from contrib.hyperliquid_perp.live.loss_guards import LossGuards
from contrib.hyperliquid_perp.live.order_gate import RealOrderGate
from contrib.hyperliquid_perp.live.protection import ProtectionOutcome
from contrib.hyperliquid_perp.live.safe_mode import SafeModeManager
from contrib.hyperliquid_perp.paper import accounting
from contrib.hyperliquid_perp.paper.clock import ManualClock
from contrib.hyperliquid_perp.paper.engine import AssetSpec
from contrib.hyperliquid_perp.paper.market_feed import ScriptedSnapshotProvider, SnapshotOutcome
from contrib.hyperliquid_perp.persistence import repository as repo
from contrib.hyperliquid_perp.persistence.db import Database
from contrib.hyperliquid_perp.persistence.models import PositionState

D = Decimal
_T0 = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)
_MARK = D(50000)


def _schedule() -> MarginSchedule:
    return MarginSchedule(coin="BTC", tiers=(MarginTier(D(0), D(50)),))


def _decision(side: str, margin: int, conf: str = "0.8") -> ParsedDecision:
    dec = TargetDecision(
        decision_mode=DecisionMode.SET_TARGET,
        target_side=TargetSide(side),
        requested_target_margin_pct=margin,
        confidence=D(conf),
        rationale="test",
        key_risks=("r",),
    )
    return ParsedDecision(decision=dec, is_valid=True, invalid_reason=None, raw_response="{}")


def _live_config(
    *, max_notional="500", ceiling="1000", max_open_orders=5, execution=None
) -> LiveConfig:
    cfg = {
        "mode": "testnet_live",
        "network": "testnet",
        "safety": {
            "allowed_symbols": ["BTC"],
            "leverage": 1,
            "max_target_margin_pct": 60,
            "max_notional_usdc": max_notional,
            "absolute_notional_ceiling": ceiling,
            "max_open_orders": max_open_orders,
        },
    }
    if execution is not None:
        cfg["execution"] = execution
    return LiveConfig.from_dict(cfg)


class _FakeSubmitter:
    def __init__(self, outcome="acknowledged") -> None:
        self.calls: list[dict] = []
        self.outcome = outcome

    def submit_ioc_limit(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(outcome=SimpleNamespace(value=self.outcome))


class _FakeKillSwitch:
    def __init__(self) -> None:
        self.ticks = 0
        self.stop_new_orders = False
        self.release_calls = 0
        self.release_result = True

    def tick(self) -> None:
        self.ticks += 1

    def release_safe_mode(self) -> bool:
        # Mirrors KillSwitchManager: success lowers the latch, a failed proving
        # refresh leaves it up and returns False.
        self.release_calls += 1
        if self.release_result:
            self.stop_new_orders = False
        return self.release_result


class _FakeReconciler:
    def __init__(self, clean=True) -> None:
        self.calls: list[str] = []
        self.kill_switch_flags: list[bool] = []
        self.clean = clean

    def reconcile_and_apply(self, trigger, *, safe_mode, ws_restored, kill_switch_active):
        self.calls.append(trigger)
        self.kill_switch_flags.append(kill_switch_active)
        return SimpleNamespace(clean=self.clean)


class _FakeProtection:
    def __init__(self, outcome=ProtectionOutcome.PROTECTED) -> None:
        self.outcome = outcome
        self.calls = 0
        # Mirrors ProtectionManager.orders_changed_last_sync (§12.2 rule 6):
        # tests flip it to assert the engine reconciles a protection-change tick.
        self.orders_changed_last_sync = False
        # What the engine handed the SL band each sync (§3.6): the reconciler's
        # mirrored exchange estimate, or None before the first mirror.
        self.liquidation_prices: list[Decimal | None] = []

    def sync(self, *, position, liquidation_price, mark, plan_active):
        self.calls += 1
        self.liquidation_prices.append(liquidation_price)
        if position.is_flat:
            return ProtectionOutcome.FLAT
        return self.outcome


class _FakeWs:
    def __init__(self, messages=None, stale=False) -> None:
        self._messages = list(messages or [])
        self._stale = stale

    def drain(self):
        m, self._messages = self._messages, []
        return m

    def is_stale(self, now=None):
        return self._stale


class _FakeFillProcessor:
    def __init__(self) -> None:
        self.messages: list = []

    def ingest_message(self, message):
        self.messages.append(message)
        return [SimpleNamespace(outcome=SimpleNamespace(value="applied"))]


def _build(
    tmp_path,
    *,
    equity="4000",
    live=None,
    protection=None,
    submitter=None,
    reconciler=None,
    open_orders=None,
    ws=None,
    seed=(),
):
    db = Database(tmp_path / "live.db")
    accounting.initialize_run(
        db,
        run_id="r",
        mode="live",
        initial_balance_usdc=D(equity),
        schema_version=7,
        initial_positions=list(seed),
    )
    clock = ManualClock(_T0)
    live_cfg = live or _live_config()
    gate = RealOrderGate(
        allow_real_orders=True,
        mode=ExecutionMode.TESTNET_LIVE,
        allowed_symbols=("BTC",),
        agent_authorized=True,
        startup_reconciliation_passed=True,
        kill_switch_active=True,
        state_reconciled=True,
    )
    safe_mode = SafeModeManager(db=db, run_id="r", gate=gate, clock=clock)
    loss_guards = LossGuards(db=db, run_id="r", safety=live_cfg.safety, safe_mode=safe_mode)
    fake_sub = submitter or _FakeSubmitter()
    engine = LiveExecutionEngine(
        db=db,
        run_id="r",
        asset=AssetSpec(coin="BTC", sz_decimals=3, margin_schedule=_schedule()),
        live_config=live_cfg,
        risk_config=RiskConfig(leverage=D(1), max_target_margin_pct=60),
        decision_config=DecisionConfig(),
        provider=ScriptedSnapshotProvider("BTC", []),
        submitter=fake_sub,
        gate=gate,
        kill_switch=_FakeKillSwitch(),
        safe_mode=safe_mode,
        reconciler=reconciler or _FakeReconciler(),
        protection=protection or _FakeProtection(),
        loss_guards=loss_guards,
        fill_processor=_FakeFillProcessor(),
        ws_stream=ws or _FakeWs(),
        fetch_open_orders=lambda: open_orders if open_orders is not None else [],
        clock=clock,
    )
    return db, clock, engine, gate, fake_sub


def _script(engine, snaps):
    engine._provider = ScriptedSnapshotProvider("BTC", snaps)


def _snap(mark=_MARK, mid=_MARK):
    return (D(mark), D(mid))


# -- start_plan / slice scheduling ------------------------------------------


def test_entry_plan_builds_and_schedules_slices(tmp_path):
    db, clock, engine, gate, sub = _build(tmp_path)
    _script(engine, [_snap()])  # start_plan fetches one snapshot
    reg = engine.start_plan(_decision("long", 5), output_id="o1")
    # equity 4000, margin 5%, lev 1 -> notional 200 -> 0.004 BTC -> 4 slices
    assert reg.plan_id is not None
    assert reg.reason is None
    assert gate.active_slice_plan is True
    plan = repo.get_execution_plan(db.conn, reg.plan_id)
    assert plan["status"] == "active"
    assert plan["planned_slices"] == 4

    _script(engine, [_snap(), _snap(), _snap(), _snap(), _snap()])
    engine.tick()  # t0: slice 0
    assert len(sub.calls) == 1
    assert sub.calls[0]["order_role"] == "entry"
    assert sub.calls[0]["side"] == "buy"
    assert sub.calls[0]["limit_price"] > _MARK  # buy limit is mid*(1+slip)
    clock.advance(30)
    engine.tick()  # t30: slice 1
    assert len(sub.calls) == 2
    clock.advance(90)  # jump past several intervals
    engine.tick()  # catch-up is one-per-tick: slice 2 only
    assert len(sub.calls) == 3
    engine.tick()  # next tick sends slice 3, plan completes
    assert len(sub.calls) == 4
    assert engine._leg is None  # plan terminated
    assert gate.active_slice_plan is False


def test_notional_cap_rejects(tmp_path):
    db, clock, engine, gate, sub = _build(tmp_path, live=_live_config(max_notional="100"))
    _script(engine, [_snap()])
    # margin 5% of 4000 -> notional 200 > max_notional_usdc 100.
    reg = engine.start_plan(_decision("long", 5), output_id="o1")
    assert reg.reason == "max_notional_usdc"
    assert gate.active_slice_plan is False
    # 2026-07-22: a live check refusing a gate-APPROVED target writes ONE
    # rejected execution_plans row — the ai_outputs row reads order_created=1,
    # so without this trace the refusal is invisible everywhere.
    assert reg.plan_id is not None
    plan = repo.get_execution_plan(db.conn, reg.plan_id)
    assert plan["status"] == "rejected"
    assert plan["status_reason"] == "max_notional_usdc"
    assert plan["planned_slices"] == 0
    assert plan["output_id"] == "o1"
    assert plan["total_qty"] is None  # no plan was ever built to quantify


def test_max_open_orders_rejects_and_reconciles(tmp_path):
    recon = _FakeReconciler()
    db, clock, engine, gate, sub = _build(
        tmp_path,
        live=_live_config(max_open_orders=1),
        reconciler=recon,
        open_orders=[{"cloid": "0xdead"}],
    )
    with db.transaction() as conn:
        repo.insert_cloid_mapping(
            conn,
            cloid_logical="hta_r_BTC_o_p_na_000_stop_loss",
            cloid_hex="0xdead",
            run_id="r",
            symbol="BTC",
            order_role="stop_loss",
            created_at=_T0,
        )
    _script(engine, [_snap()])
    reg = engine.start_plan(_decision("long", 5))
    assert reg.reason == "max_open_orders"
    assert "mismatch" in recon.calls  # §10.5 triggers a reconciliation
    # The refusal leaves its one rejected execution_plans row (2026-07-22).
    assert reg.plan_id is not None
    plan = repo.get_execution_plan(db.conn, reg.plan_id)
    assert plan["status"] == "rejected"
    assert plan["status_reason"] == "max_open_orders"


def test_non_list_open_orders_is_fail_closed_at_cap(tmp_path):
    """A malformed (non-list) open-orders response cannot express a trustworthy
    count — count_bot_open_orders would read it as 0 (fail-OPEN). It must be treated
    as at-cap (fail-closed), exactly like a read failure, and must NOT reconcile
    (its own read just failed, unlike a genuine over-cap breach)."""
    recon = _FakeReconciler()
    db, clock, engine, gate, sub = _build(
        tmp_path, reconciler=recon, open_orders={"unexpected": "envelope"}
    )
    _script(engine, [_snap()])
    reg = engine.start_plan(_decision("long", 5))
    assert reg.reason == "max_open_orders"  # refused past the §10.5 cap
    assert gate.active_slice_plan is False
    assert "mismatch" not in recon.calls  # a read failure does not reconcile


def test_terminated_plan_records_unknown_residual(tmp_path):
    """v1 does not attribute live fills to their plan, so a terminated plan's
    residual is recorded as NULL (unknown), never the frozen planned-total, which
    would falsely claim the whole plan went unfilled. Real per-plan fill tracking
    (and an authoritative residual) lands with PR 6."""
    db, clock, engine, gate, sub = _build(tmp_path)
    _script(engine, [_snap()])
    reg = engine.start_plan(_decision("long", 5), output_id="o1")  # 4 slices
    _script(engine, [_snap()] * 6)
    engine.tick()  # slice 0
    clock.advance(30)
    engine.tick()  # slice 1
    clock.advance(120)  # all remaining slices due; catch-up is one per tick
    engine.tick()  # slice 2
    engine.tick()  # slice 3 -> plan completes
    plan = repo.get_execution_plan(db.conn, reg.plan_id)
    assert plan["status"] == "completed"
    assert plan["residual_qty"] is None  # unknown in v1, not the planned total


# -- emergency close --------------------------------------------------------


def test_emergency_close_on_needs_close(tmp_path):
    prot = _FakeProtection(outcome=ProtectionOutcome.NEEDS_EMERGENCY_CLOSE)
    db, clock, engine, gate, sub = _build(tmp_path, protection=prot)
    with db.transaction() as conn:
        repo.upsert_current_position(
            conn,
            "r",
            PositionState(coin="BTC", size=D("0.05"), entry_price=D(50000)),
            updated_at=_T0,
        )
    engine._was_flat = False
    _script(engine, [_snap()])
    engine.tick()
    close = [c for c in sub.calls if c["order_role"] == "emergency_close"]
    assert len(close) == 1
    assert close[0]["side"] == "sell"  # closing a long
    assert close[0]["reduce_only"] is True
    assert close[0]["size"] == D("0.05")


# -- protection sync inputs -------------------------------------------------


def test_protection_sync_is_fed_the_mirrored_liquidation_price(tmp_path):
    prot = _FakeProtection()
    db, clock, engine, gate, sub = _build(tmp_path, protection=prot)
    with db.transaction() as conn:
        repo.upsert_current_position(
            conn,
            "r",
            PositionState(coin="BTC", size=D("0.05"), entry_price=D(50000)),
            updated_at=_T0,
        )
    engine._was_flat = False
    _script(engine, [_snap(), _snap()])
    engine.tick()
    # Nothing mirrored yet (a fresh position before its first reconcile pass):
    # None must reach sync so the SL band falls back to the entry-based one.
    assert prot.liquidation_prices == [None]

    # The reconciler's mirror lands on the row...
    with db.transaction() as conn:
        repo.set_position_liquidation_price(conn, "r", "BTC", D("43210.5"))
    clock.advance(30)
    engine.tick()
    # ...and the very next sync bands the SL off it. The probe sits far from the
    # mark and from any entry-derived fallback, so an engine that dropped the
    # exchange value could not reproduce it by accident.
    assert prot.liquidation_prices == [None, D("43210.5")]


# -- settlement detection ---------------------------------------------------


def test_settlement_recorded_on_flat_transition(tmp_path):
    db, clock, engine, gate, sub = _build(tmp_path)
    with db.transaction() as conn:
        repo.upsert_current_position(
            conn,
            "r",
            PositionState(coin="BTC", size=D("0.05"), entry_price=D(50000)),
            updated_at=_T0,
        )
    engine._was_flat = False
    # Anchor above the run's wallet (4000): the segment ends below it -> a loss.
    engine._loss_guards.ensure_settlement_anchor(D(4010), now=_T0)
    with db.transaction() as conn:
        repo.upsert_current_position(conn, "r", PositionState.flat("BTC"), updated_at=_T0)
    _script(engine, [_snap()])
    engine.tick()
    row = repo.get_scheduler_state(db.conn, "r")
    assert row["consecutive_loss_count"] == 1


# -- no market data ---------------------------------------------------------


def test_no_market_data_holds(tmp_path):
    db, clock, engine, gate, sub = _build(tmp_path)
    _script(engine, [SnapshotOutcome.TIMEOUT])
    result = engine.tick()
    assert result.status.value == "no_market_data"
    assert sub.calls == []  # no slices/protection submitted without a mark


# -- review fixes -----------------------------------------------------------


def test_emergency_close_uses_wider_aggressive_band(tmp_path):
    prot = _FakeProtection(outcome=ProtectionOutcome.NEEDS_EMERGENCY_CLOSE)
    db, clock, engine, gate, sub = _build(tmp_path, protection=prot)
    with db.transaction() as conn:
        repo.upsert_current_position(
            conn,
            "r",
            PositionState(coin="BTC", size=D("0.05"), entry_price=D(50000)),
            updated_at=_T0,
        )
    engine._was_flat = False
    _script(engine, [_snap()])
    engine.tick()
    close = next(c for c in sub.calls if c["order_role"] == "emergency_close")
    # §9.4 aggressive band (>= 3%): a sell close limit sits far below the routine
    # ±0.5% band (which would be ~49750); at 3% it's ~48500.
    assert close["limit_price"] <= D(49000)


def test_flip_abandoned_at_deadline_when_not_flat(tmp_path):
    from contrib.hyperliquid_perp.live.engine import _PendingFlip

    db, clock, engine, gate, sub = _build(tmp_path)
    # Close leg terminal (leg None) but the position never reached flat (an IOC
    # underfill left residual). At the flip deadline the pending flip must be
    # abandoned, not sit forever blocking new plans.
    with db.transaction() as conn:
        repo.upsert_current_position(
            conn,
            "r",
            PositionState(coin="BTC", size=D("0.01"), entry_price=D(50000)),
            updated_at=_T0,
        )
    engine._leg = None
    engine._flip = _PendingFlip(
        flip_plan_id="flip1",
        parsed=_decision("short", 5),
        output_id="o",
        deadline=_T0,
        open_budget=60,
    )
    gate.active_slice_plan = True
    clock.advance(1)  # now > deadline
    engine._maybe_advance_flip(None, clock.now(), [])
    assert engine._flip is None
    assert gate.active_slice_plan is False


def test_open_orders_read_failure_fails_closed(tmp_path):
    """§10.5: an open-orders read failure is fail-closed — an unknowable count must
    NOT wave a new plan through (the fabricated-sentinel version read as count 0)."""
    db, clock, engine, gate, sub = _build(tmp_path)

    def _boom():
        raise RuntimeError("open-orders read down")

    engine._fetch_open_orders = _boom
    _script(engine, [_snap()])
    reg = engine.start_plan(_decision("long", 5))
    assert reg.reason == "max_open_orders"
    assert gate.active_slice_plan is False
    # The fail-closed refusal is traced like the genuine over-cap one.
    assert reg.plan_id is not None
    plan = repo.get_execution_plan(db.conn, reg.plan_id)
    assert plan["status"] == "rejected"
    assert plan["status_reason"] == "max_open_orders"


def test_emergency_close_clears_pending_flip(tmp_path):
    """A §17.2 emergency close during a pending-flip window must drop the flip, so
    once the close reaches flat _maybe_advance_flip cannot re-open the flip's
    ORIGINAL target off a stale decision."""
    from contrib.hyperliquid_perp.live.engine import _PendingFlip

    prot = _FakeProtection(outcome=ProtectionOutcome.NEEDS_EMERGENCY_CLOSE)
    db, clock, engine, gate, sub = _build(tmp_path, protection=prot)
    with db.transaction() as conn:
        repo.upsert_current_position(
            conn,
            "r",
            PositionState(coin="BTC", size=D("0.05"), entry_price=D(50000)),
            updated_at=_T0,
        )
    engine._was_flat = False
    engine._leg = None
    engine._flip = _PendingFlip(
        flip_plan_id="flip1",
        parsed=_decision("short", 5),
        output_id="o",
        deadline=_T0.replace(hour=13),  # future: only emergency close can clear it here
        open_budget=60,
    )
    gate.active_slice_plan = True
    _script(engine, [_snap()])
    engine.tick()
    assert [c for c in sub.calls if c["order_role"] == "emergency_close"]
    assert engine._flip is None  # the pending flip was abandoned by the close
    assert gate.active_slice_plan is False


def test_emergency_close_escalates_to_manual_safe_mode_after_flat(tmp_path):
    """§13.5: a §17.2 emergency close escalates to MANUAL safe mode once the
    position reaches flat — deferred to post-flat so the gate never blocks the
    close order itself."""
    prot = _FakeProtection(outcome=ProtectionOutcome.NEEDS_EMERGENCY_CLOSE)
    db, clock, engine, gate, sub = _build(tmp_path, protection=prot)
    with db.transaction() as conn:
        repo.upsert_current_position(
            conn,
            "r",
            PositionState(coin="BTC", size=D("0.05"), entry_price=D(50000)),
            updated_at=_T0,
        )
    engine._was_flat = False
    _script(engine, [_snap()])
    engine.tick()  # emergency close fires; position not yet flat
    assert engine._safe_mode.current() is None  # not escalated until flat
    # The close fills → the position reaches flat.
    with db.transaction() as conn:
        repo.upsert_current_position(conn, "r", PositionState.flat("BTC"), updated_at=_T0)
    _script(engine, [_snap()])
    engine.tick()  # detects the flat transition → manual safe mode
    state = engine._safe_mode.current()
    assert state is not None and state.safe_mode_type == "manual"
    assert state.reason == "emergency_close"
    assert gate.manual_safe_mode is True


def test_flip_open_leg_uses_shared_budget_and_linkage(tmp_path):
    """§9.1 rule 5: a flip's open leg builds with the flip's shared slice budget
    and carries flip_plan_id / flip_leg='open' — not a fresh full-grid entry."""
    from contrib.hyperliquid_perp.live.engine import _PendingFlip

    db, clock, engine, gate, sub = _build(tmp_path)  # run starts flat
    engine._leg = None
    engine._flip = _PendingFlip(
        flip_plan_id="flip1",
        parsed=_decision("long", 5),  # 200 notional -> 4 slices at full grid
        output_id="o",
        deadline=_T0.replace(hour=13),
        open_budget=2,
    )
    gate.active_slice_plan = True
    _script(engine, [_snap()])
    engine._maybe_advance_flip(None, clock.now(), [])
    assert engine._flip is None
    assert engine._leg is not None
    assert engine._leg.flip_leg == "open"
    assert engine._leg.flip_plan_id == "flip1"
    plan = repo.get_execution_plan(db.conn, engine._leg.plan_id)
    assert plan["flip_leg"] == "open"
    assert plan["flip_plan_id"] == "flip1"
    assert plan["planned_slices"] <= 2  # capped by open_budget, not MAX_SLICES (would be 4)


def test_flip_open_leg_retries_on_transient_decline_until_it_registers(tmp_path):
    """§9.1 rule 5 (retry): a transient no-order reason at the settle tick must NOT
    abandon the flip's open leg — hold it and retry within the envelope, so a single
    market-data blip no longer silently drops the second leg."""
    from contrib.hyperliquid_perp.live.engine import _PendingFlip

    db, clock, engine, gate, sub = _build(tmp_path)  # run flat
    engine._leg = None
    engine._flip = _PendingFlip(
        flip_plan_id="flip1",
        parsed=_decision("long", 5),
        output_id="o",
        deadline=_T0.replace(hour=13),  # far future
        open_budget=2,
    )
    gate.active_slice_plan = True
    # Tick 1: no market data -> start_plan returns pending_market_data -> hold + retry.
    _script(engine, [SnapshotOutcome.TIMEOUT])
    engine._maybe_advance_flip(None, clock.now(), [])
    assert engine._flip is not None  # held, not abandoned
    assert engine._leg is None
    assert gate.active_slice_plan is True  # kept raised so no new target slips in
    # Tick 2: market data returns -> the open leg registers.
    _script(engine, [_snap()])
    engine._maybe_advance_flip(None, clock.now(), [])
    assert engine._flip is None
    assert engine._leg is not None and engine._leg.flip_leg == "open"


def test_flip_open_leg_abandoned_at_deadline_when_flat(tmp_path):
    """If the open leg still cannot be gated at the flip deadline, the pending flip
    is abandoned rather than held forever."""
    from contrib.hyperliquid_perp.live.engine import _PendingFlip

    db, clock, engine, gate, sub = _build(tmp_path)  # run flat
    engine._leg = None
    engine._flip = _PendingFlip(
        flip_plan_id="flip1",
        parsed=_decision("long", 5),
        output_id="o",
        deadline=_T0,  # already at the deadline
        open_budget=2,
    )
    gate.active_slice_plan = True
    clock.advance(1)  # now > deadline
    engine._maybe_advance_flip(None, clock.now(), [])
    assert engine._flip is None
    assert gate.active_slice_plan is False


def test_emergency_close_records_triggered_event(tmp_path):
    """§17 audit: the §17.2 emergency close leaves an emergency_close_triggered
    protection event (recorded whether or not the submit landed)."""
    prot = _FakeProtection(outcome=ProtectionOutcome.NEEDS_EMERGENCY_CLOSE)
    db, clock, engine, gate, sub = _build(tmp_path, protection=prot)
    with db.transaction() as conn:
        repo.upsert_current_position(
            conn,
            "r",
            PositionState(coin="BTC", size=D("0.05"), entry_price=D(50000)),
            updated_at=_T0,
        )
    engine._was_flat = False
    _script(engine, [_snap()])
    engine.tick()
    events = [e["event_type"] for e in repo.iter_protection_order_events(db.conn, "r")]
    assert "emergency_close_triggered" in events


def test_emergency_close_sets_escalation_flag_even_if_audit_write_fails(tmp_path, monkeypatch):
    """The §13.5 escalation flag must survive a failure of the (non-critical)
    emergency_close_triggered audit write — the flag is set first, the write is
    best-effort."""
    prot = _FakeProtection(outcome=ProtectionOutcome.NEEDS_EMERGENCY_CLOSE)
    db, clock, engine, gate, sub = _build(tmp_path, protection=prot)
    with db.transaction() as conn:
        repo.upsert_current_position(
            conn,
            "r",
            PositionState(coin="BTC", size=D("0.05"), entry_price=D(50000)),
            updated_at=_T0,
        )
    engine._was_flat = False

    def _boom(*a, **k):
        raise RuntimeError("audit write down")

    monkeypatch.setattr(repo, "insert_protection_order_event", _boom)
    _script(engine, [_snap()])
    engine.tick()
    assert engine._emergency_close_pending is True  # escalation survives the audit miss


def test_flip_open_leg_abandoned_on_structural_decline(tmp_path, monkeypatch):
    """A structural open-leg decline (no_legal_slice / zero_delta) won't change
    within the envelope, so the flip is abandoned at once — not retried every tick
    (which would re-insert a rejected plan row until the deadline)."""
    from contrib.hyperliquid_perp.live.engine import PlanRegistration, _PendingFlip

    db, clock, engine, gate, sub = _build(tmp_path)  # flat
    engine._leg = None
    engine._flip = _PendingFlip(
        flip_plan_id="flip1",
        parsed=_decision("long", 5),
        output_id="o",
        deadline=_T0.replace(hour=13),
        open_budget=2,
    )
    gate.active_slice_plan = True
    monkeypatch.setattr(
        engine,
        "start_plan",
        lambda *a, **k: PlanRegistration(
            gate=None, plan_id=None, disposition=None, reason="no_legal_slice"
        ),
    )
    engine._maybe_advance_flip(None, clock.now(), [])
    assert engine._flip is None  # abandoned, not retried
    assert gate.active_slice_plan is False


# -- round-3 review fixes ----------------------------------------------------


def test_emergency_close_prices_off_mid_not_mark(tmp_path):
    """The §17.2 emergency IOC goes to the BOOK, so its band is computed off MID
    (market_feed contract: triggers watch mark, fills reference mid) — a lagging
    mark in exactly the violent move that fires this can miss the book entirely
    and leave the IOC cancelled unfilled."""
    from contrib.hyperliquid_perp.paper.stops import round_to_tick

    prot = _FakeProtection(outcome=ProtectionOutcome.NEEDS_EMERGENCY_CLOSE)
    db, clock, engine, gate, sub = _build(tmp_path, protection=prot)
    with db.transaction() as conn:
        repo.upsert_current_position(
            conn,
            "r",
            PositionState(coin="BTC", size=D("0.05"), entry_price=D(100_000)),
            updated_at=_T0,
        )
    engine._was_flat = False
    _script(engine, [_snap(mark=100_000, mid=99_000)])
    engine.tick()
    close = next(c for c in sub.calls if c["order_role"] == "emergency_close")
    # SELL close: mid × (1 − 3%) tick-rounded = 96030 — NOT mark-based 97000.
    expected = round_to_tick(D(99_000) * (1 - D("0.03")), engine._asset.tick_size, up=False)
    assert close["limit_price"] == expected
    assert close["limit_price"] < D(97_000)


def test_fill_ingest_failure_propagates_out_of_tick(tmp_path):
    """_drain_ws must NOT swallow ingest failures: an infra/DB error while booking
    an already-drained fill propagates to the loop's tick guard — swallowing it
    would drop the fill with no case row, no retry flag and no operator signal."""
    db, clock, engine, gate, sub = _build(tmp_path, ws=_FakeWs(messages=[{"raw": 1}]))

    class _BoomProcessor:
        def ingest_message(self, message):
            raise RuntimeError("ingest infra down")

    engine._fill_processor = _BoomProcessor()
    _script(engine, [_snap()])
    with pytest.raises(RuntimeError, match="ingest infra down"):
        engine.tick()


def test_gate_closed_slice_pauses_and_resumes_when_gate_reopens(tmp_path):
    """A closed wire gate pauses the plan: the slice is never sent, so §9.2 rule 2
    does not apply — the cursor holds and the plan resumes from the SAME slice
    once the gate reopens (no slice skipped, none double-sent)."""
    db, clock, engine, gate, sub = _build(tmp_path)
    _script(engine, [_snap()])
    reg = engine.start_plan(_decision("long", 5), output_id="o1")  # 4 slices
    gate.kill_switch_active = False  # close the wire gate
    _script(engine, [_snap()] * 6)
    res = engine.tick()
    assert sub.calls == []  # nothing sent
    assert engine._leg.submitted == 0  # cursor held
    assert any(e.startswith(f"slices_paused:{reg.plan_id}:") for e in res.events)
    clock.advance(30)
    engine.tick()
    assert sub.calls == []  # still paused, cursor still held
    gate.kill_switch_active = True  # the gate reopens
    clock.advance(90)  # every slice is due by now
    for _ in range(4):
        engine.tick()  # one per tick, starting from slice 0
    assert len(sub.calls) == 4
    assert len({c["cloid_logical"] for c in sub.calls}) == 4  # each slice sent once
    plan = repo.get_execution_plan(db.conn, reg.plan_id)
    assert plan["status"] == "completed"


def test_gate_closed_plan_expires_at_deadline_not_completed(tmp_path):
    """A plan the gate never admits still terminates honestly at its deadline as
    ``expired`` — never ``completed`` with zero slices on the wire."""
    db, clock, engine, gate, sub = _build(tmp_path)
    _script(engine, [_snap()])
    reg = engine.start_plan(_decision("long", 5), output_id="o1")
    gate.kill_switch_active = False
    _script(engine, [_snap()] * 2)
    engine.tick()  # paused
    clock.advance(3601)  # past the 1-hour plan lifetime
    engine.tick()  # still paused -> the deadline expires the plan
    assert sub.calls == []
    plan = repo.get_execution_plan(db.conn, reg.plan_id)
    assert plan["status"] == "expired"
    assert engine._leg is None
    assert gate.active_slice_plan is False


def test_open_gate_plan_expires_at_deadline_without_sending_final_slice(tmp_path):
    """The open-gate sibling (r13 fix): the deadline is a HARD envelope even
    with the wire gate OPEN and an unsent due slice — the first tick at/past
    the deadline expires the plan rather than firing one last stale slice
    (which, after an outage spanning the deadline, could be half the position
    at a decision older than the whole plan window)."""
    db, clock, engine, gate, sub = _build(tmp_path)
    _script(engine, [_snap()])
    reg = engine.start_plan(_decision("long", 5), output_id="o1")  # 4 slices
    _script(engine, [_snap()] * 2)
    clock.advance(3600)  # exactly the 1-hour deadline; every slice due, gate OPEN
    res = engine.tick()
    assert sub.calls == []  # nothing sent at/past the deadline
    assert res.slices_submitted == 0
    plan = repo.get_execution_plan(db.conn, reg.plan_id)
    assert plan["status"] == "expired"
    assert engine._leg is None
    assert gate.active_slice_plan is False


def test_exchange_rejected_slice_advances_and_is_never_resent(tmp_path):
    """§9.2 rule 2: an exchange-rejected slice reached the wire — the cursor
    advances past it and that slice is never re-sent."""
    db, clock, engine, gate, sub = _build(tmp_path, submitter=_FakeSubmitter(outcome="rejected"))
    _script(engine, [_snap()])
    reg = engine.start_plan(_decision("long", 5), output_id="o1")  # 4 slices
    _script(engine, [_snap()] * 4)
    res = engine.tick()
    assert len(sub.calls) == 1
    assert res.slices_submitted == 0  # a reject is not an ack
    assert engine._leg.submitted == 1  # but the cursor advanced
    clock.advance(120)
    for _ in range(3):
        engine.tick()
    assert len(sub.calls) == 4  # every slice attempted exactly once
    assert len({c["cloid_logical"] for c in sub.calls}) == 4  # none re-sent
    plan = repo.get_execution_plan(db.conn, reg.plan_id)
    assert plan["status"] == "completed"


def test_raising_submitter_advances_cursor_without_retry(tmp_path):
    """An ambiguous transport failure may have reached the wire: the submitter
    recorded the attempt, so the cursor advances (v1 keeps §9.2 rule-2 simplicity
    over a §8.3 same-cloid resend) and the tick survives."""

    class _RaisingSubmitter:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        def submit_ioc_limit(self, **kw):
            self.calls.append(kw)
            raise RuntimeError("transport down")

    db, clock, engine, gate, sub = _build(tmp_path, submitter=_RaisingSubmitter())
    _script(engine, [_snap()])
    engine.start_plan(_decision("long", 5), output_id="o1")  # 4 slices
    _script(engine, [_snap()] * 2)
    res = engine.tick()
    assert len(sub.calls) == 1
    assert res.slices_submitted == 0
    assert engine._leg.submitted == 1  # advanced — no retry of slice 0
    clock.advance(30)
    engine.tick()
    assert len(sub.calls) == 2
    assert engine._leg.submitted == 2
    assert len({c["cloid_logical"] for c in sub.calls}) == 2  # slice 0 never re-sent


def test_catch_up_submits_at_most_one_slice_per_tick(tmp_path):
    """A stalled plan catches up at ONE slice per tick — never a burst of the
    whole backlog at a single price."""
    db, clock, engine, gate, sub = _build(tmp_path)
    _script(engine, [_snap()])
    engine.start_plan(_decision("long", 5), output_id="o1")  # 4 slices
    clock.advance(90)  # slices 0..3 all due at once
    _script(engine, [_snap()] * 2)
    engine.tick()
    assert len(sub.calls) == 1  # one attempt this tick, not four
    engine.tick()
    assert len(sub.calls) == 2


def test_emergency_close_pending_cleared_when_protection_recovers(tmp_path):
    """§13.5 staleness guard: an emergency close that never reached flat is over
    once protection visibly recovers (PROTECTED) — a LATER flat is a normal exit,
    not the close landing, so no manual escalation fires."""
    prot = _FakeProtection(outcome=ProtectionOutcome.NEEDS_EMERGENCY_CLOSE)
    db, clock, engine, gate, sub = _build(tmp_path, protection=prot)
    with db.transaction() as conn:
        repo.upsert_current_position(
            conn,
            "r",
            PositionState(coin="BTC", size=D("0.05"), entry_price=D(50000)),
            updated_at=_T0,
        )
    engine._was_flat = False
    _script(engine, [_snap()])
    engine.tick()  # the close fires; the position stays open (IOC missed)
    assert engine._emergency_close_pending is True
    prot.outcome = ProtectionOutcome.PROTECTED  # an SL rests again — episode over
    _script(engine, [_snap()])
    res = engine.tick()
    assert engine._emergency_close_pending is False
    assert "emergency_close_pending_cleared" in res.events
    # A later (normal) flat must NOT escalate to manual safe mode.
    with db.transaction() as conn:
        repo.upsert_current_position(conn, "r", PositionState.flat("BTC"), updated_at=_T0)
    _script(engine, [_snap()])
    engine.tick()
    assert engine._safe_mode.current() is None  # no emergency_close manual entry


def test_emergency_close_pending_survives_blocked_and_still_escalates(tmp_path):
    """BLOCKED does NOT clear the §13.5 latch (no SL rests — the episode is only
    suspended behind the wire gate), so a flat that then arrives still escalates
    to manual safe mode (regression guard)."""
    prot = _FakeProtection(outcome=ProtectionOutcome.NEEDS_EMERGENCY_CLOSE)
    db, clock, engine, gate, sub = _build(tmp_path, protection=prot)
    with db.transaction() as conn:
        repo.upsert_current_position(
            conn,
            "r",
            PositionState(coin="BTC", size=D("0.05"), entry_price=D(50000)),
            updated_at=_T0,
        )
    engine._was_flat = False
    _script(engine, [_snap()])
    engine.tick()  # the close fires
    assert engine._emergency_close_pending is True
    prot.outcome = ProtectionOutcome.BLOCKED
    _script(engine, [_snap()])
    res = engine.tick()
    assert engine._emergency_close_pending is True  # BLOCKED keeps the latch
    assert "emergency_close_pending_cleared" not in res.events
    with db.transaction() as conn:
        repo.upsert_current_position(conn, "r", PositionState.flat("BTC"), updated_at=_T0)
    _script(engine, [_snap()])
    engine.tick()  # the close reached flat with the latch intact -> manual
    state = engine._safe_mode.current()
    assert state is not None and state.safe_mode_type == "manual"
    assert state.reason == "emergency_close"


# -- settle_offline_flat (§10.4 offline settlement) --------------------------


def test_settle_offline_flat_records_missed_settlement(tmp_path):
    """§10.4: a run that comes up FLAT with the wallet off the stored anchor lost
    (or won) a whole segment offline — settle_offline_flat scores it so the loss
    breaker cannot be blinded by a crash/restart window."""
    db, clock, engine, gate, sub = _build(tmp_path)  # flat at construction
    engine._loss_guards.ensure_settlement_anchor(D(4100), now=_T0)  # wallet is 4000
    assert engine.settle_offline_flat() is True
    row = repo.get_scheduler_state(db.conn, "r")
    assert row["consecutive_loss_count"] == 1  # 4000 < 4100 anchor -> one loss
    assert D(row["last_settlement_wallet_balance"]) == D(4000)  # re-anchored


def test_settle_offline_flat_noop_without_anchor(tmp_path):
    db, clock, engine, gate, sub = _build(tmp_path)
    assert engine.settle_offline_flat() is False  # nothing to diff against


def test_settle_offline_flat_noop_when_wallet_matches_anchor(tmp_path):
    db, clock, engine, gate, sub = _build(tmp_path)
    engine._loss_guards.ensure_settlement_anchor(D(4000), now=_T0)
    assert engine.settle_offline_flat() is False
    row = repo.get_scheduler_state(db.conn, "r")
    assert row["consecutive_loss_count"] in (None, 0)  # no segment scored


def test_settle_offline_flat_noop_when_not_flat(tmp_path):
    db, clock, engine, gate, sub = _build(tmp_path)
    with db.transaction() as conn:
        repo.upsert_current_position(
            conn,
            "r",
            PositionState(coin="BTC", size=D("0.05"), entry_price=D(50000)),
            updated_at=_T0,
        )
    engine._was_flat = False  # a live position settles in-process
    engine._loss_guards.ensure_settlement_anchor(D(4100), now=_T0)
    assert engine.settle_offline_flat() is False


# -- flip construction through the real start_plan path ----------------------


def test_start_plan_opposite_target_starts_sequential_flip(tmp_path):
    """A gated target OPPOSITE the current position goes through _start_flip: the
    close leg registers reduce-only at full position size, and the pending flip
    carries the split budget and the shared §9.1-rule-5 envelope deadline."""
    from contrib.hyperliquid_perp.paper.twap import Side, split_flip_budget

    db, clock, engine, gate, sub = _build(tmp_path)
    with db.transaction() as conn:
        repo.upsert_current_position(
            conn,
            "r",
            PositionState(coin="BTC", size=D("0.004"), entry_price=_MARK),
            updated_at=_T0,
        )
    engine._was_flat = False
    _script(engine, [_snap()])
    reg = engine.start_plan(_decision("short", 5), output_id="o1")
    assert reg.plan_id is not None and reg.reason is None
    leg = engine._leg
    assert leg is not None
    assert leg.flip_leg == "close"
    assert leg.reduce_only is True
    assert leg.side is Side.SELL  # closing a long
    assert sum(leg.slice_sizes) == D("0.004")  # sized to abs(position.size)
    plan = repo.get_execution_plan(db.conn, reg.plan_id)
    assert plan["flip_leg"] == "close"
    assert plan["flip_plan_id"] == leg.flip_plan_id
    flip = engine._flip
    assert flip is not None and flip.flip_plan_id == leg.flip_plan_id
    # open_budget is split_flip_budget's open share over (close_qty, open_qty):
    # both legs are 0.004 BTC here, so the 120 budget splits 60/60.
    assert flip.open_budget == split_flip_budget(D("0.004"), D("0.004"))[1]
    assert flip.deadline == _T0 + timedelta(hours=1)
    assert gate.active_slice_plan is True


def test_flip_close_leg_db_failure_leaves_no_phantom_pending_flip(tmp_path, monkeypatch):
    """r13 fix: _start_flip assigns self._flip only AFTER _register_leg returns
    with reason None. A DB raise inside the close leg's registration must not
    strand a phantom in-memory flip with no plan row for
    _warn_orphan_flip_close_leg to find — silently supersede-able by the next
    decision."""
    db, clock, engine, gate, sub = _build(tmp_path)
    with db.transaction() as conn:
        repo.upsert_current_position(
            conn,
            "r",
            PositionState(coin="BTC", size=D("0.004"), entry_price=_MARK),
            updated_at=_T0,
        )
    engine._was_flat = False
    _script(engine, [_snap()])

    def _boom(*a, **k):
        raise RuntimeError("plans store down")

    monkeypatch.setattr(repo, "insert_execution_plan", _boom)
    with pytest.raises(RuntimeError, match="plans store down"):
        engine.start_plan(_decision("short", 5), output_id="o1")  # opposite -> flip
    assert engine._flip is None  # no phantom pending flip
    assert engine._leg is None
    assert gate.active_slice_plan is False
    # Subsequent ticks have no flip to advance.
    _script(engine, [_snap()])
    res = engine.tick()
    assert not [e for e in res.events if e.startswith("flip")]
    assert sub.calls == []


def test_flip_open_leg_regate_failure_restores_gate_and_retains_flip(tmp_path):
    """r13 fix: the open-leg re-gate's start_plan is wrapped — on ANY raise the
    envelope guard (gate.active_slice_plan) is restored to True and the pending
    flip is retained for retry. Left False, a fresh AI target could slip in
    mid-flip while the flip retries — the exact hole the flag exists to close."""
    from contrib.hyperliquid_perp.live.engine import _PendingFlip

    db, clock, engine, gate, sub = _build(tmp_path)  # run flat
    engine._leg = None
    engine._flip = _PendingFlip(
        flip_plan_id="flip1",
        parsed=_decision("long", 5),
        output_id="o",
        deadline=_T0.replace(hour=13),  # far future
        open_budget=2,
    )
    gate.active_slice_plan = True

    class _BoomProvider:
        def fetch(self, coin, *, requested_at, timeout_seconds):
            raise RuntimeError("feed transport down")

    engine._provider = _BoomProvider()
    with pytest.raises(RuntimeError, match="feed transport down"):
        engine._maybe_advance_flip(None, clock.now(), [])
    assert engine._flip is not None  # retained for retry
    assert gate.active_slice_plan is True  # the envelope guard was restored
    assert engine._leg is None
    # A later healthy tick opens the leg — the flip survived the raise.
    _script(engine, [_snap()])
    engine._maybe_advance_flip(None, clock.now(), [])
    assert engine._flip is None
    assert engine._leg is not None and engine._leg.flip_leg == "open"


def test_flip_open_transient_refusal_writes_no_rejected_rows(tmp_path):
    """The flip open-leg retry path re-gates every tick inside the envelope and
    already surfaces per tick as flip_open_pending events — _record_live_refusal
    skips the rejected row there, or the table would gain one row per tick (the
    r5 no_legal_slice lesson)."""
    from contrib.hyperliquid_perp.live.engine import _PendingFlip

    db, clock, engine, gate, sub = _build(tmp_path)  # run flat
    engine._leg = None
    engine._flip = _PendingFlip(
        flip_plan_id="flip1",
        parsed=_decision("long", 5),
        output_id="o",
        deadline=_T0.replace(hour=13),
        open_budget=2,
    )
    gate.active_slice_plan = True

    def _boom():
        raise RuntimeError("open-orders read down")

    engine._fetch_open_orders = _boom  # §10.5 fail-closed -> max_open_orders refusal
    events: list[str] = []
    _script(engine, [_snap()])
    engine._maybe_advance_flip(None, clock.now(), events)
    _script(engine, [_snap()])
    engine._maybe_advance_flip(None, clock.now(), events)
    assert engine._flip is not None  # held for retry, not abandoned
    assert len([e for e in events if e.startswith("flip_open_pending:")]) == 2  # visible per tick
    assert repo.iter_execution_plans(db.conn, "r") == []  # NO per-tick rejected-row spam
    # The read heals -> the open leg registers with its one ACTIVE row.
    engine._fetch_open_orders = lambda: []
    _script(engine, [_snap()])
    engine._maybe_advance_flip(None, clock.now(), [])
    assert engine._flip is None
    assert engine._leg is not None and engine._leg.flip_leg == "open"
    assert [p["status"] for p in repo.iter_execution_plans(db.conn, "r")] == ["active"]


# -- orphan-flip restart detection + BLOCKED tick visibility (exit round) -----


def _insert_plan(db, plan_id, *, flip_plan_id=None, flip_leg=None):
    with db.transaction() as conn:
        repo.insert_execution_plan(
            conn,
            plan_id=plan_id,
            run_id="r",
            symbol="BTC",
            status="expired",
            created_at=_T0,
            flip_plan_id=flip_plan_id,
            flip_leg=flip_leg,
            planned_slices=1,
            total_qty=D("0.004"),
            remaining_qty=D("0.004"),
            residual_qty=D(0),
            rounding_residual_qty=D(0),
            status_reason="restart_abandoned",
        )


def test_orphan_flip_close_leg_warns_at_startup(tmp_path, caplog):
    """v1 gap (fix in PR 6): a restart between a flip's two legs drops the
    pending open leg — the drop must be VISIBLE, not silent."""
    db, clock, engine, gate, sub = _build(tmp_path)
    _insert_plan(db, "p-close", flip_plan_id="f1", flip_leg="close")
    with caplog.at_level("WARNING"):
        engine._warn_orphan_flip_close_leg()
    assert "f1" in caplog.text and "open leg" in caplog.text


def test_no_orphan_warning_when_open_leg_registered(tmp_path, caplog):
    db, clock, engine, gate, sub = _build(tmp_path)
    _insert_plan(db, "p-close", flip_plan_id="f1", flip_leg="close")
    _insert_plan(db, "p-open", flip_plan_id="f1", flip_leg="open")
    with caplog.at_level("WARNING"):
        engine._warn_orphan_flip_close_leg()
    assert "open leg" not in caplog.text


def test_no_orphan_warning_when_a_later_plan_superseded_the_flip(tmp_path, caplog):
    db, clock, engine, gate, sub = _build(tmp_path)
    _insert_plan(db, "p-close", flip_plan_id="f1", flip_leg="close")
    _insert_plan(db, "p-later")  # a later decision re-planned — silent is correct
    with caplog.at_level("WARNING"):
        engine._warn_orphan_flip_close_leg()
    assert "open leg" not in caplog.text


def test_blocked_protection_tick_emits_visible_event(tmp_path):
    """A BLOCKED tick (no SL resting, wire gate closed) must count as a tick
    that DID something — the event drives the CLI's per-tick operator log."""
    prot = _FakeProtection(outcome=ProtectionOutcome.BLOCKED)
    db, clock, engine, gate, sub = _build(tmp_path, protection=prot)
    with db.transaction() as conn:
        repo.upsert_current_position(
            conn,
            "r",
            PositionState(coin="BTC", size=D("0.05"), entry_price=D(50000)),
            updated_at=_T0,
        )
    engine._was_flat = False
    _script(engine, [_snap()])
    res = engine.tick()
    assert "protection_blocked" in res.events


# -- R4 loop: no-market-data visibility / escalation --------------------------


def test_no_market_data_streak_emits_events_and_escalates(tmp_path):
    from contrib.hyperliquid_perp.live.engine import _NO_MARKET_DATA_SAFE_MODE_TICKS
    from contrib.hyperliquid_perp.live.safe_mode import REASON_NO_MARKET_DATA

    db, clock, engine, gate, sub = _build(tmp_path)
    _script(engine, [SnapshotOutcome.TIMEOUT] * _NO_MARKET_DATA_SAFE_MODE_TICKS)
    for _ in range(_NO_MARKET_DATA_SAFE_MODE_TICKS - 1):
        result = engine.tick()
        assert "no_market_data" in result.events  # visible on every miss
        clock.advance(10)
    assert engine._safe_mode.current() is None  # below the threshold: no escalation
    result = engine.tick()  # the threshold tick
    assert "no_market_data" in result.events
    state = engine._safe_mode.current()
    assert state is not None
    assert state.reason == REASON_NO_MARKET_DATA
    assert state.safe_mode_type == "recoverable"
    # A healthy snapshot resets the streak.
    _script(engine, [_snap()])
    engine.tick()
    assert engine._no_data_streak == 0


# -- R4 loop: pre-wire slice failure holds the cursor -------------------------


def test_slice_presubmit_store_failure_holds_cursor(tmp_path):
    from contrib.hyperliquid_perp.live.orders import LiveOrderPreSubmitError

    class _PreWireFailSubmitter(_FakeSubmitter):
        def __init__(self) -> None:
            super().__init__()
            self.fail_next = 0

        def submit_ioc_limit(self, **kw):
            if self.fail_next:
                self.fail_next -= 1
                raise LiveOrderPreSubmitError("pre-wire store failure")
            return super().submit_ioc_limit(**kw)

    sub = _PreWireFailSubmitter()
    db, clock, engine, gate, _ = _build(tmp_path, submitter=sub)
    _script(engine, [_snap()])
    reg = engine.start_plan(_decision("long", 5), output_id="o1")
    assert reg.plan_id is not None
    sub.fail_next = 1
    _script(engine, [_snap(), _snap()])
    result = engine.tick()  # slice 0 fails pre-wire: nothing sent, nothing recorded
    assert sub.calls == []  # never reached the wire
    assert engine._leg.submitted == 0  # cursor HELD (§9.2 rule 2 applies to sent orders only)
    assert any(e.startswith("slice_held:") for e in result.events)  # visible
    engine.tick()  # the store healed: the SAME slice is retried
    assert len(sub.calls) == 1
    assert engine._leg.submitted == 1


# -- R4 loop: settlement double-fault must not re-score the segment -----------


def test_settlement_escalation_write_failure_never_rescores_the_segment(tmp_path, monkeypatch):
    """Sibling of the driver's fail-record double fault: record_settlement COMMITS,
    then the section 13.5 manual escalation write raises. The flat cursor must have
    advanced with the commit -- a re-run next tick would read segment_pnl=0
    (wallet == anchor) and RESET the just-recorded consecutive-loss streak --
    while the still-set pending flag retries the escalation until it lands."""
    db, clock, engine, gate, sub = _build(tmp_path)
    engine._loss_guards.ensure_settlement_anchor(D(4100), now=clock.now())
    engine._was_flat = False  # simulate: the position reached flat this tick
    engine._emergency_close_pending = True

    class _Boom(Exception):
        pass

    real_enter = engine._safe_mode.enter
    calls = {"n": 0}

    def _flaky_enter(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _Boom()
        return real_enter(*a, **kw)

    monkeypatch.setattr(engine._safe_mode, "enter", _flaky_enter)
    with pytest.raises(_Boom):
        engine._detect_settlement(clock.now())
    state = repo.get_scheduler_state(db.conn, "r")
    assert state["consecutive_loss_count"] == 1  # the -100 loss was scored ONCE
    assert engine._was_flat is True  # the cursor advanced with the commit
    assert engine._emergency_close_pending is True  # the escalation is still owed
    # Next tick: no duplicate settlement (the streak survives), escalation lands.
    engine._detect_settlement(clock.now())
    state = repo.get_scheduler_state(db.conn, "r")
    assert state["consecutive_loss_count"] == 1  # NOT reset by a wallet==anchor re-score
    assert engine._emergency_close_pending is False
    manual = engine._safe_mode.current()
    assert manual is not None and manual.safe_mode_type == "manual"


# -- R4 loop: emergency close clears the flip before the fallible write -------


def test_emergency_close_clears_flip_even_when_leg_termination_raises(tmp_path, monkeypatch):
    from types import SimpleNamespace as _NS

    from contrib.hyperliquid_perp.live.engine import _PendingFlip

    db, clock, engine, gate, sub = _build(tmp_path)
    engine._flip = _PendingFlip(
        flip_plan_id="f1",
        parsed=_decision("long", 5),
        output_id="o1",
        deadline=clock.now() + timedelta(hours=1),
        open_budget=10,
    )
    engine._leg = _NS()  # non-None so _terminate_leg is invoked

    def _boom(leg, now, status, events):
        raise RuntimeError("db down")

    monkeypatch.setattr(engine, "_terminate_leg", _boom)
    events: list[str] = []
    with pytest.raises(RuntimeError):
        engine._emergency_close(_NS(size=D("0.01")), D(50000), clock.now(), events)
    # The in-memory flip invariant was restored BEFORE the raising DB write:
    # a surviving _PendingFlip would re-open the stale flip target post-flat.
    assert engine._flip is None
    assert "flip_abandoned:f1" in events


# -- R4 loop: execution knobs wire through from config ------------------------


def test_execution_knobs_wire_through_from_config(tmp_path):
    live = _live_config(execution={"plan_duration_minutes": 10, "slice_interval_seconds": 60})
    db, clock, engine, gate, sub = _build(tmp_path, live=live)
    assert engine._plan_lifetime == timedelta(minutes=10)
    assert engine._slice_interval == 60
    assert engine._max_slices == 10  # 10min * 60s / 60s
    _script(engine, [_snap()])
    reg = engine.start_plan(_decision("long", 5), output_id="o1")
    assert reg.plan_id is not None
    _script(engine, [_snap()] * 3)
    engine.tick()  # t0: slice 0
    assert len(sub.calls) == 1
    clock.advance(30)
    engine.tick()  # 30s < the 60s interval: nothing due
    assert len(sub.calls) == 1
    clock.advance(30)
    engine.tick()  # 60s: slice 1
    assert len(sub.calls) == 2


# -- R4 loop: a protection change reconciles that tick (12.2 rule 6) ----------


def test_protection_change_triggers_reconcile_pass(tmp_path):
    rec = _FakeReconciler()
    prot = _FakeProtection()
    db, clock, engine, gate, sub = _build(tmp_path, reconciler=rec, protection=prot)
    prot.orders_changed_last_sync = True
    _script(engine, [_snap()])
    engine.tick()
    assert "protection_change" in rec.calls
    rec.calls.clear()
    prot.orders_changed_last_sync = False
    clock.advance(10)
    _script(engine, [_snap()])
    engine.tick()
    assert "protection_change" not in rec.calls  # quiet sync -> no extra pass


# -- 2026-07-23: §13.4 release door on the reconcile path ---------------------


def test_reconcile_walks_the_release_door_when_the_latch_is_up(tmp_path):
    """§13.4: with the sticky latch up, reconcile_and_apply requires
    release_safe_mode()'s verdict — the raw latch read has no release path at
    all, wedging the run (no slices, and no SL/TP either: the §4.1 kill-switch
    line is not in the protective exemption) until a process restart."""
    rec = _FakeReconciler()
    db, clock, engine, gate, sub = _build(tmp_path, reconciler=rec)
    ks = engine._kill_switch
    ks.stop_new_orders = True  # a transient refresh failure latched the switch
    engine._reconcile("heartbeat")
    assert ks.release_calls == 1  # the ONE door was walked, not bypassed
    assert rec.kill_switch_flags == [True]  # the pass saw the earned verdict
    assert ks.stop_new_orders is False


def test_reconcile_stays_latched_when_the_release_proof_fails(tmp_path):
    """A release whose proving refresh fails re-latches: the pass must see
    kill_switch_active=False (§13.4 auto-recovery withheld) and the next
    reconcile walks the door again — retry at reconcile cadence, never a
    tight loop."""
    rec = _FakeReconciler()
    db, clock, engine, gate, sub = _build(tmp_path, reconciler=rec)
    ks = engine._kill_switch
    ks.stop_new_orders = True
    ks.release_result = False  # the switch is still unhealthy
    engine._reconcile("heartbeat")
    assert rec.kill_switch_flags == [False]
    assert ks.stop_new_orders is True
    engine._reconcile("heartbeat")
    assert ks.release_calls == 2


def test_reconcile_skips_the_release_door_while_healthy(tmp_path):
    """No latch -> no release attempt: the door is §13.4 release only, never a
    routine health probe."""
    rec = _FakeReconciler()
    db, clock, engine, gate, sub = _build(tmp_path, reconciler=rec)
    engine._reconcile("heartbeat")
    assert engine._kill_switch.release_calls == 0
    assert rec.kill_switch_flags == [True]


def test_flip_under_single_slot_config_still_budgets_both_legs(tmp_path):
    """Exit-round HIGH fix: a config whose plan window holds ONE slice slot
    (duration == interval -> _max_slices == 1) must not hand the flip's open
    leg a zero budget — split_flip_budget assumes total_budget >= 2, and a 0
    open budget makes build_slice_plan raise on the open leg, crash-looping
    the tick until the flip deadline. The flip envelope floors at 2."""
    live = _live_config(execution={"plan_duration_minutes": 1, "slice_interval_seconds": 60})
    db, clock, engine, gate, sub = _build(tmp_path, live=live)
    assert engine._max_slices == 1  # the hazardous-but-legal config
    with db.transaction() as conn:
        repo.upsert_current_position(
            conn,
            "r",
            PositionState(coin="BTC", size=D("0.004"), entry_price=_MARK),
            updated_at=_T0,
        )
    engine._was_flat = False
    _script(engine, [_snap()])
    reg = engine.start_plan(_decision("short", 5), output_id="o1")  # opposite side -> flip
    assert reg.plan_id is not None and reg.reason is None
    flip = engine._flip
    assert flip is not None
    assert flip.open_budget >= 1  # the open leg can always build a plan
    # Drive the close leg to flat and advance the flip: the open leg must
    # register without raising (the pre-fix behavior was a ValueError here).
    with db.transaction() as conn:
        repo.upsert_current_position(conn, "r", PositionState.flat("BTC"), updated_at=_T0)
    engine._leg = None  # close leg terminal
    _script(engine, [_snap(), _snap()])
    engine.tick()
    assert engine._flip is None  # the open leg registered (flip consumed)
    assert engine._leg is not None and engine._leg.flip_leg == "open"


# -- loss guards wired through the engine (§10.3 / §10.4) --------------------
#
# The guards and the engine are unit-tested apart on their own fixtures; these
# two pin the LINKAGE — the engine, the guards and the gate share ONE object
# graph, and evaluate_daily_loss runs before _submit_due_slices — so a rewire
# (a different gate instance, a reordered tick) fails here even though every
# per-module test stays green.


def test_daily_loss_breach_mid_tick_pauses_pending_slice_same_tick(tmp_path):
    """§10.3 end-to-end: a drawdown past ``max_daily_loss_pct`` computed INSIDE
    tick() must pause that SAME tick's due slice — no order goes out on the
    tick that discovered the breach."""
    from contrib.hyperliquid_perp.live.safe_mode import REASON_DAILY_LOSS

    db, clock, engine, gate, sub = _build(tmp_path)
    with db.transaction() as conn:
        repo.upsert_current_position(
            conn,
            "r",
            PositionState(coin="BTC", size=D("0.004"), entry_price=_MARK),
            updated_at=_T0,
        )
    engine._was_flat = False
    _script(engine, [_snap()])
    engine.tick()  # first §10.3 evaluation of the day: baseline = equity 4000
    _script(engine, [_snap()])
    reg = engine.start_plan(_decision("long", 10), output_id="o1")  # grow the long
    assert reg.plan_id is not None and reg.reason is None
    # Crash the mark: equity 4000 + 0.004*(20000-50000) = 3880 -> 3% drawdown > 2%.
    _script(engine, [_snap(mark=20000, mid=20000)])
    res = engine.tick()
    state = engine._safe_mode.current()
    assert state is not None and state.reason == REASON_DAILY_LOSS
    assert sub.calls == []  # the due slice was NOT sent on the breach tick
    assert engine._leg.submitted == 0  # cursor held (pause, not burn)
    assert any(e.startswith(f"slices_paused:{reg.plan_id}:") for e in res.events)


def test_third_consecutive_loss_blocks_next_start_plan_same_run(tmp_path):
    """§10.4 rule 3 end-to-end: the third losing segment recorded through the
    engine's own LossGuards flips the SHARED gate to manual safe mode, and the
    next start_plan on that engine is refused — not silently admitted."""
    db, clock, engine, gate, sub = _build(tmp_path)
    guards = engine._loss_guards
    guards.ensure_settlement_anchor(D(4000), now=_T0)
    guards.record_settlement(wallet_balance=D(3990), now=_T0)  # loss 1
    guards.record_settlement(wallet_balance=D(3980), now=_T0)  # loss 2
    guards.record_settlement(wallet_balance=D(3970), now=_T0)  # loss 3 -> MANUAL
    state = engine._safe_mode.current()
    assert state is not None and state.is_manual
    assert gate.manual_safe_mode is True
    _script(engine, [_snap()])
    reg = engine.start_plan(_decision("long", 5), output_id="o1")
    assert reg.reason is not None  # the §4.1 refusal, verbatim from the gate
    assert gate.active_slice_plan is False
    assert sub.calls == []
    # The §4.1 refusal of a gate-approved target is traced too (2026-07-22).
    assert reg.plan_id is not None
    plan = repo.get_execution_plan(db.conn, reg.plan_id)
    assert plan["status"] == "rejected"
    assert plan["status_reason"] == reg.reason
    assert plan["output_id"] == "o1"
