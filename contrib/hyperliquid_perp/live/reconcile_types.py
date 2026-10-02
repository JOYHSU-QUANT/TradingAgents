"""The §12 reconciliation sweep's types and vocabulary.

What every leg of the sweep constructs or reads and what its callers consume:
:class:`ReconciliationCase` (one observed §12.3 case),
:class:`ReconciliationReport` (one pass's verdict), :class:`SweepContext`
(what the fill and orders legs read off the reconciler for one pass), and
the machine-disposition constants, each the value of a member of the
registry's two disposition enums. The sweep itself is
:mod:`.reconcile` (``LiveReconciler``), with the fill legs in
:mod:`.reconcile_fills` and the orders leg in :mod:`.reconcile_orders`.
The disposition constants and ``MANUAL_CASE_REASONS`` are the sweep's
package-internal vocabulary: shared across the four modules and read
outside them only by the tests.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from ..common.enum_guard import check_enum
from ..persistence import repository as repo
from .safe_mode import (
    REASON_INVALID_LOCAL_FILL,
    REASON_NON_BOT_OWNED_ORDER,
    REASON_UNKNOWN_POSITION,
)

if TYPE_CHECKING:
    # Annotation-only: this leaf module names the types the reconciler binds
    # but never needs them at import (``from __future__ import annotations``).
    from ..persistence.db import Database
    from ..ports import Clock
    from .fill_backfill import FillBackfiller
    from .venue_identity import VenueIdentityMonitor
    from .ws_stream import LiveWsStream

__all__ = [
    "FILL_BACKFILLED_DISPOSITION",
    "FILL_BOOKED_DISPOSITION",
    "MANUAL_CASE_REASONS",
    "ORPHAN_BACKFILLED_DISPOSITION",
    "READ_SUCCEEDED_DISPOSITION",
    "ReconciliationCase",
    "ReconciliationReport",
    "SweepContext",
]

# §13.5 manual case → safe-mode entry reason. ONE definition, tied to the
# construction sites by ``ReconciliationCase.__post_init__`` (a manual case
# whose case_type is missing here fails loud at construction): without that
# guard, a future manual case type added without its reason would silently
# enter safe mode under the generic mismatch reason, hiding the specific fact
# from the §13.6 triage surface.
MANUAL_CASE_REASONS = {
    "non_bot_owned_order": REASON_NON_BOT_OWNED_ORDER,
    "invalid_local_fill": REASON_INVALID_LOCAL_FILL,
    "exchange_position_mismatch": REASON_UNKNOWN_POSITION,
}

# Choosing an exchange_value for a NEW order/position fact — the order keys in
# ``reconcile_orders``, the position and equity keys in ``LiveReconciler``'s
# position and account legs (the fill-side keys are §14.2's and §11.3's, and
# answer to those specs, not to this note):
# ask whether the fact is an INVARIANT that stands until someone disposes of it
# — an off-coin holding, an uncovered position, an equity gap — or an EPISODE
# that recurs as distinct occurrences.
#   Invariant → key it on the subject alone (``BTC|sl_missing``,
#     ``equity_out_of_tolerance``). Whatever varies while it stands belongs in
#     the row's detail, the per-pass reconciliation_diff, and a warning log —
#     never in the key, or every change of it mints a row and a manual stamp.
#   Episode   → key it on what makes the occurrence distinct (the position-size
#     transition in ``LiveReconciler._compare_position_sizes``): an independent
#     later mismatch of a different magnitude is its own fact, not a repeat
#     of the first.
# The dedupe carries no symbol column, so either way the coin/cloid stays IN the
# key.
#
# Then ask, for a fact the sweep DISPOSES of automatically, whether the
# episode can start over — because a stamped key is normally shut for good, and
# a recurrence under it would reach neither `safe-mode --status` nor §21.4's
# unresolved count. Ask it PER STAMP — being about an order does not settle it.
# Most of the orders leg's stamps dispose of a fact the sweep can find itself
# facing again (a §8.3 rule-5 resend or a reopen puts the cloid back; the reopen
# tiebreaker's read failure needs only the venue's next answer to flip), so those are
# declared PROVISIONAL (repo.PROVISIONAL_DISPOSITIONS) so the next occurrence
# gets its own row. What is NOT provisional: a human's `--stamp-case`
# disposition (their answer must not be re-asked on every pass thereafter), and
# a fact whose subject cannot return — ``local_row_backfilled`` is an order
# stamp and stays out, because once the local row exists it cannot go missing
# again (this package contains no `DELETE FROM orders`).
#
# Reopenability is not a licence to stamp early, either. How often the fact can
# return is one thing to weigh — a fault the sweep re-observes every pass with
# no venue state change in between (the §8.3 rule-10 order that never leaves the
# cursor) would mint a row per pass if its disposition were provisional, which
# is why the human stamp that disposes of one is final. The read-failure keys
# are not that shape: minting needs a failed read and the stamp before it a
# successful one, so on EITHER of them the ceiling is a row per
# unreadable→readable flap. What separates their two guards is whether anything
# else would ever close the row — see
# ``reconcile_orders._clear_read_failure_case``.


# The four dispositions whose ``ReconciliationCase.__post_init__`` check
# cannot stand in front of the write. Three are written WITHOUT building a
# ``ReconciliationCase`` at all — two stamped onto an already-persisted row,
# one passed straight to the event insert — and would otherwise have no tie
# to the vocabulary (issue #84). The fourth, the orphan back-fill's stamp,
# DOES build a case, but only after ``insert_order`` has committed: the stamp
# depends on whether the back-fill succeeded, so its __post_init__ runs on the
# wrong side of the write and an unclassified rename would leave an orders
# row with no case row explaining it (issue #104). Every other disposition
# travels through a ``ReconciliationCase`` built before its write.
#
# Each is therefore the value of a registry enum member, not a string typed
# here (the registry's MACHINE_DISPOSITIONS comment says what that settles).
# A check at the write would not be enough for them: two of the four sites
# are deliberately fail-soft (``reconcile_orders._clear_read_failure_case``,
# ``LiveReconciler._record_backfill_event``), so a stamp refused there is one
# log line and nothing else.
FILL_BOOKED_DISPOSITION = repo.FinalDisposition.RESOLVED_FILL_BOOKED.value
READ_SUCCEEDED_DISPOSITION = repo.ProvisionalDisposition.RESOLVED_READ_SUCCEEDED.value
FILL_BACKFILLED_DISPOSITION = repo.FinalDisposition.BACKFILLED.value
ORPHAN_BACKFILLED_DISPOSITION = repo.FinalDisposition.LOCAL_ROW_BACKFILLED.value


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
        # checked is ``LiveReconciler._record_cases``'s insert, which run()
        # wraps in a swallow-all — a typo'd case_type there would lose the
        # audit row with nothing but a log line. Validating here turns it into
        # a failed (unclean) leg.
        check_enum(self.case_type, repo.RECONCILIATION_CASE_TYPES, name="case_type")
        # Same argument one field over (issue #84). Every ReconciliationCase is
        # constructed by the SWEEP — a human's disposition is written straight
        # to the row by ``safe-mode --stamp-case`` (via
        # ``stamp_reconciliation_action_if_unset``) and never passes through
        # here — so ``action_taken`` is machine vocabulary, and the set that
        # decides whether a fact key REOPENS (repo.PROVISIONAL_DISPOSITIONS)
        # matches it by string. A new or renamed disposition that nobody
        # classified there fails silently in exactly the #65 direction: that
        # key shuts forever. Loud here, at the construction that introduced it.
        if self.action_taken is not None:
            check_enum(self.action_taken, repo.MACHINE_DISPOSITIONS, name="action_taken")
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
        # A disposition implies a resolution: ``LiveReconciler._record_cases``'s
        # once-per-fact restamp keys off ``action_taken`` alone, so a case
        # carrying an action while still unresolved would stamp the persisted
        # row as disposed of while the in-memory verdict (which keys off
        # ``resolved``) stays unclean — the audit trail and the verdict would
        # diverge.
        if self.action_taken is not None and not self.resolved:
            raise ValueError(
                f"a ReconciliationCase with action_taken={self.action_taken!r} "
                f"must be resolved ({self.case_type}): recorded dispositions and "
                "the pass verdict must agree"
            )
        # Every manual case must carry a specific safe-mode reason:
        # ``LiveReconciler.apply_manual_cases`` routes manual cases through
        # ``MANUAL_CASE_REASONS``, and a manual case_type missing there would
        # silently enter safe mode under the generic mismatch reason.
        if self.manual and self.case_type not in MANUAL_CASE_REASONS:
            raise ValueError(
                f"manual ReconciliationCase {self.case_type!r} has no entry in "
                "MANUAL_CASE_REASONS — add its §13.5 safe-mode reason before "
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
    # afterwards) so that it exists BEFORE ``LiveReconciler._record`` persists
    # the pass. Folding it in after run() returned would leave the durable
    # ``reconciliation_diff`` — and the row's ``reconciliation_status`` —
    # claiming "ok" for a pass whose verdict was unclean and which fired safe
    # mode.
    sweep_failures: tuple[str, ...] = field(default_factory=tuple)

    @property
    def reconciliation_clean(self) -> bool:
        """Every RECONCILIATION leg proved, nothing open — ignoring the §19.3 sweep.

        ``reconcile_and_apply`` reads it to tell "the books are fine, only the
        sweep failed" (which earns the specific ``stale_order_sweep_failed``
        reason) from a real mismatch.
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


@dataclass(frozen=True)
class SweepContext:
    """What the fill and orders legs read off the reconciler for ONE pass.

    ``LiveReconciler.run`` builds it after the pass's two account reads, once
    the seams are bound (the backfiller can be attached after construction),
    and hands it to :mod:`.reconcile_fills` and :mod:`.reconcile_orders` in
    place of the reconciler itself, so a leg reaches the reconciler only
    through what this names (``refresh_deadline`` is the reconciler's own
    bound method). The position and account legs and the recording read the
    reconciler directly. Built only by ``LiveReconciler._sweep_context``: the
    seam fields (``fetch_fills``, ``backfiller``, ``stream``, ``identity`` and
    the refresh behind ``refresh_deadline``) were checked when the reconciler
    bound them, and nothing is re-validated here.
    """

    db: Database
    run_id: str
    # The shared §13.5 venue-identity monitor every per-order orderStatus read
    # goes through (``LiveReconciler.__init__`` says why it is one instance).
    identity: VenueIdentityMonitor
    # The fill-leg seams as bound for this pass. For ``fetch_fills`` and
    # ``backfiller``, ``None`` is the reads-only wiring and lands in
    # ``ReconciliationReport.legs_skipped``.
    fetch_fills: Callable[[int, int], Any] | None
    backfiller: FillBackfiller | None
    # ``None`` in every wiring today, and not a skipped leg: the backfill
    # then floors on the newest booked fill, or the run's genesis
    # (``run_fill_backfill``).
    stream: LiveWsStream | None
    clock: Clock
    # §18.2: refreshes the dead man's switch across a leg's blocking work.
    refresh_deadline: Callable[[], None]
