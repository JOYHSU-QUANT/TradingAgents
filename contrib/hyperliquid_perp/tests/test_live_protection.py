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
from contrib.hyperliquid_perp.paper.stops import StopConfig, round_to_tick
from contrib.hyperliquid_perp.persistence import repository as repo
from contrib.hyperliquid_perp.persistence.db import Database
from contrib.hyperliquid_perp.persistence.models import PositionState, Side

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
        # "ok" | "error" | "raise" | "gate" | "duplicate", consumed per call.
        self.place_script: list[str] = []
        self.modify_script: list[str] = []
        self.status_queries: list[str] = []
        # Scripted §8.3 orderStatus-by-cloid answers, consumed per call; an
        # Exception entry RAISES (a failed status read). The default (empty)
        # answers "unknownOid" so a raised/duplicate attempt whose order did
        # NOT land resolves to "not recovered" and the repair retries.
        self.status_script: list[dict | Exception] = []
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
        if outcome == "duplicate":
            # §8.3 rule 2: the exchange already knows this cloid (a prior
            # attempt landed, its ack lost) — an error ack whose text flips
            # OrderAck.is_duplicate and routes query-before-resend.
            return OrderAck(status="error", error="Order already exists: duplicate cloid")
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
            result = self.status_script.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
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


def test_a_switch_that_cannot_report_firings_fails_loud(env):
    """A malformed switch must not be readable as "never fired".

    ``_note_firings`` used to ask ``getattr(self._kill_switch, "fired_total", 0)``.
    The parameter IS genuinely optional, but that default silently covered a
    second case it had no business covering: an object that exists and cannot
    answer reported zero firings, so the suspicion latch never tripped and every
    stale local row went on counting as proof that an SL/TP was resting on the
    book — the exact §17.3 check this method exists to run. Absent switch: quiet.
    Broken switch: loud (2026-08-01 round-14 concept scan).
    """
    client, gate = _FakeClient(), _gate()

    class _Mute:  # switch-shaped, minus the one attribute that matters
        pass

    with pytest.raises(AttributeError):
        _manager(env, client, gate, kill_switch=_Mute())._note_firings()
    # The honest optional case is untouched: no switch, no suspicion, no error.
    assert _manager(env, client, gate)._note_firings() is None


class _FakeKillSwitch:
    """Counts §18.2 refresh ticks (the protection repair delay refreshes it).

    ``fired_total`` mirrors the real manager's monotonic firing counter, which
    is what tells protection its local order rows were invalidated. Bumping it
    is how a test says "the scheduleCancel actually went off" — as distinct from
    "the switch is unhealthy", which cancels nothing.
    """

    def __init__(self, fired_total: int = 0) -> None:
        self.ticks = 0
        self.fired_total = fired_total

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


def test_sl_side_mismatch_is_not_treated_as_a_noop(env):
    """A resting order matching trigger price and qty by coincidence, on the WRONG
    side, must not short-circuit to ESTABLISHED — that would report PROTECTED while
    nothing on the book actually closes the current position. Models the shape a
    same-size flip leaves behind: the row's trigger/qty happen to match what the new
    side needs, but the side itself is stale."""
    db = env
    short = PositionState(coin="BTC", size=Decimal("-0.1"), entry_price=Decimal(50000))
    with db.transaction() as conn:
        repo.upsert_current_position(conn, "r", short, updated_at=_NOW)
    client, gate = _FakeClient(), _gate()
    mgr = _manager(db, client, gate)
    mgr.sync(
        position=short, liquidation_price=Decimal(60000), mark=Decimal(50000), plan_active=True
    )
    assert len(client.placed) == 1
    assert client.placed[0]["is_buy"] is True  # a short's SL is a BUY

    # Corrupt the resting row's side to the opposite value while trigger/qty stay
    # exactly what the next sync will compute — the coincidence the guard must not
    # trust on its own.
    with db.transaction() as conn:
        conn.execute(
            "UPDATE orders SET side = 'sell' WHERE run_id = 'r' AND order_role = 'stop_loss'"
        )
    mgr.sync(
        position=short, liquidation_price=Decimal(60000), mark=Decimal(50000), plan_active=True
    )
    # Must NOT no-op: a modify (existing_oid is still set) re-establishes the
    # correct side rather than silently trusting the stale row.
    assert len(client.modified) == 1
    assert client.modified[0]["is_buy"] is True


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


def test_the_repair_clamp_tracks_the_budget_it_was_derived_from():
    """`_MAX_REPAIR_SLEEP_S` is one slot of the §18.2 tick-gap budget.

    It is hand-computed (10.0 == 30.0 / 3) and nothing binds it: the tick gap
    lives in cli, the slot count in kill_switch, so an import-time guard would be
    circular. This test is the binding. Without it, lowering
    ``_RECOVERY_MAX_TICK_GAP_SECONDS`` to 15 would have sl_repair_delay_warning
    warning at >=5s while the clamp still permitted 10s — the constant
    contradicting its own advisory, which is the defect it was introduced to fix
    (2026-08-01 round-13 exit check).
    """
    from contrib.hyperliquid_perp.cli import _RECOVERY_MAX_TICK_GAP_SECONDS
    from contrib.hyperliquid_perp.live.kill_switch import _MAX_UNREFRESHED_REST_CALLS
    from contrib.hyperliquid_perp.live.protection import _MAX_REPAIR_SLEEP_S

    assert _MAX_REPAIR_SLEEP_S == _RECOVERY_MAX_TICK_GAP_SECONDS / _MAX_UNREFRESHED_REST_CALLS


def test_the_repair_backoff_is_capped_to_leave_room_for_two_timeouts(env):
    """The clamp gets ONE of the three §18.2 slots, not all of them.

    The stretch between two ``refresh_across_blocking_work`` calls holds the
    rung's wire call and — on the ExchangeError lane — its orderStatus recovery
    probe, two full ``network_timeout_s``, before this sleep. A clamp equal to the
    WHOLE 30s tick-gap promise therefore allowed 8 + 8 + 30 = 46s inside it at the
    RUNBOOK's recommended timeout, contradicting ``_maybe_delay``'s own docstring
    three lines below the constant (2026-08-01 round-13 concept scan).
    """
    db = env
    client, gate = _FakeClient(), _gate()
    sleeps: list[float] = []
    mgr = _manager(db, client, gate, sleeps=sleeps)
    # delay 5 x backoff 10 = 50s of nominal backoff, clamped to the slot.
    mgr._maybe_delay(1, 3, backoff=10)
    assert sleeps == [10.0], "the repair backoff is not capped at one third of the tick gap"


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
    filled would then be lost to reconciliation. And the flat sync must NOT report
    FLAT over the un-confirmed residual (§17.1 rule 4): it degrades, keeps the
    gate's failure line up, and retries the cancel next sync."""
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
    assert outcome is ProtectionOutcome.DEGRADED  # not FLAT: a residual still rests
    assert gate.unresolved_protection_failure  # the gate line stays up
    assert client.canceled  # a cancel was attempted
    row = db.conn.execute(
        "SELECT status FROM orders WHERE run_id='r' AND order_role='stop_loss'"
    ).fetchone()
    assert row["status"] == "open"  # NOT canceled — the refusal was respected
    # Once the cancel lands, sync reports FLAT and lowers the line.
    client.cancel_script = []
    outcome = mgr.sync(
        position=PositionState.flat("BTC"),
        liquidation_price=None,
        mark=Decimal(50000),
        plan_active=False,
    )
    assert outcome is ProtectionOutcome.FLAT
    assert not gate.unresolved_protection_failure


def test_degraded_protection_cleared_event_on_recovery(env):
    """§17 audit: recovering from DEGRADED (TP failed) to PROTECTED emits a
    degraded_protection_cleared event, so the trail shows protection RESTORED, not
    only that degradation was entered."""
    db = env
    _seed_long(db)
    client, gate = _FakeClient(), _gate()
    # First sync: SL placed, TP fails all 3 attempts -> DEGRADED.
    client.place_script = ["ok", "error", "error", "error"]
    mgr = _manager(db, client, gate)
    first = mgr.sync(
        position=_long_position(),
        liquidation_price=Decimal(40000),
        mark=Decimal(50000),
        plan_active=False,
    )
    assert first is ProtectionOutcome.DEGRADED
    assert gate.unresolved_protection_failure is True
    # Second sync: SL is a no-op (unchanged), TP now succeeds -> PROTECTED, and the
    # recovery emits degraded_protection_cleared.
    client.place_script = ["ok"]
    second = mgr.sync(
        position=_long_position(),
        liquidation_price=Decimal(40000),
        mark=Decimal(50000),
        plan_active=False,
    )
    assert second is ProtectionOutcome.PROTECTED
    events = [e["event_type"] for e in repo.iter_protection_order_events(db.conn, "r")]
    assert "degraded_protection_cleared" in events


def test_kill_switch_refreshed_across_repair_delays(env):
    """§18.2: each blocking repair rung refreshes the dead man's switch, so a
    repair episode cannot stretch the refresh cadence toward max_tick_gap.

    The ladder's invariant is "every wire call is followed by a refresh": each
    attempt refreshes once on the returned ack and once when the rung closes, so
    three attempts refresh SIX times. It used to be two — one per retry delay —
    which left both the final rung and every ack-bearing call uncovered
    (2026-07-31 deadline review).
    """
    db = env
    _seed_long(db)
    client, gate = _all_fail_client()  # 3 attempts, each: ack + rung close
    ks = _FakeKillSwitch()
    mgr = _manager(db, client, gate, kill_switch=ks)
    mgr.sync(
        position=_long_position(),
        liquidation_price=Decimal(40000),
        mark=Decimal(50000),
        plan_active=True,
    )
    assert ks.ticks == 6


def test_the_only_repair_rung_refreshes_although_it_never_sleeps(env):
    """Regression control isolating the final-rung fix from the delay-driven
    refreshes: a ONE-attempt ladder sleeps never, and must still refresh across
    the wire call it just made.

    Two refreshes, both from a ladder that never slept: one on the returned ack,
    one closing the only rung. Under the old ``attempt >= attempts`` early
    return the rung-close refresh did not happen at all, so reverting that half
    alone drops this to 1.
    """
    db = env
    _seed_long(db)
    client, gate = _all_fail_client()
    sleeps: list[float] = []
    ks = _FakeKillSwitch()
    mgr = _manager(
        db,
        client,
        gate,
        sleeps=sleeps,
        kill_switch=ks,
        protection=LiveProtectionConfig(sl_repair_max_attempts=1),
    )
    mgr.sync(
        position=_long_position(),
        liquidation_price=Decimal(40000),
        mark=Decimal(50000),
        plan_active=True,
    )
    assert sleeps == []  # nothing slept: the refresh is not the sleep's rider
    assert ks.ticks == 2


def test_the_orderstatus_confirmation_read_refreshes_across_itself(env):
    """§18.2: ``_row_still_rests`` blocks the single-threaded tick for a full
    network timeout, and one sync can reach it three times (SL no-op guard, SL
    covering check, TP no-op guard) because the latch clears only on a POSITIVE
    confirmation — so the repeats are exactly the degraded-network case.

    Both paths refresh. The failing read matters most: it is the one that spends
    the whole timeout getting nowhere, which is why the refresh sits in a
    ``finally`` (2026-07-31 deadline review).
    """
    db = env
    _seed_long(db)
    row = {"cloid_hex": "0x" + "a" * 32}

    # Failure path: the read rode its timeout and resolved nothing.
    client, gate = _FakeClient(), _gate()
    client.status_script = [ExchangeRequestError("status endpoint down")]
    ks = _FakeKillSwitch(fired_total=1)  # latched: rows are suspect
    mgr = _manager(db, client, gate, kill_switch=ks)
    assert mgr._row_still_rests(row, role="stop_loss") is False
    assert ks.ticks == 1

    # Success path: a confirmed-resting row refreshes too — it cost the same
    # round-trip, and returning early past the refresh is what caused the bug.
    client2, gate2 = _FakeClient(), _gate()
    client2.status_script = [_resting_status_payload("777")]
    ks2 = _FakeKillSwitch(fired_total=1)
    mgr2 = _manager(db, client2, gate2, kill_switch=ks2)
    assert mgr2._row_still_rests(row, role="stop_loss") is True
    assert ks2.ticks == 1


def test_an_unclassifiable_status_word_is_not_proof_that_a_stop_still_rests(env):
    """A word the table does not carry must fail CLOSED here.

    ``local_status_for_exchange_status`` maps an unknown word to "open" ON
    PURPOSE — for RECORDING, overstating liveness is the recoverable direction.
    But "open" is a resting status, so routing a safety verdict through it
    answers "yes, it rests" for a word we cannot classify. The reachable damage:
    the switch fires and the exchange wipes the wallet, §13.4 reopens the gate,
    the no-op guard matches the untouched local row, this returns True, and
    ``_establish`` reports ESTABLISHED before any wire call — no protection
    event, no §20.3 window, and the cancelled SL is never re-placed
    (2026-08-01 malformed-response review).
    """
    db = env
    _seed_long(db)
    row = {"cloid_hex": "0x" + "a" * 32}

    client, gate = _FakeClient(), _gate()
    client.status_script = [
        {"status": "order", "order": {"order": {"oid": "1001"}, "status": "someFutureWord"}}
    ]
    ks = _FakeKillSwitch(fired_total=1)  # latched: rows are suspect
    mgr = _manager(db, client, gate, kill_switch=ks)
    assert mgr._row_still_rests(row, role="stop_loss") is False
    # ...and nothing was cached: an unclassifiable answer confirmed no order, so
    # the next sync must still ask the exchange about this row.
    assert mgr._confirmed_cloid == {}

    # Narrowness: a KNOWN resting word still confirms, so nothing normal is
    # blocked by the stricter reading.
    client2, gate2 = _FakeClient(), _gate()
    client2.status_script = [_resting_status_payload("777")]
    mgr2 = _manager(db, client2, gate2, kill_switch=_FakeKillSwitch(fired_total=1))
    assert mgr2._row_still_rests(row, role="stop_loss") is True


def test_confirming_the_stop_loss_does_not_vouch_for_the_take_profit(env):
    """Row-suspicion is per ORDER, not one shared "spent" flag.

    ``sync`` always establishes the SL before the TP, so a shared flag was
    routinely cleared by the SL's confirmation before the TP guard ever ran —
    and after a firing the SL is commonly a FRESHLY PLACED row, whose resting
    says nothing about the stale TP beside it. The TP then took the free path and
    was reported resting while the scheduleCancel had removed it: §17.3's
    DEGRADED block lifts on a false premise and take_profit_price keeps asserting
    a price that is gone (2026-08-01 malformed-response review).
    """
    db = env
    _seed_long(db)
    sl_row = {"cloid_hex": "0x" + "5" * 32}
    tp_row = {"cloid_hex": "0x" + "7" * 32}

    client, gate = _FakeClient(), _gate()
    # First read confirms the SL; the second — for the TP — says it is gone.
    client.status_script = [
        _resting_status_payload("111"),
        {"status": "order", "order": {"order": {"oid": "222"}, "status": "canceled"}},
    ]
    mgr = _manager(db, client, gate, kill_switch=_FakeKillSwitch(fired_total=1))

    assert mgr._row_still_rests(sl_row, role="stop_loss") is True
    # The TP must still be asked about — its own evidence, not the SL's.
    assert mgr._row_still_rests(tp_row, role="take_profit") is False
    assert len(client.status_queries) == 2

    # The confirmed SL is cached, so the repeat costs nothing more...
    assert mgr._row_still_rests(sl_row, role="stop_loss") is True
    assert len(client.status_queries) == 2
    # ...but a NEW firing invalidates that confirmation too.
    mgr._kill_switch.fired_total = 2
    client.status_script = [_resting_status_payload("111")]
    assert mgr._row_still_rests(sl_row, role="stop_loss") is True
    assert len(client.status_queries) == 3


def test_a_fresh_placement_vouches_for_itself_without_an_extra_read(env):
    """An accepted ack IS the exchange saying this cloid rests.

    Without seeding it, a process that has seen one firing keeps the suspicion
    latch up forever and pays a full-timeout orderStatus on every re-place — and
    a slice plan re-places the SL as the position grows, so that is per slice, on
    the same thread whose REST budget this round is otherwise protecting
    (2026-08-01 lifecycle review).
    """
    db = env
    _seed_long(db)
    client, gate = _FakeClient(), _gate()
    mgr = _manager(db, client, gate, kill_switch=_FakeKillSwitch(fired_total=1))

    mgr.sync(
        position=_long_position(),
        liquidation_price=Decimal(40000),
        mark=Decimal(50000),
        plan_active=True,
    )
    placed = repo.active_protection_order(db.conn, "r", "BTC", "stop_loss")
    assert placed is not None
    reads_after_place = len(client.status_queries)

    # The row this sync just placed is trusted on the ack; the next sync's no-op
    # guard costs nothing.
    assert mgr._row_still_rests(placed, role="stop_loss") is True
    assert len(client.status_queries) == reads_after_place


def test_lost_ack_recovery_does_not_book_an_unclassifiable_word_as_live(env):
    """The same fail-OPEN in ``_recover_placed_order`` — and this one needs no
    kill-switch firing at all, only a lost ack.

    Its docstring promises "Recovery only ever returns True on a POSITIVE
    confirmation", but testing ``in ("canceled", "rejected")`` reads the
    unknown-word fallback as proof of life: the order gets persisted as live
    protection, ``active_protection_order`` treats it as a resting stop, and the
    repair ladder stops trying (2026-08-01 malformed-response review).
    """
    db = env
    _seed_long(db)
    client, gate = _FakeClient(), _gate()
    client.status_script = [
        {"status": "order", "order": {"order": {"oid": "4242"}, "status": "someFutureWord"}}
    ]
    mgr = _manager(db, client, gate, kill_switch=_FakeKillSwitch())
    recovered = mgr._recover_placed_order(
        role="stop_loss",
        order_id="ord-1",
        logical="log-1",
        hexid="0x" + "b" * 32,
        size=Decimal("0.01"),
        side=Side.SELL,
        trigger_price=Decimal(45000),
        limit_price=Decimal(44900),
        replaced=None,
        now=_NOW,
    )
    assert recovered is False
    # Nothing was booked: no row may claim protection off an unclassified word.
    assert repo.active_protection_order(db.conn, "r", "BTC", "stop_loss") is None


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


def test_sl_fire_band_floored_at_3pct_while_tp_keeps_routine_band(env):
    """§9.4 aggressive family: the SL only fires in the violent move it protects
    against, so its fire-time limit band is floored at 3% even when the routine
    max_slippage_pct (0.005 here) is tighter; the TP keeps the routine band — a
    missed TP is opportunity cost, a missed SL is an uncapped loss."""
    db = env
    _seed_long(db)
    client, gate = _FakeClient(), _gate()
    mgr = _manager(db, client, gate)  # max_slippage_pct = 0.005
    outcome = mgr.sync(
        position=_long_position(),
        liquidation_price=Decimal(40000),
        mark=Decimal(50000),
        plan_active=False,
    )
    assert outcome is ProtectionOutcome.PROTECTED
    sl = repo.active_protection_order(db.conn, "r", "BTC", "stop_loss")
    tp = repo.active_protection_order(db.conn, "r", "BTC", "take_profit")
    # Long: SL and TP both fire as a SELL, so each limit sits BELOW its trigger.
    sl_trigger = Decimal(sl["trigger_price"])
    tp_trigger = Decimal(tp["trigger_price"])
    assert Decimal(sl["price"]) == round_to_tick(sl_trigger * Decimal("0.97"), _TICK, up=False)
    assert Decimal(tp["price"]) == round_to_tick(tp_trigger * Decimal("0.995"), _TICK, up=False)


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
    # The RECORDED price goes too, not just the order. Only the flat path used
    # to null these columns, so every position_snapshots / ai_inputs row and
    # both CSV exports written during the plan window asserted a take-profit
    # that was no longer on the book — for as long as the plan ran.
    sl, tp = repo.get_position_protection(db.conn, "r", "BTC")
    assert tp is None
    assert sl is not None  # and the SL, which §17.1 rule 5 keeps, is untouched


def test_an_unsuspendable_tp_keeps_its_recorded_price(env):
    # Negative control for the clear above: when the cancel cannot land, a stale
    # reduce-only TP really IS still resting, so the column must keep saying so.
    # Truthfulness runs both ways.
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
    _, tp_before = repo.get_position_protection(db.conn, "r", "BTC")
    assert tp_before is not None
    client.cancel_script = ["refused"]  # the suspension cannot land
    outcome = mgr.sync(
        position=_long_position(),
        liquidation_price=Decimal(40000),
        mark=Decimal(50000),
        plan_active=True,
    )
    assert outcome is ProtectionOutcome.DEGRADED
    _, tp_after = repo.get_position_protection(db.conn, "r", "BTC")
    assert tp_after == tp_before  # still resting, still recorded


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


def test_clearing_a_flat_position_refreshes_across_each_cancel(env):
    """§18.2: ``_clear`` cancels once per §17.1-rule-4 role, so a flat position
    pays two back-to-back cancel round-trips every tick — each gets a refresh
    (2026-07-31 deadline review).
    """
    db = env
    _seed_long(db)
    client, gate = _FakeClient(), _gate()
    ks = _FakeKillSwitch()
    mgr = _manager(db, client, gate, kill_switch=ks)
    mgr.sync(
        position=_long_position(),
        liquidation_price=Decimal(40000),
        mark=Decimal(50000),
        plan_active=False,
    )
    with db.transaction() as conn:
        repo.upsert_current_position(conn, "r", PositionState.flat("BTC"), updated_at=_NOW)
    ks.ticks = 0  # count only the clearing sync
    outcome = mgr.sync(
        position=PositionState.flat("BTC"),
        liquidation_price=None,
        mark=Decimal(50000),
        plan_active=False,
    )
    assert outcome is ProtectionOutcome.FLAT
    assert ks.ticks == 2  # one per cancelled role, not one for the whole clear


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


def test_all_gate_rejected_sl_ladder_is_blocked_not_emergency_close(env):
    # A closed §4.1 gate raises LiveOrderGateRejected (a plain Exception, NOT an
    # ExchangeError) from inside place_trigger_order, BEFORE any network I/O. A
    # ladder in which EVERY attempt was refused pre-send never transmitted
    # anything, and the same gate would refuse the emergency close too — so it
    # resolves to BLOCKED (hold, retry next sync), never crashes the tick, and
    # never escalates: one stop_loss_repair_blocked event, no exhaustion.
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
    assert outcome is ProtectionOutcome.BLOCKED
    assert len(client.placed) == 3
    assert gate.unresolved_protection_failure is True  # the failure line stays up
    events = [e["event_type"] for e in repo.iter_protection_order_events(db.conn, "r")]
    assert events.count("stop_loss_repair_failed") == 3  # each attempt still audited
    assert events.count("stop_loss_repair_blocked") == 1
    assert "stop_loss_repair_exhausted" not in events
    # No SL was ever established here, so nothing is resting: the blocked event
    # carries no order_id, which is what the §20.3 validator reads as a genuine
    # unprotected window.
    blocked = [
        e
        for e in repo.iter_protection_order_events(db.conn, "r")
        if e["event_type"] == "stop_loss_repair_blocked"
    ][0]
    assert blocked["order_id"] is None
    assert "NO covering stop-loss" in blocked["detail"]


def test_a_gate_blocked_modify_records_the_still_resting_sl(env):
    # §17.4 is modify-before-cancel, so a gate-refused MODIFY leaves the
    # previous SL on the book at a stale trigger — protected, just not at the
    # band we now want. The event must say so (order_id present), or the §20.3
    # validator counts those seconds as unprotected and fails an otherwise
    # healthy 30-cycle acceptance run on one kill-switch refresh blip.
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
    resting = repo.active_protection_order(db.conn, "r", "BTC", "stop_loss")
    assert resting is not None  # an SL is established and on the book
    # The position SHRINKS (a partial close), so the SL is re-armed smaller —
    # but the one still resting is larger, so it keeps covering throughout.
    # The gate refuses every attempt pre-send; §17.4 modifies before it
    # cancels, so the refused calls are on the MODIFY path.
    client.modify_script = ["gate", "gate", "gate"]
    client.place_script = ["gate", "gate", "gate"]
    outcome = mgr.sync(
        position=_long_position(size=Decimal("0.05")),
        liquidation_price=Decimal(40000),
        mark=Decimal(50000),
        plan_active=False,
    )
    assert outcome is ProtectionOutcome.BLOCKED
    blocked = [
        e
        for e in repo.iter_protection_order_events(db.conn, "r")
        if e["event_type"] == "stop_loss_repair_blocked"
    ][0]
    assert blocked["order_id"] == resting["order_id"]
    assert "covers the position" in blocked["detail"]


def test_a_gate_blocked_resize_does_not_claim_the_undersized_sl_covers(env):
    # The marker must be COVERAGE, not existence. The commonest blocked MODIFY
    # is a RESIZE: a later slice filled and the resting SL now covers only part
    # of the position — exactly what the reconciler files as position_sl_missing.
    # Stamping the order_id there would tell the §20.3 validator "protected"
    # while part of the position carries no stop at all, and the unprotected
    # seconds (an exit-5 metric) would read 0 over a genuinely exposed run.
    db = env
    _seed_long(db, size=Decimal("0.1"))
    client, gate = _FakeClient(), _gate()
    mgr = _manager(db, client, gate)
    mgr.sync(
        position=_long_position(size=Decimal("0.1")),
        liquidation_price=Decimal(40000),
        mark=Decimal(50000),
        plan_active=False,
    )
    assert repo.active_protection_order(db.conn, "r", "BTC", "stop_loss") is not None
    # The position doubles; the resize is refused pre-send.
    client.modify_script = ["gate", "gate", "gate"]
    client.place_script = ["gate", "gate", "gate"]
    outcome = mgr.sync(
        position=_long_position(size=Decimal("0.2")),
        liquidation_price=Decimal(40000),
        mark=Decimal(50000),
        plan_active=False,
    )
    assert outcome is ProtectionOutcome.BLOCKED
    blocked = [
        e
        for e in repo.iter_protection_order_events(db.conn, "r")
        if e["event_type"] == "stop_loss_repair_blocked"
    ][0]
    assert blocked["order_id"] is None  # an under-covering SL is NOT protection
    assert "NO covering stop-loss" in blocked["detail"]
    assert "0.1" in blocked["detail"] and "0.2" in blocked["detail"]  # names the shortfall


def test_a_rate_limited_ladder_holds_instead_of_emergency_closing(env):
    # The repair ladder could not tell "the venue would not serve us" from "the
    # venue rejected this order". Three fixed-delay attempts fit inside one
    # 15-second rate-limit window, so a throttle exhausted the ladder → §17.2
    # market-closes a HEALTHY position and latches the run for a human. And a
    # venue throttles hardest in exactly the violent move a stop exists for.
    from contrib.hyperliquid_perp.exchanges.hyperliquid.errors import ExchangeThrottledError

    db = env
    _seed_long(db)
    client, gate = _FakeClient(), _gate()

    def _throttle(**_kw):
        raise ExchangeThrottledError("429 Too Many Requests")

    client.place_trigger_order = _throttle
    client.modify_trigger_order = _throttle
    outcome = _manager(db, client, gate).sync(
        position=_long_position(),
        liquidation_price=Decimal(40000),
        mark=Decimal(50000),
        plan_active=False,
    )
    assert outcome is ProtectionOutcome.BLOCKED  # NOT NEEDS_EMERGENCY_CLOSE
    assert gate.unresolved_protection_failure is True  # still blocks new risk
    blocked = [
        e
        for e in repo.iter_protection_order_events(db.conn, "r")
        if e["event_type"] == "stop_loss_repair_blocked"
    ][0]
    assert "rate-limited" in blocked["detail"]
    # The window still opens: nothing was placed, so the position may be naked.
    assert blocked["order_id"] is None


def test_one_real_rejection_among_throttles_still_exhausts(env):
    # The carve-out must be narrow. An ack that says "no" is the exchange
    # REJECTING the order — precisely the evidence a throttle is not — so a
    # ladder containing one is an ordinary exhaustion and must still escalate.
    from contrib.hyperliquid_perp.exchanges.hyperliquid.errors import ExchangeThrottledError

    db = env
    _seed_long(db)
    client, gate = _FakeClient(), _gate()
    calls = {"n": 0}
    real_place = client.place_trigger_order

    def _mixed(**kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ExchangeThrottledError("429 Too Many Requests")
        return real_place(**kw)

    client.place_trigger_order = _mixed
    client.place_script = ["error", "error", "error"]
    outcome = _manager(db, client, gate).sync(
        position=_long_position(),
        liquidation_price=Decimal(40000),
        mark=Decimal(50000),
        plan_active=False,
    )
    assert outcome is ProtectionOutcome.NEEDS_EMERGENCY_CLOSE


def _resting_status_payload(oid: str = "1001") -> dict:
    """An orderStatus answer that CONFIRMS the order is still on the book."""
    return {"status": "order", "order": {"order": {"oid": oid}, "status": "open"}}


def test_a_blocked_sl_does_not_claim_coverage_from_a_row_the_switch_invalidated(env):
    # The §20.3 hole: reaching GATE_BLOCKED over a PROTECTIVE order means the
    # kill switch is down, and a switch that went down by lapsing its deadline
    # has had the exchange cancel every order on the wallet — without touching
    # SQLite. Side, trigger and qty all still match, so the local row matches
    # perfectly and is completely wrong. Stamping order_id off it would suppress
    # the unprotected window (and CLOSE an already-open one), reporting
    # unprotected_position_seconds = 0 over a genuinely naked position — an
    # exit-0 live_ready verdict on a run that traded unprotected.
    #
    # NB the two tests above refuse at the fake CLIENT while the gate object
    # still says the switch is healthy, so they exercise the covering predicate
    # without the staleness. Here kill_switch_active False is both why the wire
    # refuses and why the exchange has already emptied the book — the real shape.
    db = env
    _seed_long(db)
    client, gate = _FakeClient(), _gate()
    ks = _FakeKillSwitch()
    mgr = _manager(db, client, gate, kill_switch=ks)
    mgr.sync(
        position=_long_position(),
        liquidation_price=Decimal(40000),
        mark=Decimal(50000),
        plan_active=False,
    )
    assert repo.active_protection_order(db.conn, "r", "BTC", "stop_loss") is not None
    # The switch FIRES: the wallet's book is emptied, the rows are untouched.
    ks.fired_total += 1
    gate.kill_switch_active = False
    client.modify_script = ["gate", "gate", "gate"]
    client.place_script = ["gate", "gate", "gate"]
    outcome = mgr.sync(
        position=_long_position(size=Decimal("0.05")),
        liquidation_price=Decimal(40000),
        mark=Decimal(50000),
        plan_active=False,
    )
    assert outcome is ProtectionOutcome.BLOCKED
    blocked = [
        e
        for e in repo.iter_protection_order_events(db.conn, "r")
        if e["event_type"] == "stop_loss_repair_blocked"
    ][0]
    # unknownOid (the default scripted answer): the exchange does not carry it.
    assert client.status_queries  # it actually asked, rather than trusting the row
    assert blocked["order_id"] is None
    assert "NO covering stop-loss" in blocked["detail"]


def test_a_blocked_sl_still_stamps_a_row_orderstatus_confirms_is_resting(env):
    # The other direction: a refresh blip that did NOT lapse the deadline leaves
    # the book intact. Confirmed live ⇒ the row is real evidence and the window
    # must stay suppressed, or one blip fails a healthy 30-cycle acceptance run.
    db = env
    _seed_long(db)
    client, gate = _FakeClient(), _gate()
    ks = _FakeKillSwitch()
    mgr = _manager(db, client, gate, kill_switch=ks)
    mgr.sync(
        position=_long_position(),
        liquidation_price=Decimal(40000),
        mark=Decimal(50000),
        plan_active=False,
    )
    resting = repo.active_protection_order(db.conn, "r", "BTC", "stop_loss")
    ks.fired_total += 1
    gate.kill_switch_active = False
    client.status_script = [_resting_status_payload(str(resting["exchange_order_id"]))]
    client.modify_script = ["gate", "gate", "gate"]
    client.place_script = ["gate", "gate", "gate"]
    outcome = mgr.sync(
        position=_long_position(size=Decimal("0.05")),
        liquidation_price=Decimal(40000),
        mark=Decimal(50000),
        plan_active=False,
    )
    assert outcome is ProtectionOutcome.BLOCKED
    blocked = [
        e
        for e in repo.iter_protection_order_events(db.conn, "r")
        if e["event_type"] == "stop_loss_repair_blocked"
    ][0]
    assert blocked["order_id"] == resting["order_id"]
    assert "covers the position" in blocked["detail"]


def test_an_unreadable_orderstatus_does_not_let_a_stale_row_claim_coverage(env):
    # Fail-closed on uncertainty, matching reconcile._has_valid_sl reading a
    # None open_orders as False. A transport error is not evidence of coverage.
    db = env
    _seed_long(db)
    client, gate = _FakeClient(), _gate()
    ks = _FakeKillSwitch()
    mgr = _manager(db, client, gate, kill_switch=ks)
    mgr.sync(
        position=_long_position(),
        liquidation_price=Decimal(40000),
        mark=Decimal(50000),
        plan_active=False,
    )
    ks.fired_total += 1
    gate.kill_switch_active = False
    client.status_script = [ExchangeRequestError("status endpoint down")]
    client.modify_script = ["gate", "gate", "gate"]
    client.place_script = ["gate", "gate", "gate"]
    mgr.sync(
        position=_long_position(size=Decimal("0.05")),
        liquidation_price=Decimal(40000),
        mark=Decimal(50000),
        plan_active=False,
    )
    blocked = [
        e
        for e in repo.iter_protection_order_events(db.conn, "r")
        if e["event_type"] == "stop_loss_repair_blocked"
    ][0]
    assert blocked["order_id"] is None


def test_a_fired_switch_makes_the_no_op_guard_re_place_the_cancelled_sl(env):
    # The severe sibling of the covering bug, and the one that costs money.
    # _establish's no-op shortcut compares side/trigger/qty against the local
    # row — every field a fired scheduleCancel leaves untouched. Trusting it
    # returns ESTABLISHED before any wire call: no protection event is written
    # (so no window ever OPENS, not merely fails to close), the outcome is
    # PROTECTED (which LOWERS the gate's unresolved_protection_failure line),
    # and — the part that costs money — once §13.4 reopens the gate the same
    # stale row keeps matching, so the SL the exchange cancelled is NEVER
    # re-placed. The no-op actively fights its own recovery.
    db = env
    _seed_long(db)
    client, gate = _FakeClient(), _gate()
    ks = _FakeKillSwitch()
    mgr = _manager(db, client, gate, kill_switch=ks)
    position = _long_position()
    mgr.sync(
        position=position,
        liquidation_price=Decimal(40000),
        mark=Decimal(50000),
        plan_active=False,
    )
    assert repo.active_protection_order(db.conn, "r", "BTC", "stop_loss") is not None
    # The switch FIRES. The position is UNCHANGED, so every field the no-op
    # guard compares still matches the row the exchange just cancelled.
    ks.fired_total += 1
    gate.kill_switch_active = False
    client.modify_script = ["gate", "gate", "gate"]
    client.place_script = ["gate", "gate", "gate"]
    outcome = mgr.sync(
        position=position,
        liquidation_price=Decimal(40000),
        mark=Decimal(50000),
        plan_active=False,
    )
    # Not a silent PROTECTED no-op: the exposure is on the record and the gate
    # line stays up.
    assert outcome is ProtectionOutcome.BLOCKED
    assert gate.unresolved_protection_failure is True
    events = [e["event_type"] for e in repo.iter_protection_order_events(db.conn, "r")]
    assert "stop_loss_repair_blocked" in events
    # §13.4 releases and the gate reopens. The row is STILL stale, so the SL has
    # to actually go back on the book rather than be assumed present.
    gate.kill_switch_active = True
    client.modify_script = []
    client.place_script = []
    before = len(client.modified) + len(client.placed)
    mgr.sync(
        position=position,
        liquidation_price=Decimal(40000),
        mark=Decimal(50000),
        plan_active=False,
    )
    assert len(client.modified) + len(client.placed) > before  # re-armed, not assumed


def test_a_refresh_blip_does_not_invalidate_the_rows(env):
    # The narrowing that keeps this fix from becoming the bug it replaced. The
    # §4.1 gate also drops on a SINGLE failed refresh, which cancels nothing —
    # keying row-suspicion on the gate meant one network blip forced a
    # fail-closed orderStatus read that the same blip would fail, so a
    # still-protected position recorded an unprotected window that only a
    # stop_loss_placed can close, and the no-op path never writes one. That is
    # "one blip fails an otherwise-healthy 30-cycle run", which §20.3's covering
    # carve-out exists to prevent. Suspicion now keys on the FIRING count.
    db = env
    _seed_long(db)
    client, gate = _FakeClient(), _gate()
    ks = _FakeKillSwitch()  # fired_total stays 0: the switch never went off
    mgr = _manager(db, client, gate, kill_switch=ks)
    position = _long_position()
    mgr.sync(
        position=position,
        liquidation_price=Decimal(40000),
        mark=Decimal(50000),
        plan_active=False,
    )
    # A refresh blip: the gate shuts, the exchange cancels nothing.
    gate.kill_switch_active = False
    outcome = mgr.sync(
        position=position,
        liquidation_price=Decimal(40000),
        mark=Decimal(50000),
        plan_active=False,
    )
    assert outcome is ProtectionOutcome.PROTECTED  # the SL really is still there
    assert client.status_queries == []  # and no fail-closed read was forced
    assert gate.unresolved_protection_failure is False
    events = [e["event_type"] for e in repo.iter_protection_order_events(db.conn, "r")]
    assert "stop_loss_repair_blocked" not in events  # no phantom window


def test_a_healthy_run_never_pays_for_an_orderstatus_confirmation(env):
    # The confirmation is latched on the switch having been seen down, not asked
    # every sync: while nothing can have mass-cancelled the book behind us, the
    # local row stands on its own and the no-op shortcut must stay free.
    db = env
    _seed_long(db)
    client, gate = _FakeClient(), _gate()
    mgr = _manager(db, client, gate)
    position = _long_position()
    mgr.sync(
        position=position,
        liquidation_price=Decimal(40000),
        mark=Decimal(50000),
        plan_active=False,
    )
    after_first = len(client.placed) + len(client.modified)  # the SL and the TP
    for _ in range(2):
        mgr.sync(
            position=position,
            liquidation_price=Decimal(40000),
            mark=Decimal(50000),
            plan_active=False,
        )
    assert len(client.placed) + len(client.modified) == after_first  # both no-ops
    assert client.status_queries == []


def test_a_wrong_side_resting_sl_does_not_count_as_coverage(env):
    # The other half of the predicate. After a flip the previous side's SL can
    # still be resting — big enough, but pointing the wrong way, so it protects
    # nothing. `_has_valid_sl` calls this case out too. Without the side clause
    # the qty test alone would pass here and suppress the window.
    db = env
    _seed_long(db, size=Decimal("0.1"))
    client, gate = _FakeClient(), _gate()
    mgr = _manager(db, client, gate)
    mgr.sync(
        position=_long_position(size=Decimal("0.1")),
        liquidation_price=Decimal(40000),
        mark=Decimal(50000),
        plan_active=False,
    )
    resting = repo.active_protection_order(db.conn, "r", "BTC", "stop_loss")
    assert resting["side"] == "sell"  # a long's SL sells
    # The position flips short; the SL for the new side is refused pre-send.
    short = PositionState(coin="BTC", size=Decimal("-0.05"), entry_price=Decimal(50000))
    with db.transaction() as conn:
        repo.upsert_current_position(conn, "r", short, updated_at=_NOW)
    client.modify_script = ["gate", "gate", "gate"]
    client.place_script = ["gate", "gate", "gate"]
    outcome = mgr.sync(
        position=short,
        liquidation_price=Decimal(60000),
        mark=Decimal(50000),
        plan_active=False,
    )
    assert outcome is ProtectionOutcome.BLOCKED
    blocked = [
        e
        for e in repo.iter_protection_order_events(db.conn, "r")
        if e["event_type"] == "stop_loss_repair_blocked"
    ][0]
    # qty 0.1 >= 0.05, so only the SIDE clause can reject this one.
    assert blocked["order_id"] is None
    assert "NO covering stop-loss" in blocked["detail"]


def test_mixed_gate_and_real_failures_still_exhaust_to_emergency_close(env):
    # Regression guard on the GATE_BLOCKED carve-out: ONE real wire failure in
    # the ladder (an ExchangeError, a rejected ack) means an attempt reached —
    # or may have reached — the exchange, so the ladder keeps §17.2 EXHAUSTED
    # semantics and escalates to the emergency close.
    db = env
    _seed_long(db)
    client, gate = _FakeClient(), _gate()
    client.place_script = ["gate", "raise", "error"]
    outcome = _manager(db, client, gate).sync(
        position=_long_position(),
        liquidation_price=Decimal(40000),
        mark=Decimal(50000),
        plan_active=True,
    )
    assert outcome is ProtectionOutcome.NEEDS_EMERGENCY_CLOSE
    assert len(client.placed) == 3
    assert gate.unresolved_protection_failure is True
    events = [e["event_type"] for e in repo.iter_protection_order_events(db.conn, "r")]
    assert "stop_loss_repair_exhausted" in events
    assert "stop_loss_repair_blocked" not in events


def test_blocked_sl_ladder_recovers_on_a_later_sync_when_the_gate_reopens(env):
    # BLOCKED is a hold, not a terminal verdict: once the gate reopens, the
    # next sync's ladder places the SL, reports PROTECTED, lowers the failure
    # line, and the audit trail records the restoration.
    db = env
    _seed_long(db)
    client, gate = _FakeClient(), _gate()
    client.place_script = ["gate", "gate", "gate"]
    mgr = _manager(db, client, gate)
    first = mgr.sync(
        position=_long_position(),
        liquidation_price=Decimal(40000),
        mark=Decimal(50000),
        plan_active=True,
    )
    assert first is ProtectionOutcome.BLOCKED
    assert gate.unresolved_protection_failure is True
    # The gate reopens: the next attempt is scripted to succeed.
    client.place_script = ["ok"]
    second = mgr.sync(
        position=_long_position(),
        liquidation_price=Decimal(40000),
        mark=Decimal(50000),
        plan_active=True,
    )
    assert second is ProtectionOutcome.PROTECTED
    assert gate.unresolved_protection_failure is False
    assert repo.active_protection_order(db.conn, "r", "BTC", "stop_loss") is not None
    events = [e["event_type"] for e in repo.iter_protection_order_events(db.conn, "r")]
    assert "stop_loss_placed" in events
    assert "degraded_protection_cleared" in events  # protection RESTORED, on the trail


def test_duplicate_cloid_ack_recovers_resting_order_without_resend(env):
    # §8.3 rules 2-4: a place ack rejected as a DUPLICATE cloid means a prior
    # attempt landed and its ack was lost. orderStatus confirming the order
    # resting recovers it — PROTECTED, no resend, no burned repair attempts.
    db = env
    _seed_long(db)
    client, gate = _FakeClient(), _gate()
    client.place_script = ["duplicate"]
    client.status_script = [
        {"status": "order", "order": {"order": {"oid": 4343}, "status": "open"}}
    ]
    outcome = _manager(db, client, gate).sync(
        position=_long_position(),
        liquidation_price=Decimal(40000),
        mark=Decimal(50000),
        plan_active=True,
    )
    assert outcome is ProtectionOutcome.PROTECTED
    assert client.status_queries  # orderStatus was consulted
    assert len(client.placed) == 1  # recovered, never re-sent
    active = repo.active_protection_order(db.conn, "r", "BTC", "stop_loss")
    assert active is not None and active["exchange_order_id"] == "4343"
    assert gate.unresolved_protection_failure is False
    events = [e["event_type"] for e in repo.iter_protection_order_events(db.conn, "r")]
    assert "stop_loss_repair_failed" not in events


def test_recovery_query_failure_counts_as_failed_attempt_not_a_crash(env):
    # An ExchangeError-triggered recovery whose orderStatus read itself RAISES
    # is unresolved: the attempt counts as failed (never a false "protected"),
    # the ladder continues and eventually exhausts — and nothing propagates
    # out of sync() (the tick must survive).
    db = env
    _seed_long(db)
    client, gate = _FakeClient(), _gate()
    client.place_script = ["raise", "raise", "raise"]
    client.status_script = [RuntimeError("orderStatus down") for _ in range(3)]
    outcome = _manager(db, client, gate).sync(
        position=_long_position(),
        liquidation_price=Decimal(40000),
        mark=Decimal(50000),
        plan_active=True,
    )
    assert outcome is ProtectionOutcome.NEEDS_EMERGENCY_CLOSE
    assert len(client.status_queries) == 3  # every attempt tried the recovery
    assert repo.active_protection_order(db.conn, "r", "BTC", "stop_loss") is None
    events = [e["event_type"] for e in repo.iter_protection_order_events(db.conn, "r")]
    assert events.count("stop_loss_repair_failed") == 3
    assert "stop_loss_repair_exhausted" in events


def test_orders_changed_last_sync_tracks_real_order_changes(env):
    """§12.2 rule 6 signal: a sync that places (or later cancels) an exchange
    order reports orders_changed_last_sync True; a steady-state no-op sync
    reports False — the engine keys its protection_change reconcile off this."""
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
    assert mgr.orders_changed_last_sync  # SL + TP were just placed
    outcome = mgr.sync(
        position=_long_position(),
        liquidation_price=Decimal(40000),
        mark=Decimal(50000),
        plan_active=False,
    )
    assert outcome is ProtectionOutcome.PROTECTED
    assert not mgr.orders_changed_last_sync  # no-op: same prices, nothing touched
    outcome = mgr.sync(
        position=PositionState.flat("BTC"),
        liquidation_price=None,
        mark=Decimal(50000),
        plan_active=False,
    )
    assert outcome is ProtectionOutcome.FLAT
    assert mgr.orders_changed_last_sync  # the flat clear cancelled resting orders
