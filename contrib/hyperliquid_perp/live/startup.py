"""§19 startup / restart recovery: never trade before the books are proven.

Runs steps 5–16 of the §19.1 startup sequence over components the CLI has
already built and verified (steps 1–4: config gates, SQLite, §6.1 agent
authorization, exchange client — the PR 1 startup skeleton):

```
 5. Arm kill switch                       → KillSwitchManager.arm()
 6–10. Read exchange orders/account/      → inside the reconciler's sweep
       positions/fills/funding              (funding backfill: PR 3 owns the
                                             pending-events rule; live funding
                                             events land with PR 5's loop)
11. Reconcile SQLite against exchange     → LiveReconciler.run("startup")
12. Cancel stale bot-owned orders         → the §19.3 sweep below
13. Check active position protection      → the reconciler's SL leg
14. Repair missing SL if needed           → PR 5's protection manager; until it
                                            lands, an unprotected position keeps
                                            the pass unclean (fail-safe)
15. Enter safe mode if reconciliation     → reconcile_and_apply("startup")
    fails
16. Only after reconciliation passes,     → gate.startup_reconciliation_passed
    allow new AI cycle
```

The recovery runs TWO reconciliation passes around the §19.3 cancels: the
first records and mechanically fixes what it can (settle stuck rows, back-fill
orphans, book missing fills); the cancels then retire the stale orders; the
second pass is the VERDICT over the post-cancel state — proving the state the
run will actually trade from, not the one it found.

§19.3 per-role handling of bot-owned open orders found at startup:

- ``entry`` / ``rebalance``  → cancel (a stale intention from a dead plan);
- ``close`` / ``emergency_close`` / ``cleanup_cancel`` → inspect before
  cancel: kept while a reduce-only order still acts against an existing
  exchange position in the closing direction — else stale. Size is
  deliberately NOT judged (decided 2026-07-17): a position shrunk during
  downtime (ADL, a manual partial close) leaves the resting close order's
  remaining size above the position, and cancelling the ONLY closing order
  over that — with no PR 4 re-placement path — would leave the position
  open; reduce-only means the exchange clamps the excess;
- ``stop_loss`` / ``take_profit`` → validated structurally (reduce-only,
  closing side, SL trigger — size coverage is the reconciler SL leg's
  aggregate job), never cancelled here: PR 4 has no repair path, and dropping
  an imperfect SL would trade protection for tidiness. An insufficient SL
  keeps the position "unprotected" and the run in safe mode until PR 5;
- non-bot-owned → untouched (§25); the reconciler's case row already routes
  it to manual safe mode.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from ..exchanges.hyperliquid.mapper import hl_closing_side, map_account_snapshot
from ..paper.clock import Clock, WallClock
from ..persistence import repository as repo
from ..persistence.cloid import LIVE_ORDER_ROLES
from ..persistence.db import Database
from .cancel import cancel_bot_order_with_evidence
from .kill_switch import KillSwitchManager
from .order_gate import RealOrderGate
from .reconcile import LiveReconciler, ReconciliationReport
from .safe_mode import REASON_STALE_ORDER_SWEEP_FAILED, SafeModeManager

__all__ = ["StartupResult", "run_startup_recovery"]

logger = logging.getLogger(__name__)

# §19.3 role classification. Every LIVE_ORDER_ROLES member appears in exactly
# one bucket — the partition check keeps a future role from silently falling
# through the sweep unhandled.
_CANCEL_ROLES = frozenset({"entry", "rebalance"})
_INSPECT_ROLES = frozenset({"close", "emergency_close", "cleanup_cancel"})
_PROTECTION_ROLES = frozenset({"stop_loss", "take_profit"})
if _CANCEL_ROLES | _INSPECT_ROLES | _PROTECTION_ROLES != LIVE_ORDER_ROLES:
    raise AssertionError(
        "the §19.3 startup sweep's role buckets no longer partition "
        f"LIVE_ORDER_ROLES: {sorted(LIVE_ORDER_ROLES)}"
    )
# The same discipline for the role → order_type vocabulary: the reconciler's
# orphan back-fill derives order_type via ``repo.ROLE_TO_ORDER_TYPE.get(role,
# "ioc_limit")``, so a new protection role added to the buckets above but not
# to that mapping would silently label a backfilled trigger order "ioc_limit".
if set(repo.ROLE_TO_ORDER_TYPE) != _PROTECTION_ROLES:
    raise AssertionError(
        "repo.ROLE_TO_ORDER_TYPE keys no longer mirror the §19.3 protection "
        f"roles: {sorted(repo.ROLE_TO_ORDER_TYPE)} vs {sorted(_PROTECTION_ROLES)}"
    )


@dataclass(frozen=True)
class StartupResult:
    """What the §19 recovery concluded — the CLI prints it, PR 5's loop gates on it."""

    report: ReconciliationReport  # the verdict pass
    safe_mode_active: bool  # any safe mode still latched after the verdict pass
    canceled_stale: tuple[str, ...] = ()  # oids cancelled by the §19.3 sweep
    kept_orders: tuple[str, ...] = ()  # bot-owned orders deliberately left resting
    sweep_failures: tuple[str, ...] = ()  # per-order cancel failures (sweep continued)

    @property
    def passed(self) -> bool:
        """Step 16: may a new AI cycle start? Derived, never stored — a free
        boolean could drift from the facts it summarizes when a future caller
        (PR 5's loop) constructs this type directly."""
        return self.report.clean and not self.sweep_failures and not self.safe_mode_active


def run_startup_recovery(
    *,
    db: Database,
    run_id: str,
    client: Any,  # HyperliquidSignedClient (duck-typed: open_orders/cancel_by_cloid)
    fetch_clearinghouse: Callable[[], Any],
    gate: RealOrderGate,
    kill_switch: KillSwitchManager,
    reconciler: LiveReconciler,
    safe_mode: SafeModeManager,
    payload_dir: Path,
    clock: Clock | None = None,
) -> StartupResult:
    """Steps 5–16 of §19.1. Returns the verdict; arming failures propagate.

    A kill-switch arming failure is a HARD error (§18.2 rule 1: no live loop
    without the dead man's switch), so it raises. Everything after arming is
    fail-safe by construction: per-order cancel failures are recorded and the
    sweep continues, and any unproven leg leaves the run in safe mode rather
    than aborting the process — safe mode still monitors and protects (§13.1).
    """
    clock = clock or WallClock()

    # Step 0 (§13.6 rule 1): a restart must not forget a persisted safe mode.
    persisted = safe_mode.hydrate_gate()
    if persisted is not None:
        logger.warning(
            "startup: run %s is already in %s safe mode", run_id, persisted.safe_mode_type
        )

    # Step 5: arm the dead man's switch before anything else can rest orders.
    kill_switch.arm()

    # Steps 6–11: the first sweep — reads, records, and the safe mechanical
    # fixes (settle stuck rows, back-fill orphans, book missing fills).
    reconciler.run("startup")

    # The recovery is this manager's ONLY owner until the CLI's shutdown, and
    # its legs are network round-trips that can stretch past the scheduled
    # deadline on a degraded link — tick between every leg (and per order in
    # the sweep below), or the constructor's max-tick-gap promise is never
    # kept and the exchange fires the wallet-wide cancel MID-recovery.
    kill_switch.tick()

    # Step 12: the §19.3 stale-order sweep over the CURRENT exchange view.
    canceled, kept, failures = _sweep_stale_orders(
        db=db,
        run_id=run_id,
        client=client,
        fetch_clearinghouse=fetch_clearinghouse,
        payload_dir=payload_dir,
        clock=clock,
        tick=kill_switch.tick,
    )
    kill_switch.tick()

    # Steps 13–15: the verdict pass over the post-cancel state. The ticks
    # above also ran _detect_expired_deadline, so a switch that fired during
    # the recovery has latched stop_new_orders by the time it is read here —
    # a lapsed deadline can never attest "kill switch healthy" to §13.4. No
    # WS stream exists in this wiring (the live loop is PR 5), so ws_restored
    # is vacuously true.
    report = reconciler.reconcile_and_apply(
        "startup",
        safe_mode=safe_mode,
        ws_restored=True,
        kill_switch_active=not kill_switch.stop_new_orders,
    )
    if failures:
        # A cancel the sweep could not land leaves a stale order resting —
        # the verdict must not read clean over it. Its orders row is still
        # live locally, so every later pass re-checks it; recoverable safe
        # mode is the same fail-safe direction as any other mismatch.
        safe_mode.enter(
            "recoverable",
            REASON_STALE_ORDER_SWEEP_FAILED,
            detail="; ".join(failures),
        )

    # Step 16: only a clean verdict with no live safe mode admits a new cycle.
    result = StartupResult(
        report=report,
        safe_mode_active=safe_mode.active,
        canceled_stale=tuple(canceled),
        kept_orders=tuple(kept),
        sweep_failures=tuple(failures),
    )
    gate.startup_reconciliation_passed = result.passed
    if result.passed:
        logger.info("startup reconciliation passed — new AI cycles are allowed (§19.1 step 16)")
    else:
        logger.error(
            "startup reconciliation DID NOT pass — the run stays in safe mode; "
            "no new AI cycle may start (§19.1 step 15)"
        )
    return result


def _sweep_stale_orders(
    *,
    db: Database,
    run_id: str,
    client: Any,
    fetch_clearinghouse: Callable[[], Any],
    payload_dir: Path,
    clock: Clock,
    tick: Callable[[], None] = lambda: None,
) -> tuple[list[str], list[str], list[str]]:
    """§19.3: classify every bot-owned open order; cancel the stale ones.

    Returns ``(canceled oids, kept oids, failure descriptions)``. Enumeration
    failure is one failure entry (the verdict pass will also fail its orders
    leg); per-order failures never stop the sweep — the same discipline as the
    §18.2 shutdown sweep. ``tick`` (the kill-switch tick) runs once per order:
    each cancel is a network round-trip, and a long sweep must keep the dead
    man's switch refreshed or it fires mid-sweep.
    """
    canceled: list[str] = []
    kept: list[str] = []
    failures: list[str] = []
    try:
        raw_orders = client.open_orders()
        if not isinstance(raw_orders, list):
            raise TypeError(f"open_orders returned {type(raw_orders).__name__}, expected a list")
    except Exception as exc:  # noqa: BLE001 — the sweep must not kill startup
        # Logged at the point of capture (traceback included): the failures
        # channel carries only the message into safe-mode detail / CLI stderr,
        # and a log-watcher needs the origin — same discipline as the §18.2
        # shutdown sweep's enumeration failure.
        logger.exception("startup sweep: open-orders enumeration failed")
        failures.append(f"stale-order sweep could not enumerate open orders: {exc}")
        return canceled, kept, failures

    positions = _exchange_positions(fetch_clearinghouse, failures)

    for order in raw_orders:
        tick()
        if not isinstance(order, dict):
            failures.append(f"malformed open_orders entry ({type(order).__name__})")
            continue
        oid = str(order.get("oid", "?"))
        cloid = order.get("cloid")
        registry = repo.get_cloid_by_hex(db.conn, cloid) if isinstance(cloid, str) else None
        if registry is None or not isinstance(cloid, str):
            continue  # non-bot-owned: never managed (§25); the reconciler routed it already
        role = registry["order_role"]
        coin = registry["symbol"]
        label = f"{oid} ({role})"
        try:
            if role in _CANCEL_ROLES:
                _cancel(db, client, run_id, payload_dir, clock, coin, cloid, registry)
                canceled.append(oid)
                logger.info("startup sweep: canceled stale %s order %s", role, oid)
            elif role in _INSPECT_ROLES:
                if _close_order_still_applies(order, positions):
                    kept.append(oid)
                    logger.info(
                        "startup sweep: kept %s order %s — it still closes the live position",
                        role,
                        oid,
                    )
                else:
                    _cancel(db, client, run_id, payload_dir, clock, coin, cloid, registry)
                    canceled.append(oid)
                    logger.info(
                        "startup sweep: canceled stale %s order %s — no position it closes",
                        role,
                        oid,
                    )
            elif role in _PROTECTION_ROLES:
                # §19.3: validate, NEVER cancel here — PR 4 cannot recreate
                # protection, and an imperfect SL beats none. The reconciler's
                # SL leg is the authority on whether protection is sufficient.
                kept.append(oid)
                if not _protection_order_valid(order, positions, role):
                    # Two causes, two truthful messages: with the positions
                    # read failed the order was never actually examined, and a
                    # log asserting "does not validate" would tell a
                    # post-mortem reader protection had lapsed when the truth
                    # is "could not look" (the read failure itself is already
                    # in ``failures`` → safe mode).
                    cause = (
                        "could not be validated — the positions read failed"
                        if positions is None
                        else "does not validate against the live position "
                        "(reduce-only/side/trigger) — repair is PR 5"
                    )
                    logger.warning("startup sweep: %s order %s %s; kept", role, oid, cause)
            else:  # pragma: no cover — the module-level partition check guards this
                failures.append(f"{label}: unknown order role")
        except Exception as exc:  # noqa: BLE001 — per-order failures never stop the sweep
            logger.warning("startup sweep: cancel of %s failed: %s", label, exc)
            failures.append(f"{label}: {type(exc).__name__}: {exc}")
    return canceled, kept, failures


def _cancel(
    db: Database,
    client: Any,
    run_id: str,
    payload_dir: Path,
    clock: Clock,
    coin: str,
    cloid_hex: str,
    registry: Any,
) -> None:
    cancel_bot_order_with_evidence(
        db=db,
        client=client,
        run_id=run_id,
        payload_dir=payload_dir,
        clock=clock,
        coin=coin,
        cloid_hex=cloid_hex,
        cloid_logical=registry["cloid_logical"],
        cancel_reason="stale_startup_cancel",
    )


def _exchange_positions(
    fetch_clearinghouse: Callable[[], Any], failures: list[str]
) -> dict[str, Decimal] | None:
    """coin → signed size for the sweep's inspect/validate steps; None = unproven.

    ``None`` (a failed read) is deliberately distinct from ``{}`` (a proven-flat
    account): the inspect step CANCELS orders based on this answer, and a read
    failure treated as "flat" would cancel a resting close/emergency-close that
    still covers a real position — an unprovable state must never drive a
    state-changing action (the same rule the reconciler's docstring states).
    """
    try:
        snapshot = map_account_snapshot(fetch_clearinghouse())
        return {p.coin: p.size for p in snapshot.positions}
    except Exception as exc:  # noqa: BLE001
        # Log-at-origin, same as the enumeration failure above: the string in
        # ``failures`` loses the traceback a post-mortem needs.
        logger.exception("startup sweep: positions read failed")
        failures.append(f"stale-order sweep could not read positions: {exc}")
        return None


def _close_order_still_applies(order: dict, positions: dict[str, Decimal] | None) -> bool:
    """A reduce-only close order is live-relevant iff it still closes something.

    Kept when a position exists in the order's coin, the order is reduce-only,
    and it is on the CLOSING side — anything else is a leftover from a
    position that has since changed. Size is deliberately not compared
    (decided 2026-07-17): an order whose remaining size exceeds the position
    (it shrank during downtime — ADL, a manual partial close) still embodies
    the live closing intent, reduce-only means the exchange clamps the
    excess, and cancelling what may be the ONLY closing order — PR 4 has no
    re-placement path — would leave the position open until manual action.
    ``positions is None`` (the read failed) keeps the order: "unknown" is not
    "flat", and keeping is the reversible direction — the already-recorded
    failure holds the run in safe mode until a later pass can prove the
    verdict.
    """
    if positions is None:
        return True
    coin = order.get("coin")
    size = positions.get(coin, Decimal(0)) if isinstance(coin, str) else Decimal(0)
    if size == 0 or not bool(order.get("reduceOnly", False)):
        return False
    return order.get("side") == hl_closing_side(size)


def _protection_order_valid(order: dict, positions: dict[str, Decimal] | None, role: str) -> bool:
    """§19.3 structural validation for a resting SL/TP — per-order shape only.

    Checks what a SINGLE order can prove about itself: reduce-only, the
    closing side of the live position, and — for a ``stop_loss`` — a real
    trigger. SIZE coverage is deliberately not judged here: protection may be
    legitimately split across legs (two SLs jointly covering the position)
    and a take_profit may cover a fraction by design (a scale-out), so a
    per-order size test misreads both. The one size authority is the
    reconciler's SL leg (``LiveReconciler._has_valid_sl``), which SUMS
    closing-side reduce-only stop_loss coverage and holds the run in safe
    mode when it falls short.

    Validation only (the sweep never cancels protection); a failed positions
    read (``None``) reports invalid — the same "cannot prove" reading the
    reconciler's SL leg takes, and it only drives a log line here.
    """
    coin = order.get("coin")
    if positions is None:
        return False
    size = positions.get(coin, Decimal(0)) if isinstance(coin, str) else Decimal(0)
    if size == 0:
        return False  # protection with no position: §17.1 rule 4 territory (PR 5 cleans up)
    if not bool(order.get("reduceOnly", False)):
        return False
    if order.get("side") != hl_closing_side(size):
        return False
    # §19.3's "and trigger": a stop_loss-role order that is actually a plain
    # resting limit (no trigger) protects nothing at the trigger level.
    # frontendOpenOrders carries isTrigger/triggerPx on trigger orders. A
    # take_profit resting as a plain reduce-only limit is a legitimate shape.
    return (
        role != "stop_loss"
        or bool(order.get("isTrigger", False))
        or order.get("triggerPx") is not None
    )
