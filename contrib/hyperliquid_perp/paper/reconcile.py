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
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from ..common.instants import parse_instant
from ..persistence import repository as repo
from ..persistence.db import Database
from . import accounting
from .engine import FundingSource

__all__ = [
    "STALE_PENDING_FUNDING",
    "ReconciliationError",
    "RestartReconciliation",
    "backfill_pending_funding",
    "classify_replay",
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
# Public: the acceptance validator ages pending events against the same
# threshold for its non-gating staleness warning.
STALE_PENDING_FUNDING = timedelta(hours=6)


def _event_label(event: sqlite3.Row) -> str:
    """``"BTC @ 2026-09-04T12:00:00+00:00"``, or a stand-in if the row won't say.

    It describes a row whose own shape may be the problem, and
    ``sqlite3.Row[...]`` answers an unknown column with ``IndexError`` — so
    every subscript AND the formatting sit inside the ``try``.

    Both its callers need that. The outer containment lane would propagate a
    raise from its own handler and abort the pass, the one outcome that lane
    exists to make impossible; and the ``already_posted`` line below runs
    OUTSIDE the outer ``try`` altogether (after every ``except … continue``),
    so a raise there has no containment at all.
    """
    try:
        return f"{event['symbol']} @ {event['funding_timestamp']}"
    except Exception:  # noqa: BLE001 — a label is never worth aborting the pass for
        return "<a pending funding_events row that could not be described>"


def _log_corrupt_event(run_id: str, event: sqlite3.Row, exc: Exception) -> None:
    """The corrupt-row lane's ERROR, from either of the two places it is reached.

    The stored timestamp is parsed before the funding reader runs and the rest
    of the stored basis after it, so the one verdict ("this ROW is bad; fix it
    in the store") is now raised from two ``except`` clauses. One wording, one
    place — the reader lane between them says something different on purpose,
    and that difference is the whole point of issue #193.
    """
    logger.error(
        "funding backfill for %s could not post %s @ %s: %s — the event "
        "stays pending and its funding P&L stays uncounted (corrupt "
        "stored row; fix it in the store to resolve it)",
        run_id,
        event["symbol"],
        event["funding_timestamp"],
        exc,
    )


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

    A corrupt stored basis (``record_funding``'s fail-loud ``ValueError`` guard,
    e.g. a legacy row whose mark is missing) is contained *per event*: logged at
    error level, left pending, and never allowed to abort the pass — at restart
    that abort would fire before the protection-only fallback (live position
    unwatched, permanent crash-loop on the same row), and mid-run it would kill
    the loop that keeps SL/TP alive. Its funding P&L stays uncounted, never
    fabricated.

    A failure of the funding READER is contained the same way but reported
    apart, because it means something else entirely (issue #193): the stored
    row is fine and the fault is ours — a drifted call signature, a naive
    clock, a stamp no ``datetime`` can hold. Sending an operator to the store
    for that costs a diagnosis.

    "Never allowed to abort" is enforced by an outer per-event handler, not by
    the inner lanes' exception lists: those lists are what issue #191 got
    through. The lanes exist to give an operator the right verdict, and the
    outer one to guarantee there is always a verdict. Every outcome that
    leaves the event PENDING counts into ``still_pending`` — whatever the
    reason, its funding P&L stays uncounted rather than fabricated. (An
    ``already_posted`` row is in neither total: it is resolved, not pending,
    so the two can sum to less than the rows iterated. It gets its own INFO.)
    """
    posted = 0
    young_pending = 0
    stale_pending = 0
    # ONE counter for every event that did not post, whatever the reason. The
    # reasons differ and are told apart where telling them apart pays — in the
    # four per-event messages below (corrupt row, funding reader, store error,
    # and the outer lane's "no lane claimed it"), which are what an operator
    # acts on and what the tests assert. A counter per reason would only ever
    # be read as this sum.
    not_posted = 0
    for event in repo.iter_funding_events(db.conn, run_id, status="pending"):
        # Rebound per event, and never read across one: ``res`` is
        # function-scoped, so a future lane that dropped out of the inner try
        # without ``continue`` would credit THIS event with the previous one's
        # posted status — a silent over-count in the acceptance numbers, with
        # no exception to notice.
        res = None
        # The OUTER lane makes "this pass may never abort" (see the docstring)
        # structural rather than a promise three handler lists have to keep.
        # Those lists are the failure mode of this very issue: an unlisted type
        # is exactly what showed up (an ``OverflowError`` from a nanosecond
        # stamp — issue #191), and a ``TypeError`` out of ``record_funding``
        # would abort the same way today. Aborting at restart fires BEFORE the
        # protection-only fork — a live position left unwatched, crash-looping
        # on the same event every retry — and mid-run it kills the loop that
        # keeps SL/TP alive. The inner lanes keep their distinct verdicts; this
        # one only guarantees there is always a verdict.
        try:
            try:
                settlement = parse_instant(event["funding_timestamp"])
            except (ValueError, TypeError) as exc:
                # ``TypeError`` too: ``parse_instant`` is
                # ``datetime.fromisoformat``, which answers a NULL or a BLOB
                # cell with ``TypeError``, not ``ValueError``. Both are the
                # same fact about the same column — a bad stored row — and
                # without this the ``TypeError`` fell to the outer lane and
                # was reported as "a defect, read the traceback rather than
                # the store", which is the exact misdirection issue #193 is
                # about, pointing the other way. (``InvalidOperation`` is not
                # listed: no ``Decimal`` is parsed here.)
                not_posted += 1
                _log_corrupt_event(run_id, event, exc)
                continue
            # The READER gets its own lane, apart from the corrupt one (issue
            # #193). ``rate_at`` answers a venue failure with ``None`` and lets
            # everything else through on purpose (issue #157) — so what arrives
            # here is a defect in OUR code, never a bad stored row, and the
            # corrupt lane's "fix it in the store" would send an operator to
            # SQLite to hunt a fault that is in the clock or in a signature.
            # ``exc_info`` keeps the traceback that diagnosis needs.
            try:
                rate = (
                    funding_source.rate_at(event["symbol"], settlement)
                    if funding_source is not None
                    else None
                )
            except Exception as exc:  # noqa: BLE001 — see the outer lane
                not_posted += 1
                logger.error(
                    "funding backfill for %s could not read the rate for %s @ %s: %s — the "
                    "funding reader failed (not a corrupt stored row); the event stays "
                    "pending and retries at the next cycle boundary or restart",
                    run_id,
                    event["symbol"],
                    event["funding_timestamp"],
                    exc,
                    exc_info=True,
                )
                continue
            if rate is None:
                # Outside the post lane below: no rate is not a failure to post,
                # it is the ordinary "not settled yet" the next pass retries.
                if now - settlement >= STALE_PENDING_FUNDING:
                    stale_pending += 1
                else:
                    young_pending += 1
                continue
            # The stored SIZE is converted in its own lane, not inside the call
            # below. It is the only other thing here that can answer a bad cell
            # with ``TypeError`` (``Decimal(None)``), and widening the call's
            # handler to catch that would have claimed every ``TypeError`` out
            # of ``record_funding`` too — a drifted signature reported as
            # "corrupt stored row; fix it in the store", sending the operator
            # to SQLite to find every row well-formed. That is issue #193's
            # misdirection pointing the other way, and it is why this one
            # expression gets its own three lines.
            try:
                position_size = Decimal(event["position_size"])  # stored basis wins anyway
            except (InvalidOperation, TypeError, ValueError) as exc:
                not_posted += 1
                _log_corrupt_event(run_id, event, exc)
                continue
            try:
                res = accounting.record_funding(
                    db,
                    run_id=run_id,
                    mode=event["mode"],
                    symbol=event["symbol"],
                    funding_timestamp=settlement,
                    position_size=position_size,
                    funding_rate=rate,
                    source="funding_history_backfill",
                    recorded_at=now,
                )
            except (ValueError, InvalidOperation) as exc:
                # The settlement basis ``record_funding`` itself guards (a
                # legacy row with no stored mark). NOT ``TypeError``: nothing
                # in this call can raise one about the STORE any more, so
                # listing it would only mislabel a code defect — the outer lane
                # takes those, and says to read the traceback.
                not_posted += 1
                _log_corrupt_event(run_id, event, exc)
                continue
            except sqlite3.Error as exc:
                # A STORE failure, not a corrupt row. ``record_funding`` opens its OWN
                # transaction, so a transient "database is locked" is reachable right
                # here. Logged apart from the corrupt lane so a transient lock is never
                # misdiagnosed as a bad row: the event stays pending, the next pass retries.
                not_posted += 1
                logger.error(
                    "funding backfill for %s hit a store error posting %s @ %s: %s — the "
                    "event stays pending and will be retried on the next pass",
                    run_id,
                    event["symbol"],
                    event["funding_timestamp"],
                    exc,
                )
                continue
        except Exception as exc:  # noqa: BLE001 — the pass may never abort; see above
            not_posted += 1
            logger.error(
                "funding backfill for %s hit an unexpected failure on %s: %s — the "
                "event stays pending; no lane claimed this one, so read the traceback "
                "before suspecting the stored row",
                run_id,
                _event_label(event),
                exc,
                exc_info=True,
            )
            continue
        if res is None:
            # Unreachable today — every path to here has assigned ``res`` — so
            # this is the guard, not a lane: it exists so a future edit cannot
            # drop an event out of BOTH totals in silence, which is the one
            # failure the counters could not show.
            not_posted += 1
            logger.warning(
                "funding backfill for %s reached the tail with no result for %s — "
                "the loop has a path that neither posts nor claims the event",
                run_id,
                _event_label(event),
            )
        elif res.status == "posted":
            posted += 1
        elif res.status == "already_posted":
            # The exactly-once key found the settlement already booked, so
            # nothing moved and this event belongs in NEITHER total — which is
            # why the two can sum to less than the rows iterated.
            #
            # Note what it does NOT mean: THIS row keeps ``status='pending'``
            # in the store. The id is derived from the hour-floored settlement,
            # and the loop only walks pending rows, so the only way here is a
            # row whose floored id points at a DIFFERENT, already-posted one —
            # a non-hour-aligned legacy or hand-edited row. It will be found
            # again every pass and counted as stale by the acceptance report
            # after six hours, so the message says the row needs repairing
            # rather than claiming it is resolved.
            logger.info(
                "funding backfill for %s found the settlement behind %s already "
                "posted under another event id — nothing to post, but the row itself "
                "stays pending and will be seen again every pass; its timestamp is "
                "very likely not on the hour, and repairing it in the store is what "
                "resolves it",
                run_id,
                _event_label(event),
            )
        else:
            # A status this loop has no verdict for. Named rather than folded
            # into the branch above it: announcing an unknown status as
            # "already posted" would be a claim about the books.
            not_posted += 1
            logger.warning(
                "funding backfill for %s got an unrecognised result %r for %s — the "
                "event is left pending; the funding result vocabulary has grown and "
                "this loop was not taught the new word",
                run_id,
                res.status,
                _event_label(event),
            )
    # A stuck-forever pending event (its rate never resolves) resets the fetch
    # source's own consecutive-failure counter on every successful fetch, so only
    # this age check can distinguish it from a rate that is merely un-published yet.
    if stale_pending:
        logger.error(
            "funding backfill for %s left %d event(s) pending past %s after their "
            "settlement hour (that funding P&L stays uncounted). Usually the rate "
            "for a settled hour will never resolve — a delisted coin, an hour the "
            "exchange never published — but this count cannot tell that apart from "
            "an event the reader or a defect has been failing on every pass, so "
            "check the per-event ERROR lines above before concluding",
            run_id,
            stale_pending,
            STALE_PENDING_FUNDING,
        )
    # Corrupt-basis events already logged their own error above — only the
    # genuinely rate-less remainder gets the "still unavailable" warning.
    if young_pending:
        logger.warning(
            "funding backfill for %s left %d event(s) pending (rate still "
            "unavailable); they retry at the next cycle boundary or restart",
            run_id,
            young_pending,
        )
    return posted, young_pending + stale_pending + not_posted


def classify_replay(db: Database, *, run_id: str) -> tuple[str, str | None, Exception | None]:
    """One replay verification in the ``last_replay_*`` breadcrumb vocabulary.

    Returns ``("ok", None, None)``, ``("mismatch", detail, None)``, or
    ``("failed", detail, exc)`` — the single classification kernel shared by
    the restart fork below and the mid-run verify (``cli._post_cycle_export``),
    so the two lanes can never drift apart. A replay that *raises* (corrupt
    stored value, I/O error) is an unverifiable-books outcome, not a crash.
    """
    try:
        replayed = accounting.replay(db, run_id=run_id)
    except Exception as exc:  # noqa: BLE001 — unverifiable books are an outcome, not a crash
        return "failed", str(exc), exc
    if not replayed.is_consistent:
        return "mismatch", replayed.mismatch_detail, None
    return "ok", None, None


class ReconciliationError(RuntimeError):
    """The committed store contradicts (or cannot verify) its own materialized state.

    Raised when the accounting replay (execution §1.2 step 4 / spec §5) finds a
    mismatch — or raises outright (corrupt stored value, I/O error) — AND the
    run is flat: trading on a store that cannot rebuild its own position/ledger
    would compound the corruption, so startup must fail loud, not proceed. With
    a non-flat position the outcome is *reported* instead
    (:attr:`RestartReconciliation.replay_error`) — refusing to start would
    leave a live position with nobody watching its SL/TP, which is worse than
    the corruption itself; the CLI then runs in protection-only mode (engine
    ticks, new decision cycles stay halted).

    ``replay_status`` carries the ``last_replay_*`` breadcrumb vocabulary for
    the refusal ("mismatch" for an inconsistent rebuild, "failed" for a replay
    that raised), mirroring the mid-run verify's two lanes.
    """

    def __init__(self, message: str, *, replay_status: str = "mismatch") -> None:
        super().__init__(message)
        self.replay_status = replay_status


@dataclass(frozen=True)
class RestartReconciliation:
    """What one restart reconciliation did — the CLI's startup report."""

    canceled_plan_ids: tuple[str, ...]
    canceled_order_ids: tuple[str, ...]
    funding_posted: int
    funding_still_pending: int
    forced_immediate_cycle: bool
    # Non-None when a non-flat position kept an unverified startup alive
    # (protection-only mode) — see ReconciliationError for the rationale.
    replay_error: str | None = None
    # The ``last_replay_*`` breadcrumb vocabulary for this outcome — the same
    # shape ReconciliationError carries ("mismatch" = inconsistent rebuild,
    # "failed" = the replay raised).
    replay_status: str = "ok"

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
        # A forced cycle sizes against the books; unverified books must never
        # force one (the mismatch/failed paths skip step 8 entirely).
        if self.replay_error is not None and self.forced_immediate_cycle:
            raise ValueError("RestartReconciliation.replay_error excludes forced_immediate_cycle")
        if self.replay_status not in ("ok", "mismatch", "failed"):
            raise ValueError(f"RestartReconciliation.replay_status invalid: {self.replay_status!r}")
        # The status names the outcome the detail describes — one without the
        # other is a self-contradicting report.
        if (self.replay_status == "ok") != (self.replay_error is None):
            raise ValueError("RestartReconciliation.replay_status must agree with replay_error")

    @property
    def replay_mismatch(self) -> bool:
        """Whether startup continued over unverifiable books (protection-only)."""
        return self.replay_error is not None


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
            try:
                needs_force = raw_next is None or parse_instant(raw_next) > now
            except ValueError:
                # A corrupt stored clock must not abort the whole restart
                # (that would skip the protection-only fork below, same
                # principle as the replay guard) — the force overwrite it
                # gets is also the repair.
                logger.error(
                    "corrupt next_decision_at %r for %s — overwriting with the "
                    "forced immediate cycle",
                    raw_next,
                    run_id,
                )
                needs_force = True
            if needs_force:
                repo.upsert_scheduler_state(conn, run_id, next_decision_at=now, updated_at=now)
                force_written = True

    # Step 5 (store side): post pending funding from the stored settlement basis.
    posted, still_pending = backfill_pending_funding(
        db, run_id=run_id, now=now, funding_source=funding_source
    )

    # Steps 4 + 7 (store side): the committed events must rebuild exactly the
    # materialized state — otherwise the store is corrupt and NEW trading must
    # stop. Flat: fail loud (nothing to protect). Non-flat: report the outcome
    # so the CLI keeps the engine ticking over the live position (SL/TP and the
    # monitor must survive — exiting would leave the position unwatched, which
    # is strictly worse than the corruption). A replay that *raises* (corrupt
    # stored value, I/O error) is the same situation one notch worse —
    # unverifiable books — so it takes the same fork under the "failed" label,
    # exactly like the mid-run verify's failed lane (_post_cycle_export).
    replay_status, detail, cause = classify_replay(db, run_id=run_id)
    replay_error: str | None = None
    if replay_status == "failed":
        replay_error = f"run {run_id!r} accounting replay raised on restart: {detail}"
    elif replay_status == "mismatch":
        replay_error = f"run {run_id!r} failed accounting replay on restart: {detail}"
    if replay_error is not None:
        if all(p.is_flat for p in repo.get_all_current_positions(db.conn, run_id)):
            raise ReconciliationError(
                replay_error + " — refusing to trade on a store that cannot rebuild its own state",
                replay_status=replay_status,
            ) from cause
        logger.error("%s — continuing in protection-only mode", replay_error)

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
        replay_status=replay_status,
    )
