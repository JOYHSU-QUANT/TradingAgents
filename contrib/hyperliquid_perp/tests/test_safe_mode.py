"""Tests for the §13 safe-mode state machine (persistence, gate wiring, doors)."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from contrib.hyperliquid_perp.live.config import ExecutionMode
from contrib.hyperliquid_perp.live.order_gate import RealOrderGate
from contrib.hyperliquid_perp.live.safe_mode import (
    REASON_KILL_SWITCH_REFRESH_FAILED,
    REASON_NON_BOT_OWNED_ORDER,
    REASON_REPEATED_MISMATCH,
    REASON_WS_DISCONNECT,
    SafeModeManager,
)
from contrib.hyperliquid_perp.paper.clock import ManualClock
from contrib.hyperliquid_perp.persistence import repository as repo
from contrib.hyperliquid_perp.persistence.db import Database

_NOW = datetime(2026, 7, 16, 8, 0, tzinfo=timezone.utc)


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
            initial_balance_usdc=Decimal(100),
            schema_version=6,
            created_at=_NOW,
        )
    gate = _gate()
    manager = SafeModeManager(db=db, run_id="r", gate=gate, clock=ManualClock(_NOW))
    yield db, gate, manager
    db.close()


def _events(db):
    return [
        (e["event_type"], e["safe_mode_type"], e["reason"], e["released_by"])
        for e in repo.iter_safe_mode_events(db.conn, "r")
    ]


# -- entering ---------------------------------------------------------------


def test_entering_recoverable_persists_state_and_history_and_shuts_the_gate(env):
    db, gate, manager = env
    assert manager.enter("recoverable", REASON_WS_DISCONNECT) is True

    state = manager.current()
    assert state is not None
    assert state.safe_mode_type == "recoverable"
    assert state.reason == REASON_WS_DISCONNECT
    row = repo.get_scheduler_state(db.conn, "r")
    assert row["safe_mode_type"] == "recoverable"
    assert row["safe_mode_entered_at"] is not None
    assert _events(db) == [("safe_mode_entered", "recoverable", REASON_WS_DISCONNECT, None)]
    # §13.2 enforcement rides the gate: the reconciled line drops, manual stays off.
    assert gate.state_reconciled is False
    assert gate.manual_safe_mode is False


def test_entering_manual_raises_the_manual_gate_flag(env):
    db, gate, manager = env
    manager.enter("manual", REASON_NON_BOT_OWNED_ORDER)
    assert gate.manual_safe_mode is True
    assert manager.current().is_manual


def test_reentering_the_same_level_is_idempotent(env):
    db, gate, manager = env
    manager.enter("recoverable", REASON_WS_DISCONNECT)
    entered_at = manager.current().entered_at
    # A stale WS re-reported every tick must neither spam history nor reset the clock.
    assert manager.enter("recoverable", REASON_KILL_SWITCH_REFRESH_FAILED) is False
    assert manager.current().entered_at == entered_at
    assert manager.current().reason == REASON_WS_DISCONNECT
    assert len(_events(db)) == 1


def test_manual_escalates_over_recoverable_and_is_recorded(env):
    db, gate, _ = env
    clock = ManualClock(_NOW)
    manager = SafeModeManager(db=db, run_id="r", gate=gate, clock=clock)
    manager.enter("recoverable", REASON_WS_DISCONNECT)
    original_entered_at = manager.current().entered_at
    clock.advance(1800)  # the escalation happens LATER (seconds)
    assert manager.enter("manual", REASON_NON_BOT_OWNED_ORDER) is True
    assert manager.current().is_manual
    assert gate.manual_safe_mode is True
    assert [e[0] for e in _events(db)] == ["safe_mode_entered", "safe_mode_escalated"]
    # An escalation is a severity change on the SAME unhealthy episode: the
    # original entry instant survives (time-gated release conditions key off it).
    assert manager.current().entered_at == original_entered_at


def test_recoverable_never_downgrades_manual(env):
    db, gate, manager = env
    manager.enter("manual", REASON_NON_BOT_OWNED_ORDER)
    assert manager.enter("recoverable", REASON_WS_DISCONNECT) is False
    assert manager.current().is_manual
    assert len(_events(db)) == 1


def test_enter_rejects_an_unknown_type(env):
    _, _, manager = env
    with pytest.raises(ValueError, match="safe_mode_type"):
        manager.enter("panic", "reason")


# -- restart persistence (§13.6 rule 1) --------------------------------------


def test_a_restart_does_not_clear_safe_mode_and_hydrate_restores_the_gate(env):
    db, _, manager = env
    manager.enter("manual", REASON_NON_BOT_OWNED_ORDER)

    fresh_gate = _gate()  # a new process: fail-closed flags, no memory
    fresh_gate.manual_safe_mode = False
    fresh = SafeModeManager(db=db, run_id="r", gate=fresh_gate, clock=ManualClock(_NOW))
    state = fresh.hydrate_gate()
    assert state is not None and state.is_manual
    assert fresh_gate.manual_safe_mode is True


def test_hydrate_on_a_clean_run_clears_a_stale_manual_flag(env):
    db, _, _ = env
    gate = _gate()
    gate.manual_safe_mode = True  # a mis-set flag must be reconciled to the store
    manager = SafeModeManager(db=db, run_id="r", gate=gate, clock=ManualClock(_NOW))
    assert manager.hydrate_gate() is None
    assert gate.manual_safe_mode is False


# -- §13.4 auto-recovery ------------------------------------------------------


def test_auto_recovery_releases_recoverable_when_all_conditions_hold(env):
    db, gate, manager = env
    manager.enter("recoverable", REASON_WS_DISCONNECT)
    assert (
        manager.try_auto_recover(
            reconciliation_clean=True, ws_restored=True, kill_switch_active=True
        )
        is True
    )
    assert manager.current() is None
    assert gate.state_reconciled is True
    events = _events(db)
    assert events[-1][0] == "safe_mode_released"
    assert events[-1][3] == "auto_recovery"


@pytest.mark.parametrize(
    ("clean", "ws", "ks"),
    [(False, True, True), (True, False, True), (True, True, False)],
)
def test_auto_recovery_requires_every_condition(env, clean, ws, ks):
    db, gate, manager = env
    manager.enter("recoverable", REASON_WS_DISCONNECT)
    assert (
        manager.try_auto_recover(reconciliation_clean=clean, ws_restored=ws, kill_switch_active=ks)
        is False
    )
    assert manager.current() is not None


def test_auto_recovery_never_releases_manual(env):
    db, gate, manager = env
    manager.enter("manual", REASON_NON_BOT_OWNED_ORDER)
    assert (
        manager.try_auto_recover(
            reconciliation_clean=True, ws_restored=True, kill_switch_active=True
        )
        is False
    )
    assert manager.current().is_manual


def test_auto_recovery_outside_safe_mode_is_a_noop(env):
    db, _, manager = env
    assert (
        manager.try_auto_recover(
            reconciliation_clean=True, ws_restored=True, kill_switch_active=True
        )
        is False
    )
    assert _events(db) == []


# -- §13.6 manual release ------------------------------------------------------


def test_manual_release_writes_the_audit_event_and_keeps_trading_blocked(env):
    db, gate, manager = env
    manager.enter("manual", REASON_NON_BOT_OWNED_ORDER)
    manager.release_manual(released_by="joy", reason="order confirmed as mine, cancelled by hand")

    assert manager.current() is None
    events = _events(db)
    assert events[-1] == ("safe_mode_released", None, None, "joy")
    # §13.6 rule 3: the manual latch lifts, but trading stays blocked until the
    # next reconciliation pass re-proves the state.
    assert gate.manual_safe_mode is False
    assert gate.state_reconciled is False


def test_manual_release_refuses_a_recoverable_safe_mode(env):
    _, _, manager = env
    manager.enter("recoverable", REASON_WS_DISCONNECT)
    with pytest.raises(ValueError, match="RECOVERABLE"):
        manager.release_manual(released_by="joy", reason="please")


def test_manual_release_refuses_a_run_not_in_safe_mode(env):
    _, _, manager = env
    with pytest.raises(ValueError, match="not in safe mode"):
        manager.release_manual(released_by="joy", reason="nothing to release")


@pytest.mark.parametrize(
    ("released_by", "reason"), [("", "why"), ("joy", ""), ("  ", "why"), ("joy", "  ")]
)
def test_manual_release_requires_identity_and_reason(env, released_by, reason):
    _, _, manager = env
    manager.enter("manual", REASON_NON_BOT_OWNED_ORDER)
    with pytest.raises(ValueError):
        manager.release_manual(released_by=released_by, reason=reason)


# -- §13.5 repeated-mismatch escalation ----------------------------------------


def test_three_consecutive_unclean_passes_escalate_to_manual(env):
    db, gate, manager = env
    manager.note_reconciliation_outcome(False)
    manager.note_reconciliation_outcome(False)
    state = manager.current()
    assert state is None or not state.is_manual
    manager.note_reconciliation_outcome(False)
    state = manager.current()
    assert state is not None and state.is_manual
    assert state.reason == REASON_REPEATED_MISMATCH


def test_a_clean_pass_resets_the_mismatch_streak(env):
    db, _, manager = env
    manager.note_reconciliation_outcome(False)
    manager.note_reconciliation_outcome(False)
    manager.note_reconciliation_outcome(True)
    manager.note_reconciliation_outcome(False)
    manager.note_reconciliation_outcome(False)
    state = manager.current()
    assert state is None or not state.is_manual


# -- offline wiring (gate=None, the CLI) ----------------------------------------


def test_offline_manager_transitions_without_a_gate(env):
    db, _, manager = env
    manager.enter("manual", REASON_NON_BOT_OWNED_ORDER)
    offline = SafeModeManager(db=db, run_id="r", gate=None, clock=ManualClock(_NOW))
    offline.release_manual(released_by="ops", reason="hand-verified")
    assert offline.current() is None
    assert _events(db)[-1][3] == "ops"


# -- repository invariants -------------------------------------------------------


def test_scheduler_state_safe_mode_trio_moves_together(env):
    db, _, _ = env
    with pytest.raises(ValueError, match="supplied together"), db.transaction() as conn:
        repo.upsert_scheduler_state(conn, "r", safe_mode_type="manual")
    with pytest.raises(ValueError, match="all set"), db.transaction() as conn:
        repo.upsert_scheduler_state(
            conn,
            "r",
            safe_mode_type="manual",
            safe_mode_reason=None,
            safe_mode_entered_at=_NOW,
        )


def test_safe_mode_event_validation(env):
    db, _, _ = env
    with db.transaction() as conn:
        with pytest.raises(ValueError, match="requires safe_mode_type and reason"):
            repo.insert_safe_mode_event(conn, run_id="r", event_type="safe_mode_entered")
        with pytest.raises(ValueError, match="released_by"):
            repo.insert_safe_mode_event(conn, run_id="r", event_type="safe_mode_released")
        with pytest.raises(ValueError, match="event_type"):
            repo.insert_safe_mode_event(conn, run_id="r", event_type="safe_mode_paused")
