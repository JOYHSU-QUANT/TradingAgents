"""The §4.1 real order gate — the one authority on "may we send this order?".

Every live exchange mutation flows through :class:`RealOrderGate`, at one of
three widths — because §4.1's conditions are not all about the same question:

- :meth:`RealOrderGate.check_new_target` is the FULL §4.1 condition list and
  gates the DECISION: may we act on a new AI target at all? PR 5's engine asks
  it once per cycle, before it builds a plan.
- :meth:`RealOrderGate.check_order` is the subset that must hold for every
  order actually put on the wire (the spec's "送出 exchange order"), and is
  what the submitter checks on every send. It omits the three decision-scoped
  conditions (risk gate approved this cycle, an active slice plan, an
  unresolved protection failure) — §9.3 forbids new entry/rebalance PLANS
  while a plan runs but explicitly ALLOWS SL repair and emergency close, so
  those conditions belong to the decision, not to each order. Applying them
  per-order would have the engine block its own slices and its own emergency
  close.
- :meth:`RealOrderGate.check_exchange_action` is the base subset (real orders
  enabled, a live mode, agent authorized) and gates the other signed actions
  (cancel, scheduleCancel). Smallest on purpose: §13.1 explicitly ALLOWS
  cancelling bot-owned orders and refreshing the kill switch in safe mode.

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

# The config-derived conditions pinned at construction; reassigning one is a
# caller bug (the runtime flags are the mutable surface), so the gate enforces
# its docstring's immutability promise instead of trusting callers to read it.
_PINNED_FIELDS = frozenset({"allow_real_orders", "mode", "allowed_symbols"})


class LiveOrderGateRejected(Exception):
    """A live exchange action was refused; ``reason`` names the failed condition."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"{NO_ORDER_REASON}: {reason}")
        self.reason = reason


@dataclass
class RealOrderGate:
    """Mutable gate state; construction pins the config-derived conditions.

    The config trio (``allow_real_orders`` / ``mode`` / ``allowed_symbols``)
    is immutable for the process lifetime — enforced: reassigning one raises
    ``AttributeError``. The rest are runtime flags flipped by the components
    that own them (kill switch manager, PR 4 reconciliation and safe-mode
    machine, PR 5 engine). Flag writers need hold no invariant beyond "set it
    when proven, clear it when lost" — the gate re-evaluates the whole
    condition list on every check, so there is no ordering hazard.
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

    def __post_init__(self) -> None:
        # allowed_symbols is membership-tested with `in`, which SILENTLY
        # degrades to a substring match on a bare str: a gate built with
        # allowed_symbols="BTC" would admit symbol "BT". from_config always
        # passes a tuple, but the class is public and hand-built in tests and
        # (soon) by PR 4/5 — so the container type is enforced, not assumed.
        if not isinstance(self.allowed_symbols, tuple):
            raise TypeError(
                "RealOrderGate.allowed_symbols must be a tuple of symbols, got "
                f"{type(self.allowed_symbols).__name__} — a bare string would make "
                "the membership test a substring match"
            )

    def __setattr__(self, name: str, value: object) -> None:
        # First assignment (dataclass __init__) passes; any rebind of a
        # config-derived condition on a live gate fails loud.
        if name in _PINNED_FIELDS and name in self.__dict__:
            raise AttributeError(
                f"RealOrderGate.{name} is pinned at construction; "
                "build a new gate instead of reconfiguring a live one"
            )
        super().__setattr__(name, value)

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

    def _first_failed(self, symbol: str, *, new_target: bool) -> str | None:
        """The FIRST failed §4.1 line, in the spec's own order.

        ONE ordered list drives both checks, each condition tagged with its
        scope, so the two can never drift apart and both always report the
        first failed line of §4.1 — stable for tests and log triage.
        """
        base = self.check_exchange_action()
        if base is not None:
            return base
        # (decision_scoped, failed, reason) — §4.1's order, verbatim.
        conditions: tuple[tuple[bool, bool, str], ...] = (
            (
                False,
                not self.startup_reconciliation_passed,
                "startup reconciliation has not passed",
            ),
            (False, not self.kill_switch_active, "kill switch is not active"),
            (
                False,
                symbol not in self.allowed_symbols,
                f"symbol {symbol!r} is not in allowed_symbols",
            ),
            (True, not self.risk_gate_approved, "risk gate has not approved a target this cycle"),
            (False, not self.state_reconciled, "account/position state is not reconciled"),
            (True, self.unresolved_protection_failure, "an unresolved protection failure exists"),
            (True, self.active_slice_plan, "an active slice plan exists (§9.3)"),
            (False, self.manual_safe_mode, "manual safe mode is active"),
        )
        for decision_scoped, failed, reason in conditions:
            if decision_scoped and not new_target:
                continue
            if failed:
                return reason
        return None

    def check_new_target(self, symbol: str) -> str | None:
        """The FULL §4.1 list: may we act on a NEW target? None means yes.

        The decision-admission question, asked ONCE per decision cycle by the
        engine (PR 5) before it commits to a plan. It carries the three
        conditions that are about the CYCLE rather than about a given order:
        ``risk_gate_approved`` (the engine clears it after each cycle),
        ``active_slice_plan`` and ``unresolved_protection_failure``.
        """
        return self._first_failed(symbol, new_target=True)

    def check_order(self, symbol: str) -> str | None:
        """May THIS order go on the wire? None means yes.

        The subset of §4.1 that must hold for EVERY order the system sends;
        :class:`~contrib.hyperliquid_perp.live.orders.LiveOrderSubmitter` asks
        it on every submit.

        Deliberately WITHOUT the three decision-scoped conditions. §9.3 says an
        active slice plan forbids ``不建立新的 entry / rebalance plan`` while
        explicitly ALLOWING ``SL repair`` and ``emergency close`` — and a plan
        runs 60 minutes across many decision cycles. Were the full list applied
        per order, the engine setting ``active_slice_plan`` would block its own
        slices AND the two order kinds §9.3 promises to allow, the emergency
        close being the one order you most need while a plan is running. The
        same reasoning covers ``unresolved_protection_failure`` (the SL repair
        that resolves it must itself be sendable) and ``risk_gate_approved`` (a
        per-cycle approval cannot gate an hour-long plan's slices; PR 5 would
        have to either lie to the gate or bypass the submitter).

        Those conditions are not dropped — they moved to
        :meth:`check_new_target`, asked before the plan is built at all.
        """
        return self._first_failed(symbol, new_target=False)

    def require_new_target(self, symbol: str) -> None:
        reason = self.check_new_target(symbol)
        if reason is not None:
            raise LiveOrderGateRejected(reason)

    def require_order(self, symbol: str) -> None:
        reason = self.check_order(symbol)
        if reason is not None:
            raise LiveOrderGateRejected(reason)

    def require_exchange_action(self) -> None:
        reason = self.check_exchange_action()
        if reason is not None:
            raise LiveOrderGateRejected(reason)
