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
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Any

from ..common.enum_guard import check_enum
from ..domains.perp.schema import AccountSnapshot, PerpPosition
from ..exchanges.hyperliquid.mapper import (
    HL_SIDE_TO_LOCAL,
    hl_closing_side,
    map_account_snapshot,
)
from ..paper import accounting
from ..paper.clock import Clock, WallClock
from ..persistence import repository as repo
from ..persistence.db import Database
from ..persistence.ids import exchange_fill_key, usable_fill_tid
from ..persistence.models import DECIMAL_CONTEXT
from .fill_backfill import (
    DEFAULT_MAX_PAGES,
    RESPONSE_FILL_CAP,
    BackfillSummary,
    FillBackfiller,
)
from .fills import ENVELOPE_FACT_KEY_PREFIX
from .orders import local_status_for_exchange_status, parse_order_status
from .payloads import write_raw_payload
from .safe_mode import (
    REASON_INVALID_LOCAL_FILL,
    REASON_NON_BOT_OWNED_ORDER,
    REASON_RECONCILIATION_MISMATCH,
    REASON_SL_MISSING,
    REASON_STALE_ORDER_SWEEP_FAILED,
    REASON_UNKNOWN_POSITION,
    SafeModeManager,
)
from .ws_stream import LiveWsStream

__all__ = [
    "EQUITY_TOLERANCE_ABS_USDC",
    "EQUITY_TOLERANCE_REL",
    "LiveReconciler",
    "ReconciliationCase",
    "ReconciliationReport",
]

logger = logging.getLogger(__name__)

# WIRE VOCABULARY OWNED HERE, not in the mapper: the frontendOpenOrders listing
# fields (oid, coin, cloid, side, origSz/sz, limitPx, reduceOnly) and the fills
# fields (tid, time) this file reads directly. The mapper owns the info-endpoint
# SNAPSHOT vocabulary — and §12.3 reconciliation plus §19.3 bot-ownership are
# decided off THIS file's reading of the wire, so an upstream schema change has
# to be answered in both places. What the mapper does own on this side is
# imported above: the side alphabet and the closing-side rule (so this file and
# the startup sweep can never disagree on which side acts against a position),
# plus map_account_snapshot for payloads this file hands over uninspected. The
# whole division of labour is in mapper's module docstring.

# §12.3 "equity difference beyond tolerance". The spec names the rule but not
# the number; these are PROVISIONAL tuning constants (same convention as the
# PR 2 deadband constants) sized for the §21 mainnet-tiny account: pending
# fees/funding legitimately float the local ledger by cents between passes, so
# the tolerance is 1% of exchange equity with a 1 USDC floor. Revisit against
# testnet_live telemetry (PR 6 acceptance) before mainnet.
EQUITY_TOLERANCE_ABS_USDC = Decimal("1")
EQUITY_TOLERANCE_REL = Decimal("0.01")

# The invalid-local-fill cross-check window (§12.3 "SQLite 有 fill，但交易所查
# 不到"). Mirrors the backfiller's trailing lookback; fills near the window
# edges are excluded from the verdict — a fill booked milliseconds ago (or one
# at the window's far edge) can be absent from one read without being invalid.
# KNOWN EXEMPTION (decided 2026-07-17): the window slides, so a local fill
# older than the lookback is permanently outside this leg's verdict — a
# double-booked fill caught inside 6h is manual severity, the same fill outside
# it is seen only by the equity-tolerance leg. Accepted for PR 4 because a
# genesis floor would page the full history every pass (and withhold the
# verdict whenever the 2000/20-page budget runs out); PR 5's durable
# "cross-checked-through" watermark extends coverage without that cost.
_FILL_CROSSCHECK_LOOKBACK = timedelta(hours=6)
_FILL_CROSSCHECK_EDGE_MARGIN = timedelta(minutes=2)

# The fail-safe fill-leg fallback: nothing fetched, nothing proven. Shared by
# every "the backfill could not run/complete" path so the two sites can never
# drift on what an unproven backfill looks like.
_FAILED_BACKFILL = BackfillSummary(
    fetched=0, applied=0, duplicate=0, unmapped=0, malformed=0, complete=False
)

# §13.5 manual case → safe-mode entry reason. ONE definition, tied to the
# construction sites by ``ReconciliationCase.__post_init__`` (a manual case
# whose case_type is missing here fails loud at construction): without that
# guard, a future manual case type added without its reason would silently
# enter safe mode under the generic mismatch reason, hiding the specific fact
# from the §13.6 triage surface.
_MANUAL_CASE_REASONS = {
    "non_bot_owned_order": REASON_NON_BOT_OWNED_ORDER,
    "invalid_local_fill": REASON_INVALID_LOCAL_FILL,
    "exchange_position_mismatch": REASON_UNKNOWN_POSITION,
}

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


# Choosing an exchange_value for a NEW order/position fact below (the fill-side
# keys are §14.2's and §11.3's, and answer to those specs, not to this note):
# ask whether the fact is an INVARIANT that stands until someone disposes of it
# — an off-coin holding, an uncovered position, an equity gap — or an EPISODE
# that recurs as distinct occurrences.
#   Invariant → key it on the subject alone (``BTC|sl_missing``,
#     ``equity_out_of_tolerance``). Whatever varies while it stands belongs in
#     the row's detail, the per-pass reconciliation_diff, and a warning log —
#     never in the key, or every change of it mints a row and a manual stamp.
#   Episode   → key it on what makes the occurrence distinct (the position-size
#     transition below): an independent later mismatch of a different magnitude
#     is its own fact, not a repeat of the first.
# The dedupe carries no symbol column, so either way the coin/cloid stays IN the
# key.
#
# Then ask, for a fact this module DISPOSES of automatically, whether the
# episode can start over — because a stamped key is normally shut for good, and
# a recurrence under it would reach neither `safe-mode --status` nor §21.4's
# unresolved count. Every stamp this module writes about an ORDER is one the
# sweep can find itself facing again (a §8.3 rule-5 resend or a reopen puts the
# cloid back; the reopen tiebreaker's read failure needs only the venue's next
# answer), so those are declared PROVISIONAL (repo.PROVISIONAL_DISPOSITIONS)
# and the next occurrence gets its own row. Two things do NOT get that
# treatment, and the difference is the point: a human's `--stamp-case`
# disposition (their answer must not be re-asked on every pass thereafter), and
# a fact whose subject cannot return at all (``local_row_backfilled`` — this
# package contains no `DELETE FROM orders`).
#
# Reopenability is not a licence to stamp early, though, and how often the fact
# can return is the thing to weigh: a key the sweep re-observes EVERY pass would
# mint a row per pass once provisionally stamped, which is the flood the dedupe
# exists to stop. Where the recurrence needs a revive, or the venue to
# contradict itself, the rows are worth having. ``_clear_read_failure_case``
# carries that comparison for its two callers, which land on opposite sides.


def _read_failure_fact_key(cloid: str) -> str:
    """The once-per-fact key for "orderStatus could not be read for this cloid".

    A DIFFERENT fact from the order's real disposition, so a different key (the
    same reasoning, and the same ``|`` suffix shape, as its mirror on the reopen
    side — ``_local_terminal_read_failure_fact_key``): both land under
    case_type ``order_missing_on_exchange``, and while they shared the bare
    cloid the first sighting to arrive owned the key — a later genuine absence
    of that cloid was swallowed by the dedupe and never recorded, which is
    exactly the pairing a venue misroute produces every tick.

    ONE function so the write site and the disposition lookup cannot drift: a
    key written one way and looked up another would leave the row open forever
    with no error anywhere.
    """
    return f"{cloid}|read_failed"


def _local_terminal_fact_key(cloid: str) -> str:
    """The reopen tiebreaker's once-per-fact key: what orderStatus ANSWERED.

    "The exchange lists this cloid open while our row is terminal" — a fact of
    its own, so a later re-settle of the same cloid must not dedupe against a
    plain-orphan sighting, and vice versa.

    Shared by the two outcomes that are readings of one situation: the reopen
    (answer LIVE, the terminal row was wrong) and the unknownOid contradiction
    (the venue's two views disagree). NOT by the read failure, which has its
    own key below — see there.
    """
    return f"{cloid}|local_terminal"


def _local_terminal_read_failure_fact_key(cloid: str) -> str:
    """The once-per-fact key for "orderStatus could not be read" HERE.

    The reopen tiebreaker's read failure is a different fact from what a
    successful read then says, and it splits off for exactly the reasons
    ``_read_failure_fact_key`` splits off the absent-order tiebreaker's — the
    two directions are mirror images and now have mirror-image keys:

    * A recorded read failure would otherwise swallow the unknownOid
      contradiction that a later pass finds under the same key: the venue
      contradicting itself is the graver fault and it would reach no durable
      row at all.
    * And the disposal would run the other way: "a later read answered" is what
      disproves the read failure, but stamping that onto a CONTRADICTION row
      would assert a read that never failed, over a detail saying it answered
      unknownOid.

    ONE function, same as its sibling: this key is looked up as well as
    written, and a lookup spelled differently from the write would leave the
    row open forever with no error anywhere.
    """
    return f"{cloid}|local_terminal_read_failed"


@dataclass(frozen=True)
class ReconciliationCase:
    """One observed §12.3 case: what, where, and whether a human must decide."""

    case_type: str
    symbol: str | None
    local_value: str | None
    exchange_value: str | None
    detail: str | None = None
    action_taken: str | None = None
    # True → §13.5 manual safe mode (non-bot order, unknown position, a fill
    # the exchange denies); False → recoverable, healable by a later pass.
    manual: bool = False
    # True → the case was RESOLVED in this pass (settled/back-filled); it is
    # recorded for the audit trail but does not make the pass unclean.
    resolved: bool = False

    def __post_init__(self) -> None:
        # Loud at construction, not at the write: the only other place this is
        # checked is ``_record``'s insert, which run() wraps in a swallow-all —
        # a typo'd case_type there would lose the audit row with nothing but a
        # log line. Validating here turns it into a failed (unclean) leg.
        check_enum(self.case_type, repo.RECONCILIATION_CASE_TYPES, name="case_type")
        # Mutually exclusive by the module's model: a manual case is one only a
        # human may dispose of, so nothing in this pass can have resolved it.
        # Enforced because ``manual_cases`` filters on ``not resolved`` — a
        # future path constructing both True would silently skip the §13.5
        # manual escalation for a case that requires it.
        if self.manual and self.resolved:
            raise ValueError(
                f"a ReconciliationCase cannot be both manual and resolved "
                f"({self.case_type}): manual means only a human may dispose of it"
            )
        # A disposition implies a resolution: ``_record``'s once-per-fact
        # restamp keys off ``action_taken`` alone, so a case carrying an action
        # while still unresolved would stamp the persisted row as disposed of
        # while the in-memory verdict (which keys off ``resolved``) stays
        # unclean — the audit trail and the verdict would diverge.
        if self.action_taken is not None and not self.resolved:
            raise ValueError(
                f"a ReconciliationCase with action_taken={self.action_taken!r} "
                f"must be resolved ({self.case_type}): recorded dispositions and "
                "the pass verdict must agree"
            )
        # Every manual case must carry a specific safe-mode reason:
        # ``reconcile_and_apply`` routes manual cases through
        # ``_MANUAL_CASE_REASONS``, and a manual case_type missing there would
        # silently enter safe mode under the generic mismatch reason.
        if self.manual and self.case_type not in _MANUAL_CASE_REASONS:
            raise ValueError(
                f"manual ReconciliationCase {self.case_type!r} has no entry in "
                "_MANUAL_CASE_REASONS — add its §13.5 safe-mode reason before "
                "constructing it as manual"
            )


@dataclass(frozen=True)
class ReconciliationReport:
    """One pass's verdict, leg by leg — the §13.4 release conditions read it."""

    trigger: str
    timestamp: datetime
    cases: tuple[ReconciliationCase, ...]
    orders_reconciled: bool
    fills_reconciled: bool
    position_reconciled: bool
    account_reconciled: bool
    position_protected: bool
    backfill_complete: bool
    errors: tuple[str, ...] = field(default_factory=tuple)
    # Legs this pass never ran, appended by the SKIPPING SITE itself (a None
    # ``backfiller`` / ``fetch_fills`` seam) so the report can never disagree
    # with what actually ran. THE canonical statement of the rule (other
    # sites just point here): a skipped leg is unproven, not proven. It is
    # deliberately OUTSIDE ``clean`` — run()'s verdict speaks for the legs it
    # ran, which the unit wirings rely on — but §13.4 auto-release attests
    # ``fully_wired`` (= no skipped legs) on try_auto_recover's signature, so
    # a half-wired caller's clean pass can hold, never lift, a latched safe
    # mode (decided 2026-07-17; the tempting shape is a PR 5 heartbeat
    # wiring without the fill seams).
    legs_skipped: tuple[str, ...] = field(default_factory=tuple)
    # The §19.3 startup stale-order sweep's per-order failures, passed in by
    # the caller that ran the sweep. A cancel that would not land leaves a
    # stale order resting, so this is a VERDICT INPUT, not an after-the-fact
    # note (decided 2026-07-17): it is inside ``clean`` — unlike
    # ``legs_skipped`` — so the pass can never read clean over it, and it is
    # carried ON the report (rather than folded into ``errors`` by the caller
    # afterwards) so that it exists BEFORE ``_record`` persists the pass.
    # Folding it in after run() returned would leave the durable
    # ``reconciliation_diff`` — and the row's ``reconciliation_status`` —
    # claiming "ok" for a pass whose verdict was unclean and which fired safe
    # mode.
    sweep_failures: tuple[str, ...] = field(default_factory=tuple)

    @property
    def reconciliation_clean(self) -> bool:
        """Every RECONCILIATION leg proved, nothing open — ignoring the §19.3 sweep.

        The verdict over what this module itself checked. ``reconcile_and_apply``
        reads it to tell "the books are fine, only the sweep failed" (which earns
        the specific ``stale_order_sweep_failed`` reason) from a real mismatch.
        """
        return (
            self.orders_reconciled
            and self.fills_reconciled
            and self.position_reconciled
            and self.account_reconciled
            and self.position_protected
            and self.backfill_complete
            and not self.errors
        )

    @property
    def clean(self) -> bool:
        """§13.4's "no unresolved mismatch": every leg proved, nothing open."""
        return self.reconciliation_clean and not self.sweep_failures

    @property
    def manual_cases(self) -> tuple[ReconciliationCase, ...]:
        return tuple(c for c in self.cases if c.manual and not c.resolved)


class LiveReconciler:
    """One run's §12 reconciliation sweep, bound to injectable exchange reads.

    ``fetch_open_orders`` / ``fetch_clearinghouse`` / ``query_order_by_cloid``
    / ``fetch_fills`` are the exchange seams (production: the signed client's
    ``open_orders``, an Info ``user_state`` read, ``query_order_by_cloid`` and
    ``user_fills_by_time``). ``backfiller`` + ``stream`` are the PR 3 fill
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
        query_order_by_cloid: Callable[[str], Any],
        fetch_fills: Callable[[int, int], Any] | None = None,
        backfiller: FillBackfiller | None = None,
        stream: LiveWsStream | None = None,
        payload_dir: Path | None = None,
        clock: Clock | None = None,
        refresh_kill_switch: Callable[[], None] | None = None,
    ) -> None:
        self._db = db
        self._run_id = run_id
        self._coin = coin
        self._fetch_open_orders = fetch_open_orders
        self._fetch_clearinghouse = fetch_clearinghouse
        self._query_order_by_cloid = query_order_by_cloid
        self._fetch_fills = fetch_fills
        self._backfiller = backfiller
        self._stream = stream
        self._payload_dir = payload_dir
        self._clock = clock or WallClock()
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

        backfill_summary = guarded(
            "fill backfill", lambda: self._run_fill_backfill(errors, legs_skipped), _FAILED_BACKFILL
        )
        orders_ok = guarded(
            "orders", lambda: self._reconcile_orders(open_orders, cases, errors, now), False
        )
        fills_ok = guarded(
            "fills", lambda: self._reconcile_fills(cases, errors, now, legs_skipped), False
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
            reason = _MANUAL_CASE_REASONS[case.case_type]
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
        return report

    # ------------------------------------------------------------- fills leg

    def _run_fill_backfill(
        self, errors: list[str], legs_skipped: list[str]
    ) -> BackfillSummary | None:
        """§12.3 "交易所有 fill，但 SQLite 沒記錄": book them via the PR 3 path.

        The epoch discipline mirrors the stream's contract: read the epoch,
        run the pass, clear with the epoch read — and only for a COMPLETE
        pass, so a capped window never retires the gap it failed to cover.
        """
        if self._backfiller is None:
            legs_skipped.append("fill_backfill")
            return None
        epoch = self._stream.backfill_epoch() if self._stream is not None else None
        if self._stream is not None:
            since = self._stream.backfill_since()
        else:
            # No WS stream in this wiring (the PR 4 startup command; the daemon
            # loop with its stream-held obligations is PR 5): the whole
            # process-was-down era is owed, so the floor is the newest booked
            # fill — or the run's genesis when none exists yet (§11.2 rule 5).
            # The trailing lookback alone would silently skip any outage longer
            # than 6h. The §11.2 v12 durable clean-backfill watermark (which
            # hardens this derivation against a crash mid-backfill) lands with
            # PR 5's daemon wiring — decided 2026-07-16.
            since = repo.last_live_fill_time(self._db.conn, self._run_id)
            if since is None:
                run_row = repo.get_run(self._db.conn, self._run_id)
                genesis_raw = None if run_row is None else run_row["created_at"]
                try:
                    since = datetime.fromisoformat(genesis_raw)
                except (TypeError, ValueError):
                    since = None
                    # Our own writer always stores ISO-8601, so this is store
                    # corruption (same reading as safe_mode's entered_at) — and
                    # the degradation is money-relevant: the floor silently
                    # becomes the bare trailing lookback, which skips any
                    # outage longer than 6h. Never degrade without a trace.
                    logger.warning(
                        "run %s genesis timestamp %r is missing/unparseable; fill "
                        "backfill floor degrades to the trailing 6h lookback — an "
                        "outage longer than that may leave fills unbooked",
                        self._run_id,
                        genesis_raw,
                    )
        try:
            summary = self._backfiller.backfill(self._clock.now(), since=since)
        except Exception as exc:  # noqa: BLE001 — transport failure = gap still open
            logger.exception("reconciliation fill backfill failed")
            errors.append(f"fill backfill failed: {exc}")
            return _FAILED_BACKFILL
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
        if summary.complete and self._stream is not None and epoch is not None:
            self._stream.mark_backfill_done(epoch)
        return summary

    def _reconcile_fills(
        self,
        cases: list[ReconciliationCase],
        errors: list[str],
        now: datetime,
        legs_skipped: list[str],
    ) -> bool:
        """The fill-ledger legs: sighting backlog + the invalid-local check."""
        ok = True
        conn = self._db.conn

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
            conn, self._run_id, case_type="fill_malformed"
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
                with self._db.transaction() as tx:
                    repo.set_reconciliation_action(tx, row["event_id"], "resolved_fill_booked")
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
        unbooked = repo.iter_unresolved_fill_sightings(conn, self._run_id)
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
        if self._fetch_fills is None:
            # The skipping site is the reporting site — see
            # ReconciliationReport.legs_skipped.
            legs_skipped.append("invalid_local_fill_crosscheck")
        if self._fetch_fills is not None:
            window_start = now - _FILL_CROSSCHECK_LOOKBACK
            logger.debug(
                "invalid-local-fill cross-check window %s → %s; local fills older "
                "than the window are outside this leg's verdict (known exemption, "
                "PR 5 watermark extends coverage)",
                window_start.isoformat(),
                now.isoformat(),
            )
            exchange_keys = self._fetch_window_fill_keys(
                self._fetch_fills, window_start, now, errors
            )
            if exchange_keys is None:
                return False  # fetch failed or window provably not covered: inconclusive
            verdict_start = window_start + _FILL_CROSSCHECK_EDGE_MARGIN
            verdict_end = now - _FILL_CROSSCHECK_EDGE_MARGIN
            for fill in repo.iter_live_fills(conn, self._run_id):
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

    def _fetch_window_fill_keys(
        self,
        fetch: Callable[[int, int], Any],
        window_start: datetime,
        now: datetime,
        errors: list[str],
    ) -> set[str] | None:
        """Every §14.2 key the exchange reports in the window, or None if unproven.

        PAGED, exactly like the backfiller: ``userFillsByTime`` caps a response
        (2000 fills), and judging "the exchange denies this fill" against a
        TRUNCATED window would flag genuinely booked fills as invalid and force
        manual safe mode on a false premise. A window that cannot be proven
        covered (page budget out, unadvanceable page) returns None — the leg
        reports inconclusive (fail-safe) instead of issuing manual verdicts.
        """
        start_ms = int(window_start.timestamp() * 1000)
        end_ms = int(now.timestamp() * 1000)
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
            # to miss — the backfiller's is in another module, this one is inline.
            # Same shape, same budget (DEFAULT_MAX_PAGES), same hazard: without a
            # refresh per page a fills-heavy window (>2000 fills, so the response
            # comes back capped and it pages again) holds the single-threaded tick
            # for up to 20 × network_timeout_s and the dead man's switch cancels
            # every resting SL/TP while the process is alive
            # (2026-07-31 deadline review, second pass).
            self._refresh_deadline()
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

    # ------------------------------------------------------------ orders leg

    def _reconcile_orders(
        self,
        open_orders: list | None,
        cases: list[ReconciliationCase],
        errors: list[str],
        now: datetime,
    ) -> bool:
        if open_orders is None:
            return False
        ok = True
        conn = self._db.conn
        exchange_open_cloids: set[str] = set()

        for order in open_orders:
            # §18.2: this loop's reopen check asks orderStatus per order, and the
            # exchange decides how many orders there are — refresh every
            # iteration so the wall of round-trips is never one unbroken gap.
            #
            # At the TOP, unlike the absent-order loop below, which refreshes
            # only for rows it is about to probe. Several branches here can reach
            # the wire and they do not share one guard, so tracking them
            # individually would be the kind of bookkeeping that silently misses
            # the branch added next year. A refresh on an iteration that turns
            # out to do no I/O costs a clock read (tick() reaches the wire only
            # when a refresh is due), which is the cheaper mistake.
            self._refresh_deadline()
            if not isinstance(order, dict):
                errors.append(f"malformed open_orders entry ({type(order).__name__})")
                ok = False
                continue
            oid = str(order.get("oid", "?"))
            coin = order.get("coin")
            cloid = order.get("cloid")
            registry = repo.get_cloid_by_hex(conn, cloid) if isinstance(cloid, str) else None
            if registry is None or not isinstance(cloid, str):
                # §12.3 row 3 / §19.3: no cloid, or a cloid our registry never
                # issued → non-bot-owned → manual safe mode; never manage it.
                ok = False
                cases.append(
                    ReconciliationCase(
                        case_type="non_bot_owned_order",
                        symbol=coin if isinstance(coin, str) else None,
                        local_value=None,
                        exchange_value=f"oid={oid}",
                        detail=(
                            f"exchange open order oid={oid} cloid={cloid!r} has no "
                            "cloid_registry mapping — not bot-owned (§19.3); manual "
                            "safe mode, the bot never manages it (§25)"
                        ),
                        manual=True,
                    )
                )
                continue
            exchange_open_cloids.add(cloid)
            local = repo.get_order_by_cloid_hex(conn, cloid)
            if local is None:
                # §12.3 row 2: bot-owned on the exchange, absent locally →
                # back-fill the local row from what the exchange reported
                # (fail-safe direction: once the row exists, every later sweep
                # — kill-switch shutdown included — sees and manages it).
                resolved = self._backfill_orphan_order(order, registry, now)
                cases.append(
                    ReconciliationCase(
                        case_type="orphan_exchange_order",
                        symbol=registry["symbol"],
                        local_value=None,
                        exchange_value=cloid,
                        detail=f"exchange open order oid={oid} had no local orders row",
                        action_taken="local_row_backfilled" if resolved else None,
                        resolved=resolved,
                    )
                )
                if not resolved:
                    ok = False
            elif local["status"] not in repo.LIVE_ORDER_STATUSES:
                # The exchange's open-orders view lists the order but the local
                # row is terminal. TWO very different stories fit this shape:
                # a past pass settled the row off a wrong unknownOid answer
                # (the send had landed — the row must reopen, §12.1), or the
                # open-orders view is merely BEHIND a cancel this very startup
                # just landed (reopening would resurrect a phantom-open row
                # for an order the run itself retired). orderStatus is the
                # tiebreaker, same as the mirror direction's non-case reading:
                # two eventually-consistent reads disagreeing is not a
                # local/exchange conflict.
                reopened, case = self._maybe_reopen_terminal_order(order, registry, local, now)
                if case is not None:
                    cases.append(case)
                if not reopened:
                    ok = False

        # §12.3 row 1: locally-live orders the exchange's open-orders view did
        # not list. Ask orderStatus directly; never resend (§8.3).
        for row in repo.iter_open_live_orders(conn):
            cloid = row["cloid_hex"]
            if cloid in exchange_open_cloids:
                continue
            # §18.2: one orderStatus round-trip per absent row, and this cursor
            # deliberately spans runs — so the count is bounded by the store's
            # history, not by anything this run configured. Refresh before each.
            self._refresh_deadline()
            settled, case = self._settle_absent_order(row, now)
            if case is not None:
                cases.append(case)
                if case.action_taken is not None:
                    # ONLY when this pass DISPOSED of the order, which is what
                    # takes it out of THIS cursor. The other outcomes leave it
                    # here, where the next pass's read is free to fail again —
                    # and a key the sweep keeps re-observing is the one shape
                    # that must not carry an automatic stamp: the stamp is
                    # provisional, so a venue flapping unreadable/readable would
                    # answer each flap with a fresh row, which is the re-sighting
                    # flood the once-per-fact dedupe exists to stop.
                    #
                    # Disposal does not make recurrence impossible — a §8.3
                    # rule-5 resend re-stamps the same order_id 'submitted'
                    # (live/orders.py) and _maybe_reopen_terminal_order revives a
                    # terminal row — it makes it need a deliberate new send or a
                    # contradicting exchange answer first. That is the bound
                    # this key relies on: on THIS side, rows come back per
                    # revive rather than per pass.
                    self._clear_read_failure_case(
                        cloid,
                        case_type="order_missing_on_exchange",
                        fact_key=_read_failure_fact_key(cloid),
                    )
            if not settled:
                ok = False
        return ok

    def _maybe_reopen_terminal_order(
        self, order: dict, registry: Any, local: Any, now: datetime
    ) -> tuple[bool, ReconciliationCase | None]:
        """Reopen a terminal local row ONLY when orderStatus proves it live.

        Returns ``(ok, case)``. orderStatus is the authority (§8.3): a LIVE
        answer proves the terminal row was wrong — reopen it (§12.1, the
        exchange wins). A TERMINAL answer proves the open-orders view is
        merely behind (typically a cancel this very startup just landed) —
        no case, nothing touched. Anything else (unknownOid contradicting the
        listing, a failed read) is an unproven conflict: recorded, unclean,
        never guessed.
        """
        cloid = local["cloid_hex"]
        oid = str(order.get("oid", "?"))
        try:
            parsed = parse_order_status(self._query_order_by_cloid(cloid), expected_cloid_hex=cloid)
        except Exception as exc:  # noqa: BLE001 — a failed read is a verdict
            # Log-at-origin, same as _settle_absent_order's sibling tiebreaker:
            # the case detail is an in-memory value (and its row write is
            # fail-soft), so without this line a transient API error here
            # would leave no trace of WHY this order failed to reconcile.
            logger.warning("orderStatus tiebreaker for cloid %s failed: %s", cloid, exc)
            return False, ReconciliationCase(
                case_type="orphan_exchange_order",
                symbol=registry["symbol"],
                local_value=f"{local['order_id']}:{local['status']}",
                exchange_value=_local_terminal_read_failure_fact_key(cloid),
                detail=(
                    f"exchange lists oid={oid} open but the local row is terminal "
                    f"({local['status']}) and orderStatus failed: {exc}"
                ),
            )
        # The read ANSWERED, whatever it answered — so the fact "we could not
        # ask this cloid" is disproved, for all three outcomes below. Disposing
        # of it here rather than per branch is the whole reason that fact has a
        # key of its own: the unknownOid branch below records a DIFFERENT fault
        # (the venue contradicting itself), and stamping "the read succeeded"
        # onto that row would say something that never happened (issue #66).
        self._clear_read_failure_case(
            cloid,
            case_type="orphan_exchange_order",
            fact_key=_local_terminal_read_failure_fact_key(cloid),
        )
        if parsed is not None:
            exchange_order_id, raw_status = parsed
            local_status = local_status_for_exchange_status(raw_status)
            if local_status in repo.LIVE_ORDER_STATUSES:
                # Confirmed live: the terminal row was wrong (the usual story:
                # a past pass settled it off a wrong unknownOid answer). The
                # row reopens, making the order visible again to
                # iter_open_live_orders and the shutdown cross-check. Persist the
                # MAPPED local status, never a literal "open": only "open" passes the
                # guard today, but hardcoding it is the same idiom that let the
                # protection manager misrecord a filled/canceled recovery as live.
                with self._db.transaction() as tx:
                    repo.update_order(
                        tx,
                        local["order_id"],
                        status=local_status,
                        status_reason="reopened_from_exchange_reconciliation",
                        exchange_order_id=exchange_order_id,
                        exchange_status=local_status,
                        exchange_raw_status=raw_status,
                        updated_at=now,
                    )
                return True, ReconciliationCase(
                    case_type="orphan_exchange_order",
                    symbol=registry["symbol"],
                    local_value=f"{local['order_id']}:{local['status']}",
                    # Distinct fact key: a later re-settle of the same cloid
                    # must not dedupe against a plain-orphan sighting.
                    exchange_value=_local_terminal_fact_key(cloid),
                    detail=(
                        f"exchange lists oid={oid} open, orderStatus confirms "
                        f"{raw_status!r}, but the local row was terminal "
                        f"({local['status']}) — reopened per §12.1"
                    ),
                    action_taken="local_row_reopened",
                    resolved=True,
                )
            # orderStatus says terminal too: the open-orders view is behind
            # (a cancel this startup just landed is the common cause). Two
            # eventually-consistent reads disagreeing for a moment is not a
            # local/exchange conflict — same reading as the mirror direction.
            # No case, therefore no ``_record`` restamp: the read failure this
            # pass disproved was disposed of above, where every answered read
            # is treated alike (issue #66 — before that, this outcome, which is
            # the COMMON one, left the read-failure row open forever).
            return True, None
        # unknownOid while open_orders LISTS the order: contradictory exchange
        # answers — unproven either way, never guessed.
        return False, ReconciliationCase(
            case_type="orphan_exchange_order",
            symbol=registry["symbol"],
            local_value=f"{local['order_id']}:{local['status']}",
            exchange_value=_local_terminal_fact_key(cloid),
            detail=(
                f"exchange lists oid={oid} open but orderStatus answers unknownOid "
                f"and the local row is terminal ({local['status']}) — contradictory "
                "exchange answers, left for the next pass"
            ),
        )

    def _backfill_orphan_order(self, order: dict, registry: Any, now: datetime) -> bool:
        """Insert the missing local row for a bot-owned exchange order."""
        try:
            side_raw = order.get("side")
            side = HL_SIDE_TO_LOCAL.get(side_raw) if isinstance(side_raw, str) else None
            if side is None:
                raise ValueError(f"open_orders entry side {side_raw!r} not recognised")
            # `is None` fallback, not dict.get's default: a PRESENT-but-null
            # origSz must still fall through to sz (get's default only covers
            # the absent-key case, and Decimal(str(None)) raises).
            raw_orig = order.get("origSz")
            if raw_orig is None:
                raw_orig = order.get("sz")
            qty = Decimal(str(raw_orig))
            remaining = Decimal(str(order.get("sz")))
            price = order.get("limitPx")
            # The registry role is the bot's own durable record of what it
            # placed — through PR 4 the only wire type is ioc_limit, but the
            # registry already carries stop_loss/take_profit roles, and once
            # PR 5 places trigger orders an orphaned SL backfilled as
            # "ioc_limit" would be a permanent audit-row mislabel.
            order_type = repo.ROLE_TO_ORDER_TYPE.get(registry["order_role"], "ioc_limit")
            with self._db.transaction() as conn:
                repo.insert_order(
                    conn,
                    # Deterministic and collision-free: one orphan row per cloid
                    # (idx_orders_cloid_hex would reject a second anyway).
                    order_id=f"orphan|{registry['cloid_hex']}",
                    mode="live",
                    run_id=registry["run_id"],
                    symbol=registry["symbol"],
                    order_role=registry["order_role"],
                    side=side,
                    order_type=order_type,
                    qty=qty,
                    filled_qty=qty - remaining,
                    remaining_qty=remaining,
                    status="open",
                    status_reason="backfilled_from_exchange_reconciliation",
                    price=None if price is None else Decimal(str(price)),
                    reduce_only=bool(order.get("reduceOnly", False)),
                    cloid_logical=registry["cloid_logical"],
                    cloid_hex=registry["cloid_hex"],
                    exchange_order_id=str(order.get("oid")),
                    exchange_status="open",
                    # The raw-status column stores the exchange's STATUS word
                    # (every other writer stores "open"/"filled"/…); the source
                    # here is presence in the open-orders listing, so "open" —
                    # the earlier draft stored the orderType word ("Limit"),
                    # which polluted the status vocabulary.
                    exchange_raw_status="open",
                    is_bot_owned=True,
                    timestamp=now,
                )
            return True
        except Exception as exc:  # noqa: BLE001 — an unfillable orphan stays a mismatch
            logger.warning(
                "could not back-fill orphan order for cloid %s: %s", registry["cloid_hex"], exc
            )
            return False

    def _settle_absent_order(
        self, row: Any, now: datetime
    ) -> tuple[bool, ReconciliationCase | None]:
        """One locally-live order absent from open_orders, put to orderStatus.

        Returns ``(settled, case)``. The §8.3 rule-10 evidence split decides
        the unknownOid answer: durable proof the exchange took the cloid makes
        "I don't know it" a MISMATCH (never a licence to resend); no proof
        means the send never landed, and the row is settled as rejected.
        """
        cloid = row["cloid_hex"]
        order_id = row["order_id"]
        try:
            payload = self._query_order_by_cloid(cloid)
            parsed = parse_order_status(payload, expected_cloid_hex=cloid)
        except Exception as exc:  # noqa: BLE001
            logger.warning("orderStatus for cloid %s failed: %s", cloid, exc)
            return False, ReconciliationCase(
                case_type="order_missing_on_exchange",
                symbol=row["symbol"],
                local_value=order_id,
                exchange_value=_read_failure_fact_key(cloid),
                detail=f"absent from open_orders and orderStatus failed: {exc}",
            )
        if parsed is None:
            if repo.has_exchange_known_cloid(self._db.conn, cloid_hex=cloid):
                return False, ReconciliationCase(
                    case_type="order_missing_on_exchange",
                    symbol=row["symbol"],
                    local_value=order_id,
                    exchange_value=cloid,
                    detail=(
                        "orderStatus answers unknownOid but durable local evidence "
                        "says the exchange took this cloid (§8.3 rule 10) — "
                        "unresolvable here, never resent"
                    ),
                )
            # No proof the exchange ever saw it: the send never landed.
            with self._db.transaction() as conn:
                repo.update_order(
                    conn,
                    order_id,
                    status="rejected",
                    status_reason="send_never_reached_exchange",
                    updated_at=now,
                )
            return True, ReconciliationCase(
                case_type="order_missing_on_exchange",
                symbol=row["symbol"],
                local_value=order_id,
                exchange_value=cloid,
                detail="unknownOid with no §8.3 rule-10 evidence — settled as rejected",
                action_taken="settled_never_sent",
                resolved=True,
            )
        exchange_order_id, raw_status = parsed
        local_status = local_status_for_exchange_status(raw_status)
        if local_status in repo.LIVE_ORDER_STATUSES:
            # Still live per orderStatus; open_orders was merely behind. Not a
            # §12.3 case — two eventually-consistent exchange reads disagreeing
            # for a moment is not a local/exchange conflict.
            return True, None
        with self._db.transaction() as conn:
            repo.update_order(
                conn,
                order_id,
                status=local_status,
                exchange_order_id=exchange_order_id,
                exchange_status=local_status,
                exchange_raw_status=raw_status,
                updated_at=now,
            )
        return True, ReconciliationCase(
            case_type="order_missing_on_exchange",
            symbol=row["symbol"],
            local_value=order_id,
            exchange_value=cloid,
            detail=f"settled from orderStatus: {raw_status}",
            action_taken=f"settled_{local_status}",
            resolved=True,
        )

    def _clear_read_failure_case(self, cloid: str, *, case_type: str, fact_key: str) -> None:
        """Dispose of a past unreadable-orderStatus row that a later read disproved.

        ``_record``'s restamp reaches only rows under the SAME fact key, and
        each tiebreaker's read failure deliberately has one of its own (see
        ``_read_failure_fact_key`` and ``_local_terminal_read_failure_fact_key``)
        — so nothing else would ever close these rows. Left open, one transient
        API error would hold the §21.4 ``unresolved_reconciliation_mismatch``
        count above zero for the rest of the run, stampable only by hand, for a
        fact a later pass disproved.

        Both callers pass their own ``case_type``/``fact_key`` pair; the two
        keys land under DIFFERENT case types (the absent-order tiebreaker files
        ``order_missing_on_exchange``, the reopen tiebreaker
        ``orphan_exchange_order``), which is why neither is derived here.

        What "disproved" means differs by caller, and the difference is
        deliberate rather than an oversight in either:

        * The reopen tiebreaker stamps on the successful read itself, whatever
          it answered. It can therefore be re-observed without any revive: the
          local row stays terminal and the exchange goes on listing it, so an
          orderStatus that alternates unreadable/readable mints a row per flap.
          Accepted, because those rows are CLOSED ones — `safe-mode --status`
          and the §21.4 count read only the current occurrence, and the exit-5
          ``orphan_exchange_order_count`` gate already fails at one row. What is
          bought is the common case: "orderStatus says terminal too" produces no
          case at all, so nothing else would ever close this row (issue #66).
        * The absent-order tiebreaker stamps only once the pass SETTLED the
          order, which is what takes it out of a cursor that would otherwise
          re-observe it every pass (see there). Two successful-read outcomes
          therefore leave its row open: unknownOid against §8.3 rule-10 evidence
          (that order already needs a human, so the extra row sits on a §12.3
          case someone is reading anyway), and "still live per orderStatus" —
          where the order may then be retired by a writer that is not this sweep
          (a §19.3 cancel, the kill switch, the protection manager), and the row
          holds §21.4's unresolved count above zero with nothing else reporting
          a problem. Accepted for the opposite reason: here the flap WOULD be
          per pass, and the open row is the cheaper of the two costs.

        Fail-soft, like the liquidation mirror: this is the audit trail's
        disposition, not a verdict input — a store that refuses the stamp must
        not fail the orders leg. The cost of losing it is one stale open row,
        the same shape the callers' guards deliberately leave behind elsewhere.
        """
        try:
            existing = repo.get_exchange_reconciliation_case(
                self._db.conn,
                self._run_id,
                case_type=case_type,
                exchange_value=fact_key,
            )
            # Pre-checked outside the write unit deliberately: a successful read
            # is the common case and almost never has a row to close, and
            # opening a BEGIN IMMEDIATE per absent order to discover that would
            # be the expensive way to do nothing.
            if existing is None or existing["action_taken"] is not None:
                return
            with self._db.transaction() as tx:
                # Which is exactly why the write has to be the if-unset one
                # rather than _record's set_reconciliation_action: the check
                # above is a separate step, so an operator (or a `--stamp-case`
                # racing this pass) may have disposed of the row in between, and
                # THEIR disposition is the one a human will look for
                # (2026-07-30 concurrency review).
                repo.stamp_reconciliation_action_if_unset(
                    tx, existing["event_id"], "resolved_read_succeeded"
                )
        except Exception as exc:  # noqa: BLE001 — audit disposition, see above
            logger.warning(
                "could not stamp the resolved orderStatus read failure for cloid %s "
                "(fact %s; %s: %s); the row stays open for a later pass or a human",
                cloid,
                fact_key,
                type(exc).__name__,
                exc,
            )

    # ---------------------------------------------------------- position leg

    def _reconcile_positions(
        self,
        open_orders: list | None,
        snapshot: AccountSnapshot | None,
        cases: list[ReconciliationCase],
    ) -> tuple[bool, bool]:
        """§12.3 position rows + the SL-protection invariant → (ok, protected)."""
        if snapshot is None:
            return False, False
        ok = True
        conn = self._db.conn

        # Any exchange position OUTSIDE the configured symbol is unknown to
        # this run — §13.5 "unknown exchange position" → manual.
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

        exch = snapshot.position_for(self._coin)
        exch_size = Decimal(0) if exch is None else exch.size
        local = repo.get_current_position(conn, self._run_id, self._coin)
        local_size = Decimal(0) if local is None else local.size

        # Mirror the exchange-reported liquidation estimate onto the local row
        # (§12.1: the exchange is the truth source). The engine's SL band and
        # the ai_inputs ``estimated_liquidation_price`` column read it back via
        # ``get_current_position``. An explicit ``None`` when the exchange is
        # flat — or reports no liquidationPx — so a stale estimate never
        # survives a flat; only reached with a SUCCESSFUL clearinghouse read
        # (a failed read returns above and proves nothing either way). Skipped
        # without a local row: the writer is UPDATE-only, so it would be a
        # guaranteed no-op, and the size-mismatch lane below owns that case.
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
        # positions agree?", and the successful read above already answered it.
        # The write is a cache for the SL band, not the evidence this leg
        # produces — letting a transient store error retroactively mark the
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

        if exch_size != local_size:
            ok = False
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

        # §12.3 last row / §17: an exchange position without a valid SL.
        protected = True
        if exch is not None:
            protected = self._has_valid_sl(open_orders, exch)
            # The case row is written only when the absence was OBSERVED: with
            # open_orders None the truth is "could not look", already carried
            # by the orders-leg error and the fail-safe ``protected=False`` —
            # a durable row asserting "no valid SL" off a failed read would
            # tell a post-mortem reader protection had lapsed when it may not
            # have (and occupy the fact's once-per-fact dedupe slot).
            if not protected and open_orders is not None:
                sl_detail = (
                    f"live position of {exch_size} has no valid reduce-only SL "
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
                        local_value=None if local is None else str(local_size),
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
        return ok, protected

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
                covered += Decimal(str(order.get("sz")))
            except Exception:  # noqa: BLE001 — an unparseable size covers nothing
                # Fail-safe direction (counts as zero coverage), but never
                # silently: the sibling degradation paths in this module all
                # leave a trace, and "SL judged missing because a size failed
                # to parse" must be diagnosable from the log.
                logger.warning(
                    "SL order sz %r (cloid %s) is unparseable while proving §17.1 "
                    "coverage; counting it as zero",
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
        """Persist the pass: case rows + the §16.3/§16.4 snapshot rows."""
        now = report.timestamp
        status = "ok" if report.clean else "mismatch"
        diff = json.dumps(
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
        raw_path = None
        if self._payload_dir is not None and raw_clearinghouse is not None:
            raw_path = write_raw_payload(
                payload_dir=self._payload_dir,
                kind="clearinghouse",
                key=self._run_id,
                payload=raw_clearinghouse,
                now=now,
            )
        # Each case is an INDEPENDENT fact and gets its own transaction (the
        # same isolation the snapshot rows below already have): one case's
        # write failing on a busy store must not roll back — or stop — the
        # sibling facts observed in the same pass. The row a human most needs
        # (the manual case that fired safe mode) would otherwise vanish with
        # an unrelated row's failure, leaving the pass's audit trail empty.
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
                        timestamp=now,
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
        if backfill_summary is not None and backfill_summary.applied > 0:
            # §12.3 row 5, resolved in-pass: fills the exchange had that
            # SQLite lacked were booked through the PR 3 path. Not deduped
            # (no exchange_value): each pass that booked something is its
            # own event.
            try:
                with self._db.transaction() as conn:
                    repo.insert_exchange_reconciliation_event(
                        conn,
                        run_id=self._run_id,
                        trigger=report.trigger,
                        case_type="exchange_fill_missing_local",
                        symbol=self._coin,
                        action_taken="backfilled",
                        detail=(
                            f"booked {backfill_summary.applied} missing fill(s) via REST "
                            f"backfill ({backfill_summary.fetched} fetched, "
                            f"{backfill_summary.duplicate} duplicate)"
                        ),
                        timestamp=now,
                    )
            except Exception:  # noqa: BLE001 — same isolation as the case rows
                logger.exception("reconciliation backfill event row could not be recorded")
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

    def _write_snapshots(
        self,
        conn: Any,
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
        # the paper engine's writer uses (paper.accounting) under the SAME
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
