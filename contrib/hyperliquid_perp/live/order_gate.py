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
  enabled, a live mode, agent authorized, plus ``allowed_symbols`` when the
  caller names one) and gates the other signed actions (cancel, scheduleCancel,
  updateLeverage). Smallest on purpose: §13.1 explicitly ALLOWS cancelling
  bot-owned orders and refreshing the kill switch in safe mode.

  Who passes a symbol is a DECISION, not a matter of who has one to pass.
  ``scheduleCancel`` genuinely has none (it arms the whole wallet), but the two
  cancels do take a coin and put it on the wire. They still pass ``None``,
  because the cancel entry point is SHARED and two of its callers source the
  coin from outside this run's config: the §19.3 startup sweep reads it from
  the local cloid registry, whose rows are not run-scoped and can predate the
  current ``allowed_symbols``, and the §18.2 shutdown sweep reads it from the
  exchange's own open-order response. Enforcing the allowlist on that entry
  point would turn exactly those orders into ours-but-uncancellable — §19.3
  records the failure into the reconciliation verdict (safe mode), §18.2
  refuses to disarm the wallet-wide backstop — so a safety check there would
  manufacture unclearable state. (The live SL/TP manager cancels too, and ITS
  coin is the configured symbol; it just shares the exempted entry point.)
  ``updateLeverage`` has one caller and one lineage — its coin IS this run's
  configured symbol — so it passes it (2026-08-17, issue #28).

  ``updateLeverage`` sits in this subset by a decision, not by §13.1's
  cancel-family wording. It is NOT de-risking — raising leverage magnifies an
  existing position — so the safe-mode exemption the cancel family earns does
  not transfer to it on merit. It stays here because of ONE path: its only
  caller is the §20.2 smoke suite (test 2), and while a full suite run does
  clear the safe-mode lines first (the pre-flight §19.1 recovery runs whenever
  an order-placing test is selected, and proves ``state_reconciled``), a
  ``--only update_leverage`` rerun after a failed test 2 does NOT run that
  pre-flight — and rerunning a failed test to overwrite its latest-per-key
  verdict is how the §20.2 gate is meant to be repaired. Behind the safe-mode
  lines that targeted rerun becomes impossible; a full-suite rerun would still
  repair the gate, so the cost is the narrow path, not the gate itself. The
  containment is on the caller side — no production cycle calls it,
  and config load hard-rejects any ``live.safety.leverage`` other than 1 —
  plus the ``allowed_symbols`` line above, which stops a signed leverage change
  aimed at any other coin. Should a production caller ever appear, that
  containment is gone and this belongs behind the safe-mode lines instead.

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
from typing import Literal

from ..persistence.cloid import LIVE_ORDER_ROLES
from .config import ExecutionMode, LiveConfig

__all__ = [
    "NO_ORDER_REASON",
    "PROTECTIVE_ORDER_ROLES",
    "LiveOrderGateRejected",
    "RealOrderGate",
]

# §4.1: the one no_order_reason vocabulary entry for a gate rejection.
NO_ORDER_REASON = "live_order_gate_rejected"

# The three §4.1 enforcement scopes (_first_failed's condition table). Literal —
# not bare str — so a mistyped scope tag on a future condition entry is a type
# error instead of a silently never-matched (and therefore never-skipped) check.
_ConditionScope = Literal["always", "decision", "safe_mode"]

# §13.1 / §17.2: the de-risking / protection order roles that stay sendable while
# a safe mode is active. Safe mode blocks anything that ADDS risk (§13.2), but the
# run keeps PROTECTING (§13.1) and the §17.2 emergency close is the one order most
# needed then — and the SL repair that heals a §12.3 SL-missing safe mode must
# itself be sendable, or the clean reconciliation pass that would release it (it
# needs a valid SL) can never happen. These roles are exempt from the two
# safe-mode gate lines only (state_reconciled / manual_safe_mode); the base wire
# preconditions and the armed kill switch still bind them.
PROTECTIVE_ORDER_ROLES = frozenset({"stop_loss", "take_profit", "emergency_close"})
# Literal copy of LIVE_ORDER_ROLES members: a role renamed there without this
# file would silently drop it from the safe-mode exemption — the one order most
# needed under safe mode would be gated. Fail at import.
if not PROTECTIVE_ORDER_ROLES <= LIVE_ORDER_ROLES:
    raise AssertionError("PROTECTIVE_ORDER_ROLES drifted from LIVE_ORDER_ROLES")

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

    def check_exchange_action(self, symbol: str | None) -> str | None:
        """The base preconditions for ANY signed mutation; None means allowed.

        ``symbol`` is required and has no default: passing ``None`` says "this
        action is account-wide" as a written decision rather than as an
        omission, so a future signed action cannot slip past the allowlist by
        forgetting an argument. When a symbol IS given it must be in
        ``allowed_symbols`` — a signed change aimed at a coin this run was never
        configured for is a wiring bug. Which callers pass one, and why the
        cancels deliberately do not despite having a coin, is in the module
        docstring.
        """
        if not self.allow_real_orders:
            return "allow_real_orders is false"
        if self.mode not in _LIVE_MODES:
            return f"mode {self.mode.value!r} is not a live mode"
        if not self.agent_authorized:
            return "agent authorization has not passed (§6.1)"
        if symbol is not None and symbol not in self.allowed_symbols:
            return f"symbol {symbol!r} is not in allowed_symbols"
        return None

    def _first_failed(
        self, symbol: str, *, new_target: bool, protective: bool = False
    ) -> str | None:
        """The FIRST failed §4.1 line, in the spec's own order.

        ONE ordered list drives every check, each condition tagged with the
        SCOPE that decides who it applies to, so the checks can never drift
        apart and all report the first failed line of §4.1 — stable for tests
        and log triage:

        - ``"always"`` — every order (the base wire preconditions).
        - ``"decision"`` — only a NEW-target admission; skipped per order (§9.3
          forbids new plans mid-plan but allows SL repair / emergency close).
        - ``"safe_mode"`` — every order EXCEPT a protection / de-risking one:
          §13.1 keeps the run protecting and §17.2's emergency close is the one
          order most needed in safe mode, so ``protective`` orders skip these.
        """
        # ``None``: the order paths carry the symbol through the ordered
        # condition table below, at §4.1's own position for it (after startup
        # reconciliation and the kill switch), so checking it here as well would
        # report the wrong first-failed line.
        base = self.check_exchange_action(None)
        if base is not None:
            return base
        # (scope, failed, reason) — §4.1's order, verbatim.
        conditions: tuple[tuple[_ConditionScope, bool, str], ...] = (
            (
                "always",
                not self.startup_reconciliation_passed,
                "startup reconciliation has not passed",
            ),
            ("always", not self.kill_switch_active, "kill switch is not active"),
            (
                "always",
                symbol not in self.allowed_symbols,
                f"symbol {symbol!r} is not in allowed_symbols",
            ),
            (
                "decision",
                not self.risk_gate_approved,
                "risk gate has not approved a target this cycle",
            ),
            ("safe_mode", not self.state_reconciled, "account/position state is not reconciled"),
            (
                "decision",
                self.unresolved_protection_failure,
                "an unresolved protection failure exists",
            ),
            ("decision", self.active_slice_plan, "an active slice plan exists (§9.3)"),
            ("safe_mode", self.manual_safe_mode, "manual safe mode is active"),
        )
        for scope, failed, reason in conditions:
            if scope == "decision" and not new_target:
                continue
            if scope == "safe_mode" and protective:
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

        A ``check_order`` blocked by a safe-mode line (state_reconciled /
        manual_safe_mode) is the RIGHT answer for a risk-adding order; a
        protection / de-risking order (SL, TP, §17.2 emergency close) asks
        :meth:`check_protective_order` instead, which is additionally exempt.
        """
        return self._first_failed(symbol, new_target=False)

    def check_protective_order(self, symbol: str) -> str | None:
        """May THIS protection / de-risking order go on the wire? None means yes.

        The SL / TP / emergency-close subset (:data:`PROTECTIVE_ORDER_ROLES`).
        Like :meth:`check_order` it drops the three decision-scoped conditions,
        and it ADDITIONALLY drops the two safe-mode lines (``state_reconciled``,
        ``manual_safe_mode``): §13.1 keeps the run protecting in safe mode and
        §17.2's emergency close is the one order you most need then — and the SL
        repair that heals a §12.3 ``position_sl_missing`` safe mode must itself be
        sendable, or the clean reconciliation pass that would release the safe
        mode (it requires a resting SL) can never happen and the position rides
        unprotected until a human intervenes. Still fully bound by the base wire
        preconditions AND the armed kill switch: a protective order is exempt from
        the safe-mode gate, never from the dead man's switch.
        """
        return self._first_failed(symbol, new_target=False, protective=True)

    def require_new_target(self, symbol: str) -> None:
        reason = self.check_new_target(symbol)
        if reason is not None:
            raise LiveOrderGateRejected(reason)

    def require_order(self, symbol: str) -> None:
        reason = self.check_order(symbol)
        if reason is not None:
            raise LiveOrderGateRejected(reason)

    def require_protective_order(self, symbol: str) -> None:
        reason = self.check_protective_order(symbol)
        if reason is not None:
            raise LiveOrderGateRejected(reason)

    def require_exchange_action(self, symbol: str | None) -> None:
        reason = self.check_exchange_action(symbol)
        if reason is not None:
            raise LiveOrderGateRejected(reason)
