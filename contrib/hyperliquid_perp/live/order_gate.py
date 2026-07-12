"""The §4.1 real order gate — the one authority on "may we send this order?".

Every live exchange mutation flows through :class:`RealOrderGate`:

- :meth:`RealOrderGate.check_order` is the full §4.1 condition list and gates
  ORDER PLACEMENT (the spec's "送出 exchange order").
- :meth:`RealOrderGate.check_exchange_action` is the base subset (real orders
  enabled, a live mode, agent authorized) and gates the other signed actions
  (cancel, scheduleCancel). Deliberately smaller: §13.1 explicitly ALLOWS
  cancelling bot-owned orders and refreshing the kill switch in safe mode, so
  those actions must not be blocked by the per-order conditions.

The gate is fail-closed: every runtime condition a later startup step proves
(agent authorization, startup reconciliation, kill switch armed, state
reconciled) starts ``False``, so a gate that nobody wired up rejects
everything. PR 2 wires the conditions it owns (kill switch); PR 4/5 flip the
reconciliation / safe-mode / plan flags from their state machines.

A rejection is recorded by callers as ``order_created = false`` with
``no_order_reason = live_order_gate_rejected`` (§4.1); the specific failed
condition travels in :class:`LiveOrderGateRejected`.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import ExecutionMode, LiveConfig

__all__ = ["NO_ORDER_REASON", "LiveOrderGateRejected", "RealOrderGate"]

# §4.1: the one no_order_reason vocabulary entry for a gate rejection.
NO_ORDER_REASON = "live_order_gate_rejected"

# The §3 modes in which real orders can exist at all. mainnet_live is included
# because §4.1 lists it — config load already rejects it (§22), so its presence
# here can never open a path; omitting it would misstate the spec's rule.
_LIVE_MODES = frozenset(
    {ExecutionMode.TESTNET_LIVE, ExecutionMode.MAINNET_TINY, ExecutionMode.MAINNET_LIVE}
)


class LiveOrderGateRejected(Exception):
    """A live exchange action was refused; ``reason`` names the failed condition."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"{NO_ORDER_REASON}: {reason}")
        self.reason = reason


@dataclass
class RealOrderGate:
    """Mutable gate state; construction pins the config-derived conditions.

    The config trio (``allow_real_orders`` / ``mode`` / ``allowed_symbols``)
    is immutable for the process lifetime; the rest are runtime flags flipped
    by the components that own them (kill switch manager, PR 4 reconciliation
    and safe-mode machine, PR 5 engine). Flag writers need hold no invariant
    beyond "set it when proven, clear it when lost" — the gate re-evaluates
    the whole condition list on every check, so there is no ordering hazard.
    """

    allow_real_orders: bool
    mode: ExecutionMode
    allowed_symbols: tuple[str, ...]
    # Proven-at-startup conditions (§6.1 / §19.1) — fail-closed.
    agent_authorized: bool = False
    startup_reconciliation_passed: bool = False
    # Live-loop conditions — fail-closed.
    kill_switch_active: bool = False
    state_reconciled: bool = False
    # "no X" conditions: True means the blocking condition EXISTS. They start
    # False (a fresh process has no protection failure / plan / manual safe
    # mode of its own); PR 4's startup recovery sets them from the persisted
    # state before the first order can pass the fail-closed flags above.
    unresolved_protection_failure: bool = False
    active_slice_plan: bool = False
    manual_safe_mode: bool = False
    # Set per decision cycle by the engine (PR 5): the §4.1 "risk gate
    # approved target" condition. The engine clears it after the cycle so a
    # stale approval from cycle N can never authorize an order in cycle N+1.
    risk_gate_approved: bool = False

    @classmethod
    def from_config(cls, config: LiveConfig) -> RealOrderGate:
        return cls(
            allow_real_orders=config.allow_real_orders,
            mode=config.mode,
            allowed_symbols=config.safety.allowed_symbols,
        )

    def check_exchange_action(self) -> str | None:
        """The base preconditions for ANY signed mutation; None means allowed."""
        if not self.allow_real_orders:
            return "allow_real_orders is false"
        if self.mode not in _LIVE_MODES:
            return f"mode {self.mode.value!r} is not a live mode"
        if not self.agent_authorized:
            return "agent authorization has not passed (§6.1)"
        return None

    def check_order(self, symbol: str) -> str | None:
        """The full §4.1 list for order placement; None means allowed.

        Conditions are checked in the spec's order so the reported reason is
        the FIRST failed line of §4.1 — stable for tests and log triage.
        """
        base = self.check_exchange_action()
        if base is not None:
            return base
        if not self.startup_reconciliation_passed:
            return "startup reconciliation has not passed"
        if not self.kill_switch_active:
            return "kill switch is not active"
        if symbol not in self.allowed_symbols:
            return f"symbol {symbol!r} is not in allowed_symbols"
        if not self.risk_gate_approved:
            return "risk gate has not approved a target this cycle"
        if not self.state_reconciled:
            return "account/position state is not reconciled"
        if self.unresolved_protection_failure:
            return "an unresolved protection failure exists"
        if self.active_slice_plan:
            return "an active slice plan exists (§9.3)"
        if self.manual_safe_mode:
            return "manual safe mode is active"
        return None

    def require_order(self, symbol: str) -> None:
        reason = self.check_order(symbol)
        if reason is not None:
            raise LiveOrderGateRejected(reason)

    def require_exchange_action(self) -> None:
        reason = self.check_exchange_action()
        if reason is not None:
            raise LiveOrderGateRejected(reason)
