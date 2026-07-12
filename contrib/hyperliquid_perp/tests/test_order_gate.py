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


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ({"allow_real_orders": False}, "allow_real_orders"),
        ({"mode": ExecutionMode.PAPER}, "not a live mode"),
        ({"agent_authorized": False}, "agent authorization"),
        ({"startup_reconciliation_passed": False}, "startup reconciliation"),
        ({"kill_switch_active": False}, "kill switch"),
        ({"risk_gate_approved": False}, "risk gate"),
        ({"state_reconciled": False}, "not reconciled"),
        ({"unresolved_protection_failure": True}, "protection failure"),
        ({"active_slice_plan": True}, "slice plan"),
        ({"manual_safe_mode": True}, "manual safe mode"),
    ],
)
def test_each_failed_condition_rejects_with_its_own_reason(mutation, expected):
    gate = _open_gate(**mutation)
    reason = gate.check_order("BTC")
    assert reason is not None and expected in reason
    with pytest.raises(LiveOrderGateRejected) as excinfo:
        gate.require_order("BTC")
    assert NO_ORDER_REASON in str(excinfo.value)
    assert excinfo.value.reason == reason


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
