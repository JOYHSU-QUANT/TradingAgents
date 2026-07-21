"""Tests for the §10 live risk checks and loss guards (PR 5)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from contrib.hyperliquid_perp.live.config import ExecutionMode, LiveSafetyConfig
from contrib.hyperliquid_perp.live.loss_guards import LossGuards
from contrib.hyperliquid_perp.live.order_gate import RealOrderGate
from contrib.hyperliquid_perp.live.safe_mode import (
    REASON_CONSECUTIVE_LOSS,
    REASON_DAILY_LOSS,
    SafeModeManager,
)
from contrib.hyperliquid_perp.paper.clock import ManualClock
from contrib.hyperliquid_perp.persistence import repository as repo
from contrib.hyperliquid_perp.persistence.db import Database

_NOW = datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc)


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


def _safety(**overrides) -> LiveSafetyConfig:
    base = {
        "allowed_symbols": ("BTC",),
        "max_notional_usdc": Decimal(100),
        "absolute_notional_ceiling": Decimal(500),
        "max_open_orders": 5,
        "max_daily_loss_pct": Decimal(2),
        "max_consecutive_loss_count": 3,
    }
    base.update(overrides)
    return LiveSafetyConfig(**base)


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
    clock = ManualClock(_NOW)
    gate = _gate()
    safe_mode = SafeModeManager(db=db, run_id="r", gate=gate, clock=clock)
    yield db, gate, safe_mode, clock
    db.close()


def _guards(env, **safety_overrides) -> LossGuards:
    db, _gate_obj, safe_mode, _clock = env
    return LossGuards(db=db, run_id="r", safety=_safety(**safety_overrides), safe_mode=safe_mode)


def _register(db, *, logical: str, hexid: str, role: str = "stop_loss") -> None:
    with db.transaction() as conn:
        repo.insert_cloid_mapping(
            conn,
            cloid_logical=logical,
            cloid_hex=hexid,
            run_id="r",
            symbol="BTC",
            order_role=role,
            created_at=_NOW,
        )


# -- §10.1 hard notional cap ------------------------------------------------


def test_notional_cap(env):
    guards = _guards(env)  # max_notional_usdc = 100
    assert guards.notional_exceeds_cap(Decimal(100)) is False  # at the cap is fine
    assert guards.notional_exceeds_cap(Decimal("100.01")) is True


# -- §10.5 max open orders --------------------------------------------------


def test_max_open_orders_counts_only_bot_owned(env):
    db, *_ = env
    guards = _guards(env, max_open_orders=2)
    _register(db, logical="hta_r_BTC_o_p_na_000_stop_loss", hexid="0xaaaa")
    _register(db, logical="hta_r_BTC_o_p_na_000_take_profit", hexid="0xbbbb", role="take_profit")
    open_orders = [
        {"oid": 1, "cloid": "0xaaaa"},  # bot-owned
        {"oid": 2, "cloid": "0xbbbb"},  # bot-owned
        {"oid": 3, "cloid": "0xffff"},  # unknown cloid — not ours
        {"oid": 4},  # no cloid — not ours
        "malformed",  # non-dict — ignored
    ]
    assert guards.count_bot_open_orders(open_orders) == 2
    assert guards.max_open_orders_reached(open_orders) is True
    # Below the cap when one bot order is dropped.
    assert guards.max_open_orders_reached(open_orders[1:]) is False


def test_max_open_orders_non_list_is_zero(env):
    guards = _guards(env)
    assert guards.count_bot_open_orders(None) == 0
    assert guards.count_bot_open_orders({"not": "a list"}) == 0


# -- §10.3 daily loss cap ---------------------------------------------------


def test_daily_loss_rolls_baseline_on_first_evaluation(env):
    guards = _guards(env)
    result = guards.evaluate_daily_loss(account_equity=Decimal(1000), now=_NOW)
    assert result.rolled is True
    assert result.baseline_equity == Decimal(1000)
    assert result.breached is False
    row = repo.get_scheduler_state(env[0].conn, "r")
    assert row["day_start_date"] == "2026-07-20"
    assert Decimal(row["day_start_equity"]) == Decimal(1000)


def test_daily_loss_breach_enters_recoverable_safe_mode(env):
    db, gate, safe_mode, _clock = env
    guards = _guards(env)  # max_daily_loss_pct = 2
    guards.evaluate_daily_loss(account_equity=Decimal(1000), now=_NOW)
    # 2% of 1000 = 20; a drop to 979 (>2%) breaches, 981 (<2%) does not.
    ok = guards.evaluate_daily_loss(account_equity=Decimal(981), now=_NOW + timedelta(minutes=1))
    assert ok.breached is False
    assert safe_mode.active is False

    hit = guards.evaluate_daily_loss(account_equity=Decimal(979), now=_NOW + timedelta(minutes=2))
    assert hit.breached is True
    assert hit.entered_safe_mode is True
    state = safe_mode.current()
    assert state is not None and state.reason == REASON_DAILY_LOSS
    assert state.safe_mode_type == "recoverable"
    assert gate.state_reconciled is False


def test_daily_loss_reentry_is_idempotent(env):
    db, _gate_obj, safe_mode, _clock = env
    guards = _guards(env)
    guards.evaluate_daily_loss(account_equity=Decimal(1000), now=_NOW)
    first = guards.evaluate_daily_loss(account_equity=Decimal(900), now=_NOW + timedelta(minutes=1))
    assert first.entered_safe_mode is True
    again = guards.evaluate_daily_loss(account_equity=Decimal(880), now=_NOW + timedelta(minutes=2))
    assert again.breached is True
    assert again.entered_safe_mode is False  # already latched — no new history row
    events = repo.iter_safe_mode_events(db.conn, "r")
    assert [e["event_type"] for e in events] == ["safe_mode_entered"]


def test_daily_loss_new_utc_day_rolls_and_clears(env):
    db, _gate_obj, safe_mode, clock = env
    guards = _guards(env)
    guards.evaluate_daily_loss(account_equity=Decimal(1000), now=_NOW)
    guards.evaluate_daily_loss(account_equity=Decimal(900), now=_NOW + timedelta(minutes=1))
    assert safe_mode.active is True
    # Next UTC day: the baseline rolls to the new opening equity; no breach.
    next_day = datetime(2026, 7, 21, 0, 0, 30, tzinfo=timezone.utc)
    rolled = guards.evaluate_daily_loss(account_equity=Decimal(900), now=next_day)
    assert rolled.rolled is True
    assert rolled.baseline_equity == Decimal(900)
    assert rolled.breached is False


def test_daily_loss_non_positive_baseline_is_documented_inert(env):
    # PINNED as documented behavior: a baseline <= 0 cannot express a
    # percentage drawdown, so the §10.3 guard is deliberately inert with a
    # non-positive day-start equity — drawdown_pct 0, never breached, however
    # far equity falls that day. A margin-called / empty account is blocked by
    # the other guards layered over this one, not by §10.3.
    db, _gate_obj, safe_mode, _clock = env
    guards = _guards(env)
    # First evaluation of the day with ZERO equity: the baseline rolls to 0.
    zero = guards.evaluate_daily_loss(account_equity=Decimal(0), now=_NOW)
    assert zero.rolled is True
    assert zero.baseline_equity == Decimal(0)
    assert zero.drawdown_pct == Decimal(0)
    assert zero.breached is False
    # Stays False for the rest of the day regardless of equity.
    later = guards.evaluate_daily_loss(account_equity=Decimal(-25), now=_NOW + timedelta(hours=1))
    assert later.rolled is False
    assert later.drawdown_pct == Decimal(0)
    assert later.breached is False
    assert safe_mode.active is False
    # A NEGATIVE baseline on the next day's roll is equally inert.
    next_day = datetime(2026, 7, 21, 0, 0, 30, tzinfo=timezone.utc)
    negative = guards.evaluate_daily_loss(account_equity=Decimal(-10), now=next_day)
    assert negative.rolled is True
    assert negative.baseline_equity == Decimal(-10)
    assert negative.breached is False
    worse = guards.evaluate_daily_loss(
        account_equity=Decimal(-50), now=next_day + timedelta(hours=2)
    )
    assert worse.drawdown_pct == Decimal(0)
    assert worse.breached is False
    assert safe_mode.active is False


def test_daily_loss_auto_release_time_gate(env):
    db, gate, safe_mode, clock = env
    guards = _guards(env)
    guards.evaluate_daily_loss(account_equity=Decimal(1000), now=_NOW)
    guards.evaluate_daily_loss(account_equity=Decimal(900), now=_NOW + timedelta(minutes=1))
    assert safe_mode.active is True
    # Same UTC day: even a clean reconciliation attestation must NOT release it.
    clock.set(_NOW + timedelta(hours=2))
    assert (
        safe_mode.try_auto_recover(
            reconciliation_clean=True, ws_restored=True, kill_switch_active=True, fully_wired=True
        )
        is False
    )
    assert safe_mode.active is True
    # Past the next UTC midnight: the §10.3 gate opens and §13.4 releases it.
    clock.set(datetime(2026, 7, 21, 0, 0, 30, tzinfo=timezone.utc))
    assert (
        safe_mode.try_auto_recover(
            reconciliation_clean=True, ws_restored=True, kill_switch_active=True, fully_wired=True
        )
        is True
    )
    assert safe_mode.active is False


# -- §10.4 consecutive loss cap ---------------------------------------------


def test_settlement_anchor_first_then_scores(env):
    db, *_ = env
    guards = _guards(env)
    # No anchor yet: the first settlement only establishes it.
    first = guards.record_settlement(wallet_balance=Decimal(1000), now=_NOW)
    assert first.anchored is True
    assert first.consecutive_loss_count == 0
    # A losing segment (wallet fell below the anchor) counts one loss.
    second = guards.record_settlement(wallet_balance=Decimal(990), now=_NOW + timedelta(hours=1))
    assert second.anchored is False
    assert second.is_loss is True
    assert second.segment_pnl == Decimal(-10)
    assert second.consecutive_loss_count == 1


def test_ensure_anchor_lets_first_settlement_score(env):
    db, *_ = env
    guards = _guards(env)
    guards.ensure_settlement_anchor(Decimal(1000), now=_NOW)
    # Now the first real settlement is scored against genesis.
    result = guards.record_settlement(wallet_balance=Decimal(950), now=_NOW + timedelta(hours=1))
    assert result.anchored is False
    assert result.is_loss is True
    assert result.consecutive_loss_count == 1


def test_gain_resets_consecutive_count(env):
    db, *_ = env
    guards = _guards(env)
    guards.ensure_settlement_anchor(Decimal(1000), now=_NOW)
    guards.record_settlement(wallet_balance=Decimal(990), now=_NOW + timedelta(hours=1))
    guards.record_settlement(wallet_balance=Decimal(980), now=_NOW + timedelta(hours=2))
    # A profitable segment (wallet rose) resets the streak to zero.
    gain = guards.record_settlement(wallet_balance=Decimal(1010), now=_NOW + timedelta(hours=3))
    assert gain.is_loss is False
    assert gain.consecutive_loss_count == 0


def test_three_consecutive_losses_enter_manual(env):
    db, gate, safe_mode, _clock = env
    guards = _guards(env)  # max_consecutive_loss_count = 3
    guards.ensure_settlement_anchor(Decimal(1000), now=_NOW)
    guards.record_settlement(wallet_balance=Decimal(990), now=_NOW + timedelta(hours=1))
    guards.record_settlement(wallet_balance=Decimal(980), now=_NOW + timedelta(hours=2))
    assert safe_mode.active is False
    third = guards.record_settlement(wallet_balance=Decimal(970), now=_NOW + timedelta(hours=3))
    assert third.consecutive_loss_count == 3
    assert third.entered_manual is True
    state = safe_mode.current()
    assert state is not None and state.reason == REASON_CONSECUTIVE_LOSS
    assert state.safe_mode_type == "manual"
    assert gate.manual_safe_mode is True


def test_consecutive_count_persists_across_manager_instances(env):
    db, gate, safe_mode, clock = env
    guards = _guards(env)
    guards.ensure_settlement_anchor(Decimal(1000), now=_NOW)
    guards.record_settlement(wallet_balance=Decimal(990), now=_NOW + timedelta(hours=1))
    # A fresh LossGuards over the same store sees the persisted count/anchor.
    fresh = LossGuards(db=db, run_id="r", safety=_safety(), safe_mode=safe_mode)
    result = fresh.record_settlement(wallet_balance=Decimal(985), now=_NOW + timedelta(hours=2))
    assert result.consecutive_loss_count == 2
