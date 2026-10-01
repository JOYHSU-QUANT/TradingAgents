"""The §18.5 ``kill_switch_events`` row: the writer side of its detail tokens.

``validation_metrics`` parses these back out, so each token is written here,
once, and :func:`record_kill_switch_event` is the one transaction that lands
a row — for the manager and for the smoke suite alike (see its docstring for
why the suite writes rows itself).
"""

from __future__ import annotations

from datetime import datetime

from ..persistence import repository as repo
from ..persistence.db import Database

__all__ = [
    "SUITE_AUTHORED_TOKEN",
    "deadline_detail",
    "is_suite_authored",
    "record_kill_switch_event",
    "stamp_suite_authored",
]


def deadline_detail(seconds: int, note: str) -> str:
    """The ONE way to write "this row installed N seconds of cover".

    ``validation_metrics._stated_deadline_seconds`` parses this token back out to size
    every stretch of silence, so the two are a cross-module contract — and a
    contract enforced by three independent f-strings agreeing by eye is not
    enforced at all. The drift is already demonstrable one screen away:
    ``kill_switch_cancel_triggered`` renders the same concept as
    ``(deadline 120s)`` — a space, not ``=`` — which the reader silently ignores.
    Ignoring it there is correct, but nothing structural made it so. With one
    formatter there is a single writer to pin (2026-08-01 round-14 simplify pass).
    """
    return f"deadline={seconds}s {note}"


# Marks a row the SMOKE SUITE wrote rather than the daemon. Both are real cover
# and both must count toward outage seconds and toward the deadline in force —
# the suite genuinely arms and renews the wallet-wide trigger. What suite rows
# must NOT do is satisfy the §20.3 SAMPLE FLOOR, which asks a different question:
# "has this run exercised the switch enough for its availability number to mean
# anything?" A handful of back-to-back suites on one run-id clear that floor at
# 100% with the daemon never started, so it would be answered entirely by
# evidence from a phase that cannot speak to hours of unattended running
# (2026-08-01 round-15 review; user decision: exclude from the count only). The
# concrete figure is quoted once, in RUNBOOK §20.3.
#
# It has a SECOND consumer since round 17, and weakening the marker moves that
# one too: ``validation_metrics.py`` derives the DAEMON subsequence from it, and the
# last row of that subsequence is the run's clean-shutdown verdict. "Sample
# floor only" was true for exactly one round (2026-08-01 round-21 review).
#
# A token rather than a substring sniff of free text, and read back through
# ``is_suite_authored`` — same writer/reader discipline as ``deadline_detail``,
# for the same reason: the one thing that must not happen is the two sides
# drifting apart silently. RUNBOOK §20.3 shows operators this literal, and a test
# pins the doc against this constant.
#
# STAMPED BY THE WRITER, never by each call site. Per-call-site lasted one round
# and was wrong twice over. Three of the suite's six writers never got it — the
# failed refresh among them, which made the exclusion branch that reads it
# unreachable dead code and the RUNBOOK's claim about it false. And the one that
# actually mattered: the suite ALSO drives a real KillSwitchManager for its
# pre-flight recovery and restart tests 15-17, whose own ``tick()`` emits
# refreshes across a suite that takes minutes per test. Those bought §20.3 sample
# credit exactly as before, so the exclusion was never in force on a real run —
# hidden only by an offline test clock that does not elapse (2026-08-01 round-16
# review; user decision: mark at the manager).
SUITE_AUTHORED_TOKEN = "writer=live-smoke"


def stamp_suite_authored(detail: str | None) -> str:
    """Append the marker, preserving whatever the row already said."""
    return SUITE_AUTHORED_TOKEN if not detail else f"{detail} {SUITE_AUTHORED_TOKEN}"


def is_suite_authored(detail: str | None) -> bool:
    """Whether this row was written during ``live-smoke`` rather than by the daemon."""
    # The token as the LAST whitespace-delimited field — the only shape
    # ``stamp_suite_authored`` writes — and not a substring anywhere in the
    # column. ``detail`` is free text shared by six writers, one of which dumps
    # a JSON blob carrying raw exchange and SQLite exception text into it, so a
    # bare ``in`` let any row that merely QUOTED the token leave the daemon
    # subsequence and take the run's clean-shutdown verdict with it. The reader
    # of the same column in validation_metrics.py answers this with an event-type
    # allowlist; this predicate runs over every event type and cannot, so it
    # anchors instead (2026-08-01 round-18 review).
    if not detail:
        return False
    return detail.split()[-1:] == [SUITE_AUTHORED_TOKEN]


def record_kill_switch_event(
    db: Database,
    *,
    run_id: str,
    event_type: str,
    timestamp: datetime,
    detail: str | None = None,
    error: str | None = None,
    suite_authored: bool = False,
) -> None:
    """Append one §18.5 row in its own transaction.

    Shared by :meth:`KillSwitchManager._record` and the smoke suite, which drives
    ``scheduleCancel`` on the raw signed client and so has to write these rows
    itself. Only the WRITER is shared, deliberately not the state machine: the
    suite needs arm→refresh→clear→clear, a shape ``arm()``/``refresh()`` forbid,
    and installs ``max(config, 120s)`` of cover rather than the configured value.

    ``suite_authored`` stamps the marker here, so EVERY row a caller writes is
    marked by the fact of who the caller is — not by remembering at each call
    site, which is how three of the suite's six writers went unmarked.

    Fail-loud (an unguarded transaction): the row is the only durable evidence the
    acceptance measure has, and a caller that must not raise says so at its own
    call site rather than making silence the default.
    """
    with db.transaction() as conn:
        repo.insert_kill_switch_event(
            conn,
            run_id=run_id,
            event_type=event_type,
            detail=stamp_suite_authored(detail) if suite_authored else detail,
            error_message=error,
            timestamp=timestamp,
        )
