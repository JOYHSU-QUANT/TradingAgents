"""The shared no-decision escalation policy (issue #50).

One policy, four consumers: the streak threshold, the store query behind it,
the recency window, the shortfall wording the paper and live VALIDATORS print,
and the per-cycle log escalation the paper and live RUNNING loops call
(:func:`note_cycle_outcome`). It lived in :mod:`.validation` because the two
validators already shared that module — but that module is a read-only
acceptance validator by contract, and a writer the running loops call did not
belong under that sentence (issue #94). Here the policy has a home whose
docstring is true of every function in it.

"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from ..common.constants import STALE_MARKET_DATA_ERROR
from .scheduler import CYCLE_INTERVAL, parse_instant

__all__ = [
    "NO_DECISION_STREAK_THRESHOLD",
    "TrailingFailureStreaks",
    "no_decision_shortfall",
    "note_cycle_outcome",
    "trailing_failure_streaks",
]

logger = logging.getLogger(__name__)

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
    about the same store shape. ``None`` in three cases: the streak is under
    the threshold; the newest terminal cycle's stamp is unparseable
    (``latest_terminal_at is None`` — withhold rather than guess); or that
    cycle is older than :data:`_STREAK_RECENCY_WINDOW` — a stopped or archived
    run's last hours are not a reason it "cannot decide now", and gating on
    them would disqualify an otherwise complete acceptance run for having
    ended during an exchange outage.

    A stamp AHEAD of ``now`` is reported, not withheld: it is trivially recent,
    and only the size of a positive gap can make a streak stale. That case is
    reachable in production — ``live.validate_live_run`` reads ``now`` before
    its store query, so a daemon finalizing the third ``api_failed`` cycle
    between the two calls stamps it after ``now``, and that run must not pass
    the gate; a validator host whose clock sits behind the daemon's does the
    same thing (issue #94 first proposed withholding here; review of that
    ordering reversed it).

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
