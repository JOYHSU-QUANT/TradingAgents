"""Acceptance validator for one paper run (phase2-spec §5).

Read-only: recomputes what the spec says must be recomputable and counts what
must be traceable, entirely from the SQLite store —

- the 13 summary metrics (§5 驗收輸出指標);
- chain integrity: every order traces to a persisted ``output_id`` (system
  reduce-only orders — SL / TP / liquidation / emergency closes — instead must
  correspond to a position that existed, i.e. an earlier fill or a seed
  position), and every fill references a persisted order;
- snapshot recomputability: each ``account_snapshots`` / ``position_snapshots``
  row must satisfy its own arithmetic identities under the pinned
  ``DECIMAL_CONTEXT`` (the writers computed them under the same pin, so
  equality is exact, not approximate);
- accounting replay consistency (:func:`accounting.replay`).

The Phase-3 gate (§5 可以進 Phase 3 的條件): ``cycle_count >= 30``, zero
orphans / snapshot mismatches / replay mismatches, and no CURRENT run of
cycles that all failed to decide (issue #50). ``total_pnl > 0`` is
explicitly *not* a criterion.

A checker that *raises* on a corrupt store (a cell ``Decimal()`` cannot read,
a replay that cannot complete) is contained as an integrity outcome — a
counted failure line and the CLI's exit-5 lane, never a generic crash —
extending :func:`reconcile.classify_replay`'s "unverifiable books are an
outcome" rule to the acceptance report. Metrics the raise makes underivable
print as ``n/a`` (never fabricated); everything computed before it survives.

Non-gating ``warnings`` also surface store-persisted completeness signals the
operator would otherwise only find in a dead process's log: funding events
still pending long past settlement (their P&L is uncounted by design) and a
config drift recorded at the last resume.

One deliberate exception to "read-only": this module also owns the shared
no-decision escalation policy (issue #50) — the streak threshold, the store
query behind it, the recency window, the shortfall wording, and the per-cycle
log escalation the two RUNNING loops call (:func:`note_cycle_outcome`). It
lives here because the paper and live *validators* are its other two
consumers and this is the module they already share; a reader should not take
the "read-only" sentence above as covering that one function.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext

from ..common.constants import STALE_MARKET_DATA_ERROR
from ..persistence import repository as repo
from ..persistence.db import Database
from ..persistence.models import DECIMAL_CONTEXT
from . import accounting
from .reconcile import STALE_PENDING_FUNDING
from .scheduler import CYCLE_INTERVAL, parse_instant

__all__ = [
    "MIN_CYCLES_FOR_PHASE3",
    "NO_DECISION_STREAK_THRESHOLD",
    "TrailingFailureStreaks",
    "ValidationReport",
    "no_decision_shortfall",
    "note_cycle_outcome",
    "trailing_failure_streaks",
    "validate_run",
]

logger = logging.getLogger(__name__)

MIN_CYCLES_FOR_PHASE3 = 30

# Consecutive trailing cycles that reached NO decision (``api_failed``, whatever
# their §6.2 class) at or past which the run is "not at the gate" (issue #50).
# Three = 12h of the 4h cadence, the same "three decision cycles" the freshness
# guard itself bounds candle age at, and the count the repo's other escalations
# use (funding-source ERROR, repeated-mismatch manual safe mode). A shortfall
# (exit 4), never an integrity failure: the store is sound, the run's INPUTS are
# not, and the streak clears by itself the moment a cycle reaches a decision
# again — so an exchange maintenance window reads as "not yet", not as a verdict
# that survives it.
#
# Deliberately class-BLIND, with the stale-feed subset reported separately for
# its better wording: keying the gate on ``stale_market_data`` alone would have
# left every OTHER way of failing every cycle forever — including a failure of
# the very l2Book endpoint the freshness guard now depends on, which files as
# ``connection`` / ``malformed_response`` — exactly as silent as the stalled
# feed was before this mechanism existed.
NO_DECISION_STREAK_THRESHOLD = 3

# How recently the newest terminal cycle must have CHANGED STATE for a streak
# to still describe the run's current state. Past this the run is stopped or
# archived, and the streak is a fact about its last hours, not a reason it
# "cannot decide now" — without this an acceptance run that happened to end
# during an exchange maintenance window would report exit 4 forever, with
# "start it again and get one more decided cycle" as the only remedy. Two
# cycles: one is inside the ordinary jitter of a live run's next boundary.
_STREAK_RECENCY_WINDOW = 2 * CYCLE_INTERVAL


@dataclass(frozen=True)
class TrailingFailureStreaks:
    """How long the run has been failing to decide, counted back from the newest.

    A frozen dataclass rather than a ``NamedTuple`` for one reason: the
    invariants below have to be ENFORCED. ``NamedTuple`` builds through
    ``__new__`` and never calls ``__post_init__``, so the same guard written on
    a NamedTuple is decoration — it was, until a test tried to trip it.

    ``no_decision`` is the run of trailing ``api_failed`` cycles whatever their
    class; ``stale_feed`` is the leading (newest-first) part of that run whose
    class is ``stale_market_data``, so it is always <= ``no_decision`` and is
    reported separately only to earn its more specific operator wording.

    ``latest_terminal_at`` is when the newest terminal cycle last CHANGED STATE
    (its ``timestamp``), which is what decides whether these numbers still
    describe the run's current state (see :data:`_STREAK_RECENCY_WINDOW`).
    Deliberately not ``scheduled_at``: a cycle stranded by a crash is
    terminalized on restart carrying its ORIGINAL slot, hours or days old, so
    dating the streak by the slot would read a run that just came back as
    stopped — and suppress the shortfall on exactly the run that has been
    unable to decide the longest. ``None`` when the newest row's stamp cannot
    be parsed at all; the caller then withholds a verdict rather than guessing.
    """

    no_decision: int
    stale_feed: int
    latest_terminal_at: datetime | None

    def __post_init__(self) -> None:
        # Both reports print these on every run, so a nonsensical pair would
        # render raw into the summary; and ``no_decision_shortfall`` picks its
        # wording from ``stale_feed >= no_decision``, which a violated
        # ordering would turn into "all refused as stale market data" over a
        # streak that was mostly something else. The query cannot produce
        # either, but this type is also built by hand (tests, any future
        # caller) — the same reason the sibling reports guard their counts.
        if self.no_decision < 0 or self.stale_feed < 0:
            raise ValueError(
                f"TrailingFailureStreaks counts must be >= 0, got "
                f"no_decision={self.no_decision}, stale_feed={self.stale_feed}"
            )
        if self.stale_feed > self.no_decision:
            raise ValueError(
                f"stale_feed ({self.stale_feed}) is a subset of no_decision "
                f"({self.no_decision}) and cannot exceed it"
            )


# Cycles that actually produced (or fail-closed) a decision. api_failed is
# deliberately NOT counted toward cycle_count / the ≥30 gate: spec §3.1 grants
# "此 cycle 視為已完成" to invalid_output only, and an api_failed cycle never
# exercised the decision→order→fill chain the 30-cycle run exists to validate.
# It is reported separately as api_failed_count.
_COMPLETED_CYCLE_STATUSES = ("completed", "invalid_output")

# Import-time completeness guard: this subset must be exactly "every terminal
# attempt status except api_failed". A new terminal status added to the
# canonical vocabulary fails here, forcing an explicit decision on whether it
# counts toward the ≥30-cycle gate instead of being silently missed.
if set(repo.TERMINAL_ATTEMPT_STATUSES) - set(_COMPLETED_CYCLE_STATUSES) != {"api_failed"}:
    raise ValueError(
        "_COMPLETED_CYCLE_STATUSES must cover every terminal attempt status "
        "except api_failed; the vocabulary drifted"
    )


@dataclass(frozen=True)
class ValidationReport:
    """The §5 summary metrics plus the Phase-3 verdict for one run."""

    run_id: str
    cycle_count: int
    api_failed_count: int
    order_count: int
    fill_count: int
    rejected_order_count: int
    orphan_order_count: int
    orphan_fill_count: int
    snapshot_mismatch_count: int
    accounting_replay_mismatch_count: int
    max_exposure_pct: Decimal | None
    max_effective_leverage: Decimal | None
    # The three replay-derived ledger totals are None — printed ``n/a`` — when
    # the accounting replay itself raised (books unverifiable, a counted
    # failure); a fabricated 0 would misread as a verified flat ledger.
    realized_pnl: Decimal | None
    # Unrealized leg of ``total_pnl``, valued at ``unrealized_as_of`` (the last
    # account snapshot's mark, up to ~one cycle stale) — surfaced separately so
    # the headline ``total_pnl`` doesn't silently fold a stale/absent valuation.
    # ``unrealized_as_of`` is None when the run has no account snapshot at all,
    # in which case the unrealized leg is 0 (an open position reads as flat for
    # PnL — the staleness the split makes explicit). The leg itself is None
    # when the stored cell is unreadable (that row is already a counted
    # snapshot failure): unavailable, not zero.
    unrealized_pnl: Decimal | None
    unrealized_as_of: str | None
    total_pnl: Decimal | None
    total_fees: Decimal | None
    net_funding_pnl: Decimal | None
    failures: tuple[str, ...]
    # Surfaced-but-not-gating conditions (e.g. an exposure_pct identity that
    # could not be verified because duplicate same-timestamp account snapshots
    # made its companion ambiguous). Kept apart from ``failures`` so the
    # count/failure identity below stays exact.
    warnings: tuple[str, ...] = ()
    # The run's trailing no-decision streaks (issue #50), counted back from the
    # newest terminal cycle: ``no_decision`` is every ``api_failed``,
    # ``stale_feed`` the leading run of those that were freshness refusals.
    streaks: TrailingFailureStreaks = TrailingFailureStreaks(0, 0, None)
    # Not-yet-at-the-gate reasons that carry a printed cause (exit 4). STORED,
    # not derived from ``streaks``: whether a streak still describes the run's
    # CURRENT state depends on ``now``, which is an input to the validator and
    # not a field of this report. Same shape as the live report's field, whose
    # six other sources are likewise resolved by the validator — a hand-built
    # report can therefore state a streak without the matching line, exactly as
    # it can for those six, and the tests own that.
    shortfalls: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        # Every INTEGRITY count must have exactly one printed failure line and
        # vice versa — a future category added to one side but not the other
        # would print a report whose counts and reasons disagree. Scoped to the
        # integrity counts on purpose: the streaks gate through ``shortfalls``
        # (exit 4, "not yet"), never through ``failures`` (exit 5, "the store is
        # broken"), so they are deliberately outside this identity.
        expected = (
            self.orphan_order_count
            + self.orphan_fill_count
            + self.snapshot_mismatch_count
            + self.accounting_replay_mismatch_count
        )
        if len(self.failures) != expected:
            raise ValueError(
                f"ValidationReport.failures has {len(self.failures)} entries but the "
                f"gating counts sum to {expected}"
            )
        # No account snapshot means the unrealized leg was valued at 0 (the split
        # the docstring above promises). A non-zero (or absent) unrealized_pnl
        # with a None as_of would print a self-contradicting summary
        # ("unrealized: 5" next to "as_of: n/a … valued at 0"); enforce the
        # documented pairing. (None != 0, so the absent case is rejected too.)
        if self.unrealized_as_of is None and self.unrealized_pnl != 0:
            raise ValueError(
                "ValidationReport.unrealized_pnl must be 0 when unrealized_as_of is None "
                f"(no account snapshot), got {self.unrealized_pnl}"
            )
        # The ledger trio comes from one replay: absent only together, and only
        # as a *counted* integrity failure — an n/a ledger beside a clean
        # replay count would claim the books verified while hiding the totals.
        ledger = (self.realized_pnl, self.total_fees, self.net_funding_pnl)
        if any(v is None for v in ledger):
            if not all(v is None for v in ledger):
                raise ValueError(
                    "ValidationReport ledger fields (realized_pnl, total_fees, "
                    "net_funding_pnl) come from one replay and must be absent together"
                )
            if self.accounting_replay_mismatch_count == 0:
                raise ValueError(
                    "ValidationReport ledger fields are absent (replay raised) but "
                    "accounting_replay_mismatch_count is 0 — an uncounted integrity failure"
                )
        # total_pnl is derivable exactly when every component is.
        component_missing = self.realized_pnl is None or self.unrealized_pnl is None
        if (self.total_pnl is None) != component_missing:
            raise ValueError(
                "ValidationReport.total_pnl must be absent exactly when a component "
                "(ledger trio or unrealized leg) is absent"
            )

    @property
    def phase3_ready(self) -> bool:
        """Spec §5: enough cycles and a fully consistent, traceable store.

        ...and a run that is currently REACHING decisions: one whose last
        three cycles all failed is not ready however many cycles it
        accumulated before its inputs went wrong (issue #50).
        """
        return (
            self.cycle_count >= MIN_CYCLES_FOR_PHASE3
            and self.orphan_order_count == 0
            and self.orphan_fill_count == 0
            and self.snapshot_mismatch_count == 0
            and self.accounting_replay_mismatch_count == 0
            and not self.shortfalls
        )

    def summary_lines(self) -> list[str]:
        """The report as printable lines (the CLI's output shape)."""

        def _fmt(value: Decimal | None) -> str:
            return "n/a" if value is None else str(value)

        lines = [
            f"run_id: {self.run_id}",
            f"cycle_count: {self.cycle_count}",
            f"api_failed_count: {self.api_failed_count}",
            f"order_count: {self.order_count}",
            f"fill_count: {self.fill_count}",
            f"rejected_order_count: {self.rejected_order_count}",
            f"orphan_order_count: {self.orphan_order_count}",
            f"orphan_fill_count: {self.orphan_fill_count}",
            f"snapshot_mismatch_count: {self.snapshot_mismatch_count}",
            f"accounting_replay_mismatch_count: {self.accounting_replay_mismatch_count}",
            f"max_exposure_pct: {_fmt(self.max_exposure_pct)}",
            f"max_effective_leverage: {_fmt(self.max_effective_leverage)}",
            f"realized_pnl: {_fmt(self.realized_pnl)}",
            f"unrealized_pnl: {_fmt(self.unrealized_pnl)}",
            "unrealized_as_of: "
            + (self.unrealized_as_of or "n/a (no account snapshot; valued at 0)"),
            f"total_pnl: {_fmt(self.total_pnl)}",
            f"total_fees: {_fmt(self.total_fees)}",
            f"net_funding_pnl: {_fmt(self.net_funding_pnl)}",
            f"no_decision_streak: {self.streaks.no_decision}",
            f"stale_feed_refusal_streak: {self.streaks.stale_feed}",
            f"phase3_ready: {'yes' if self.phase3_ready else 'no'}",
        ]
        lines.extend(f"failure: {reason}" for reason in self.failures)
        lines.extend(f"shortfall: {reason}" for reason in self.shortfalls)
        lines.extend(f"warning: {reason}" for reason in self.warnings)
        return lines


def trailing_failure_streaks(conn: sqlite3.Connection, run_id: str) -> TrailingFailureStreaks:
    """The run's trailing no-decision streaks, newest cycle first (issue #50).

    Walks the run's TERMINAL attempts from the most recent backwards and counts
    while each is an ``api_failed``; the first cycle that decided — a target,
    or an unparseable model answer — ends the count. So it measures "how long
    has this run been unable to decide RIGHT NOW", not "how often has it ever":
    a run that recovers clears it at the next decided cycle. An ``in_progress``
    attempt is skipped, not a break — it has not said anything yet. Shared by
    the paper and live validators.

    ``status`` is checked as well as ``error_type``. Today that is
    belt-and-braces — both finalize paths write ``error_type=None`` explicitly
    when a cycle decides (``scheduler._finalize``, ``decision._gate``), so a
    ``completed`` row cannot carry a stale class — but keying on the class
    alone would silently start counting decided cycles the day either of those
    explicit clears is dropped.

    The reverse scan is index-driven, not a sort: ``decision_attempts`` has
    ``UNIQUE (run_id, scheduled_at)``, whose implicit index satisfies this
    ORDER BY directly, so the cursor stays lazy and the early ``break`` really
    does stop reading. Dropping that constraint would silently turn this into
    a full sort of the run's history.
    """
    rows = conn.execute(
        "SELECT status, error_type, timestamp FROM decision_attempts"
        " WHERE run_id = ? AND status != 'in_progress'"
        " ORDER BY scheduled_at DESC, rowid DESC",
        (run_id,),
    )
    no_decision = stale_feed = 0
    latest_at: datetime | None = None
    dated = False
    still_stale = True
    for row in rows:
        if not dated:
            # The NEWEST row only, whatever it says: a corrupt stamp leaves the
            # run undated rather than silently dating it from an older cycle,
            # which would withhold the shortfall a whole cycle early.
            dated = True
            try:
                latest_at = parse_instant(row["timestamp"])
            except ValueError:
                latest_at = None
        if row["status"] != "api_failed":
            break
        no_decision += 1
        if still_stale and row["error_type"] == STALE_MARKET_DATA_ERROR:
            stale_feed += 1
        else:
            still_stale = False
    return TrailingFailureStreaks(no_decision, stale_feed, latest_at)


def _streak_hours(streak: int) -> int:
    """``streak`` cycles as an approximate span, at the scheduler's cadence.

    Reads the module-level ``CYCLE_INTERVAL``, so a cadence change moves this
    with it. It does NOT track a ``LiveDecisionDriver`` constructed with a
    non-default ``cycle_interval`` — no production wiring passes one, and the
    shortfall wording is shared with the live VALIDATOR, which reads a store
    and has no driver to ask.
    """
    return int(streak * CYCLE_INTERVAL.total_seconds() // 3600)


def note_cycle_outcome(streak: int, status: str, error_type: str | None, *, run_id: str) -> int:
    """Advance or reset a loop's in-process no-decision streak, and log it.

    The running half of the issue #50 escalation: ``validate`` judges by the
    durable streak in the store; this keeps the live one and turns the
    per-cycle WARNING into an ERROR from the threshold on — the same 3-strike
    log escalation the funding source uses — so a log scraper sees the change
    without a store query. Returns the new streak; the caller keeps it.

    Call it on EVERY cycle-terminal outcome, not only the failures. The store
    query this mirrors (:func:`trailing_failure_streaks`) breaks on any cycle
    that decided, so a counter fed only the ``api_failed`` branch would drift
    from the verdict its own ERROR text points the operator at: `stale, stale,
    completed, stale` would log "3 consecutive, ~12h with no decision" over a
    run that decided in between, and ``validate`` would then print no shortfall
    at all.

    In-process only: a restart starts the count over, and the store's streak is
    the one the verdict reads. ``error_type`` only selects the wording — the
    count is class-blind, because every way of reaching no decision blocks the
    run equally (issue #50 was raised about a stalled feed, but a failure of
    the l2Book endpoint the freshness guard now depends on is just as silent).
    """
    if status != "api_failed":
        return 0
    streak += 1
    # ``error_type`` is None for a non-retryable bug and for a restart-
    # interrupted cycle — no §6.2 word applies. Name that rather than
    # interpolating a bare ``None`` into a line an operator reads.
    named_class = error_type or "unclassified"
    if streak < NO_DECISION_STREAK_THRESHOLD:
        logger.warning(
            "decision cycle for %s failed (%s) — %d consecutive with no decision "
            "(escalates to ERROR and a validate shortfall at %d)",
            run_id,
            named_class,
            streak,
            NO_DECISION_STREAK_THRESHOLD,
        )
    elif error_type == STALE_MARKET_DATA_ERROR:
        logger.error(
            "decision cycle for %s refused as stale market data — %d consecutive "
            "(~%dh with no decision): the candle feed stopped advancing or this "
            "host's clock is off (the refusal message says which); any open "
            "position is riding SL/TP alone, and `validate` reports this as a "
            "shortfall until a cycle decides again",
            run_id,
            streak,
            _streak_hours(streak),
        )
    else:
        logger.error(
            "decision cycle for %s failed (%s) — %d consecutive with no decision "
            "(~%dh): any open position is riding SL/TP alone; `validate` reports "
            "this as a shortfall until a cycle decides again",
            run_id,
            named_class,
            streak,
            _streak_hours(streak),
        )
    return streak


def no_decision_shortfall(streaks: TrailingFailureStreaks, *, now: datetime) -> str | None:
    """The ``shortfall:`` line for a run that cannot currently decide, or ``None``.

    Shared by both validators, so the paper and live reports say the same thing
    about the same store shape. ``None`` in two cases: the streak is under the
    threshold, or the newest terminal cycle is older than
    :data:`_STREAK_RECENCY_WINDOW` — a stopped or archived run's last hours are
    not a reason it "cannot decide now", and gating on them would disqualify an
    otherwise complete acceptance run for having ended during an exchange
    outage.

    One line, not two: the stale-feed streak is a subset of the no-decision one,
    so when it accounts for the whole run the wording names the feed and the
    clock; otherwise it names the §6.2 class count instead of guessing.
    """
    if streaks.no_decision < NO_DECISION_STREAK_THRESHOLD:
        return None
    if (
        streaks.latest_terminal_at is None
        or now - streaks.latest_terminal_at > _STREAK_RECENCY_WINDOW
    ):
        return None
    count = streaks.no_decision
    hours = _streak_hours(count)
    if streaks.stale_feed >= count:
        cause = (
            "all refused as stale market data — the candle feed stopped advancing or "
            "this host's clock is off; the refusal messages say which"
        )
    else:
        cause = (
            f"mixed causes — {streaks.stale_feed} refused as stale market data, "
            f"{count - streaks.stale_feed} other failures; see error_type in "
            "decision_attempts"
        )
    return (
        f"no_decision_streak = {count} (the last {count} cycles, ~{hours}h, {cause}. "
        f"Any open position is riding SL/TP alone; the streak clears by itself at the "
        f"next decided cycle (need < {NO_DECISION_STREAK_THRESHOLD}))"
    )


def _dec_or_none(value) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _count(conn: sqlite3.Connection, sql: str, params: tuple) -> int:
    return conn.execute(sql, params).fetchone()[0]


def _max_readable(conn: sqlite3.Connection, sql: str, run_id: str) -> Decimal | None:
    """Max of a single-column numeric scan, skipping cells ``Decimal()`` rejects
    — an unreadable cell's row is already a counted failure in the identity
    phase, so the maximum simply omits it."""
    best: Decimal | None = None
    for (raw,) in conn.execute(sql, (run_id,)):
        try:
            value = Decimal(raw)
        except (InvalidOperation, TypeError, ValueError):
            continue
        if best is None or value > best:
            best = value
    return best


def _orphan_orders(conn: sqlite3.Connection, run_id: str) -> list[str]:
    """Orders that trace to nothing (spec §5: Decision → Order).

    Three orphan shapes: an ``output_id`` that resolves to no ``ai_outputs``
    row; a position-*opening* order with no ``output_id`` at all (only the AI
    decision path may open exposure); and a system reduce-only order (SL / TP /
    liquidation / emergency) for a symbol that never had a position to reduce —
    no fill at or before the order's timestamp and no seed position.
    """
    orphans: list[str] = []
    seeded = {p.coin for p in repo.get_run_seed_positions(conn, run_id)}
    for order in repo.iter_orders(conn, run_id):
        output_id = order["output_id"]
        if output_id is not None:
            found = conn.execute(
                "SELECT 1 FROM ai_outputs WHERE output_id = ?", (output_id,)
            ).fetchone()
            if found is None:
                orphans.append(order["order_id"])
            continue
        if not order["reduce_only"]:
            orphans.append(order["order_id"])
            continue
        if order["symbol"] in seeded:
            continue
        prior_fill = conn.execute(
            "SELECT 1 FROM fills WHERE run_id = ? AND symbol = ? AND timestamp <= ? LIMIT 1",
            (run_id, order["symbol"], order["timestamp"]),
        ).fetchone()
        if prior_fill is None:
            orphans.append(order["order_id"])
    return orphans


def _account_row_identities_ok(row: sqlite3.Row) -> bool:
    """One account_snapshots row's arithmetic identities (execution §6.1)."""
    wallet = Decimal(row["wallet_balance"])
    equity = Decimal(row["account_equity"])
    available = Decimal(row["available_balance"])
    unrealized = Decimal(row["unrealized_pnl"])
    used_im = Decimal(row["used_initial_margin"])
    maint = Decimal(row["total_maintenance_margin"])
    notional = Decimal(row["total_position_notional"])
    total_pnl = Decimal(row["total_pnl"])
    expected_pnl = (
        Decimal(row["realized_pnl"])
        + unrealized
        - Decimal(row["total_fees"])
        + Decimal(row["net_funding_pnl"])
    )
    leverage = _dec_or_none(row["effective_leverage"])
    ratio = _dec_or_none(row["margin_ratio"])
    return (
        equity == wallet + unrealized
        and available == equity - used_im
        and total_pnl == expected_pnl
        and (
            (leverage is None and equity <= 0)
            or (leverage is not None and equity > 0 and leverage == notional / equity)
        )
        and (
            (ratio is None and maint == 0)
            or (ratio is not None and maint != 0 and ratio == equity / maint)
        )
    )


def _position_row_identities_ok(
    conn: sqlite3.Connection, run_id: str, row: sqlite3.Row, warnings: list[str]
) -> bool:
    """One position_snapshots row's identities, incl. the exposure companion."""
    size = Decimal(row["position_size"])
    mark = Decimal(row["mark_price"])
    entry = _dec_or_none(row["entry_price"])
    notional = Decimal(row["position_notional"])
    unrealized = Decimal(row["unrealized_pnl"])
    maint = Decimal(row["maintenance_margin"])
    rate = _dec_or_none(row["maintenance_margin_rate"])
    deduction = _dec_or_none(row["maintenance_deduction"])
    side = row["side"]
    ok = notional == abs(size * mark) and side == (
        "long" if size > 0 else ("short" if size < 0 else "flat")
    )
    if entry is not None:
        ok = ok and unrealized == size * (mark - entry)
    if rate is not None and deduction is not None:
        ok = ok and maint == notional * rate - deduction
    # exposure_pct references the same-instant account equity.
    exposure = _dec_or_none(row["exposure_pct"])
    if exposure is not None:
        matches = conn.execute(
            "SELECT account_equity FROM account_snapshots WHERE run_id = ? AND timestamp = ?",
            (run_id, row["timestamp"]),
        ).fetchall()
        if len(matches) == 1:
            equity = Decimal(matches[0][0])
            ok = ok and equity > 0 and exposure == notional / equity * 100
        elif not matches:
            # The writer commits both snapshots in one transaction; a
            # missing companion is store corruption, not a data gap.
            ok = False
        else:
            warnings.append(
                f"exposure_pct unverifiable for position_snapshots rowid "
                f"{row['row_key']}: {len(matches)} account snapshots share "
                f"timestamp {row['timestamp']}"
            )
    return ok


def _snapshot_mismatches(conn: sqlite3.Connection, run_id: str) -> tuple[list[str], list[str]]:
    """``(mismatches, warnings)`` over the stored snapshot rows.

    Every identity above restates how the engine's snapshot writer computed the
    value (execution §6.1 / §6.6, phase2-data §11–§12); recomputation runs under
    the same pinned context, so any inequality is real corruption or writer
    drift, never rounding noise. A position snapshot whose same-instant account
    companion is *missing* is a mismatch (the writer commits both in one
    transaction); one whose companion is *ambiguous* (duplicate timestamps)
    leaves ``exposure_pct`` unverifiable and is reported as a warning instead —
    silently skipping it would let a timestamp-duplicating writer bug reduce
    validation coverage.

    A row the checker cannot even read (a stored cell ``Decimal()`` rejects)
    is contained *per row* as a mismatch — corruption is exactly what the
    identity check exists to catch, so it must count toward the exit-5 verdict
    rather than crash the report (classify_replay's rule), and every other row
    still gets checked.
    """
    bad: list[str] = []
    warnings: list[str] = []
    with localcontext(DECIMAL_CONTEXT):
        for table, check in (
            ("account_snapshots", _account_row_identities_ok),
            (
                "position_snapshots",
                lambda row: _position_row_identities_ok(conn, run_id, row, warnings),
            ),
        ):
            # ``rowid AS row_key``: on a table with an INTEGER PRIMARY KEY, a
            # bare ``rowid`` result column takes that column's name, not "rowid".
            rows = conn.execute(
                f"SELECT rowid AS row_key, * FROM {table} WHERE run_id = ? ORDER BY rowid",
                (run_id,),
            ).fetchall()
            for row in rows:
                try:
                    ok = check(row)
                except Exception as exc:  # noqa: BLE001 — unreadable row = corruption, an outcome
                    bad.append(f"{table} rowid {row['row_key']} (identity check raised: {exc})")
                    continue
                if not ok:
                    bad.append(f"{table} rowid {row['row_key']}")
    return bad, warnings


def validate_run(db: Database, *, run_id: str, now: datetime | None = None) -> ValidationReport:
    """Compute the §5 acceptance report for ``run_id`` (read-only).

    ``now`` (default: wall clock) ages still-pending funding events for the
    non-gating staleness warning, and dates the no-decision streak against
    :data:`_STREAK_RECENCY_WINDOW`. That second use makes ``now`` a GATING
    input: the same store validated hours apart can move from exit 4 to exit
    0 as a stopped run ages out of the window. The verdict is reproducible
    given the same ``now``, not from the store alone.
    """
    with db.read_transaction() as conn:
        if repo.get_run(conn, run_id) is None:
            raise ValueError(f"run {run_id!r} does not exist; nothing to validate")

        placeholders = ", ".join("?" for _ in _COMPLETED_CYCLE_STATUSES)
        cycle_count = _count(
            conn,
            f"SELECT COUNT(*) FROM decision_attempts WHERE run_id = ?"
            f" AND status IN ({placeholders})",
            (run_id, *_COMPLETED_CYCLE_STATUSES),
        )
        api_failed_count = _count(
            conn,
            "SELECT COUNT(*) FROM decision_attempts WHERE run_id = ? AND status = 'api_failed'",
            (run_id,),
        )
        streaks = trailing_failure_streaks(conn, run_id)
        order_count = _count(conn, "SELECT COUNT(*) FROM orders WHERE run_id = ?", (run_id,))
        fill_count = _count(conn, "SELECT COUNT(*) FROM fills WHERE run_id = ?", (run_id,))
        rejected_order_count = _count(
            conn,
            "SELECT COUNT(*) FROM orders WHERE run_id = ? AND status = 'rejected'",
            (run_id,),
        )
        orphan_orders = _orphan_orders(conn, run_id)
        orphan_fills = [
            row["fill_id"]
            for row in conn.execute(
                "SELECT f.fill_id AS fill_id FROM fills f"
                " LEFT JOIN orders o ON o.order_id = f.order_id"
                " WHERE f.run_id = ? AND o.order_id IS NULL",
                (run_id,),
            ).fetchall()
        ]
        snapshot_mismatches, warnings = _snapshot_mismatches(conn, run_id)

        max_exposure = _max_readable(
            conn,
            "SELECT exposure_pct FROM position_snapshots"
            " WHERE run_id = ? AND exposure_pct IS NOT NULL",
            run_id,
        )
        max_leverage = _max_readable(
            conn,
            "SELECT effective_leverage FROM account_snapshots"
            " WHERE run_id = ? AND effective_leverage IS NOT NULL",
            run_id,
        )

        last_snapshot = conn.execute(
            "SELECT unrealized_pnl, timestamp FROM account_snapshots WHERE run_id = ?"
            " ORDER BY rowid DESC LIMIT 1",
            (run_id,),
        ).fetchone()

        # Store-persisted completeness signals for the non-gating warnings
        # below — read inside the same snapshot as everything else.
        pending_events = list(repo.iter_funding_events(conn, run_id, status="pending"))
        sched_state = repo.get_scheduler_state(conn, run_id)

        # Replay runs inside THIS snapshot (not its own) so the whole report —
        # counts, snapshot checks, and the replayed ledger the §5 totals come
        # from — describes one point in time even against a live writer. A
        # replay that *raises* (a stored value it cannot read) is an
        # unverifiable-books outcome — classify_replay's rule — counted as a
        # gating failure, never a crash: exit-5's "investigate the store" is
        # for exactly this operator.
        replay_raised: str | None = None
        try:
            replayed = accounting.replay_within(conn, run_id=run_id)
        except Exception as exc:  # noqa: BLE001 — unverifiable books are an outcome, not a crash
            replayed = None
            replay_raised = f"{type(exc).__name__}: {exc}"
    if replayed is None:
        replay_mismatch_count = 1
        realized = fees = funding = None
    else:
        replay_mismatch_count = len(replayed.position_mismatches) + (
            0 if replayed.account_matches else 1
        )
        realized = replayed.ledger.realized_pnl
        fees = replayed.ledger.total_fees
        funding = replayed.ledger.net_funding_pnl
    # The unrealized leg is valued at the LAST account snapshot's mark (up to
    # ~one cycle stale): an offline validator has no fresh mark, and fetching one
    # would make the report irreproducible against the store. The valuation
    # instant is surfaced as ``unrealized_as_of`` and the leg is split out so a
    # run ended holding a position can't misread as flat.
    if last_snapshot is not None:
        unrealized_as_of: str | None = last_snapshot[1]
        try:
            unrealized: Decimal | None = Decimal(last_snapshot[0])
        except (InvalidOperation, TypeError, ValueError):
            # Unreadable stored cell: that snapshot row is already a counted
            # failure above; the leg is unavailable, not zero.
            unrealized = None
    else:
        unrealized = Decimal(0)
        unrealized_as_of = None
    if realized is None or fees is None or funding is None or unrealized is None:
        total_pnl = None
    else:
        with localcontext(DECIMAL_CONTEXT):
            total_pnl = realized - fees + funding + unrealized

    failures: list[str] = []
    failures.extend(f"orphan order {order_id}" for order_id in orphan_orders)
    failures.extend(f"orphan fill {fill_id}" for fill_id in orphan_fills)
    failures.extend(f"snapshot identity failed: {where}" for where in snapshot_mismatches)
    if replayed is None:
        failures.append(f"accounting replay raised: {replay_raised} — books unverifiable")
    else:
        failures.extend(
            f"replay position mismatch: {symbol}" for symbol in replayed.position_mismatches
        )
        if not replayed.account_matches:
            failures.append("replay ledger mismatch: current_account_state disagrees")

    # Non-gating completeness warnings (surface, never gate — the exposure_pct
    # precedent): a funding hour still pending long past settlement will likely
    # never resolve, and its P&L is deliberately uncounted rather than
    # fabricated — the acceptance reader must see that the totals are partial.
    if now is None:
        now = datetime.now(timezone.utc)
    stale_pending = 0
    corrupt_pending = 0
    for event in pending_events:
        try:
            settlement = parse_instant(event["funding_timestamp"])
        except ValueError:
            # backfill_pending_funding's vocabulary: an unparseable stored
            # timestamp is a *corrupt* row, not a stale one — it resolves only
            # by repairing the store, so it gets its own warning line.
            corrupt_pending += 1
            continue
        if now - settlement >= STALE_PENDING_FUNDING:
            stale_pending += 1
    if stale_pending:
        warnings.append(
            f"{stale_pending} funding event(s) still pending more than "
            f"{STALE_PENDING_FUNDING} after settlement — their funding P&L is "
            "uncounted in net_funding_pnl (stored basis only, never fabricated)"
        )
    if corrupt_pending:
        warnings.append(
            f"{corrupt_pending} pending funding event(s) have an unparseable "
            "funding_timestamp — corrupt stored row(s); their funding P&L stays "
            "uncounted until the store is repaired"
        )
    if sched_state is not None and sched_state["last_config_drift_status"] == "drift":
        warnings.append(
            "config drift recorded at last resume "
            f"({sched_state['last_config_drift_at']}): "
            f"{sched_state['last_config_drift_error']} — cycles before that resume "
            "ran under different parameters than this aggregate implies"
        )

    return ValidationReport(
        run_id=run_id,
        cycle_count=cycle_count,
        api_failed_count=api_failed_count,
        order_count=order_count,
        fill_count=fill_count,
        rejected_order_count=rejected_order_count,
        orphan_order_count=len(orphan_orders),
        orphan_fill_count=len(orphan_fills),
        snapshot_mismatch_count=len(snapshot_mismatches),
        accounting_replay_mismatch_count=replay_mismatch_count,
        max_exposure_pct=max_exposure,
        max_effective_leverage=max_leverage,
        realized_pnl=realized,
        unrealized_pnl=unrealized,
        unrealized_as_of=unrealized_as_of,
        total_pnl=total_pnl,
        total_fees=fees,
        net_funding_pnl=funding,
        failures=tuple(failures),
        warnings=tuple(warnings),
        streaks=streaks,
        shortfalls=tuple(line for line in (no_decision_shortfall(streaks, now=now),) if line),
    )
