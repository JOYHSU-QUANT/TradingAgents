"""The §12 sweep's fill legs: the REST backfill and the fill-ledger checks.

Two legs of :meth:`~.reconcile.LiveReconciler.run`, each a function over a
:class:`~.reconcile_types.SweepContext`: :func:`run_fill_backfill` books the
fills the exchange has and SQLite lacks (§12.3 "交易所有 fill，但 SQLite 沒記錄")
through the PR 3 backfiller, and :func:`reconcile_fills` sweeps the sighting
backlog and runs the invalid-local-fill cross-check (§12.3 "SQLite 有 fill，
但交易所查不到"). Both are called inside ``run()``'s guarded lanes:
:func:`run_fill_backfill` appends to ``errors`` / ``legs_skipped`` and returns
the backfill summary; :func:`reconcile_fills` appends to ``cases`` / ``errors``
/ ``legs_skipped`` and returns the leg's verdict.

Wire vocabulary read here: the fills fields ``tid`` and ``time`` — see the
division of labour in the mapper's module docstring and in the WIRE
VOCABULARY note at the top of ``reconcile.py``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any, NamedTuple

from ..common.instants import epoch_ms, whole_hours_label
from ..persistence import repository as repo
from ..persistence.ids import exchange_fill_key, usable_fill_tid
from .fill_backfill import (
    DEFAULT_LOOKBACK,
    DEFAULT_MAX_PAGES,
    RESPONSE_FILL_CAP,
    BackfillSummary,
    FillBackfiller,
)
from .fills import ENVELOPE_FACT_KEY_PREFIX
from .reconcile_types import FILL_BOOKED_DISPOSITION, ReconciliationCase, SweepContext

__all__ = [
    "FAILED_BACKFILL",
    "CrosscheckWindow",
    "crosscheck_window",
    "fetch_window_fill_keys",
    "lookback_label",
    "reconcile_fills",
    "run_fill_backfill",
]

logger = logging.getLogger(__name__)

# The invalid-local-fill cross-check window (§12.3 "SQLite 有 fill，但交易所查
# 不到") is NOT a module constant: it is the trailing lookback of the backfiller
# the reconciler holds, read at each sweep — ``crosscheck_window`` below
# (issue #149, after #102) carries the KNOWN EXEMPTION that says why the two
# must be one number. Fills near the window edges are excluded from the
# verdict — a fill booked milliseconds ago (or one at the window's far edge)
# can be absent from one read without being invalid.
_FILL_CROSSCHECK_EDGE_MARGIN = timedelta(minutes=2)

# The fail-safe fill-leg fallback: nothing fetched, nothing proven. Shared by
# every "the backfill could not run/complete" path so the two sites can never
# drift on what an unproven backfill looks like.
FAILED_BACKFILL = BackfillSummary(
    fetched=0, applied=0, duplicate=0, unmapped=0, malformed=0, complete=False
)


class CrosscheckWindow(NamedTuple):
    """``(span, owner)``: the window, and the name a refusal cites."""

    span: timedelta
    owner: str


def crosscheck_window(backfiller: FillBackfiller | None) -> CrosscheckWindow:
    """The invalid-local-fill cross-check window and the name of what owns it.

    The window is the bound backfiller's trailing lookback, read off it at
    each sweep — not bound at construction and not restated from the
    module default (issue #149, after #102). The KNOWN EXEMPTION below
    holds only while the two windows are one number, and the default
    equals every instance's lookback only until the first wiring passes
    ``lookback_seconds`` through from config — a cross-check still pinned
    to the default would then call every local fill between the two
    windows "a fill the exchange denies" and open manual cases on a false
    premise.

    KNOWN EXEMPTION (decided 2026-07-17): the window slides, so a local
    fill older than the lookback is permanently outside this leg's verdict
    — a double-booked fill caught inside the window is manual severity,
    the same fill outside it is seen only by the equity-tolerance leg.
    Accepted for PR 4 because a genesis floor would page the full history
    every pass (and withhold the verdict whenever the 2000/20-page budget
    runs out); PR 5's durable "cross-checked-through" watermark extends
    coverage without that cost.

    With no backfiller (reads-only wirings: tests, offline verdicts) there
    is no backfill leg for the window to keep parity with, and it is only
    "how far back the cross-check reads": the module default stands in
    (decided 2026-09-01). The owner name is what a refusal names (see
    ``lookback_label``).
    """
    if backfiller is None:
        return CrosscheckWindow(DEFAULT_LOOKBACK, "fill_backfill.DEFAULT_LOOKBACK")
    return CrosscheckWindow(backfiller.lookback, "FillBackfiller.lookback")


def lookback_label(window: CrosscheckWindow) -> str:
    """The cross-check window as the operator reads it in the genesis-corruption warning.

    Two callers: the warning in ``run_fill_backfill``, and ``LiveReconciler``'s
    backfiller setter, which calls it for the refusal alone.

    Whole hours only: a lookback that is not one would render truncated
    ("5h" for 5h30m) and understate how long an outage can go unbooked, so
    this refuses rather than round — retuning the backfill window to a
    fraction of an hour is a change that message has to be rewritten for.
    """
    return whole_hours_label(window.span, what=window.owner)


def run_fill_backfill(
    ctx: SweepContext, errors: list[str], legs_skipped: list[str]
) -> BackfillSummary | None:
    """§12.3 "交易所有 fill，但 SQLite 沒記錄": book them via the PR 3 path.

    The epoch discipline mirrors the stream's contract: read the epoch,
    run the pass, clear with the epoch read — and only for a COMPLETE
    pass, so a capped window never retires the gap it failed to cover.
    """
    if ctx.backfiller is None:
        legs_skipped.append("fill_backfill")
        return None
    epoch = ctx.stream.backfill_epoch() if ctx.stream is not None else None
    if ctx.stream is not None:
        since = ctx.stream.backfill_since()
    else:
        # No WS stream in this wiring (every wiring today — the startup command
        # and the v1 loop alike, see ``cli/live_loop``'s scope note): the whole
        # process-was-down era is owed, so the floor is the newest booked
        # fill — or the run's genesis when none exists yet (§11.2 rule 5).
        # The trailing lookback alone would silently skip any outage longer
        # than itself. The §11.2 v12 durable clean-backfill watermark (which
        # hardens this derivation against a crash mid-backfill) lands with
        # PR 5's daemon wiring — decided 2026-07-16.
        since = repo.last_live_fill_time(ctx.db.conn, ctx.run_id)
        if since is None:
            run_row = repo.get_run(ctx.db.conn, ctx.run_id)
            genesis_raw = None if run_row is None else run_row["created_at"]
            try:
                since = datetime.fromisoformat(genesis_raw)
            except (TypeError, ValueError):
                since = None
                # Our own writer always stores ISO-8601, so this is store
                # corruption (same reading as safe_mode's entered_at) — and
                # the degradation is money-relevant: the floor silently
                # becomes the bare trailing lookback, which skips any
                # outage longer than itself. Never degrade without a trace.
                logger.warning(
                    "run %s genesis timestamp %r is missing/unparseable; fill "
                    "backfill floor degrades to the trailing %s lookback — an "
                    "outage longer than that may leave fills unbooked",
                    ctx.run_id,
                    genesis_raw,
                    lookback_label(crosscheck_window(ctx.backfiller)),
                )
    try:
        summary = ctx.backfiller.backfill(ctx.clock.now(), since=since)
    except Exception as exc:  # noqa: BLE001 — transport failure = gap still open
        logger.exception("reconciliation fill backfill failed")
        errors.append(f"fill backfill failed: {exc}")
        return FAILED_BACKFILL
    if not summary.complete:
        # An incomplete pass (page budget exhausted / capped window that did
        # not raise) already flips backfill_complete → the verdict is
        # unclean, but without a trace here the safe-mode detail and the
        # reconciliation_diff would carry no reason. Name it, like every
        # other blocking leg does (the exception path above already does).
        errors.append(
            f"fill backfill incomplete ({summary.applied} booked, page budget "
            "exhausted or window uncovered) — some exchange fills may remain "
            "unbooked (§12.3)"
        )
        logger.warning("reconciliation fill backfill incomplete — gap not fully covered")
    if summary.complete and ctx.stream is not None and epoch is not None:
        ctx.stream.mark_backfill_done(epoch)
    return summary


def reconcile_fills(
    ctx: SweepContext,
    cases: list[ReconciliationCase],
    errors: list[str],
    now: datetime,
    legs_skipped: list[str],
) -> bool:
    """The fill-ledger legs: sighting backlog + the invalid-local check."""
    ok = True
    conn = ctx.db.conn

    # Sweep the malformed backlog first: a sighting whose bare-tid key has
    # since been booked (a §8.3 recovery re-ingested it) resolves itself.
    # Any malformed sighting still un-actioned after this AGE blocks the
    # pass (decided 2026-07-17): a fill the exchange reported but SQLite
    # never booked (an unusable tid, a digest-keyed body) is "交易所有
    # fill、本地沒記錄" — unbooked money a human must stamp action_taken on,
    # not an audit backlog the verdict may read past. The reason flows into
    # ``errors`` so the fact reaches the safe-mode detail and the persisted
    # reconciliation_diff, like every other blocking leg.
    unresolved_malformed = 0
    for row in repo.iter_exchange_reconciliation_events(
        conn, ctx.run_id, case_type="fill_malformed"
    ):
        if row["action_taken"] is not None:
            continue
        key = row["exchange_value"]
        # Three shapes reach here and only ONE is keyed by a tid: a bare
        # tid, a ``unparsed-`` digest, and an ``envelope-`` fact key (a
        # stream-level fault — wrong wallet, channel schema drift). The
        # latter two are unkeyable and can only be settled by a human.
        #
        # The envelope arm is not merely a shortcut. The key it carries is
        # a CONSTANT this code chose, but the tid the resolver would derive
        # from it is untrusted input: a venue fill whose tid is literally
        # "envelope-wrong-user" books under exchange_fill_key
        # "tid|envelope-wrong-user", and letting the lookup run would find
        # it and stamp the STREAM fault resolved_fill_booked — retiring the
        # one signal that says we are being served another wallet's fills,
        # with no human ever seeing it. fills._malformed_key fences the same
        # namespace on the write side; this is the read side of that fence
        # (2026-08-17 exit sweep).
        if not key or key.startswith("unparsed-") or key.startswith(ENVELOPE_FACT_KEY_PREFIX):
            unresolved_malformed += 1  # unkeyable: human territory
            continue
        try:
            booked = repo.get_fill_by_exchange_key(conn, exchange_fill_key(tid=key))
        except ValueError:
            unresolved_malformed += 1  # the malformed tid violates the key derivation
            continue
        if booked is not None:
            with ctx.db.transaction() as tx:
                repo.set_reconciliation_action(tx, row["event_id"], FILL_BOOKED_DISPOSITION)
        else:
            unresolved_malformed += 1
    if unresolved_malformed:
        ok = False
        errors.append(
            f"{unresolved_malformed} malformed fill sighting(s) unresolved — the "
            "exchange reported fills SQLite never booked; a human must stamp "
            "action_taken (§12.3)"
        )

    # §12.3 (v10/v11): unmapped sightings still absent from the ledger are
    # the known "exchange has a fill we have not booked" backlog. Record it
    # in ``errors`` (not just the log): the flip to unclean must reach the
    # safe-mode detail and the persisted reconciliation_diff, or an operator
    # sees safe mode fire with no cause named on any operator-facing surface.
    unbooked = repo.iter_unresolved_fill_sightings(conn, ctx.run_id)
    if unbooked:
        ok = False
        errors.append(
            f"{len(unbooked)} unmapped fill sighting(s) still unbooked — exchange "
            "fills absent from the local ledger (§12.3)"
        )
        logger.warning(
            "%d unmapped fill sighting(s) still unbooked — fills are not reconciled",
            len(unbooked),
        )

    # §12.3 "SQLite 有 fill，但交易所查不到" → invalid_local_fill (manual:
    # money the exchange denies is booked money we cannot trust).
    if ctx.fetch_fills is None:
        # The skipping site is the reporting site — see
        # ReconciliationReport.legs_skipped.
        legs_skipped.append("invalid_local_fill_crosscheck")
    else:
        lookback = crosscheck_window(ctx.backfiller).span
        window_start = now - lookback
        logger.debug(
            "invalid-local-fill cross-check window %s → %s; local fills older "
            "than the window are outside this leg's verdict (known exemption, "
            "PR 5 watermark extends coverage)",
            window_start.isoformat(),
            now.isoformat(),
        )
        exchange_keys = fetch_window_fill_keys(
            ctx.fetch_fills, window_start, now, errors, refresh_deadline=ctx.refresh_deadline
        )
        if exchange_keys is None:
            return False  # fetch failed or window provably not covered: inconclusive
        verdict_start = window_start + _FILL_CROSSCHECK_EDGE_MARGIN
        verdict_end = now - _FILL_CROSSCHECK_EDGE_MARGIN
        for fill in repo.iter_live_fills(conn, ctx.run_id):
            fill_time = fill["exchange_fill_time"]
            try:
                # NULL and unparseable land in the SAME lane: the writer
                # requires exchange_fill_time (PR 3 guard), so either shape
                # is store corruption, and a silent exemption would leave
                # that fill permanently outside the §12.3 verdict.
                stamp = datetime.fromisoformat(fill_time)
            except (TypeError, ValueError):
                # run()'s contract is "records everything, raises nothing";
                # a bad stored timestamp is a verdict, not a crash.
                errors.append(
                    f"fill {fill['fill_id']} has missing/unparseable "
                    f"exchange_fill_time {fill_time!r} — fills leg cannot be proven"
                )
                ok = False
                continue
            if not (verdict_start <= stamp <= verdict_end):
                continue
            if fill["exchange_fill_key"] not in exchange_keys:
                ok = False
                cases.append(
                    ReconciliationCase(
                        case_type="invalid_local_fill",
                        symbol=fill["symbol"],
                        local_value=fill["fill_id"],
                        exchange_value=fill["exchange_fill_key"],
                        detail=(
                            f"fill {fill['fill_id']} booked locally at {fill_time} is "
                            "absent from the exchange's fill history — its accounting "
                            "cannot be trusted (§14: never re-applied; human review)"
                        ),
                        manual=True,
                    )
                )
    return ok


def fetch_window_fill_keys(
    fetch: Callable[[int, int], Any],
    window_start: datetime,
    now: datetime,
    errors: list[str],
    *,
    refresh_deadline: Callable[[], None],
) -> set[str] | None:
    """Every §14.2 key the exchange reports in the window, or None if unproven.

    PAGED, exactly like the backfiller: ``userFillsByTime`` caps a response
    (2000 fills), and judging "the exchange denies this fill" against a
    TRUNCATED window would flag genuinely booked fills as invalid and force
    manual safe mode on a false premise. A window that cannot be proven
    covered (page budget out, unadvanceable page) returns None — the leg
    reports inconclusive (fail-safe) instead of issuing manual verdicts.
    """
    start_ms = epoch_ms(window_start, what="fill cross-check window start")
    end_ms = epoch_ms(now, what="fill cross-check 'now'")
    keys: set[str] = set()
    for _ in range(DEFAULT_MAX_PAGES):
        try:
            raw = fetch(start_ms, end_ms)
            if not isinstance(raw, list):
                raise ValueError(f"user_fills_by_time returned {type(raw).__name__}")
        except Exception as exc:  # noqa: BLE001 — a failed read is a verdict
            logger.exception("fill cross-check fetch failed")
            errors.append(f"fill cross-check fetch failed: {exc}")
            return None
        # §18.2: the SECOND page ladder on this tick, and the one that is easy
        # to miss — the backfiller's is in fill_backfill.py, this one is here.
        # Same shape, same budget (DEFAULT_MAX_PAGES), same hazard: without a
        # refresh per page a fills-heavy window (>2000 fills, so the response
        # comes back capped and it pages again) holds the single-threaded tick
        # for up to 20 × network_timeout_s and the dead man's switch cancels
        # every resting SL/TP while the process is alive
        # (2026-07-31 deadline review, second pass).
        refresh_deadline()
        newest_ms: int | None = None
        for f in raw:
            tid = f.get("tid") if isinstance(f, dict) else None
            if not usable_fill_tid(tid):
                # An entry this pass cannot key (non-dict entry, or a tid
                # shape ingest treats as malformed — one shared predicate,
                # so the two legs cannot drift) is an entry the membership
                # verdict below cannot see. Treating the window as covered
                # anyway would flag a genuinely booked local fill as
                # invalid_local_fill and force MANUAL safe mode on a false
                # premise — the one lane where unprovable coverage would
                # have produced a verdict instead of this leg's own
                # withhold rule.
                errors.append(
                    "fill cross-check window contains an entry with no usable "
                    "tid — invalid-fill verdicts withheld"
                )
                return None
            keys.add(exchange_fill_key(tid=tid))
            t = f.get("time")
            if isinstance(t, int) and (newest_ms is None or t > newest_ms):
                newest_ms = t
        if len(raw) < RESPONSE_FILL_CAP:
            return keys  # the exchange gave everything: the window is covered
        if newest_ms is None or newest_ms <= start_ms:
            # Cannot advance: paging again would refetch the same page.
            errors.append(
                "fill cross-check window not covered (capped page with no "
                "advanceable timestamp) — invalid-fill verdicts withheld"
            )
            return None
        start_ms = newest_ms
    errors.append(
        "fill cross-check window not covered (response still capped after "
        f"{DEFAULT_MAX_PAGES} pages) — invalid-fill verdicts withheld"
    )
    return None
