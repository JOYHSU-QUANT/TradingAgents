"""Restart reconciliation (phase2-execution §1.2) — the nine-step boundary.

A process start over an existing run never resumes, catches up, or completes an
old plan; it draws a fresh decision boundary:

1. stop every remaining slice of the old plan(s)      → cancel in SQLite
2. mark the old plan(s) ``canceled_restart``          → :func:`reconcile_on_restart`
3. record every unexecuted quantity as residual       → ``residual_qty``
4. rebuild the actual position from committed fills   → :func:`accounting.replay`
5. post overdue funding + fetch a fresh snapshot      → pending-event backfill here;
                                                         the snapshot is the engine's
                                                         first tick
6. handle liquidation / emergency close / gap SL / TP → the engine's first tick,
                                                         armed via ``flag_restart_gap``
7. reconcile position / account / SL-TP / scheduler   → replay comparison + the
                                                         engine constructor's hydration
8. immediately start a new AI cycle                   → ``next_decision_at`` forced
                                                         to "now" when a plan died
9. ``next_decision_at = new decision_at + 4h``        → the scheduler's normal
                                                         completion path

Steps 1–5, 7 (the store side) and 8 live here; steps 5's snapshot, 6 and 9 are
owned by the engine tick and the scheduler, wired in this order by the CLI.

Funding backfill scope (PR3 decision, carried forward): only *pending* events —
whose settlement basis (size + mark) was captured when the hour elapsed — are
posted here, using the stored basis and a rate from the injected source. Hours
that elapsed while no process was running left no event row and no basis mark;
fabricating one (a candle close, the restart mark) would violate the
never-fabricate rule (execution §6.5), so those hours are logged as skipped and
stay unposted.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from ..persistence import repository as repo
from ..persistence.db import Database
from . import accounting
from .engine import FundingSource
from .scheduler import parse_instant

__all__ = [
    "ReconciliationError",
    "RestartReconciliation",
    "backfill_pending_funding",
    "reconcile_on_restart",
]

logger = logging.getLogger(__name__)


def backfill_pending_funding(
    db: Database,
    *,
    run_id: str,
    now: datetime,
    funding_source: FundingSource | None,
) -> tuple[int, int]:
    """Post every backfillable pending funding event; return ``(posted, still_pending)``.

    Runs at restart (step 5) AND at every cycle boundary (execution §6.5:
    a pending settlement is backfilled 稍後 by funding history — during the
    run, not restart-only). Each posting uses the *stored* settlement basis and
    ``record_funding``'s exactly-once transition, so repeated passes can never
    double-post; a rate still unavailable simply stays pending for the next pass.
    """
    posted = 0
    still_pending = 0
    for event in repo.iter_funding_events(db.conn, run_id, status="pending"):
        settlement = parse_instant(event["funding_timestamp"])
        rate = (
            funding_source.rate_at(event["symbol"], settlement)
            if funding_source is not None
            else None
        )
        if rate is None:
            still_pending += 1
            continue
        res = accounting.record_funding(
            db,
            run_id=run_id,
            mode=event["mode"],
            symbol=event["symbol"],
            funding_timestamp=settlement,
            position_size=Decimal(event["position_size"]),  # stored basis wins anyway
            funding_rate=rate,
            source="funding_history_backfill",
            recorded_at=now,
        )
        if res.status == "posted":
            posted += 1
    if still_pending:
        logger.warning(
            "funding backfill for %s left %d event(s) pending (rate still "
            "unavailable); they retry at the next cycle boundary or restart",
            run_id,
            still_pending,
        )
    return posted, still_pending


class ReconciliationError(RuntimeError):
    """The committed store contradicts its own materialized state.

    Raised when the accounting replay (execution §1.2 step 4 / spec §5) finds a
    mismatch: trading on a store that cannot rebuild its own position/ledger
    would compound the corruption, so startup must fail loud, not proceed.
    """


@dataclass(frozen=True)
class RestartReconciliation:
    """What one restart reconciliation did — the CLI's startup report."""

    canceled_plan_ids: tuple[str, ...]
    canceled_order_ids: tuple[str, ...]
    funding_posted: int
    funding_still_pending: int
    forced_immediate_cycle: bool

    def __post_init__(self) -> None:
        # Step 8 only fires for an abandoned target — a forced cycle with no
        # canceled plan would be a self-contradicting report the operator log
        # prints verbatim.
        if self.forced_immediate_cycle and not self.canceled_plan_ids:
            raise ValueError(
                "RestartReconciliation.forced_immediate_cycle requires a canceled plan"
            )
        if self.funding_posted < 0 or self.funding_still_pending < 0:
            raise ValueError("RestartReconciliation funding counters must be >= 0")

    @property
    def canceled_any_plan(self) -> bool:
        return bool(self.canceled_plan_ids)


def reconcile_on_restart(
    db: Database,
    *,
    run_id: str,
    now: datetime,
    funding_source: FundingSource | None = None,
) -> RestartReconciliation:
    """Run the store-side restart steps for ``run_id``; see the module docstring.

    Idempotent: a second call over the same store finds nothing live to cancel,
    nothing pending it can post twice (exactly-once via ``record_funding``), and
    only re-verifies the replay. The engine/scheduler must be constructed *after*
    this returns, so they hydrate from the reconciled state.
    """
    if repo.get_run(db.conn, run_id) is None:
        raise ValueError(f"run {run_id!r} does not exist; nothing to reconcile")

    # Steps 1–3: one transaction — plans to canceled_restart with their residual,
    # their still-live orders to canceled. A crash mid-way rolls back to a state
    # this function simply re-runs.
    canceled_plans: list[str] = []
    canceled_orders: list[str] = []
    with db.transaction() as conn:
        for plan in repo.iter_execution_plans(conn, run_id, statuses=repo.LIVE_PLAN_STATUSES):
            remaining = Decimal(plan["remaining_qty"]) if plan["remaining_qty"] else Decimal(0)
            repo.update_execution_plan(
                conn,
                plan["plan_id"],
                status="canceled_restart",
                remaining_qty=remaining,
                residual_qty=max(remaining, Decimal(0)),
                status_reason="restart",
                updated_at=now,
            )
            canceled_plans.append(plan["plan_id"])
        for order in repo.iter_orders(conn, run_id, statuses=repo.LIVE_ORDER_STATUSES):
            repo.update_order(
                conn,
                order["order_id"],
                status="canceled",
                status_reason="canceled_restart",
                updated_at=now,
            )
            canceled_orders.append(order["order_id"])

    # Step 5 (store side): post pending funding from the stored settlement basis.
    posted, still_pending = backfill_pending_funding(
        db, run_id=run_id, now=now, funding_source=funding_source
    )

    # Steps 4 + 7 (store side): the committed events must rebuild exactly the
    # materialized state — otherwise the store is corrupt and trading must stop.
    replayed = accounting.replay(db, run_id=run_id)
    if not replayed.is_consistent:
        raise ReconciliationError(
            f"run {run_id!r} failed accounting replay on restart: "
            f"position mismatches {list(replayed.position_mismatches)!r}, "
            f"account_matches={replayed.account_matches} — refusing to trade "
            "on a store that cannot rebuild its own state"
        )

    # Step 8: a canceled plan means the old target is abandoned — the next AI
    # cycle starts immediately (spec §3). An in-progress attempt IS that cycle
    # (it resumes on the first poll), so only force the clock when none exists.
    forced = False
    if canceled_plans and repo.find_in_progress_attempt(db.conn, run_id) is None:
        state = repo.get_scheduler_state(db.conn, run_id)
        raw_next = state["next_decision_at"] if state is not None else None
        if raw_next is None or parse_instant(raw_next) > now:
            with db.transaction() as conn:
                repo.upsert_scheduler_state(conn, run_id, next_decision_at=now, updated_at=now)
            forced = True

    if canceled_plans:
        logger.info(
            "restart reconciliation for %s: canceled plans %s, canceled orders %s",
            run_id,
            canceled_plans,
            canceled_orders,
        )
    return RestartReconciliation(
        canceled_plan_ids=tuple(canceled_plans),
        canceled_order_ids=tuple(canceled_orders),
        funding_posted=posted,
        funding_still_pending=still_pending,
        forced_immediate_cycle=forced,
    )
