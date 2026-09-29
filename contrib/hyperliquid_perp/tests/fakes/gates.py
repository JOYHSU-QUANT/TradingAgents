"""Builders for the §4.1 real-order gate, one per check ``RealOrderGate`` offers."""

from __future__ import annotations

from contrib.hyperliquid_perp.live.config import ExecutionMode
from contrib.hyperliquid_perp.live.order_gate import RealOrderGate

_PROTECTIVE = {"startup_reconciliation_passed": True, "kill_switch_active": True}
_ORDER = {**_PROTECTIVE, "state_reconciled": True}
_NEW_TARGET = {**_ORDER, "risk_gate_approved": True}


def exchange_action_gate(**conditions) -> RealOrderGate:
    """Without ``conditions``, passes ``check_exchange_action``."""
    kwargs = {
        "allow_real_orders": True,
        "mode": ExecutionMode.TESTNET_LIVE,
        "allowed_symbols": ("BTC",),
        "agent_authorized": True,
    }
    kwargs.update(conditions)
    return RealOrderGate(**kwargs)


def protective_order_gate() -> RealOrderGate:
    """Passes ``check_protective_order``."""
    return exchange_action_gate(**_PROTECTIVE)


def order_gate() -> RealOrderGate:
    """Passes ``check_order``."""
    return exchange_action_gate(**_ORDER)


def new_target_gate(**overrides) -> RealOrderGate:
    """Without ``overrides``, passes ``check_new_target``.

    Overrides go through the constructor — the config trio is pinned after
    construction, so a variant gate is built, never mutated into shape.
    """
    return exchange_action_gate(**{**_NEW_TARGET, **overrides})
