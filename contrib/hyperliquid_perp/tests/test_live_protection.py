"""Tests for the §17 live SL/TP protection manager (PR 5)."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from contrib.hyperliquid_perp.exchanges.hyperliquid.errors import ExchangeRequestError
from contrib.hyperliquid_perp.exchanges.hyperliquid.signed_client import CancelAck, OrderAck
from contrib.hyperliquid_perp.live.config import ExecutionMode, LiveProtectionConfig
from contrib.hyperliquid_perp.live.order_gate import LiveOrderGateRejected, RealOrderGate
from contrib.hyperliquid_perp.live.protection import ProtectionManager, ProtectionOutcome
from contrib.hyperliquid_perp.paper.clock import ManualClock
from contrib.hyperliquid_perp.paper.stops import StopConfig
from contrib.hyperliquid_perp.persistence import repository as repo
from contrib.hyperliquid_perp.persistence.db import Database
from contrib.hyperliquid_perp.persistence.models import PositionState

_NOW = datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc)
_TICK = Decimal("1")
_QTY_STEP = Decimal("0.00001")


class _FakeClient:
    """A scripted trigger-order transport with a tiny in-memory oid counter."""

    def __init__(self) -> None:
        self.placed: list[dict] = []
        self.modified: list[dict] = []
        self.canceled: list[str] = []
        self._oid = 1000
        self.place_script: list[str] = []  # "ok" | "error" | "raise" | "gate", consumed per call
        self.modify_script: list[str] = []
        self.status_queries: list[str] = []
        # Scripted §8.3 orderStatus-by-cloid answers, consumed per call; the
        # default (empty) answers "unknownOid" so a raised/duplicate attempt whose
        # order did NOT land resolves to "not recovered" and the repair retries.
        self.status_script: list[dict] = []
        self.cancel_script: list[str] = []  # "ok" | "raise", consumed per cancel

    def _next_oid(self) -> str:
        self._oid += 1
        return str(self._oid)

    def _ack(self, script: list[str]) -> OrderAck:
        outcome = script.pop(0) if script else "ok"
        if outcome == "raise":
            raise ExchangeRequestError("network down")
        if outcome == "gate":
            # The bound §4.1 gate rejects INSIDE the client, before any network
            # I/O — a plain Exception, not an ExchangeError.
            raise LiveOrderGateRejected("kill_switch_active")
        if outcome == "error":
            return OrderAck(status="error", error="rejected by exchange")
        return OrderAck(status="resting", exchange_order_id=self._next_oid())

    def place_trigger_order(self, **kw) -> OrderAck:
        self.placed.append(kw)
        return self._ack(self.place_script)

    def modify_trigger_order(self, **kw) -> OrderAck:
        self.modified.append(kw)
        return self._ack(self.modify_script)

    def cancel_by_cloid(self, *, coin: str, cloid_hex: str) -> CancelAck:
        self.canceled.append(cloid_hex)
        outcome = self.cancel_script.pop(0) if self.cancel_script else "ok"
        if outcome == "raise":
            raise ExchangeRequestError("cancel down")
        if outcome == "refused":
            # A non-exceptional per-order rejection (e.g. "already filled").
            return CancelAck(success=False, error="already filled")
        return CancelAck(success=True)

    def query_order_by_cloid(self, cloid_hex: str) -> dict:
        self.status_queries.append(cloid_hex)
        if self.status_script:
            return self.status_script.pop(0)
        return {"status": "unknownOid"}


def _gate() -> RealOrderGate:
    return RealOrderGate(
        allow_real_orders=True,
        mode=ExecutionMode.TESTNET_LIVE,
        allowed_symbols=("BTC",),
        agent_authorized=True,
        startup_reconciliation_passed=True,
        kill_switch_active=True,
        state_reconciled=True,
    )


@pytest.fixture
def env():
    db = Database(":memory:")
    with db.transaction() as conn:
        repo.insert_run(
            conn,
            run_id="r",
            mode="live",
            initial_balance_usdc=Decimal(1000),
            schema_version=7,
            created_at=_NOW,
        )
    yield db
    db.close()


class _FakeKillSwitch:
    """Counts §18.2 refresh ticks (the protection repair delay refreshes it)."""

    def __init__(self) -> None:
        self.ticks = 0

    def tick(self) -> None:
        self.ticks += 1


def _manager(db, client, gate, *, sleeps=None, protection=None, kill_switch=None):
    sleep = (lambda s: sleeps.append(s)) if sleeps is not None else (lambda s: None)
    return ProtectionManager(
        db=db,
        run_id="r",
        coin="BTC",
        client=client,
        gate=gate,
        tick_size=_TICK,
        qty_step=_QTY_STEP,
        stop_config=StopConfig(),
        max_slippage_pct=Decimal("0.005"),
        protection_config=protection or LiveProtectionConfig(),
        owner_prefix="hta",
        clock=ManualClock(_NOW),
        sleep=sleep,
        kill_switch=kill_switch,
    )


def _seed_long(db, *, size=Decimal("0.1"), entry=Decimal(50000)):
    with db.transaction() as conn:
        repo.upsert_current_position(
            conn, "r", PositionState(coin="BTC", size=size, entry_price=entry), updated_at=_NOW
        )


def _long_position(size=Decimal("0.1"), entry=Decimal(50000)) -> PositionState:
    return PositionState(coin="BTC", size=size, entry_price=entry)


def _all_fail_client():
    client = _FakeClient()
    client.place_script = ["error", "error", "error"]
    return client, _gate()


# -- SL placement / modify --------------------------------------------------


def test_sl_placed_on_new_long(env):
    db = env
    _seed_long(db)
    client, gate = _FakeClient(), _gate()
    mgr = _manager(db, client, gate)
    outcome = mgr.sync(
        position=_long_position(),
        liquidation_price=Decimal(40000),
        mark=Decimal(50000),
        plan_active=True,  # TP suspended: isolate the SL placement
    )
    assert outcome is ProtectionOutcome.PROTECTED
    assert len(client.placed) == 1
    sl = client.placed[0]
    assert sl["is_buy"] is False  # a long is protected by a SELL
    assert sl["tpsl"] == "sl"
    assert sl["reduce_only"] is True
    assert sl["trigger_price"] < Decimal(50000)  # below entry
    assert sl["limit_price"] < sl["trigger_price"]  # marketable-down for a sell
    order = repo.active_protection_order(db.conn, "r", "BTC", "stop_loss")
    assert order is not None and order["exchange_order_id"] == "1001"
    reg = repo.get_cloid_by_hex(db.conn, order["cloid_hex"])
    assert reg is not None and reg["order_role"] == "stop_loss"
    protection = repo.get_position_protection(db.conn, "r", "BTC")
    assert protection is not None and protection[0] is not None  # SL price set
    events = [e["event_type"] for e in repo.iter_protection_order_events(db.conn, "r")]
    assert "stop_loss_placed" in events
    assert gate.unresolved_protection_failure is False


def test_sl_modified_when_already_resting(env):
    db = env
    _seed_long(db)
    client, gate = _FakeClient(), _gate()
    mgr = _manager(db, client, gate)
    mgr.sync(
        position=_long_position(),
        liquidation_price=Decimal(40000),
        mark=Decimal(50000),
        plan_active=True,
    )
    mgr.sync(
        position=_long_position(size=Decimal("0.2")),
        liquidation_price=Decimal(40000),
        mark=Decimal(50000),
        plan_active=True,
    )
    assert len(client.modified) == 1
    assert client.modified[0]["target"] == "1001"  # the prior SL's exchange oid
    active = repo.active_protection_order(db.conn, "r", "BTC", "stop_loss")
    assert active["exchange_order_id"] == "1002"
    assert Decimal(active["qty"]) == Decimal("0.2")
    events = [e["event_type"] for e in repo.iter_protection_order_events(db.conn, "r")]
    assert "stop_loss_modified" in events


def test_sl_noop_when_unchanged(env):
    db = env
    _seed_long(db)
    client, gate = _FakeClient(), _gate()
    mgr = _manager(db, client, gate)
    mgr.sync(
        position=_long_position(),
        liquidation_price=Decimal(40000),
        mark=Decimal(50000),
        plan_active=True,
    )
    mgr.sync(
        position=_long_position(),
        liquidation_price=Decimal(40000),
        mark=Decimal(50000),
        plan_active=True,
    )
    assert len(client.placed) == 1
    assert len(client.modified) == 0


# -- SL repair / emergency close --------------------------------------------


def test_sl_repair_retries_then_succeeds(env):
    db = env
    _seed_long(db)
    client, gate = _FakeClient(), _gate()
    client.place_script = ["error", "raise", "ok"]  # third attempt lands
    sleeps: list[float] = []
    mgr = _manager(db, client, gate, sleeps=sleeps)
    outcome = mgr.sync(
        position=_long_position(),
        liquidation_price=Decimal(40000),
        mark=Decimal(50000),
        plan_active=True,
    )
    assert outcome is ProtectionOutcome.PROTECTED
    assert len(client.placed) == 3
    assert sleeps == [5.0, 5.0]  # two retry delays between three attempts
    assert gate.unresolved_protection_failure is False


def test_sl_repair_exhausted_needs_emergency_close(env):
    db = env
    _seed_long(db)
    client, gate = _all_fail_client()
    mgr = _manager(db, client, gate)
    outcome = mgr.sync(
        position=_long_position(),
        liquidation_price=Decimal(40000),
        mark=Decimal(50000),
        plan_active=True,
    )
    assert outcome is ProtectionOutcome.NEEDS_EMERGENCY_CLOSE
    assert len(client.placed) == 3  # sl_repair_max_attempts
    assert gate.unresolved_protection_failure is True
    events = [e["event_type"] for e in repo.iter_protection_order_events(db.conn, "r")]
    assert events.count("stop_loss_repair_failed") == 3
    assert "stop_loss_repair_exhausted" in events


def test_sl_repair_recovers_landed_order_from_order_status(env):
    """§8.3: a lost ack on a SUCCESSFUL place is recovered via orderStatus, never
    re-sent — so a benign network blip cannot force a spurious emergency close of
    an already-protected position."""
    db = env
    _seed_long(db)
    client, gate = _FakeClient(), _gate()
    client.place_script = ["raise"]  # the first attempt's ack is lost
    client.status_script = [
        {"status": "order", "order": {"order": {"oid": 4242}, "status": "open"}}
    ]
    mgr = _manager(db, client, gate)
    outcome = mgr.sync(
        position=_long_position(),
        liquidation_price=Decimal(40000),
        mark=Decimal(50000),
        plan_active=True,
    )
    assert outcome is ProtectionOutcome.PROTECTED  # NOT emergency close
    assert client.status_queries  # orderStatus was consulted
    assert len(client.placed) == 1  # recovered, not re-sent
    active = repo.active_protection_order(db.conn, "r", "BTC", "stop_loss")
    assert active is not None and active["exchange_order_id"] == "4242"
    assert gate.unresolved_protection_failure is False


def test_sl_recovery_of_canceled_order_is_not_treated_as_protected(env):
    """§8.3 recovery: an orderStatus that comes back in the CANCELED family (e.g.
    reduceOnlyCanceled) is NOT a live protective order. It must not be persisted as
    open and reported protected — it counts as a failed attempt, so repair exhausts
    to emergency close. (Previously only 'rejected' was filtered, so the whole
    Canceled family slipped through as a false 'protected'.)"""
    db = env
    _seed_long(db)
    client, gate = _FakeClient(), _gate()
    client.place_script = ["raise", "raise", "raise"]  # every ack lost
    client.status_script = [
        {"status": "order", "order": {"order": {"oid": 7}, "status": "reduceOnlyCanceled"}}
        for _ in range(3)
    ]
    mgr = _manager(db, client, gate)
    outcome = mgr.sync(
        position=_long_position(),
        liquidation_price=Decimal(40000),
        mark=Decimal(50000),
        plan_active=True,
    )
    assert outcome is ProtectionOutcome.NEEDS_EMERGENCY_CLOSE
    assert repo.active_protection_order(db.conn, "r", "BTC", "stop_loss") is None
    assert gate.unresolved_protection_failure is True


def test_sl_recovery_of_filled_order_persists_filled_not_open(env):
    """A recovered SL whose orderStatus is FILLED (the trigger already fired) is
    recorded with its true 'filled' status, never a hardcoded 'open' — so
    active_protection_order cannot mistake a spent stop for live protection and let
    the no-op guard skip re-arming."""
    db = env
    _seed_long(db)
    client, gate = _FakeClient(), _gate()
    client.place_script = ["raise"]  # ack lost; the order actually filled
    client.status_script = [
        {"status": "order", "order": {"order": {"oid": 99}, "status": "filled"}}
    ]
    mgr = _manager(db, client, gate)
    outcome = mgr.sync(
        position=_long_position(),
        liquidation_price=Decimal(40000),
        mark=Decimal(50000),
        plan_active=True,
    )
    assert outcome is ProtectionOutcome.PROTECTED  # the placement landed (and fired)
    assert repo.active_protection_order(db.conn, "r", "BTC", "stop_loss") is None
    row = db.conn.execute(
        "SELECT status, remaining_qty FROM orders WHERE run_id='r' AND order_role='stop_loss'"
    ).fetchone()
    assert row["status"] == "filled"
    assert Decimal(row["remaining_qty"]) == Decimal(0)


def test_cancel_refused_by_exchange_does_not_mark_row_canceled(env):
    """A cancel the exchange REFUSES without raising (CancelAck.success=False, e.g.
    'already filled') must not mislabel the row canceled — a resting SL that actually
    filled would then be lost to reconciliation. The raw ack was previously discarded."""
    db = env
    _seed_long(db)
    client, gate = _FakeClient(), _gate()
    mgr = _manager(db, client, gate)
    # Place an SL first (a resting stop_loss row to clear later).
    mgr.sync(
        position=_long_position(),
        liquidation_price=Decimal(40000),
        mark=Decimal(50000),
        plan_active=True,
    )
    assert repo.active_protection_order(db.conn, "r", "BTC", "stop_loss") is not None
    # Position goes flat -> _clear -> _cancel_role, but the exchange refuses the cancel.
    client.cancel_script = ["refused"]
    outcome = mgr.sync(
        position=PositionState.flat("BTC"),
        liquidation_price=None,
        mark=Decimal(50000),
        plan_active=False,
    )
    assert outcome is ProtectionOutcome.FLAT
    assert client.canceled  # a cancel was attempted
    row = db.conn.execute(
        "SELECT status FROM orders WHERE run_id='r' AND order_role='stop_loss'"
    ).fetchone()
    assert row["status"] == "open"  # NOT canceled — the refusal was respected


def test_kill_switch_refreshed_across_repair_delays(env):
    """§18.2: each blocking repair delay refreshes the dead man's switch, so a
    repair episode cannot stretch the refresh cadence toward max_tick_gap."""
    db = env
    _seed_long(db)
    client, gate = _all_fail_client()  # 3 attempts → 2 delays
    ks = _FakeKillSwitch()
    mgr = _manager(db, client, gate, kill_switch=ks)
    mgr.sync(
        position=_long_position(),
        liquidation_price=Decimal(40000),
        mark=Decimal(50000),
        plan_active=True,
    )
    assert ks.ticks == 2  # one refresh per retry delay


def test_tp_cancel_failure_during_plan_degrades(env):
    """§17.1 rule 5: a TP suspend-cancel that could not land must NOT report
    PROTECTED — the stale reduce-only TP can still fire against the plan."""
    db = env
    _seed_long(db)
    client, gate = _FakeClient(), _gate()
    mgr = _manager(db, client, gate)
    # No plan yet: establishes both SL and TP.
    mgr.sync(
        position=_long_position(),
        liquidation_price=Decimal(40000),
        mark=Decimal(50000),
        plan_active=False,
    )
    assert repo.active_protection_order(db.conn, "r", "BTC", "take_profit") is not None
    # A plan starts; the TP must be suspended, but its cancel FAILS.
    client.cancel_script = ["raise"]
    outcome = mgr.sync(
        position=_long_position(),
        liquidation_price=Decimal(40000),
        mark=Decimal(50000),
        plan_active=True,
    )
    assert outcome is ProtectionOutcome.DEGRADED
    assert gate.unresolved_protection_failure is True
    # The stale TP is still resting (the cancel did not land).
    assert repo.active_protection_order(db.conn, "r", "BTC", "take_profit") is not None


def test_no_safe_sl_band_needs_emergency_close(env):
    db = env
    _seed_long(db)
    client, gate = _FakeClient(), _gate()
    mgr = _manager(db, client, gate)
    # Liquidation right at entry: no safe band → CLOSE_NOW → emergency close.
    outcome = mgr.sync(
        position=_long_position(),
        liquidation_price=Decimal(49999),
        mark=Decimal(50000),
        plan_active=True,
    )
    assert outcome is ProtectionOutcome.NEEDS_EMERGENCY_CLOSE
    assert client.placed == []  # never even attempted a placement
    assert gate.unresolved_protection_failure is True


# -- TP lifecycle -----------------------------------------------------------


def test_tp_placed_when_no_plan(env):
    db = env
    _seed_long(db)
    client, gate = _FakeClient(), _gate()
    mgr = _manager(db, client, gate)
    outcome = mgr.sync(
        position=_long_position(),
        liquidation_price=Decimal(40000),
        mark=Decimal(50000),
        plan_active=False,
    )
    assert outcome is ProtectionOutcome.PROTECTED
    tp = [p for p in client.placed if p["tpsl"] == "tp"]
    assert len(tp) == 1
    assert tp[0]["trigger_price"] > Decimal(50000)  # above entry for a long TP
    protection = repo.get_position_protection(db.conn, "r", "BTC")
    assert protection[1] is not None  # TP price set


def test_tp_suspended_during_plan_cancels_resting(env):
    db = env
    _seed_long(db)
    client, gate = _FakeClient(), _gate()
    mgr = _manager(db, client, gate)
    mgr.sync(
        position=_long_position(),
        liquidation_price=Decimal(40000),
        mark=Decimal(50000),
        plan_active=False,
    )
    tp_order = repo.active_protection_order(db.conn, "r", "BTC", "take_profit")
    assert tp_order is not None
    mgr.sync(
        position=_long_position(),
        liquidation_price=Decimal(40000),
        mark=Decimal(50000),
        plan_active=True,
    )
    assert tp_order["cloid_hex"] in client.canceled
    assert repo.active_protection_order(db.conn, "r", "BTC", "take_profit") is None
    assert repo.active_protection_order(db.conn, "r", "BTC", "stop_loss") is not None


def test_tp_failure_is_degraded_not_close(env):
    db = env
    _seed_long(db)
    client, gate = _FakeClient(), _gate()
    client.place_script = ["ok", "error", "error", "error"]  # SL lands, every TP fails
    mgr = _manager(db, client, gate)
    outcome = mgr.sync(
        position=_long_position(),
        liquidation_price=Decimal(40000),
        mark=Decimal(50000),
        plan_active=False,
    )
    assert outcome is ProtectionOutcome.DEGRADED
    assert gate.unresolved_protection_failure is True
    events = [e["event_type"] for e in repo.iter_protection_order_events(db.conn, "r")]
    assert "degraded_protection_entered" in events
    assert repo.active_protection_order(db.conn, "r", "BTC", "stop_loss") is not None


# -- clearing on flat -------------------------------------------------------


def test_flat_cancels_resting_protection(env):
    db = env
    _seed_long(db)
    client, gate = _FakeClient(), _gate()
    mgr = _manager(db, client, gate)
    mgr.sync(
        position=_long_position(),
        liquidation_price=Decimal(40000),
        mark=Decimal(50000),
        plan_active=False,
    )
    sl = repo.active_protection_order(db.conn, "r", "BTC", "stop_loss")
    tp = repo.active_protection_order(db.conn, "r", "BTC", "take_profit")
    with db.transaction() as conn:
        repo.upsert_current_position(conn, "r", PositionState.flat("BTC"), updated_at=_NOW)
    outcome = mgr.sync(
        position=PositionState.flat("BTC"),
        liquidation_price=None,
        mark=Decimal(50000),
        plan_active=False,
    )
    assert outcome is ProtectionOutcome.FLAT
    assert sl["cloid_hex"] in client.canceled
    assert tp["cloid_hex"] in client.canceled
    assert repo.get_position_protection(db.conn, "r", "BTC") == (None, None)
    assert gate.unresolved_protection_failure is False


def test_short_position_sl_is_a_buy(env):
    db = env
    short = PositionState(coin="BTC", size=Decimal("-0.1"), entry_price=Decimal(50000))
    with db.transaction() as conn:
        repo.upsert_current_position(conn, "r", short, updated_at=_NOW)
    client, gate = _FakeClient(), _gate()
    mgr = _manager(db, client, gate)
    mgr.sync(
        position=short, liquidation_price=Decimal(60000), mark=Decimal(50000), plan_active=True
    )
    sl = client.placed[0]
    assert sl["is_buy"] is True  # a short is protected by a BUY
    assert sl["trigger_price"] > Decimal(50000)  # above entry for a short SL
    assert sl["limit_price"] > sl["trigger_price"]  # marketable-up for a buy


def test_gate_rejection_does_not_crash_the_protection_pass(env):
    # A closed §4.1 gate raises LiveOrderGateRejected (a plain Exception, NOT an
    # ExchangeError) from inside place_trigger_order — it must count as a failed
    # repair attempt, never crash the tick loop out from under the position.
    db = env
    _seed_long(db)
    client, gate = _FakeClient(), _gate()
    client.place_script = ["gate", "gate", "gate"]
    outcome = _manager(db, client, gate).sync(
        position=_long_position(),
        liquidation_price=Decimal(40000),
        mark=Decimal(50000),
        plan_active=True,
    )
    assert outcome is ProtectionOutcome.NEEDS_EMERGENCY_CLOSE  # exhausted, not crashed
    assert len(client.placed) == 3
    events = [e["event_type"] for e in repo.iter_protection_order_events(db.conn, "r")]
    assert events.count("stop_loss_repair_failed") == 3
