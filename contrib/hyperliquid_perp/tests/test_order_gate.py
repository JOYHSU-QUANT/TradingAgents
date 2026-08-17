"""Tests for the §4.1 real order gate (fail-closed condition matrix)."""

from __future__ import annotations

import pytest

from contrib.hyperliquid_perp.live.config import ExecutionMode, LiveConfig
from contrib.hyperliquid_perp.live.order_gate import (
    NO_ORDER_REASON,
    LiveOrderGateRejected,
    RealOrderGate,
)


def _open_gate(**overrides) -> RealOrderGate:
    """A gate with every §4.1 condition satisfied for BTC.

    Overrides go through the constructor — the config trio is pinned after
    construction, so a variant gate is built, never mutated into shape.
    """
    kwargs = {
        "allow_real_orders": True,
        "mode": ExecutionMode.TESTNET_LIVE,
        "allowed_symbols": ("BTC",),
        "agent_authorized": True,
        "startup_reconciliation_passed": True,
        "kill_switch_active": True,
        "state_reconciled": True,
        "risk_gate_approved": True,
    }
    kwargs.update(overrides)
    return RealOrderGate(**kwargs)


def test_fully_satisfied_gate_allows_the_order():
    gate = _open_gate()
    assert gate.check_order("BTC") is None
    gate.require_order("BTC")  # does not raise


def test_gate_is_fail_closed_from_config():
    config = LiveConfig.from_dict({"mode": "testnet_live", "network": "testnet"})
    gate = RealOrderGate.from_config(config)
    # Nothing proven yet: even a permissive config cannot place an order.
    assert gate.check_order("BTC") is not None
    with pytest.raises(LiveOrderGateRejected):
        gate.require_order("BTC")


# Conditions that must hold for ANY order to reach the wire — enforced by both
# check_order and check_new_target.
_WIRE_SCOPED = [
    ({"allow_real_orders": False}, "allow_real_orders"),
    ({"mode": ExecutionMode.PAPER}, "not a live mode"),
    ({"agent_authorized": False}, "agent authorization"),
    ({"startup_reconciliation_passed": False}, "startup reconciliation"),
    ({"kill_switch_active": False}, "kill switch"),
    ({"state_reconciled": False}, "not reconciled"),
    ({"manual_safe_mode": True}, "manual safe mode"),
]
# Conditions that gate the DECISION, not the order — check_new_target only.
_DECISION_SCOPED = [
    ({"risk_gate_approved": False}, "risk gate"),
    ({"unresolved_protection_failure": True}, "protection failure"),
    ({"active_slice_plan": True}, "slice plan"),
]


@pytest.mark.parametrize(("mutation", "expected"), _WIRE_SCOPED + _DECISION_SCOPED)
def test_each_failed_condition_rejects_a_new_target_with_its_own_reason(mutation, expected):
    gate = _open_gate(**mutation)
    reason = gate.check_new_target("BTC")
    assert reason is not None and expected in reason
    with pytest.raises(LiveOrderGateRejected) as excinfo:
        gate.require_new_target("BTC")
    assert NO_ORDER_REASON in str(excinfo.value)
    assert excinfo.value.reason == reason


@pytest.mark.parametrize(("mutation", "expected"), _WIRE_SCOPED)
def test_each_wire_scoped_condition_also_rejects_the_order_itself(mutation, expected):
    gate = _open_gate(**mutation)
    reason = gate.check_order("BTC")
    assert reason is not None and expected in reason
    with pytest.raises(LiveOrderGateRejected) as excinfo:
        gate.require_order("BTC")
    assert NO_ORDER_REASON in str(excinfo.value)
    assert excinfo.value.reason == reason


@pytest.mark.parametrize(("mutation", "expected"), _DECISION_SCOPED)
def test_decision_scoped_conditions_do_not_block_an_order_on_the_wire(mutation, expected):
    # The whole point of the split. An active slice plan blocks a NEW target
    # (§9.3: 不建立新的 entry / rebalance plan) but must not block that plan's own
    # slices, nor the SL repair and emergency close §9.3 explicitly ALLOWS — the
    # emergency close being the one order you most need while a plan is running.
    # Were these checked per-order, the engine would gate-reject its own sends.
    gate = _open_gate(**mutation)
    assert gate.check_order("BTC") is None
    gate.require_order("BTC")  # does not raise
    reason = gate.check_new_target("BTC")
    assert reason is not None and expected in reason


# The two safe-mode lines a PROTECTIVE order (SL / TP / §17.2 emergency close) is
# ADDITIONALLY exempt from — §13.1 keeps the run protecting in safe mode, and the
# SL repair that heals a §12.3 SL-missing safe mode must itself be sendable, or the
# clean reconciliation pass that would release it (it needs a resting SL) never runs.
_SAFE_MODE_LINES = [
    ({"state_reconciled": False}, "not reconciled"),
    ({"manual_safe_mode": True}, "manual safe mode"),
]


@pytest.mark.parametrize(("mutation", "expected"), _SAFE_MODE_LINES)
def test_protective_order_is_exempt_from_the_safe_mode_lines(mutation, expected):
    gate = _open_gate(**mutation)
    assert expected in gate.check_order("BTC")  # a risk-adding order is blocked
    assert gate.check_protective_order("BTC") is None  # protection stays sendable
    gate.require_protective_order("BTC")  # does not raise


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ({"kill_switch_active": False}, "kill switch"),
        ({"agent_authorized": False}, "agent authorization"),
        ({"startup_reconciliation_passed": False}, "startup reconciliation"),
        ({"allow_real_orders": False}, "allow_real_orders"),
    ],
)
def test_protective_order_still_bound_by_base_and_kill_switch(mutation, expected):
    # The exemption is from the safe-mode lines ONLY: a protective order is still
    # refused when the dead man's switch is down or a base precondition fails.
    gate = _open_gate(**mutation)
    reason = gate.check_protective_order("BTC")
    assert reason is not None and expected in reason
    with pytest.raises(LiveOrderGateRejected):
        gate.require_protective_order("BTC")


def test_symbol_outside_allowlist_is_rejected():
    gate = _open_gate()
    assert gate.check_order("BTC") is None
    reason = gate.check_order("ETH")
    assert reason is not None and "allowed_symbols" in reason


def test_exchange_action_check_is_the_base_subset():
    gate = _open_gate()
    # Safe-mode-ish state: §13.1 still allows cancels / kill switch refresh.
    gate.manual_safe_mode = True
    gate.kill_switch_active = False
    gate.state_reconciled = False
    assert gate.check_exchange_action() is None
    gate.require_exchange_action()  # does not raise
    # ...but the base trio still applies.
    gate.agent_authorized = False
    assert gate.check_exchange_action() is not None
    with pytest.raises(LiveOrderGateRejected):
        gate.require_exchange_action()


def test_exchange_action_enforces_allowed_symbols_when_one_is_named():
    # updateLeverage names a coin, so the base subset can check it — a signed
    # leverage change aimed at a coin this run never traded is refused.
    gate = _open_gate()
    assert gate.check_exchange_action("BTC") is None
    reason = gate.check_exchange_action("ETH")
    assert reason is not None and "allowed_symbols" in reason
    with pytest.raises(LiveOrderGateRejected):
        gate.require_exchange_action("ETH")


def test_exchange_action_without_a_symbol_skips_the_allowlist():
    # The account-wide actions (cancel, scheduleCancel, the §19.3 sweep) carry
    # no symbol. Omitting one must not be read as an empty/absent symbol and
    # refused — that would gate the cancels §13.1 explicitly allows. The
    # allowlist here holds a DIFFERENT coin, so a check that invented a symbol
    # from the gate's own config would fail this.
    gate = _open_gate(allowed_symbols=("ETH",))
    assert gate.check_exchange_action() is None
    gate.require_exchange_action()  # does not raise


def test_exchange_action_stays_out_of_the_safe_mode_lines():
    # Locks the 2026-08-17 decision (issue #28): updateLeverage remains in the
    # base subset — it must pass while a safe mode is active, because its only
    # caller is the §20.2 smoke suite, which runs before the §19.1 recovery
    # that proves state_reconciled. Containment is caller-side (no production
    # cycle calls it) plus the allowed_symbols line above. If a future change
    # moves it behind the safe-mode lines, the smoke gate becomes unsatisfiable
    # and this test says so.
    gate = _open_gate(state_reconciled=False, manual_safe_mode=True)
    assert gate.check_exchange_action("BTC") is None


def test_mainnet_live_counts_as_a_live_mode_per_spec():
    # §4.1 lists mainnet_live; config load rejects it long before a gate can
    # exist, but the gate must state the spec's rule, not re-legislate it.
    gate = _open_gate(mode=ExecutionMode.MAINNET_LIVE)
    assert gate.check_order("BTC") is None


def test_config_trio_is_pinned_at_construction():
    # The docstring's immutability promise is enforced: the config-derived
    # conditions cannot be reconfigured on a live gate, while the runtime
    # flags remain the mutable surface their owners flip.
    gate = _open_gate()
    for attr, value in (
        ("allow_real_orders", False),
        ("mode", ExecutionMode.PAPER),
        ("allowed_symbols", ("ETH",)),
    ):
        with pytest.raises(AttributeError, match="pinned at construction"):
            setattr(gate, attr, value)
    # Unchanged by the failed writes...
    assert gate.allow_real_orders is True
    assert gate.mode is ExecutionMode.TESTNET_LIVE
    assert gate.allowed_symbols == ("BTC",)
    # ...and the runtime flags still flip freely.
    gate.kill_switch_active = False
    assert gate.check_order("BTC") is not None


def test_allowed_symbols_must_be_a_tuple_not_a_bare_string():
    # `symbol not in self.allowed_symbols` degrades to a SUBSTRING match on a
    # str: a gate built with allowed_symbols="BTC" would admit "BT". from_config
    # always passes a tuple, but the class is public and hand-built by tests and
    # (soon) PR 4/5.
    with pytest.raises(TypeError, match="tuple"):
        RealOrderGate(
            allow_real_orders=True,
            mode=ExecutionMode.TESTNET_LIVE,
            allowed_symbols="BTC",  # type: ignore[arg-type]
        )
