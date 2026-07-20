"""Tests for the §9 live sliced-TWAP execution engine (PR 5)."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

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


def _live_config(*, max_notional="500", ceiling="1000", max_open_orders=5) -> LiveConfig:
    return LiveConfig.from_dict(
        {
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
    )


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

    def tick(self) -> None:
        self.ticks += 1


class _FakeReconciler:
    def __init__(self, clean=True) -> None:
        self.calls: list[str] = []
        self.clean = clean

    def reconcile_and_apply(self, trigger, *, safe_mode, ws_restored, kill_switch_active):
        self.calls.append(trigger)
        return SimpleNamespace(clean=self.clean)


class _FakeProtection:
    def __init__(self, outcome=ProtectionOutcome.PROTECTED) -> None:
        self.outcome = outcome
        self.calls = 0

    def sync(self, *, position, liquidation_price, mark, plan_active):
        self.calls += 1
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
        fetch_clearinghouse=lambda: {},
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
    engine.tick()  # remaining slices catch up (2 and 3), plan completes
    assert len(sub.calls) == 4
    assert engine._leg is None  # plan terminated
    assert gate.active_slice_plan is False


def test_notional_cap_rejects(tmp_path):
    db, clock, engine, gate, sub = _build(tmp_path, live=_live_config(max_notional="100"))
    _script(engine, [_snap()])
    # margin 5% of 4000 -> notional 200 > max_notional_usdc 100.
    reg = engine.start_plan(_decision("long", 5))
    assert reg.reason == "max_notional_usdc"
    assert reg.plan_id is None
    assert gate.active_slice_plan is False


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
    assert reg.plan_id is None
    assert gate.active_slice_plan is False


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
