"""Live exchange reconciliation — the §12 sweep over orders, fills, positions,
account equity and SL protection.

§12.1: the exchange is the truth source for live state; SQLite is the audit
trail. This module never lets the local record overrule the exchange — every
§12.3 case is RECORDED (``exchange_reconciliation_events`` + the snapshot rows'
``reconciliation_status``/``reconciliation_diff``), the ones with a safe
mechanical fix are fixed (settle a stuck order from ``orderStatus``, back-fill
an orphan's local row, book missing fills through the PR 3 backfiller), and the
rest flip the pass to MISMATCH, which the caller turns into safe mode
(:meth:`LiveReconciler.reconcile_and_apply`).

One deliberate asymmetry: the sweep only ever writes what the exchange PROVED
(an ``orderStatus`` verdict, a booked fill, an ack). It never zeroes a phantom
local position or fabricates a correcting entry — "以交易所為準" is reached by
booking the exchange events that explain the difference (the fill backfill),
and when those cannot be found the books are wrong in a way only a human should
touch: the case stays open, the pass stays unclean, and the §13.5 repeated-
mismatch ladder escalates to manual safe mode.

Reads are seams (callables) so every §12.3 case is testable with fakes;
production binds them to the PR 1 signed client and Info wrapper.

Four modules. This one holds ``LiveReconciler``: the seams, ``run()``'s
guarded lanes, the safe-mode application, the position and account legs
and the recording. Each pass hands a :class:`~.reconcile_types.SweepContext`
to the fill legs in :mod:`.reconcile_fills` and the orders leg in
:mod:`.reconcile_orders`; the case/report types and the disposition
vocabulary are :mod:`.reconcile_types`.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Any

from ..common.enum_guard import check_enum
from ..common.seam_guard import require_object_seam, require_seam
from ..domains.perp.schema import AccountSnapshot, PerpPosition
from ..exchanges.hyperliquid.mapper import (
    hl_closing_side,
    map_account_snapshot,
    require_decimal,
)
from ..persistence import repository as repo
from ..persistence.db import Database
from ..persistence.models import DECIMAL_CONTEXT, PositionState
from ..ports import Clock
from ..runtime import accounting
from ..runtime.clock import WallClock
from . import reconcile_fills, reconcile_orders
from .fill_backfill import (
    BackfillSummary,
    FillBackfiller,
)
from .orders import OrderStatusQuery
from .payloads import write_raw_payload
from .reconcile_fills import FAILED_BACKFILL
from .reconcile_types import (
    FILL_BACKFILLED_DISPOSITION,
    MANUAL_CASE_REASONS,
    ReconciliationCase,
    ReconciliationReport,
    SweepContext,
)
from .safe_mode import (
    REASON_RECONCILIATION_MISMATCH,
    REASON_SL_MISSING,
    REASON_STALE_ORDER_SWEEP_FAILED,
    SafeModeManager,
)
from .venue_identity import (
    EscalationHolder,
    VenueIdentityMonitor,
    escalate_identity_fault,
)
from .ws_stream import LiveWsStream

__all__ = [
    "EQUITY_TOLERANCE_ABS_USDC",
    "EQUITY_TOLERANCE_REL",
    "LiveReconciler",
]

logger = logging.getLogger(__name__)

# WIRE VOCABULARY OWNED BY THE SWEEP, not by the mapper: the frontendOpenOrders
# listing fields (oid, coin, cloid, side, origSz/sz, limitPx, reduceOnly, tif —
# read by ``reconcile_orders`` and, for the SL coverage, by ``_has_valid_sl``
# below) and the fills fields (tid, time — read by ``reconcile_fills``). The
# mapper owns the info-endpoint SNAPSHOT vocabulary — and §12.3 reconciliation
# plus §19.3 bot-ownership are decided off the sweep's reading of the wire, so
# an upstream schema change has to be answered in both places. What the mapper
# does own on this side, the two sweep modules that need it import for
# themselves: the side alphabet (``reconcile_orders``) and the closing-side
# rule (here — so this
# reconciler and the startup sweep can never disagree on which side acts
# against a position), plus map_account_snapshot for payloads this file hands
# over uninspected. The whole division of labour is in mapper's module
# docstring.

# §12.3 "equity difference beyond tolerance". The spec names the rule but not
# the number; these are PROVISIONAL tuning constants (same convention as the
# PR 2 deadband constants) sized for the §21 mainnet-tiny account: pending
# fees/funding legitimately float the local ledger by cents between passes, so
# the tolerance is 1% of exchange equity with a 1 USDC floor. Revisit against
# testnet_live telemetry (PR 6 acceptance) before mainnet.
EQUITY_TOLERANCE_ABS_USDC = Decimal("1")
EQUITY_TOLERANCE_REL = Decimal("0.01")


# The equity-mismatch case row's once-per-fact key. Deliberately a CONSTANT:
# the observed account value drifts with mark prices between passes, so keying
# on it (the way position rows key on their stable size) would defeat the
# (case_type, exchange_value) dedupe and write one audit row per pass for the
# same unhealed fact (decided 2026-07-17). The live magnitudes stay visible in
# the first row's detail and in every pass's warning log.
_EQUITY_MISMATCH_FACT_KEY = "equity_out_of_tolerance"

# How much of any ONE string the per-pass reconciliation_diff carries (the blob
# itself is not bounded — it holds one entry per case, error and sweep failure
# the pass found). Every
# string this module composes itself fits well inside it; the cap is for the
# parts it does NOT author — the ``{exc}`` interpolated into case details, leg
# errors and sweep failures, whose length is the venue's (or a client library's)
# choice, not this code's. The blob is written twice per pass for as long as the
# fault lasts, so an unbounded error body would be persisted at that rate; the
# untruncated text stays on the case row it was first observed under.
_DIFF_STRING_MAX_CHARS = 300


def _clip(text: str | None) -> str | None:
    """One string as the per-pass diff carries it: a bounded head."""
    return None if text is None else text[:_DIFF_STRING_MAX_CHARS]


def _diff_json(report: ReconciliationReport, backfill_summary: BackfillSummary | None) -> str:
    """The per-pass ``reconciliation_diff`` blob: every case, error and sweep failure, clipped."""
    return json.dumps(
        {
            "trigger": report.trigger,
            "cases": [
                {
                    "case_type": c.case_type,
                    "symbol": c.symbol,
                    "local": c.local_value,
                    "exchange": c.exchange_value,
                    # The pass's own account of the fact, and the only
                    # DURABLE home for what varies while an invariant-keyed
                    # fact stands: the case row is written once (its first
                    # detail), `safe-mode --status` prints no detail at all,
                    # and the warning log rotates. Without this, an off-coin
                    # holding that grew all week would be a post-mortem with
                    # one number in it — the one from Monday.
                    #
                    # Clipped like every other string here: two details
                    # carry a venue exception (a failed orderStatus read, a
                    # failed reopen tiebreaker), and iter_open_live_orders
                    # spans runs — so one outage mints one per still-live
                    # order the store carries, twice per pass, every pass it
                    # lasts. The head carries the diagnosis.
                    "detail": _clip(c.detail),
                    "resolved": c.resolved,
                    # Severity survives into the durable record: two cases
                    # can share a case_type (position mismatch on our coin
                    # vs an unknown coin) yet differ on whether a human
                    # must decide.
                    "manual": c.manual,
                }
                for c in report.cases
            ],
            # Both channels also end in ``{exc}`` (a leg that raised, an
            # order that would not cancel — the latter one entry per order),
            # so both are clipped for the same reason the details are. The
            # full text reaches the operator through the log and the
            # safe-mode entry detail, which are not written per pass.
            "errors": [_clip(e) for e in report.errors],
            # A verdict input like any other (see the field's comment): the
            # durable diff is what a post-mortem reads, and a row marked
            # "mismatch" whose diff named no cause would send that reader
            # hunting through a CLI transcript they no longer have.
            "sweep_failures": [_clip(f) for f in report.sweep_failures],
            "backfill": None
            if backfill_summary is None
            else {
                "fetched": backfill_summary.fetched,
                "applied": backfill_summary.applied,
                "complete": backfill_summary.complete,
            },
        },
        sort_keys=True,
    )


class LiveReconciler:
    """One run's §12 reconciliation sweep, bound to injectable exchange reads.

    ``fetch_open_orders`` / ``fetch_clearinghouse`` / ``fetch_fills`` are the
    exchange seams (production: the signed client's ``open_orders``, an Info
    ``user_state`` read and ``user_fills_by_time``); the orderStatus seam is
    the shared ``identity`` monitor (§13.5, issue #80), or — when none is
    passed, in tests and offline verdicts — a private monitor built over
    ``query_order_by_cloid``. ``backfiller`` + ``stream`` are the PR 3 fill
    leg; either may be None in reads-only wirings (tests, offline verdicts),
    which skips booking and reports the fill leg from the sighting backlog
    alone — every skipped seam lands in the report's ``legs_skipped`` (see
    that field for the §13.4 consequence).
    """

    def __init__(
        self,
        *,
        db: Database,
        run_id: str,
        coin: str,
        fetch_open_orders: Callable[[], Any],
        fetch_clearinghouse: Callable[[], Any],
        query_order_by_cloid: OrderStatusQuery | None = None,
        fetch_fills: Callable[[int, int], Any] | None = None,
        backfiller: FillBackfiller | None = None,
        stream: LiveWsStream | None = None,
        payload_dir: Path | None = None,
        clock: Clock | None = None,
        refresh_kill_switch: Callable[[], None] | None = None,
        identity: VenueIdentityMonitor | None = None,
    ) -> None:
        # Refused at construction, not on the first sweep where each seam is
        # called inside a fail-soft ``except Exception`` lane — see
        # ``common.seam_guard`` (issue #159). ``fetch_fills`` alone may be None:
        # the reads-only wiring, reported through ``legs_skipped``.
        require_seam(
            "fetch_open_orders",
            fetch_open_orders,
            kind="exchange",
            shape="() -> frontendOpenOrders list",
        )
        require_seam(
            "fetch_clearinghouse",
            fetch_clearinghouse,
            kind="exchange",
            shape="() -> clearinghouseState payload",
        )
        if fetch_fills is not None:
            require_seam(
                "fetch_fills", fetch_fills, kind="exchange", shape="(start_ms, end_ms) -> fills list"
            )
        # ``refresh_kill_switch`` is called inside the guarded lanes too (every
        # sweep's ``_refresh_deadline``); ``None`` is the test wiring — both
        # production sites pass one — and anything else must be callable
        # (issue #224).
        if refresh_kill_switch is not None:
            require_seam(
                "refresh_kill_switch",
                refresh_kill_switch,
                kind="kill-switch refresh",
                shape="() -> None",
            )
        # ``stream`` is three seams on one object — ``backfill_epoch() ->
        # epoch``, ``backfill_since() -> datetime | None``,
        # ``mark_backfill_done(epoch) -> bool`` — each called inside the guarded
        # fill leg (``reconcile_fills.run_fill_backfill``), so the refusal names
        # the missing method (issue #169). ``None`` is every wiring today: no
        # production site binds a stream to the reconciler (the v1 loop runs
        # the REST backfill without a socket — ``cli/live_loop``'s scope note),
        # so this covers the seam for the wiring that will.
        if stream is not None:
            require_object_seam(
                "stream",
                stream,
                kind="LiveWsStream fill-leg",
                methods=("backfill_epoch", "backfill_since", "mark_backfill_done"),
            )
        self._db = db
        self._run_id = run_id
        self._coin = coin
        self._fetch_open_orders = fetch_open_orders
        self._fetch_clearinghouse = fetch_clearinghouse
        self._fetch_fills = fetch_fills
        self._backfiller = backfiller  # the setter checks what the cross-check reads off it
        self._stream = stream
        self._payload_dir = payload_dir
        self._clock = clock or WallClock()
        # §13.5 (issue #80): every per-order orderStatus read (the orders leg,
        # ``reconcile_orders``) goes through the shared venue-identity monitor,
        # so an answer this build cannot read as being about the cloid it asked
        # for is COUNTED across passes (and across the other consumers —
        # protection, the kill switch) instead of merely re-recording the same
        # unresolved case forever. The CLI
        # passes its one shared instance; a reconciler built without one
        # (tests, offline verdicts) gets a private monitor over the raw seam.
        # One or the other, never both: a ``query_order_by_cloid`` passed beside
        # a monitor would be silently ignored, and a wrapped or recording seam
        # would then be bypassed without a word.
        if identity is None:
            if query_order_by_cloid is None:
                raise ValueError("LiveReconciler needs an identity monitor or query_order_by_cloid")
            identity = VenueIdentityMonitor(
                query_order_by_cloid=query_order_by_cloid,
                db=db,
                run_id=run_id,
                symbol=coin,
                payload_dir=payload_dir,
                clock=self._clock,
            )
        elif query_order_by_cloid is not None:
            raise ValueError(
                "LiveReconciler takes EITHER an identity monitor OR query_order_by_cloid — "
                "the monitor owns the orderStatus seam"
            )
        else:
            # An object seam like ``stream``: every ``probe`` the orders leg makes
            # runs inside a guarded lane that turns any exception into an
            # unproven case, so a stand-in without one would fail every
            # orderStatus read softly, forever (issue #224). ``latched`` /
            # ``latched_site`` are
            # what ``escalate_identity_fault`` reads off it after each pass.
            require_object_seam(
                "identity",
                identity,
                kind="VenueIdentityMonitor",
                methods=("probe",),
                attrs=("latched", "latched_site"),
            )
        self._identity = identity
        # §18.2: a full sweep is the longest wall of REST traffic on the
        # single-threaded live tick — two account reads, a paged fill backfill,
        # and an orderStatus round-trip PER order in two separate loops whose
        # length is bounded by the book, not by config (one of them deliberately
        # spans runs). The only refresh otherwise is the one at the top of the
        # tick, so a slow sweep lets the dead man's switch cancel every resting
        # order on the wallet while the process is alive and mid-reconcile.
        # Optional for TESTS, not for production: both real construction sites
        # (the live loop and the smoke restart recovery) arm a switch and pass
        # this (2026-07-31 deadline review).
        self._refresh_kill_switch = refresh_kill_switch

    def _refresh_deadline(self) -> None:
        """Refresh the dead man's switch across this sweep's blocking work (§18.2)."""
        if self._refresh_kill_switch is not None:
            self._refresh_kill_switch()

    @property
    def _backfiller(self) -> FillBackfiller | None:
        return self._backfiller_slot

    @_backfiller.setter
    def _backfiller(self, backfiller: FillBackfiller | None) -> None:
        """Bind the fill leg — and check, on the binding, what the cross-check reads off it.

        The window and its operator label follow whichever backfiller is bound
        (see ``reconcile_fills.crosscheck_window``), so both refusals sit here
        rather than in ``__init__``: a stand-in without a ``lookback`` (the cross-check
        window) or a ``backfill`` (the fill leg) is named as a mis-wiring —
        the object form of the seam guard, one seam over — instead of
        surfacing as an AttributeError inside a guarded leg, and a
        fractional-hour lookback is refused before the first sweep whether the
        backfiller arrived at construction (both production sites) or was
        attached afterwards (tests). It refused at import while the window was
        a module constant.
        """
        if backfiller is not None:
            require_object_seam(
                "backfiller",
                backfiller,
                kind="FillBackfiller",
                methods=("backfill",),
                attrs=("lookback",),
            )
        # Checked BEFORE the slot is written, so a refused binding does not land:
        # the label the genesis warning would render is what refuses.
        reconcile_fills.lookback_label(reconcile_fills.crosscheck_window(backfiller))
        self._backfiller_slot = backfiller

    def _sweep_context(self) -> SweepContext:
        """What the fill and orders legs read for this pass — see ``SweepContext``."""
        return SweepContext(
            db=self._db,
            run_id=self._run_id,
            identity=self._identity,
            fetch_fills=self._fetch_fills,
            backfiller=self._backfiller,
            stream=self._stream,
            clock=self._clock,
            refresh_deadline=self._refresh_deadline,
        )

    # ------------------------------------------------------------------ run

    def run(self, trigger: str, *, sweep_failures: tuple[str, ...] = ()) -> ReconciliationReport:
        """One full §12.3 sweep; records everything, raises nothing.

        A leg whose exchange read fails is reported UNRECONCILED (with the
        error) rather than raising: §12.2 runs this from heartbeats and
        shutdown paths that must keep running, and "could not prove" already
        maps to the fail-safe verdict (unclean → safe mode).

        ``sweep_failures`` carries the §19.3 startup stale-order sweep's
        per-order failures into the verdict — see
        ``ReconciliationReport.sweep_failures`` for why they must arrive here
        (before the pass is recorded) rather than be folded in afterwards.
        """
        check_enum(trigger, repo.RECONCILIATION_TRIGGERS, name="trigger")
        now = self._clock.now()
        cases: list[ReconciliationCase] = []
        errors: list[str] = []
        # Filled by the legs themselves (the skipping site is the reporting
        # site — see ReconciliationReport.legs_skipped), like ``errors``.
        legs_skipped: list[str] = []

        # -- exchange reads (each leg degrades independently) ---------------
        open_orders: list | None = None
        try:
            raw_orders = self._fetch_open_orders()
            if isinstance(raw_orders, list):
                open_orders = raw_orders
            else:
                errors.append(f"open_orders returned {type(raw_orders).__name__}, expected a list")
        except Exception as exc:  # noqa: BLE001 — a failed read is a verdict, not a crash
            # Logged at the point of capture (traceback included), like the
            # guarded() legs below: the errors channel carries only the message
            # string into the verdict/safe-mode detail, and a log-watcher must
            # not need the CLI's aggregated stderr to diagnose the origin.
            logger.exception("reconciliation open_orders read failed")
            errors.append(f"open_orders failed: {exc}")
        # §18.2: the two account reads are sequential and each can ride a full
        # network timeout; refresh between them and after them so the pair never
        # counts as one gap.
        self._refresh_deadline()

        snapshot: AccountSnapshot | None = None
        raw_clearinghouse: Any = None
        try:
            raw_clearinghouse = self._fetch_clearinghouse()
            snapshot = map_account_snapshot(raw_clearinghouse)
        except Exception as exc:  # noqa: BLE001
            logger.exception("reconciliation clearinghouse state read failed")
            errors.append(f"clearinghouse state read failed: {exc}")
        self._refresh_deadline()
        ctx = self._sweep_context()

        # -- legs -----------------------------------------------------------
        # Each leg is individually guarded: the docstring's "raises nothing"
        # must hold for the legs' own DB reads/writes too (a transient
        # `database is locked` mid-settle is a routine hazard, not an edge
        # case), and a crashed leg maps to the same fail-safe verdict as a
        # failed exchange read — unproven, unclean, safe mode. Cases a leg
        # appended before crashing are kept: records everything.
        def guarded(name: str, leg: Callable[[], Any], fallback: Any) -> Any:
            try:
                return leg()
            except Exception as exc:  # noqa: BLE001 — a crashed leg is an unproven leg
                logger.exception("%s leg crashed", name)
                errors.append(f"{name} leg crashed: {exc}")
                return fallback

        # The fill and orders legs are called through their modules, never bound
        # by name here: the tests patch the module attribute to prove ``guarded``.
        backfill_summary = guarded(
            "fill backfill",
            lambda: reconcile_fills.run_fill_backfill(ctx, errors, legs_skipped),
            FAILED_BACKFILL,
        )
        orders_ok = guarded(
            "orders",
            lambda: reconcile_orders.reconcile_orders(ctx, open_orders, cases, errors, now),
            False,
        )
        fills_ok = guarded(
            "fills",
            lambda: reconcile_fills.reconcile_fills(ctx, cases, errors, now, legs_skipped),
            False,
        )
        # Fallback (False, False): unknown ≠ protected — the same fail-safe
        # direction as a failed clearinghouse read (§17.1 rule 1 demands
        # proof, not absence of disproof).
        position_ok, protected = guarded(
            "position",
            lambda: self._reconcile_positions(open_orders, snapshot, cases),
            (False, False),
        )
        account_ok = guarded(
            "account", lambda: self._reconcile_account(snapshot, cases, errors), False
        )

        report = ReconciliationReport(
            trigger=trigger,
            timestamp=now,
            cases=tuple(cases),
            orders_reconciled=orders_ok,
            fills_reconciled=fills_ok,
            position_reconciled=position_ok,
            account_reconciled=account_ok,
            position_protected=protected,
            backfill_complete=backfill_summary is None or backfill_summary.complete,
            errors=tuple(errors),
            legs_skipped=tuple(legs_skipped),
            sweep_failures=tuple(sweep_failures),
        )
        try:
            self._record(report, snapshot, raw_clearinghouse, backfill_summary)
        except Exception:  # noqa: BLE001 — the verdict must survive its own recording
            # "Records everything, raises nothing" includes the recording leg
            # itself: a DB failure here must not crash the pass — the caller
            # still gets the in-memory verdict and drives safe mode off it
            # (an unclean pass stays unclean; a clean one is only unrecorded).
            logger.exception(
                "reconciliation (%s) verdict computed but could not be recorded", trigger
            )
        if not report.clean:
            logger.warning(
                "reconciliation (%s) UNCLEAN: orders=%s fills=%s position=%s account=%s "
                "protected=%s backfill=%s cases=%d errors=%s sweep_failures=%s",
                trigger,
                orders_ok,
                fills_ok,
                position_ok,
                account_ok,
                protected,
                report.backfill_complete,
                len([c for c in cases if not c.resolved]),
                "; ".join(errors) or "none",
                "; ".join(report.sweep_failures) or "none",
            )
        return report

    def apply_manual_cases(self, report: ReconciliationReport, safe_mode: SafeModeManager) -> None:
        """Latch manual safe mode for every unresolved manual case in ``report``.

        Public because §19 startup drives it from BOTH recovery passes: a
        manual fact (a non-bot order, an unknown-coin position) observed in the
        first record-and-fix pass is STICKY evidence that someone else operates
        this wallet (§13.5 requires human acknowledgement), so it must latch
        even if the fact clears before the verdict pass (decided 2026-07-17).
        Idempotent: a reason re-entered while manual is latched records one
        ``safe_mode_reason_added`` row per episode and never resets
        ``entered_at``, so calling it in both passes is safe.
        """
        for case in report.manual_cases:
            # Membership is guaranteed by ReconciliationCase.__post_init__.
            reason = MANUAL_CASE_REASONS[case.case_type]
            safe_mode.enter("manual", reason, detail=case.detail or case.case_type)

    def reconcile_and_apply(
        self,
        trigger: str,
        *,
        safe_mode: SafeModeManager,
        ws_restored: bool,
        kill_switch_active: bool,
        sweep_failures: tuple[str, ...] = (),
    ) -> ReconciliationReport:
        """Run a pass and drive the safe-mode machine from its verdict.

        Manual cases enter manual safe mode with their own reason; any other
        unclean pass enters (or keeps) recoverable safe mode; a clean pass
        counts toward §13.4 auto-recovery, with the caller attesting the two
        conditions the reconciler cannot see (WS restored, kill switch
        healthy — pass ``kill_switch.release_safe_mode()``'s verdict there
        when the kill-switch latch is up). The attestations are deliberately
        REQUIRED (no defaults): they are §13.4 release conditions the caller
        proves THIS tick, and a defaulted ``True`` would let a future call
        site auto-release a recoverable safe mode without evidence. The
        reconciler's own wiring is attested the same way (``fully_wired`` —
        see ReconciliationReport.legs_skipped).

        ``sweep_failures`` (the §19.3 startup stale-order sweep's per-order
        failures) are a verdict INPUT, not a separate post-hoc entry: a cancel
        that could not land leaves a stale order resting, so carrying them into
        the pass BEFORE any release keeps a reconciliation-clean pass from
        auto-releasing — and re-anchoring ``entered_at`` on — an episode the
        sweep already made unhealthy (decided 2026-07-17). They go through
        ``run()`` so the recorded pass agrees with the verdict; the entry names
        ``stale_order_sweep_failed`` specifically when the reconciliation legs
        were otherwise clean.
        """
        report = self.run(trigger, sweep_failures=tuple(sweep_failures))
        self.apply_manual_cases(report, safe_mode)
        safe_mode.note_reconciliation_outcome(report.clean)
        if report.clean:
            if report.legs_skipped:
                # See ReconciliationReport.legs_skipped: fully_wired=False
                # below withholds the release; the latch holds.
                logger.info(
                    "reconciliation (%s) clean but skipped leg(s): %s — §13.4 "
                    "auto-release withheld (partially wired reconciler)",
                    trigger,
                    ", ".join(report.legs_skipped),
                )
            if not safe_mode.try_auto_recover(
                reconciliation_clean=True,
                ws_restored=ws_restored,
                kill_switch_active=kill_switch_active,
                fully_wired=not report.legs_skipped,
            ):
                # Not in recoverable safe mode (nothing to release), or manual
                # is still latched: a clean pass still re-proves the §4.1
                # "state is reconciled" line. The third decline cause —
                # recoverable safe mode whose §13.4 conditions are not yet
                # proven — is refused by set_state_reconciled itself (that
                # flag is recoverable safe mode's only gate line).
                safe_mode.set_state_reconciled(True)
        else:
            safe_mode.set_state_reconciled(False)
            if not report.manual_cases:
                unresolved = sorted({c.case_type for c in report.cases if not c.resolved})
                # EVERY cause this pass found, not just the first kind of cause:
                # unresolved case types, errors-only legs (an unmapped-fill
                # backlog, an incomplete backfill) and sweep failures live in
                # three separate channels, and a compound failure used to let
                # whichever channel came first suppress the others from the
                # §13.6 triage surface entirely.
                causes = [*unresolved, *report.errors, *report.sweep_failures]
                # The named reasons: a sweep-only failure and a position whose
                # only problem is a missing/insufficient SL (repair is PR 5's
                # manager) are each queryable in safe_mode_events as such;
                # anything else is the generic mismatch.
                if report.reconciliation_clean and report.sweep_failures:
                    reason = REASON_STALE_ORDER_SWEEP_FAILED
                elif (
                    unresolved == ["position_sl_missing"]
                    and not report.errors
                    and not report.sweep_failures
                ):
                    reason = REASON_SL_MISSING
                else:
                    reason = REASON_RECONCILIATION_MISMATCH
                safe_mode.enter("recoverable", reason, detail="; ".join(causes))
        # §13.5 (issue #80): the per-order orderStatus probes this pass just
        # ran feed the shared venue-identity streak, and this method is the
        # reconciler's one moment with the safe-mode machine in hand. LAST,
        # after the pass's own bookkeeping — see escalate_identity_fault for
        # why every holder runs it last and on the level.
        if escalate_identity_fault(
            self._identity, safe_mode, holder=EscalationHolder.RECONCILIATION, trigger=trigger
        ):
            logger.error(
                "reconciliation (%s): venue-identity fault latched — manual safe mode", trigger
            )
        return report

    # ---------------------------------------------------------- position leg

    def _reconcile_positions(
        self,
        open_orders: list | None,
        snapshot: AccountSnapshot | None,
        cases: list[ReconciliationCase],
    ) -> tuple[bool, bool]:
        """§12.3 position rows + the SL-protection invariant → (ok, protected).

        Three verdict phases plus the advisory mirror, one helper each, in this
        order: the off-coin holdings (manual), the liquidation-price mirror (a
        cache write, no verdict), the size compare, and the §17.1 SL coverage.
        The verdict helpers append to ``cases`` and return their flag, like
        ``run()``'s legs. The ``snapshot is None`` return here is what lets the
        helpers read ``exch is None`` as "a successful read proved flat".
        """
        if snapshot is None:
            return False, False
        off_coin_ok = self._note_off_coin_positions(snapshot, cases)
        exch = snapshot.position_for(self._coin)
        exch_size = Decimal(0) if exch is None else exch.size
        local = repo.get_current_position(self._db.conn, self._run_id, self._coin)
        local_size = Decimal(0) if local is None else local.size
        self._mirror_liquidation_price(exch, local)
        sizes_ok = self._compare_position_sizes(
            local_size=local_size, exch_size=exch_size, cases=cases
        )
        protected = self._check_sl_coverage(open_orders, exch, local, cases)
        return off_coin_ok and sizes_ok, protected

    def _note_off_coin_positions(
        self, snapshot: AccountSnapshot, cases: list[ReconciliationCase]
    ) -> bool:
        """§13.5 "unknown exchange position": a manual case per holding outside this run's coin."""
        ok = True
        for pos in snapshot.positions:
            if pos.coin != self._coin:
                ok = False
                off_coin_detail = (
                    f"exchange holds a {pos.coin} position of {pos.size} but this "
                    f"run trades only {self._coin} — unknown position (§13.5), "
                    "manual safe mode"
                )
                # ONE sentence, logged and recorded: the row below is written
                # once (see its key), so this log is where later passes of the
                # same unhealed holding report their live size.
                logger.warning("%s", off_coin_detail)
                cases.append(
                    ReconciliationCase(
                        case_type="exchange_position_mismatch",
                        symbol=pos.coin,
                        local_value=None,
                        # The fact is "the wallet holds a coin this run does not
                        # trade" — ONE fact per coin, and a manual case so it
                        # persists until a human disposes of it. The size is not
                        # part of it: keying on it minted a fresh audit row (and
                        # a fresh manual stamp chore) for every partial fill, and
                        # even for a re-stringified Decimal("0.10") vs "0.1".
                        #
                        # The coin STAYS in the key: the dedupe key is (run_id,
                        # case_type, exchange_value) with NO symbol column, so a
                        # bare qualifier would collide two different off-coins
                        # and silently drop the second manual safe-mode fact.
                        exchange_value=f"{pos.coin}|unknown_coin",
                        detail=off_coin_detail,
                        manual=True,
                    )
                )
        return ok

    def _mirror_liquidation_price(
        self, exch: PerpPosition | None, local: PositionState | None
    ) -> None:
        """Mirror the exchange's liquidation estimate onto the local row (§12.1); advisory.

        Precondition: the clearinghouse read SUCCEEDED, so ``exch is None``
        means the exchange proved this coin flat — never "unknown"
        (``_reconcile_positions`` returns before calling this on a failed
        read).
        """
        exch_size = Decimal(0) if exch is None else exch.size
        local_size = Decimal(0) if local is None else local.size
        # The engine's SL band and the ai_inputs ``estimated_liquidation_price``
        # column read it back via ``get_current_position``. An explicit ``None``
        # when the exchange is flat — or reports no liquidationPx — so a stale
        # estimate never survives a flat. Skipped without a local row: the
        # writer is UPDATE-only, so it would be a guaranteed no-op, and
        # ``_compare_position_sizes`` owns that case.
        #
        # Also ``None`` when the two views DISAGREE on direction — the flip fill
        # landed between this pass's clearinghouse read and its fill backfill,
        # so ``exch`` still describes the pre-flip side. A liquidation price is
        # only meaningful for the side it was computed on; stamping the old
        # side's estimate onto the freshly flipped row is precisely what makes
        # the SL band answer CLOSE_NOW on a healthy position (§3.6
        # ``liquidation_too_close`` → §17.2 emergency close → §13.5 manual
        # latch). Withholding costs nothing: the band falls back to the
        # entry-based one until a pass sees both views agree.
        #
        # ADVISORY, unlike every other write in this reconciler (decision
        # 2026-07-29): this leg's verdict answers "do the local and exchange
        # positions agree?", and the successful clearinghouse read already
        # answered it. The write is a cache for the SL band, not the evidence
        # this leg produces — letting a transient store error retroactively
        # mark the
        # position unreconciled AND unprotected would drive safe mode (halted
        # cycles, manual §13.6 release) off a metadata failure. A failure costs
        # one tick of staleness: the next pass rewrites it, and the fallback it
        # leaves is the entry-based band the engine used for every run before
        # this mirror existed.
        liq = None if exch is None else exch.liquidation_price
        same_direction = exch_size != 0 and local_size != 0 and (exch_size > 0) == (local_size > 0)
        if not same_direction:
            liq = None
        if local is not None and liq != local.liquidation_price:
            try:
                with self._db.transaction() as tx:
                    repo.set_position_liquidation_price(tx, self._run_id, self._coin, liq)
            except Exception as exc:  # noqa: BLE001 — advisory cache, see above
                logger.warning(
                    "liquidation mirror for %s failed (%s: %s) — the row keeps its "
                    "previous estimate this tick; the next pass rewrites it",
                    self._coin,
                    type(exc).__name__,
                    exc,
                )

    def _compare_position_sizes(
        self, *, local_size: Decimal, exch_size: Decimal, cases: list[ReconciliationCase]
    ) -> bool:
        """§12.3 size rows: when the two views differ, one case keyed on the transition."""
        if exch_size == local_size:
            return True
        if exch_size == 0 and local_size != 0:
            case_type, detail = (
                "local_position_phantom",
                "SQLite has a position but the exchange is flat — the exchange is "
                "the truth source; the closing fills must be booked (backfill), "
                "never fabricated (§12.3)",
            )
        elif exch_size != 0 and local_size == 0:
            case_type, detail = (
                "exchange_position_mismatch",
                "exchange has a position but SQLite believes flat — new entries stop "
                "until the missing fills are booked (§12.3)",
            )
        else:
            case_type, detail = (
                "exchange_position_mismatch",
                "position sizes differ — fills are missing or double-booked (§12.3)",
            )
        cases.append(
            ReconciliationCase(
                case_type=case_type,
                symbol=self._coin,
                local_value=str(local_size),
                # The distinct fact is the (local → exchange) size
                # transition, not the exchange size alone: a phantom
                # (local X, exchange 0) always has exch_size 0, so keying
                # on it would collide EVERY phantom this run ever sees onto
                # one row. Encode the coin and both sides so an independent
                # later mismatch of a different magnitude gets its own audit
                # row while an unhealed one still dedupes across passes.
                exchange_value=f"{self._coin}:{local_size}->{exch_size}",
                detail=detail,
            )
        )
        return False

    def _check_sl_coverage(
        self,
        open_orders: list | None,
        exch: PerpPosition | None,
        local: PositionState | None,
        cases: list[ReconciliationCase],
    ) -> bool:
        """§12.3 last row / §17.1 rule 1: an exchange position must carry a valid SL.

        Precondition: the clearinghouse read SUCCEEDED, so ``exch is None`` means
        the exchange proved this coin flat (protected) — never "unknown", which
        is the ``(False, False)`` return in ``_reconcile_positions``.
        """
        if exch is None:
            return True
        if self._has_valid_sl(open_orders, exch):
            return True
        # The case row is written only when the absence was OBSERVED: with
        # open_orders None the truth is "could not look", already carried
        # by the orders-leg error and the fail-safe ``protected=False`` —
        # a durable row asserting "no valid SL" off a failed read would
        # tell a post-mortem reader protection had lapsed when it may not
        # have (and occupy the fact's once-per-fact dedupe slot).
        if open_orders is None:
            return False
        sl_detail = (
            f"live position of {exch.size} has no valid reduce-only SL "
            "covering its size — repair is the PR 5 protection manager; "
            "until then the run stays in safe mode (§17.1 rule 1)"
        )
        # Same shape as the off-coin fact above: recorded once, logged
        # with the live size every pass.
        logger.warning("%s %s", self._coin, sl_detail)
        cases.append(
            ReconciliationCase(
                case_type="position_sl_missing",
                symbol=self._coin,
                local_value=None if local is None else str(local.size),
                # The fact is "this coin's live position is uncovered",
                # not the size it happened to have when first observed:
                # the gap stays open until the §17 protection manager
                # heals it, and every partial fill or partial close in
                # between used to mint another row — one more manual
                # stamp for the operator, all describing one lapse.
                # Coin-qualified for the same reason the off-coin key is
                # (the dedupe key carries no symbol).
                exchange_value=f"{self._coin}|sl_missing",
                detail=sl_detail,
            )
        )
        return False

    def _has_valid_sl(self, open_orders: list | None, position: PerpPosition) -> bool:
        """A bot-owned reduce-only stop_loss order covering the full position.

        Coverage counts only orders on the CLOSING side of the CURRENT
        position: a stale SL left from an opposite-direction position (the bot
        was short, flipped long while down) rests reduce-only with the right
        role and size but would never protect the position it now shares a
        book with — counting it would silently pass §17.1 rule 1 on a
        position with no real stop.
        """
        if open_orders is None:
            return False
        conn = self._db.conn
        need = abs(position.size)
        closing_side = hl_closing_side(position.size)
        covered = Decimal(0)
        for order in open_orders:
            if not isinstance(order, dict):
                continue
            cloid = order.get("cloid")
            if not isinstance(cloid, str):
                continue
            registry = repo.get_cloid_by_hex(conn, cloid)
            if registry is None or registry["order_role"] != "stop_loss":
                continue
            if registry["symbol"] != position.coin:
                continue
            if not bool(order.get("reduceOnly", False)):
                continue
            if order.get("side") != closing_side:
                continue
            try:
                # ``require_decimal``, not ``Decimal(str(...))``: the except
                # below only ever saw values that FAIL to parse, and the two
                # that matter here parse fine. Measured against the compare
                # this sum feeds, after the loop: a NaN sz makes ``covered``
                # NaN and ``covered >= need`` RAISES InvalidOperation (trapped by
                # default), which the run()-level ``guarded`` turns into an
                # unproven position leg — conservative, but every pass, with an
                # error that names nothing. An "inf" sz is worse and silent:
                # ``Decimal("Infinity") >= need`` is True, so a position with no
                # real stop is reported PROTECTED and §17.1 rule 1 passes on it
                # (issue #81). Both now land in this same fail-safe branch.
                covered += require_decimal(order.get("sz"), field="sz")
            except Exception:  # noqa: BLE001 — an unusable size covers nothing
                # Fail-safe direction (counts as zero coverage), but never
                # silently: the sibling degradation paths in this module all
                # leave a trace, and "SL judged missing because a size failed
                # to parse" must be diagnosable from the log.
                logger.warning(
                    "SL order sz %r (cloid %s) is not a usable number while proving "
                    "§17.1 coverage; counting it as zero",
                    order.get("sz"),
                    cloid,
                )
                continue
        return covered >= need

    # ----------------------------------------------------------- account leg

    def _reconcile_account(
        self,
        snapshot: AccountSnapshot | None,
        cases: list[ReconciliationCase],
        errors: list[str],
    ) -> bool:
        """§12.3 equity-tolerance row: exchange equity vs the local ledger."""
        if snapshot is None:
            return False
        ledger = repo.get_current_account_state(self._db.conn, self._run_id)
        if ledger is None:
            errors.append("no local current_account_state row — ledger genesis missing")
            return False
        # Local equity = wallet_balance (realized/fees/funding already folded)
        # + unrealized at the EXCHANGE's own mark — using their unrealized
        # isolates the comparison to ledger drift instead of mark drift.
        exch_unrealized = sum((p.unrealized_pnl for p in snapshot.positions), Decimal(0))
        local_equity = ledger.wallet_balance + exch_unrealized
        diff = abs(snapshot.account_value - local_equity)
        tolerance = max(EQUITY_TOLERANCE_ABS_USDC, snapshot.account_value * EQUITY_TOLERANCE_REL)
        if diff > tolerance:
            # Logged with the LIVE magnitudes every pass: the case row below
            # dedupes on a stable fact key, so later passes of the same
            # unhealed mismatch land here (and in the verdict), not in new
            # audit rows.
            logger.warning(
                "equity mismatch: |exchange %s − local %s| = %s exceeds tolerance %s",
                snapshot.account_value,
                local_equity,
                diff,
                tolerance,
            )
            cases.append(
                ReconciliationCase(
                    case_type="equity_mismatch",
                    symbol=None,
                    local_value=str(local_equity),
                    exchange_value=_EQUITY_MISMATCH_FACT_KEY,
                    detail=(
                        f"|exchange {snapshot.account_value} − local {local_equity}| = "
                        f"{diff} exceeds tolerance {tolerance} "
                        "(pending fees/funding cannot explain it) — safe mode (§12.3)"
                    ),
                )
            )
            return False
        return True

    # ------------------------------------------------------------- recording

    def _record(
        self,
        report: ReconciliationReport,
        snapshot: AccountSnapshot | None,
        raw_clearinghouse: Any,
        backfill_summary: BackfillSummary | None,
    ) -> None:
        """Persist the pass: raw payload, case rows, backfill event, §16.3/§16.4 snapshot rows.

        The raw payload file, then three writes, each isolated from the others:
        the case rows and the backfill event (their helpers say why), and the
        snapshot rows carrying the verdict and the diff (the comment at that
        write says why).
        """
        now = report.timestamp
        status = "ok" if report.clean else "mismatch"
        diff = _diff_json(report, backfill_summary)
        raw_path = None
        if self._payload_dir is not None and raw_clearinghouse is not None:
            raw_path = write_raw_payload(
                payload_dir=self._payload_dir,
                kind="clearinghouse",
                key=self._run_id,
                payload=raw_clearinghouse,
                now=now,
            )
        self._record_cases(report)
        self._record_backfill_event(report, backfill_summary)
        if snapshot is not None:
            # A SEPARATE transaction, fail-soft: the case rows above are the
            # record a human needs to understand why safe mode fired, and a
            # snapshot-write failure (constraint, disk, lock timeout) inside
            # the same unit would roll them back too — losing exactly the
            # durable evidence of the pass that found the problem. A failed
            # snapshot degrades to "cases recorded, snapshot rows skipped",
            # same convention as write_raw_payload.
            try:
                with self._db.transaction() as conn:
                    self._write_snapshots(conn, snapshot, status, diff, raw_path, now)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "reconciliation snapshot rows could not be written (case rows are safe)"
                )

    def _record_cases(self, report: ReconciliationReport) -> None:
        """One transaction per case row; a resolved repeat restamps the existing row."""
        # Per case, not per pass (the snapshot rows in ``_record`` are isolated
        # the same way): one case's write failing on a busy store must not
        # roll back — or stop — the sibling facts observed in the same pass.
        # The row a human most needs (the manual case that fired safe mode)
        # would otherwise vanish with an unrelated row's failure, leaving the
        # pass's audit trail empty.
        for case in report.cases:
            try:
                with self._db.transaction() as conn:
                    wrote = repo.insert_exchange_reconciliation_event(
                        conn,
                        run_id=self._run_id,
                        trigger=report.trigger,
                        case_type=case.case_type,
                        symbol=case.symbol,
                        local_value=case.local_value,
                        exchange_value=case.exchange_value,
                        action_taken=case.action_taken,
                        detail=case.detail,
                        timestamp=report.timestamp,
                    )
                    if not wrote and case.action_taken is not None and case.exchange_value:
                        # The once-per-fact dedupe swallowed this insert, but
                        # THIS pass resolved the fact (a retry settling an
                        # order the first pass could only record) — stamp the
                        # disposition on the existing row, or the backlog
                        # permanently shows as unresolved a case that was in
                        # fact settled.
                        #
                        # Reached only for a key the dedupe still shuts, so its
                        # latest row is unresolved, or bears a disposition that
                        # is not provisional — a HUMAN's, or one of the machine
                        # stamps for a fact that cannot return. The test below
                        # tells them apart, and it is not redundant: a
                        # provisionally disposed-of key took the insert above
                        # instead (carrying its own disposition), while an
                        # operator's `--stamp-case` answer is not the daemon's
                        # to overwrite.
                        existing = repo.get_exchange_reconciliation_case(
                            conn,
                            self._run_id,
                            case_type=case.case_type,
                            exchange_value=case.exchange_value,
                        )
                        if existing is not None and existing["action_taken"] is None:
                            repo.set_reconciliation_action(
                                conn, existing["event_id"], case.action_taken
                            )
            except Exception:  # noqa: BLE001 — sibling case rows must still land
                logger.exception(
                    "reconciliation case row could not be recorded (%s, %s)",
                    case.case_type,
                    case.exchange_value,
                )

    def _record_backfill_event(
        self, report: ReconciliationReport, backfill_summary: BackfillSummary | None
    ) -> None:
        """§12.3 row 5, resolved in-pass: one event per pass that booked fills."""
        if backfill_summary is None or backfill_summary.applied <= 0:
            return
        # The fills were booked through the PR 3 path; the event is not deduped
        # (no exchange_value).
        try:
            with self._db.transaction() as conn:
                repo.insert_exchange_reconciliation_event(
                    conn,
                    run_id=self._run_id,
                    trigger=report.trigger,
                    case_type="exchange_fill_missing_local",
                    symbol=self._coin,
                    action_taken=FILL_BACKFILLED_DISPOSITION,
                    detail=(
                        f"booked {backfill_summary.applied} missing fill(s) via REST "
                        f"backfill ({backfill_summary.fetched} fetched, "
                        f"{backfill_summary.duplicate} duplicate)"
                    ),
                    timestamp=report.timestamp,
                )
        except Exception:  # noqa: BLE001 — same isolation as the case rows
            logger.exception("reconciliation backfill event row could not be recorded")

    def _write_snapshots(
        self,
        conn: sqlite3.Connection,
        snapshot: AccountSnapshot,
        status: str,
        diff: str,
        raw_path: str | None,
        now: datetime,
    ) -> None:
        """§16.3/§16.4: snapshot rows carrying the exchange view + the verdict.

        Every money column is either the local ledger's own number or the
        exchange's verbatim figure (mark derived as positionValue/|size| —
        arithmetic on exchange numbers, not a model; maintenance margin is the
        account-level ``crossMaintenanceMarginUsed``, which under the enforced
        single-symbol constraint (§25 #4) is this position's). Nothing is
        fabricated: rows are SKIPPED (with a warning) when the exchange did not
        report a figure a NOT NULL column needs — never written with a guessed 0.
        """
        ledger = repo.get_current_account_state(conn, self._run_id)
        if ledger is None or snapshot.cross_maintenance_margin_used is None:
            logger.warning(
                "skipping reconciliation snapshot rows: %s",
                "no local ledger" if ledger is None else "no crossMaintenanceMarginUsed",
            )
            return
        exch_unrealized = sum((p.unrealized_pnl for p in snapshot.positions), Decimal(0))
        total_notional = snapshot.total_position_notional
        if total_notional is None:
            if any(p.position_value is None for p in snapshot.positions):
                # A position without positionValue cannot be summed: writing
                # the partial total would UNDERSTATE exposure (and a fully
                # unpriced book would read 0 — the same zero-looks-like-flat
                # trap AccountSnapshot guards for account_value). Skip the row
                # rather than write a number known to be wrong.
                logger.warning(
                    "skipping reconciliation snapshot rows: open position(s) carry no "
                    "positionValue to derive total notional from"
                )
                return
            # The `any` guard above proved every position_value non-None; the
            # inline filter is for the type checker, not a second policy.
            total_notional = sum(
                (p.position_value for p in snapshot.positions if p.position_value is not None),
                Decimal(0),
            )
        # The local-view columns must satisfy the §6.1/§6.6 arithmetic
        # identities the mode-agnostic ``validate`` recomputes over every
        # account_snapshots row (paper/validation.py _account_row_identities_ok,
        # under DECIMAL_CONTEXT). Derive them with the SAME canonical formulas
        # the paper engine's writer uses (runtime.accounting) under the SAME
        # pinned context — a hand-rolled expression here once stored an INVERTED
        # margin_ratio (maint/equity) and a duplicate of exchange_withdrawable,
        # which read as corruption to the auditor. The exchange's own figures
        # are preserved verbatim in the exchange_* columns below; the local
        # available_balance is equity − used_initial_margin, never the raw
        # withdrawable. ``used_initial_margin`` / ``total_maintenance_margin``
        # are the exchange's account-level margin (§25 #4 single symbol), and
        # every derived column keys off them and the local equity.
        maint = snapshot.cross_maintenance_margin_used
        used_im = snapshot.total_margin_used
        with localcontext(DECIMAL_CONTEXT):
            equity = accounting.account_equity(ledger.wallet_balance, exch_unrealized)
            total_pnl = (
                ledger.realized_pnl + exch_unrealized - ledger.total_fees + ledger.net_funding_pnl
            )
            leverage = accounting.effective_leverage(total_notional, equity)
            available = accounting.available_balance(equity, used_im)
            ratio = accounting.margin_ratio(equity, maint)
        repo.insert_account_snapshot(
            conn,
            timestamp=now,
            mode="live",
            run_id=self._run_id,
            wallet_balance=ledger.wallet_balance,
            account_equity=equity,
            available_balance=available,
            realized_pnl=ledger.realized_pnl,
            unrealized_pnl=exch_unrealized,
            total_pnl=total_pnl,
            total_fees=ledger.total_fees,
            net_funding_pnl=ledger.net_funding_pnl,
            total_position_notional=total_notional,
            effective_leverage=leverage,
            used_initial_margin=used_im,
            total_maintenance_margin=maint,
            margin_ratio=ratio,
            exchange_account_value=snapshot.account_value,
            exchange_withdrawable=snapshot.withdrawable,
            exchange_margin_used=snapshot.total_margin_used,
            exchange_unrealized_pnl=exch_unrealized,
            exchange_raw_payload_path=raw_path,
            reconciliation_status=status,
            reconciliation_diff=diff,
        )
        exch = snapshot.position_for(self._coin)
        local = repo.get_current_position(conn, self._run_id, self._coin)
        if exch is not None:
            mark = (
                exch.position_value / abs(exch.size)
                if exch.position_value is not None and exch.size != 0
                else None
            )
            if mark is None:
                logger.warning(
                    "skipping reconciliation position snapshot: no positionValue to derive mark"
                )
                return
            protection = repo.get_position_protection(conn, self._run_id, self._coin)
            sl, tp = protection if protection is not None else (None, None)
            repo.insert_position_snapshot(
                conn,
                timestamp=now,
                mode="live",
                run_id=self._run_id,
                symbol=self._coin,
                position_size=exch.size,
                side="long" if exch.size > 0 else "short",
                entry_price=exch.entry_price,
                mark_price=mark,
                position_notional=exch.position_value,
                exposure_pct=None,
                unrealized_pnl=exch.unrealized_pnl,
                realized_pnl=Decimal(0) if local is None else local.realized_pnl,
                maintenance_margin=snapshot.cross_maintenance_margin_used,
                estimated_liquidation_price=None,
                exchange_liquidation_price=exch.liquidation_price,
                stop_loss_price=sl,
                take_profit_price=tp,
                exchange_position_size=exch.size,
                exchange_entry_price=exch.entry_price,
                exchange_unrealized_pnl=exch.unrealized_pnl,
                exchange_margin_used=exch.margin_used,
                exchange_raw_payload_path=raw_path,
                reconciliation_status=status,
                reconciliation_diff=diff,
            )
