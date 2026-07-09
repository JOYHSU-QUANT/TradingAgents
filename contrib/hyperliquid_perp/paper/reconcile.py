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
from datetime import datetime, timedelta
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

# A pending funding event still unresolved this long after its settlement hour is
# almost certainly stuck, not merely un-published: an hourly rate normally posts
# within the hour, and the history source's window widens to cover any age. Past
# this age the log escalates to ERROR so an operator can tell "the rate for a
# settled hour will likely never resolve" (delisted coin, a gap the exchange never
# published) apart from the ordinary "not settled yet" warning it otherwise mimics
# — its funding P&L is silently omitted from the totals until it does resolve.
_STALE_PENDING_FUNDING = timedelta(hours=6)


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
    stale_pending = 0
    for event in repo.iter_funding_events(db.conn, run_id, status="pending"):
        settlement = parse_instant(event["funding_timestamp"])
        rate = (
            funding_source.rate_at(event["symbol"], settlement)
            if funding_source is not None
            else None
        )
        if rate is None:
            still_pending += 1
            if now - settlement >= _STALE_PENDING_FUNDING:
                stale_pending += 1
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
    # A stuck-forever pending event (its rate never resolves) resets the fetch
    # source's own consecutive-failure counter on every successful fetch, so only
    # this age check can distinguish it from a rate that is merely un-published yet.
    if stale_pending:
        logger.error(
            "funding backfill for %s left %d event(s) pending past %s after their "
            "settlement hour (rate for a settled hour likely never resolves — "
            "delisted coin or an hour the exchange never published; that funding "
            "P&L stays uncounted)",
            run_id,
            stale_pending,
            _STALE_PENDING_FUNDING,
        )
    young_pending = still_pending - stale_pending
    if young_pending:
        logger.warning(
            "funding backfill for %s left %d event(s) pending (rate still "
            "unavailable); they retry at the next cycle boundary or restart",
            run_id,
            young_pending,
        )
    return posted, still_pending


class ReconciliationError(RuntimeError):
    """The committed store contradicts its own materialized state.

    Raised when the accounting replay (execution §1.2 step 4 / spec §5) finds a
    mismatch AND the run is flat: trading on a store that cannot rebuild its
    own position/ledger would compound the corruption, so startup must fail
    loud, not proceed. With a non-flat position the mismatch is *reported*
    instead (:attr:`RestartReconciliation.replay_error`) — refusing to start
    would leave a live position with nobody watching its SL/TP, which is worse
    than the corruption itself; the CLI then runs in protection-only mode
    (engine ticks, new decision cycles stay halted).
    """


@dataclass(frozen=True)
class RestartReconciliation:
    """What one restart reconciliation did — the CLI's startup report."""

    canceled_plan_ids: tuple[str, ...]
    canceled_order_ids: tuple[str, ...]
    funding_posted: int
    funding_still_pending: int
    forced_immediate_cycle: bool
    # Non-None when a non-flat position kept a mismatched startup alive
    # (protection-only mode) — see ReconciliationError for the rationale.
    replay_error: str | None = None

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
        # A forced cycle sizes against the books; mismatched books must never
        # force one (the mismatch path skips step 8 entirely).
        if self.replay_error is not None and self.forced_immediate_cycle:
            raise ValueError("RestartReconciliation.replay_error excludes forced_immediate_cycle")

    @property
    def replay_mismatch(self) -> bool:
        """Whether startup continued over unverifiable books (protection-only)."""
        return self.replay_error is not None

    @property
    def replay_status(self) -> str:
        """The ``last_replay_*`` breadcrumb vocabulary for this outcome."""
        return "mismatch" if self.replay_mismatch else "ok"

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

    # Steps 1–3 + 8: one transaction — plans to canceled_restart with their
    # residual, their still-live orders to canceled, and the immediate-cycle force
    # (step 8) written alongside so it can never be stranded by a later crash. A
    # crash mid-way rolls back to a state this function simply re-runs.
    canceled_plans: list[str] = []
    canceled_orders: list[str] = []
    force_written = False
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

        # Step 8 (store side), written in THIS transaction — not a later one. A
        # canceled plan abandons the old target, so the next AI cycle must start
        # immediately (spec §3). Deriving the force from ``canceled_plans`` and
        # committing ``next_decision_at=now`` in a separate later transaction would
        # strand the store, on a crash during the funding/replay work below, with
        # canceled plans but the *old* clock: a re-run rebuilds ``canceled_plans``
        # empty (the plans are already ``canceled_restart``, no longer LIVE) and so
        # never forces, idling the position until the stale clock (up to 4h) while
        # SL/TP still ticks. An in-progress attempt IS that cycle (it resumes on the
        # first poll), so only force when none exists. Writing before replay is safe:
        # if replay later fails the run either halts (protection-only — the scheduler
        # never polls) or exits (flat mismatch), so ``now`` is inert in both, and the
        # reported ``forced_immediate_cycle`` below stays False over mismatched books.
        if canceled_plans and repo.find_in_progress_attempt(conn, run_id) is None:
            state = repo.get_scheduler_state(conn, run_id)
            raw_next = state["next_decision_at"] if state is not None else None
            if raw_next is None or parse_instant(raw_next) > now:
                repo.upsert_scheduler_state(conn, run_id, next_decision_at=now, updated_at=now)
                force_written = True

    # Step 5 (store side): post pending funding from the stored settlement basis.
    posted, still_pending = backfill_pending_funding(
        db, run_id=run_id, now=now, funding_source=funding_source
    )

    # Steps 4 + 7 (store side): the committed events must rebuild exactly the
    # materialized state — otherwise the store is corrupt and NEW trading must
    # stop. Flat: fail loud (nothing to protect). Non-flat: report the mismatch
    # so the CLI keeps the engine ticking over the live position (SL/TP and the
    # monitor must survive — exiting would leave the position unwatched, which
    # is strictly worse than the corruption).
    replayed = accounting.replay(db, run_id=run_id)
    replay_error: str | None = None
    if not replayed.is_consistent:
        detail = f"run {run_id!r} failed accounting replay on restart: {replayed.mismatch_detail}"
        if all(p.is_flat for p in repo.get_all_current_positions(db.conn, run_id)):
            raise ReconciliationError(
                detail + " — refusing to trade on a store that cannot rebuild its own state"
            )
        replay_error = detail
        logger.error("%s — continuing in protection-only mode", detail)

    # Step 8 report: the store-side force was committed above with the cancels.
    # It only counts as an immediate cycle when the books verified — a mismatch
    # runs protection-only, where the forced clock is inert (and a forced cycle
    # over unrebuildable books is exactly what RestartReconciliation forbids).
    forced = force_written and replay_error is None

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
        replay_error=replay_error,
    )
