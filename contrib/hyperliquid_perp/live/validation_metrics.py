"""What the live acceptance validator reads from the store (refactor plan v2, T3-a).

:func:`read_live_run_facts` makes every read behind
:func:`.validation.validate_live_run` in one pass over one read transaction and
returns them as a :class:`LiveRunFacts`; the verdict is drawn from those facts
in :mod:`.validation`, whose module docstring says where each metric comes from
and why. Everything here goes through :mod:`..persistence.repository`: this
module writes no SQL of its own (``tests/common/test_layering.py`` counts the
sites outside ``persistence/``).
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, localcontext
from operator import itemgetter
from typing import NamedTuple

from ..common.config_coercion import int_from_yaml
from ..common.decimal_context import DECIMAL_CONTEXT
from ..common.instants import parse_instant
from ..persistence import repository as repo
from ..runtime import accounting
from ..runtime.no_decision import TrailingFailureStreaks, trailing_failure_streaks
from .config import DEFAULT_SCHEDULE_CANCEL_SECONDS as _CONFIG_DEFAULT_DEADLINE_S
from .kill_switch_events import is_suite_authored
from .safe_mode import REASON_DAILY_LOSS
from .smoke_catalog import SmokeGateReport, smoke_gate_report

__all__ = ["DEFAULT_SCHEDULE_CANCEL_SECONDS", "LiveRunFacts", "read_live_run_facts"]

# The cover one successful schedule buys, used only when the run's own genesis
# config cannot be read. The real number comes from the run itself (see
# _schedule_cancel_seconds), because it is a CONFIGURABLE deadline and a fixed
# constant cannot reason about it.
#
# There used to be an absolute 300s "the process must have been down" threshold
# here, and it was wrong twice over. It was 2.5x the 120s deadline it reasoned
# about, so the whole band in which the switch demonstrably fires was billed as
# healthy covered time; and with a legal ``refresh_interval_seconds`` above 300
# EVERY healthy gap exceeded it, nothing was ever added to covered, and the gate
# passed unconditionally forever. Worse, inferring "the process was down" from
# silence is not sound at all — a dead process and a fired switch leave the SAME
# silence — and resolving that ambiguity toward "down" meant a 301s outage scored
# ZERO outage seconds while a 299s one scored 299: the longer outage passing at
# 100% while the shorter correctly failed (2026-08-01 round-13 review).
# DERIVED from live.config.KillSwitchConfig.schedule_cancel_seconds's default: a run
# that omits the key is measured against whatever THAT default armed it with, so a
# separate literal here would silently measure every such run against the wrong
# deadline the day one of them moved (issue #102). Decimal, because everything
# this module compares it against is.
DEFAULT_SCHEDULE_CANCEL_SECONDS = Decimal(_CONFIG_DEFAULT_DEADLINE_S)

# A real minimum-valid ISO-8601 instant for the "since beginning of time" query
# (has_safe_mode_reason_event does a lexicographic string compare): a real
# calendar date, not the pseudo-"0000-00-00" that no ISO parser accepts, so the
# sentinel survives a future switch to parsed-datetime comparison.
_SINCE_BEGINNING = "0001-01-01T00:00:00+00:00"

# Live counts a "cycle" STRICTLY: only ``completed``. This deliberately diverges
# from the paper validator (paper/validation.py), which also admits
# ``invalid_output`` — a cycle where the scheduler ran but the model's output
# could not be parsed. "The pipeline executed" is a fair measure for a paper
# baseline; it is the wrong bar for a live acceptance gate, which is asserting
# the bot can trade. On testnet the ≥30 cycle gate is backstopped by
# live_order_count, but §21.4 (mainnet_tiny) deliberately carries NO order count
# (2026-07-27) — so under the paper vocabulary 30 consecutive unparseable cycles
# that placed nothing would report live_ready / exit 0. That shape is not
# hypothetical: paper-BTC produced 6/6 invalid_output after a model swap.
# invalid_output cycles are still surfaced, as a non-gating warning in
# :mod:`.validation`.
# One narrow window lands an "answered, unparseably" cycle in api_failed rather
# than invalid_output (issue #206; accepted in spec §3.1's revision box, (c)):
# an invalid answer is never stored as resumable (PR #204), so if the gate is
# then blocked or its persist fails and the process restarts, adoption finds no
# response to resume and fails the cycle closed. The gate above is untouched
# (invalid_output never counted here); what moves is that such a cycle counts
# in api_failed_count, EXTENDS the no-decision streak instead of resetting it,
# and is missing from the non-gating invalid_output_count — and this report
# cannot tell it from a cycle the AI never answered, because the row cannot.
_COMPLETED_CYCLE_STATUSES = (repo.AttemptStatus.COMPLETED.value,)
# The registry stays partitioned into exactly what this file classifies: a NEW
# terminal attempt status must be counted or explicitly excluded HERE, at
# import, not silently dropped from a mainnet-facing gate.
if set(repo.TERMINAL_ATTEMPT_STATUSES) - set(_COMPLETED_CYCLE_STATUSES) != {
    repo.AttemptStatus.API_FAILED.value,
    repo.AttemptStatus.INVALID_OUTPUT.value,
}:
    raise AssertionError(
        "live cycle-count vocabulary drifted from repository.TERMINAL_ATTEMPT_STATUSES"
    )

# §12.3 case types that ARE a reconciliation mismatch for the §21.4 gate. A
# fill_unmapped sighting resolves by booking the fill (its own lane), so it is
# handled separately, not counted here.
_MISMATCH_CASE_TYPES = frozenset(
    {
        "fill_malformed",
        "fill_money_drift",
        "fill_fee_drift",
        "order_missing_on_exchange",
        "orphan_exchange_order",
        "non_bot_owned_order",
        "invalid_local_fill",
        "exchange_fill_missing_local",
        "exchange_position_mismatch",
        "local_position_phantom",
        "equity_mismatch",
        "position_sl_missing",
    }
)
# This set is validation's literal copy of the §12.3 case-type registry minus
# the one type with its own lane. A case type added to the registry without
# this file would silently fall out of the §21.4 mismatch count — a run with a
# real open case could still report live_ready. Fail at import, exactly like
# the smoke-key assert in :mod:`.validation` (same failure class, same guard).
if _MISMATCH_CASE_TYPES | {"fill_unmapped"} != repo.RECONCILIATION_CASE_TYPES:
    raise AssertionError(
        "validation's mismatch case types drifted from repository.RECONCILIATION_CASE_TYPES"
    )
# The §17 protection-event vocabulary this file rebuilds unprotected windows
# from (see _unprotected_windows). Module-level and registry-bound for the same
# reason as the two guards above, and the failure is worse than theirs: an
# onset name that no longer matches yields unprotected_position_seconds = 0, so
# a run with REAL unprotected windows passes a mainnet-facing exit-5 gate
# vacuously. Same for the close set — an unmatched close leaves every window
# open to "now" and fails a healthy run instead.
_SL_REPAIR_BLOCKED = "stop_loss_repair_blocked"
_UNPROTECTED_ONSET_EVENTS = frozenset({"stop_loss_repair_exhausted", _SL_REPAIR_BLOCKED})
# A close event must mean "the episode ENDED", never "we tried to end it".
# ``emergency_close_triggered`` is deliberately NOT here. Two independent reasons,
# either one fatal: (1) protection.sync stamps the onset from its OWN clock read
# while engine.tick hands _emergency_close the earlier tick-start instant, so the
# pair is non-monotone and _window_seconds clamped every §17.2 escalation to a
# 0-second window — the severe lane, the one carrying an exit-5 gate, read exactly
# like a run that was never unprotected; (2) it records an IOC *submission*. sync
# re-fires that close every tick until it lands, so the position is still
# unprotected after it — the window has not ended.
# GENERAL WARNING for any future reader of ``protection_order_events``: rows written
# within one tick are NOT reliably orderable by ``timestamp``, because the engine and
# the protection manager each take their own clock read. Order by ``event_id``
# (insertion) and never compute a duration between an engine-stamped and a
# protection-stamped event type.
# ``degraded_protection_cleared`` is the canonical end instead: protection emits it
# at exactly the three sites where unresolved_protection_failure goes True->False
# (flat, plan-active protected, fully protected), always from its own clock in a
# LATER tick, so an onset/close pair is monotone by construction. It also closes
# the two windows the other names structurally miss — a flat whose _clear found no
# resting order to cancel (so no ``protection_cleared`` row), and an _establish
# no-op that returns ESTABLISHED without any placed/modified row.
_SL_PLACED = "stop_loss_placed"
_SL_MODIFIED = "stop_loss_modified"
_UNPROTECTED_CLOSE_EVENTS = frozenset(
    {
        _SL_PLACED,
        _SL_MODIFIED,
        "protection_cleared",
        "degraded_protection_cleared",
    }
)
if not (_UNPROTECTED_ONSET_EVENTS | _UNPROTECTED_CLOSE_EVENTS) <= repo.PROTECTION_ORDER_EVENT_TYPES:
    raise AssertionError(
        "validation's unprotected-window event types drifted from "
        "repository.PROTECTION_ORDER_EVENT_TYPES"
    )

# Likewise for the §19.4 refresh-rate metric: an unmatched name makes total == 0,
# which ``validation._apply_refresh_gate`` reads as a shortfall rather than a
# failure — a
# quieter wrong answer than a crash, on a gate both profiles depend on.
_KILL_SWITCH_REFRESH_OK = "kill_switch_refreshed"
_KILL_SWITCH_REFRESH_FAILED = "kill_switch_refresh_failed"
# ``arm()`` writes this immediately after a SUCCESSFUL schedule_cancel, so as
# evidence that the switch is live it is identical to a refresh — and it is the
# row a restart produces. Reading it as an outage-closer is what stops a run that
# was stopped mid-outage from billing its entire downtime as unprotected.
_KILL_SWITCH_ARMED = "kill_switch_armed"
# The switch DEMONSTRABLY FIRED: kill_switch._detect_expired_deadline writes this
# when more than schedule_cancel_seconds passed since the last successful
# schedule, meaning the exchange has already cancelled every order on the wallet
# — SL and TP included. Until 2026-07-31 the acceptance validator read only the
# two refresh names above, so a firing was invisible to every gate: an API outage
# spanning the deadline is ONE refresh_failed row and a few minutes of outage
# time, which over a multi-day run stays well inside the >= 99% bar, and the
# naked window it opened produces no protection
# event either, because the gate refuses protective orders for the same reason.
# A run whose dead man's switch actually fired must never read live_ready.
_KILL_SWITCH_FIRED = "kill_switch_cancel_triggered"
# The wallet-wide trigger could not be cleared at shutdown. §18.2's keep_protective
# shutdown deliberately leaves SL/TP resting; a trigger left armed over them
# cancels exactly those at its deadline. Non-gating (the run itself is over) but
# the operator has to know the wallet was left armed.
_KILL_SWITCH_DISARM_FAILED = "kill_switch_disarm_failed"
# The ONE row that proves the wallet-wide trigger is no longer armed:
# ``shutdown()`` writes it immediately after ``clear_scheduled_cancel()`` returns
# (kill_switch.py). After it there is nothing left to fire, so the silence that
# follows is a stopped daemon and not exposure.
#
# EXACTLY ONE NAME, and specifically not the two ``shutdown_cancel_orders_*``
# rows. Those bracket the order sweep, which runs BEFORE the trigger is cleared,
# so at both of them the switch is still armed. Naming them here got the
# exemption backwards in both directions at once: a real stop-and-restart (whose
# last row is ``kill_switch_disarmed``) was charged its entire downtime as
# outage and failed a healthy run, while a SIGKILL landing between "started" and
# "completed" — the switch still armed and about to fire — was handed the
# exemption. Verified against the writer, not assumed (2026-08-01 round-13
# incremental review).
_KILL_SWITCH_CLEAN_ENDINGS = frozenset({"kill_switch_disarmed"})
if (
    not {
        _KILL_SWITCH_REFRESH_OK,
        _KILL_SWITCH_REFRESH_FAILED,
        _KILL_SWITCH_FIRED,
        _KILL_SWITCH_DISARM_FAILED,
    }
    <= repo.KILL_SWITCH_EVENT_TYPES
):
    raise AssertionError(
        "validation's kill-switch event types drifted from repository.KILL_SWITCH_EVENT_TYPES"
    )


def _schedule_cancel_seconds(config_json: str | None) -> Decimal:
    """How long one successful schedule covered THIS run, from its own genesis.

    The availability measure has to know the run's real deadline: it is the line
    between "silence we were still covered through" and "silence in which the
    exchange cancelled every order on the wallet". A module constant cannot know
    it — ``schedule_cancel_seconds`` is configurable (floored at 5, unbounded
    above), so any fixed threshold is simultaneously too strict for one run and
    too lax for another. Reading it per-run is what stops a legal config from
    moving the acceptance verdict.

    Same degrade discipline as :func:`.validation.execution_mode` — a corrupt or partial
    genesis record falls back to the default rather than crashing a read-only
    reporter — with one addition: a non-positive or non-numeric value falls back
    too, because a zero deadline would charge every gap as unprotected and turn
    a garbled config into a guaranteed exit 5.
    """
    if not config_json:
        return DEFAULT_SCHEDULE_CANCEL_SECONDS
    try:
        parsed = json.loads(config_json)
    except (ValueError, TypeError):
        return DEFAULT_SCHEDULE_CANCEL_SECONDS
    live = parsed.get("live") if isinstance(parsed, dict) else None
    switch = live.get("kill_switch") if isinstance(live, dict) else None
    raw = switch.get("schedule_cancel_seconds") if isinstance(switch, dict) else None
    if raw is None:
        return DEFAULT_SCHEDULE_CANCEL_SECONDS
    # COERCE exactly as the config layer does; never type-test. ``config_json``
    # stores the ``live:`` block VERBATIM, before coercion (cli._drift._run_config_subset),
    # so a perfectly legal ``schedule_cancel_seconds: "300"`` arrives here as a
    # str — and an ``isinstance(raw, (int, float))`` test silently fell back to
    # 120, measuring the run against a deadline it never had. That is precisely
    # the failure this helper exists to prevent: every healthy 121-300s gap would
    # then be charged as a full outage and a healthy run fails at exit 5.
    # ``int_from_yaml`` already rejects bools (True would otherwise be a 1-second
    # deadline) and non-integral floats (2026-08-01 round-13 exit check).
    try:
        seconds = Decimal(int_from_yaml(raw))
    except (ValueError, TypeError):
        return DEFAULT_SCHEDULE_CANCEL_SECONDS
    return seconds if seconds > 0 else DEFAULT_SCHEDULE_CANCEL_SECONDS


# The deadline ``arm()`` reports it installed, read back out of the row it wrote
# (``kill_switch_events.deadline_detail``, which ``arm()`` calls). Taking the number
# from the ARMING rather than from genesis is what makes the measure survive a
# resume under an edited config: ``runs.config_json`` is written once, at
# ``--create``, and a changed ``live.kill_switch`` block on a later resume is only
# a printed warning (cli's params-drift lane), so genesis 600 resumed at 120 billed
# every real 121-600s lapse as covered and reported a genuinely exposed run as
# live_ready (2026-08-01 round-14 review).
#
# Read from ``kill_switch_refreshed`` as well as ``kill_switch_armed`` — and from
# those two event types only (the tally's allowlist; the sweep's completed row
# dumps arbitrary JSON into this same column). The daemon's
# refresh rows carry no detail, so they simply say nothing and leave the standing
# value alone — but ``live-smoke`` renews the cover at ``max(config, 120s)``, which
# is LONGER than the armed row's number whenever a run is configured below 120. A
# reader that only listened to arm() would then measure the smoke phase against a
# cover shorter than the one actually installed and invent outages that never
# happened. Whoever installs the cover states it; whoever states it is believed.
#
# This makes the detail format a CONTRACT between modules, so a test pins writer
# against reader. Parsing is deliberately forgiving: an unreadable or absent detail
# leaves the deadline already in force standing rather than inventing one, so a
# format change degrades to the genesis value — the previous behaviour — instead
# of to nonsense.
_STATED_DEADLINE_RE = re.compile(r"\bdeadline=(\d+)s\b")


def _stated_deadline_seconds(detail: str | None) -> Decimal | None:
    """The cover this row says it installed, or None when it does not say."""
    if not detail:
        return None
    match = _STATED_DEADLINE_RE.search(detail)
    if match is None:
        return None
    seconds = Decimal(match.group(1))
    # Same non-positive guard as the genesis reader: a zero deadline would charge
    # every gap as unprotected and turn one garbled row into a guaranteed exit 5.
    return seconds if seconds > 0 else None


@dataclass(frozen=True)
class _StrandedAttempts:
    """The run's non-terminal decision attempts, as the acceptance report sees them.

    Deliberately NOT ``repo.find_in_progress_attempt``: that helper RAISES on
    the two-row case (it is the daemon's fail-loud, and it is what wedges
    adoption in the first place), and a read-only acceptance validator must
    report a broken store rather than crash on it — the whole point of looking
    is that this run may never have got a daemon past boot.

    ``oldest_at`` is the oldest row's ``timestamp`` (when it last CHANGED
    STATE), the same basis ``trailing_failure_streaks`` dates its streak by and
    for the same reason: ``scheduled_at`` is the cycle's original slot, which a
    stranded attempt carries unchanged from before the crash, so ages taken
    from it would count downtime the run cannot be blamed for. ``None`` when
    the stamp will not parse; the caller reports THAT as its own integrity
    failure rather than guessing an age — the column is NOT NULL and written
    only by the repository, so a value this validator cannot read is a corrupt
    row, and staying silent about it would let a strictly more broken store
    pass the gate that a merely stale one fails.
    """

    count: int
    oldest_id: str | None
    oldest_at: datetime | None

    def __post_init__(self) -> None:
        # A frozen dataclass rather than a NamedTuple for the reason
        # ``TrailingFailureStreaks`` spells out in runtime/no_decision.py:
        # NamedTuple builds through __new__ and never calls __post_init__, so
        # the same guard written there is decoration. The three verdicts this
        # type feeds are all gated on ``count``, so a mismatched instance
        # either vanishes from the report or renders its own hole into it:
        # ``count=1`` with no ``oldest_id`` prints the shortfall as
        # "stranded_decision_cycle = None". The query cannot build one; a
        # hand-built one (a test, a future caller) is what this catches.
        if self.count < 0:
            raise ValueError(f"_StrandedAttempts count must be >= 0, got {self.count}")
        if bool(self.count) != (self.oldest_id is not None):
            raise ValueError(
                f"_StrandedAttempts count={self.count} disagrees with "
                f"oldest_id={self.oldest_id!r}: rows exist iff the oldest is named"
            )
        if not self.count and self.oldest_at is not None:
            raise ValueError("_StrandedAttempts has no rows but carries an oldest_at")


def _stranded_in_progress(conn: sqlite3.Connection, run_id: str) -> _StrandedAttempts:
    rows = repo.iter_in_progress_attempts(conn, run_id)
    if not rows:
        return _StrandedAttempts(0, None, None)
    oldest = rows[0]
    try:
        oldest_at = parse_instant(oldest["timestamp"])
    except (ValueError, TypeError):
        oldest_at = None
    return _StrandedAttempts(len(rows), str(oldest["decision_attempt_id"]), oldest_at)


class _SafeModeState(NamedTuple):
    """The run's CURRENT safe-mode episode, if it is in one."""

    mode_type: str | None  # "manual" / "recoverable" / None
    reason: str | None


def _current_safe_mode(conn: sqlite3.Connection, run_id: str) -> _SafeModeState:
    """Whether the run is sitting in a safe-mode episode right now, and why.

    Until 2026-07-31 the only safe-mode question this file asked was "was the
    §10.3 daily-loss cap involved", so every other reason was invisible to every
    gate. §10.4's three-consecutive-loss guard enters a MANUAL episode — one
    safe_mode.py documents as "a human must confirm" — and leaves no
    reconciliation case and no protection event, so its ONLY trace is this row.
    Meanwhile §13.1 keeps decision cycles running (it blocks risk-ADDING orders,
    not the loop), so cycle_count keeps climbing. A run that is locked out of
    placing new orders pending human sign-off would report live_ready and exit 0.
    """
    row = repo.get_scheduler_state(conn, run_id)
    if row is None or row["safe_mode_type"] is None:
        return _SafeModeState(None, None)
    return _SafeModeState(str(row["safe_mode_type"]), row["safe_mode_reason"])


def _daily_loss_still_active(conn: sqlite3.Connection, run_id: str) -> bool:
    """Whether the run is CURRENTLY in a safe-mode episode naming the §10.3 cap.

    Episode-scoped, the same shape ``safe_mode.enter`` uses for its own
    idempotence: the current-state trio keeps only the FIRST reason, so a
    daily-loss trigger absorbed into an episode entered for something else lives
    only in the history rows — hence the ``since_iso=entered_at`` read rather than
    a plain ``safe_mode_reason`` comparison.
    """
    row = repo.get_scheduler_state(conn, run_id)
    if row is None or row["safe_mode_type"] is None:
        return False
    if row["safe_mode_reason"] == REASON_DAILY_LOSS:
        return True
    entered_at = row["safe_mode_entered_at"]
    if entered_at is None:
        return False
    return repo.has_safe_mode_reason_event(
        conn, run_id, reason=REASON_DAILY_LOSS, since_iso=entered_at
    )


def _unprotected_windows(
    conn: sqlite3.Connection, run_id: str, now: datetime
) -> tuple[Decimal, int, bool]:
    """``(total_seconds, window_count, has_open_window)`` from protection events.

    Per symbol, an unprotected window opens on ``stop_loss_repair_exhausted`` or
    on a ``stop_loss_repair_blocked`` that left no COVERING stop-loss resting,
    and closes only when the episode genuinely ENDED: an SL back on the book
    (``stop_loss_placed`` / ``stop_loss_modified``), the position leaving exposure
    (``protection_cleared``), or protection's own "failure line came down" event
    (``degraded_protection_cleared``, which covers both recovery-to-protected and
    flat). A §17.2 emergency close is NOT a close — see the vocabulary comment
    above for why counting the submission zeroed this whole metric. A window still
    open at the end of the log is measured to ``now`` and flagged — the position
    was last seen unprotected, which is worse than a closed one, not better.

    The window COUNT is returned alongside the seconds because the caller gates on
    both: a clamped-to-zero window (out-of-order stamps on a corrupt store, or an
    onset and close inside the same clock tick) must never be indistinguishable
    from a run that was never unprotected at all.

    The ``blocked`` qualifier matters. §17.4 is modify-before-cancel, so the wire
    gate refusing a MODIFY can leave the previous SL resting at a stale trigger:
    not the band the manager wants, but not unprotected either. Counting those
    seconds would fail an otherwise-healthy 30-cycle acceptance run on a single
    kill-switch refresh blip that coincided with an SL adjustment — and §20.3's
    ``unprotected_position_seconds = 0`` is an exit-5, non-curable verdict.

    But the test protection.py applies before stamping the event is COVERAGE,
    not existence — closing side and ``qty >= position``, agreeing with
    ``reconcile._has_valid_sl`` on the two rules that decide the answer (that
    one also sums coverage across the exchange's open orders and requires
    reduce-only). protection.py reads its single local row and then CONFIRMS it
    against orderStatus before stamping, because the branch that stamps only
    runs while the kill switch is down — and a lapsed deadline has the exchange
    cancel the whole wallet without touching those rows. (Until 2026-07-31 this
    said the single local row made protection "the stricter of the two"; reading
    one row instead of the book is not stricter, it is a different source, and in
    the one state this branch runs in it is the source that has just been
    invalidated.) So the commonest blocked MODIFY, a RESIZE
    whose old SL now covers only part of the position, still opens a window:
    part of that position genuinely has no stop. ``order_id`` present ⇒ a
    covering SL was resting — and if a window was ALREADY open for that symbol
    (a prior non-covering onset), the position just became covered again
    without the gate flag itself resolving, so this closes it: the same
    coverage-not-existence principle applied to the close side, not just the
    onset side (2026-07-30 — a covering blocked event used to only suppress a
    new onset, leaving an open window running until an unrelated later close
    event, over-counting real protected time as unprotected).
    """
    onset = _UNPROTECTED_ONSET_EVENTS
    close = _UNPROTECTED_CLOSE_EVENTS
    total = Decimal(0)
    windows = 0
    has_open = False
    open_at: dict[str, datetime] = {}  # symbol -> onset instant

    # Each window is clamped to >= 0 in ISOLATION (out-of-order timestamps make
    # one window negative), so a single drift can never subtract from another
    # window's genuine unprotected seconds before the sum.
    def _close_open_window(symbol: str, when: datetime) -> None:
        nonlocal total, windows
        total += _window_seconds(open_at.pop(symbol), when)
        windows += 1

    # Instants at which the switch DEMONSTRABLY fired. From that moment until an
    # SL is confirmed back on the book, no ``order_id`` stamp is evidence: the
    # exchange cancelled the wallet's whole book and left every local row saying
    # "open". protection.py now confirms against orderStatus before stamping, so
    # this is the second line — it also covers rows written by a build that
    # predates that fix, which matters because a 30-cycle acceptance run can span
    # a deploy.
    #
    # Cross-writer timestamps are compared here, which the module warning above
    # says are not reliably orderable within a tick. That is tolerable in this
    # direction only: mis-ordering marks a stamp suspect slightly EARLY, which
    # opens a window (fail-closed). It must never be inverted into suppressing
    # one.
    fired_at = sorted(
        parse_instant(row["timestamp"])
        for row in repo.iter_kill_switch_events(conn, run_id)
        if row["event_type"] == _KILL_SWITCH_FIRED
    )
    next_firing = 0
    stamp_is_evidence = True

    for row in repo.iter_protection_order_events(conn, run_id):
        symbol = row["symbol"]
        event = row["event_type"]
        when = parse_instant(row["timestamp"])
        while next_firing < len(fired_at) and fired_at[next_firing] <= when:
            next_firing += 1
            stamp_is_evidence = False
        if event in (_SL_PLACED, _SL_MODIFIED):
            # An SL positively back on the book: the rows agree with the
            # exchange again, so stamps become evidence once more.
            stamp_is_evidence = True
        if event in onset:
            if event == _SL_REPAIR_BLOCKED and row["order_id"] and stamp_is_evidence:
                # A refused MODIFY (gate or throttle) over a still-COVERING SL
                # (protection.py stamps the id only then; see the docstring,
                # and it confirms against orderStatus first). Stale trigger,
                # not an unprotected window — and if one was already open for
                # this symbol, it just resolved: close it (2026-07-30).
                if symbol in open_at:
                    _close_open_window(symbol, when)
                continue
            # A fresh onset while already open keeps the earliest onset (the
            # window never closed), so only record if not already open.
            open_at.setdefault(symbol, when)
        elif event in close and symbol in open_at:
            _close_open_window(symbol, when)
    # Any window still open: measure to ``now`` and flag it.
    for started in open_at.values():
        total += _window_seconds(started, now)
        windows += 1
        has_open = True
    return total, windows, has_open


def _window_seconds(start: datetime, end: datetime) -> Decimal:
    """Seconds between two event instants, clamped at zero.

    Module level so the unprotected-window measurement and the kill-switch
    outage measurement cannot disagree about what a duration is. The clamp is
    load-bearing: event pairs are not guaranteed monotone (a clock correction
    mid-run), and a negative segment would silently CREDIT time against the
    total, making an exposed run look better than a clean one.
    """
    secs = Decimal(str((end - start).total_seconds()))
    return secs if secs > 0 else Decimal(0)


class _KillSwitchTally(NamedTuple):
    """What the §18.5 event log says about this run's dead man's switch."""

    refresh_rate: Decimal | None  # None when there is nothing to measure
    refreshed: int
    failed: int
    fired_count: int  # deadlines the exchange demonstrably acted on
    disarm_failed_count: int
    outage_seconds: Decimal  # wall time the wallet's cover had LAPSED
    outage_episodes: int  # distinct lapses, however many retries each took
    covered_seconds: Decimal  # wall time the switch was supposed to be running
    # Refreshes written DURING live-smoke. Real cover, deliberately not sample
    # credit — but the operator has to be told they exist, or a run with 120 of
    # them reads "no refresh events yet" beside non-zero covered seconds.
    suite_refreshed: int
    # Suite-authored FAILURES. Counted for the same reason as the successes:
    # subtracted from ``failed`` and tallied nowhere, a suite whose refreshes
    # all failed reported "no kill-switch refresh events yet" beside 600
    # uncovered seconds (2026-08-01 round-17 review).
    suite_failed: int
    # The DAEMON's last kill-switch event is not a completed shutdown, so its
    # log stops mid-flight: the process was killed rather than stopped.
    # Everything after that instant is unmeasurable (see _kill_switch_tally), so
    # this is the only honest trace of it. ``None`` when the run has no daemon
    # rows at all — with no daemon to report on, answering either way is a claim
    # rather than a reading (2026-08-01 round-17 review).
    ended_without_clean_shutdown: bool | None
    # An outage still open at the RUN's last event. Usually its final word was a
    # failed refresh, but NOT only that: silence longer than the standing
    # deadline opens one too, and neither ``fired``, ``disarm_failed`` nor the
    # shutdown-sweep rows close it — so this can be true with no
    # ``refresh_failed`` row anywhere in the table, and the summary must not
    # send the operator looking for one. Cannot be measured (no later event),
    # so it is reported.
    #
    # Deliberately run-scoped where the flag above is daemon-scoped, and the two
    # are not nested. They answer different questions: "was the daemon killed"
    # is a fact about the daemon that no later row can revise, while this one
    # asks whether the availability figure is a LOWER BOUND because its tail is
    # unmeasurable — and a later ``armed``/``refreshed``/``disarmed`` row,
    # whoever wrote it, closes that tail and gets the seconds charged in full.
    # The test is the LAST such row, not "did the re-run arm": smoke's pre-flight
    # arms on any order-placing selection, so that condition is nearly always
    # true and says nothing. A daemon that died inside an outage followed by a
    # CLEAN live-smoke re-run reads "clean shutdown: no, ended_in_outage: no";
    # if that re-run then fails its own refresh AND its exit disarm, that is a
    # SECOND episode and this is yes. The exposure is in the outage seconds
    # either way (2026-08-01 round-21 review; user decision: keep run-scoped).
    ended_in_outage: bool

    @property
    def refresh_total(self) -> int:
        return self.refreshed + self.failed


def _kill_switch_tally(
    conn: sqlite3.Connection, run_id: str, config_json: str | None
) -> _KillSwitchTally:
    """Availability by DURATION, plus the two OUTCOME counts, in one pass.

    The rate alone is an availability metric and cannot express the thing §20.3
    actually cares about: whether the switch ever FIRED. Those are different
    questions with different answers — a firing is preceded by a run of refresh
    failures whose share of a multi-day run rounds to nothing.

    Measured as OUTAGE TIME over covered time, never as a ratio of event counts.
    Counting rows made the verdict depend on how often the manager happened to
    retry, which is a tuning parameter, not a property of the run: when the retry
    loop was rate-limited (and one outage stopped writing one row per attempt),
    the very same 24h run with twelve 90-second outages moved from 98.3% —
    correctly failing — to 99.6%, passing, while nothing about its exposure
    changed. Time is the thing the operator is actually promised: an outage is
    the window in which a lapsed deadline would cancel the resting SL/TP, and it
    is exactly as dangerous whether we retried into it four times or forty
    (2026-08-01 lifecycle review).

    An outage opens at a ``kill_switch_refresh_failed`` — or on silence past the
    standing deadline, so it can open with no failure row at all — and closes at
    the next ``kill_switch_refreshed``, ``kill_switch_armed`` (emitted right
    after a successful schedule, and therefore the same evidence) or
    ``kill_switch_disarmed`` (a signed round-trip that returned, so the same
    evidence again). It is charged for as long as it ran. Nothing else closes
    one: ``fired``, ``disarm_failed`` and the shutdown-sweep rows leave it open
    (2026-08-01 round-21 review: three copies of this sentence named only the
    refresh).

    SILENCE LONGER THAN THE RUN'S OWN DEADLINE IS ALSO AN OUTAGE. One successful
    schedule buys ``schedule_cancel_seconds`` of cover; a stretch longer than that
    with no row renewing it is a stretch in which the exchange cancelled every
    order on the wallet, whether the process was wedged, throttled, or dead. There
    used to be a rule here reading such a stretch as "the process was down, judge
    nothing", and it was unsound in the most direct way available: a dead process
    and a fired switch leave the SAME silence, so the rule threw away precisely
    the case with the worst consequence. It also made the number NON-MONOTONE — a
    301s outage scored 0 outage seconds and passed at 100% while a 299s one scored
    299 and correctly failed — so a run merely had to stay broken a little longer
    to be certified (2026-08-01 round-13 review).

    The deadline comes from the run itself, never a constant: it is
    operator-configurable, so a fixed threshold is both too strict for one run and
    too lax for another. The constant it replaced was 300s against a 120s default
    deadline, which billed the whole 120–300s band — every stretch in which the
    switch demonstrably fires — as healthy covered time. Each stretch is measured
    against the cover IN FORCE across it: a stated deadline is believed on the
    two schedule-installing event types — ``kill_switch_armed`` and
    ``kill_switch_refreshed`` alike, and on no other (the sweep's completed row
    dumps arbitrary JSON into the same column) — while the daemon's refreshes
    carry no detail and leave the standing value alone, and the genesis config
    supplies only the starting value. Both events, not just arming: the smoke
    suite renews at
    ``max(config, 120s)``, so on a run configured below 120 its floored rows —
    the per-test refreshes, plus test 14's ``armed``/``refreshed`` pair — are
    the ONLY evidence of the longer cover actually in force. Reading genesis
    alone was wrong for any run that resumed under
    an edited ``live.kill_switch`` block — which the CLI merely warns about — so a
    run armed at 120 but created at 600 had every real lapse billed as covered.

    THE WINDOW ENDS AT THE LAST EVENT, never at ``now``. Wall time since the run
    stopped measures when the operator got round to running ``validate``, nothing
    about the run; charging it would make the verdict drift with the clock, which
    is the defect the previous revision was written to fix and which stays fixed
    here. The cost is that a run which crashed and never restarted has no later
    row to measure its final lapse against, so that case is REPORTED rather than
    measured — ``ended_without_clean_shutdown``. Reported and not gated: a run
    being validated WHILE STILL RUNNING has no shutdown row either, and the log
    cannot tell that apart from a killed one, so the flag informs the operator
    without getting a vote.
    """
    refreshed = 0
    suite_refreshed = 0
    suite_failed = 0
    failed = 0
    fired = 0
    disarm_failed = 0
    outage_total = Decimal(0)
    covered = Decimal(0)
    episodes = 0
    in_outage = False
    previous: datetime | None = None
    previous_event: str | None = None
    # The STARTING deadline only. Every schedule-installing row that names one
    # (``kill_switch_armed`` or ``kill_switch_refreshed``) replaces it for the
    # stretches that follow, so a run resumed under an edited config — or covered
    # by the smoke suite's longer deadline — is measured against the cover each
    # stretch actually had.
    deadline = _schedule_cancel_seconds(config_json)
    # By TIMESTAMP, not by insertion order: the rows arrive ordered by event_id,
    # and a clock correction (or a test seeding a second batch) makes those two
    # orders disagree — which would then collapse the window and read a healthy
    # run as 0% available.
    events = sorted(
        (
            (parse_instant(row["timestamp"]), row["event_type"], row["detail"])
            for row in repo.iter_kill_switch_events(conn, run_id)
        ),
        # Keyed on (time, type) and NOT on the whole tuple: ``detail`` is free text
        # and nullable, so letting it into the sort key would order nothing
        # meaningful and would raise str-vs-None on an exact tie.
        key=itemgetter(0, 1),
    )
    for when, event, detail in events:
        # A stretch that OPENS on a deliberate release counts on NEITHER side: the
        # trigger was cleared on purpose, so it says nothing about whether the
        # switch was available. Crediting it to ``covered`` alone would let an
        # operator sitting at 98% stop the daemon for an hour, restart, and dilute
        # a failing run into a pass on downtime they chose
        # (2026-08-01 round-13 incremental review).
        if previous is not None and previous_event not in _KILL_SWITCH_CLEAN_ENDINGS:
            gap = _window_seconds(previous, when)
            # Everything else is time the switch was supposed to be covering, so
            # it counts toward the denominator whether or not it was an outage.
            covered += gap
            if in_outage:
                # A recorded outage: charged in full for as long as it ran, which
                # is what makes the number independent of the retry cadence.
                outage_total += gap
            elif gap > deadline:
                # SILENCE LONGER THAN THE COVER. Nothing renewed the schedule
                # across this stretch, so the exchange cancelled every order on
                # the wallet partway through it — SL and TP included — whatever
                # the process was doing. Charged in full, to BOTH sides: this is
                # the case a dead process produces, and the one the old "silence
                # means downtime, judge nothing" rule threw away.
                outage_total += gap
                episodes += 1
                # Still exposed as far as the log knows. Without this the row at
                # the far end — typically the ``kill_switch_refresh_failed`` that
                # the wedged process finally managed to write — opened a SECOND
                # episode for one continuous lapse. The event handlers below still
                # get the final say: an ``armed``/``refreshed`` at the far end
                # closes it immediately.
                in_outage = True
        previous = when
        previous_event = event
        # A row that names the cover it installed re-sizes every stretch after it:
        # from here on THAT is the line between "silence we were covered through"
        # and "silence the exchange swept the wallet in". Rows that say nothing
        # leave the standing deadline alone.
        #
        # Only rows that actually INSTALL cover get a vote on the deadline. "Any
        # row that states one" was too wide: ``shutdown_cancel_orders_completed``
        # dumps arbitrary JSON into this same column, so a cloid that happened to
        # contain the token would have re-sized the run's protection deadline.
        # Arming and refreshing are the two events that put a schedule on the
        # exchange; each install site records one of those two names right
        # after the wire call, and no path records an install under any
        # other name.
        if event in (_KILL_SWITCH_REFRESH_OK, _KILL_SWITCH_ARMED):
            stated_deadline = _stated_deadline_seconds(detail)
            if stated_deadline is not None:
                deadline = stated_deadline
            # Both prove the switch is scheduled; only the refresh counts toward
            # the sample floor, which asks "has it been EXERCISED enough to judge".
            #
            # And only the DAEMON'S refreshes. Suite-authored rows are real cover —
            # they count in full toward outage seconds and toward the deadline
            # above — but the floor asks whether this run exercised the switch
            # enough for its availability figure to mean anything, and a few
            # back-to-back smoke suites answer it at 100% with the daemon never
            # started. That figure would describe the smoke phase, not the thing
            # §20.3 certifies for real money (2026-08-01 round-15). The concrete
            # figure is quoted once, in RUNBOOK §20.3, where a doc-pin test
            # derives it from ``smoke.REFRESHES_PER_FULL_SUITE`` (issue #100).
            if event == _KILL_SWITCH_REFRESH_OK:
                if is_suite_authored(detail):
                    suite_refreshed += 1
                else:
                    refreshed += 1
            in_outage = False
        elif event == _KILL_SWITCH_REFRESH_FAILED:
            # Same split: a suite-authored failure still OPENS an outage episode
            # (the cover really did lapse), it just does not buy sample credit.
            if is_suite_authored(detail):
                suite_failed += 1
            else:
                failed += 1
            if not in_outage:
                in_outage = True
                episodes += 1
        elif event == _KILL_SWITCH_FIRED:
            fired += 1
        elif event == _KILL_SWITCH_DISARM_FAILED:
            disarm_failed += 1
        elif event in _KILL_SWITCH_CLEAN_ENDINGS:
            # A successful signed round-trip (clear_scheduled_cancel returned), so
            # it is the same class of positive wire evidence as ``armed`` and ENDS
            # an outage — it just is not a refresh, so it earns no sample credit.
            # Without this a run stopped cleanly after a network blip reported
            # "clean shutdown: yes" and "ended inside an outage: yes" at once, and
            # the shortfall named a failed-refresh row that was not the last row
            # (2026-08-01 round-13 exit check).
            in_outage = False
    # A clean ending closes the run's story only if the DAEMON wrote it. Re-running
    # live-smoke after a SIGKILL puts the suite's exit disarm last, which reported
    # "clean shutdown: yes" for a run whose daemon was killed (2026-08-01 round-16).
    # The gap-level exemption above is unaffected: that trigger really was cleared,
    # whoever cleared it.
    # Over the DAEMON's rows, not the run's last row. Requiring the final row to
    # be an unmarked clean ending killed the SIGKILL-laundering case but also
    # condemned two runs whose daemon stopped cleanly: a smoke-only run-id, and —
    # the common one — a daemon that shut down cleanly and was then followed by
    # the live-smoke re-run the CLI itself tells the operator to do. Both reported
    # "clean shutdown: no" with the daemon's own disarm sitting in the table
    # (2026-08-01 round-17 review).
    daemon_rows = [row for row in events if not is_suite_authored(row[2])]
    ended_dirty = None if not daemon_rows else daemon_rows[-1][1] not in _KILL_SWITCH_CLEAN_ENDINGS
    # An outage still OPEN at the last event is a different, sharper fact than a
    # merely dirty ending. Usually the last thing the run managed to say was "I
    # failed to refresh" — but NOT only that: silence past the standing deadline
    # opens one too, so this can be true with no ``refresh_failed`` row in the
    # table at all, and the two field comments say so (2026-08-01 round-20
    # review: this third gloss, the one on the computation itself, was the one
    # round 19's "everywhere" missed). The window cannot measure past its own
    # end, so an outage that
    # never closed contributes zero seconds and a run that failed and NEVER came
    # back scored a clean 100% — better than the same run recovering two minutes
    # later, which is the worse-run-scores-better shape all over again. It cannot
    # be charged (that would drift with the clock) so it is surfaced instead
    # (2026-08-01 round-13 incremental review).
    ended_in_outage = in_outage
    # Pinned to the shared context like every other layer's arithmetic: this is
    # the acceptance report's ONLY division, and it decides an exit-5 verdict.
    with localcontext(DECIMAL_CONTEXT):
        if refreshed + failed == 0 or covered <= 0:
            # None is "cannot say", and it routes to the shortfall lane (keep
            # running) rather than to a verdict. Both arms are genuine absences of
            # evidence: no refresh rows at all, and — the arm that used to return
            # a PERFECT SCORE — rows that span no wall time. A zero denominator
            # cannot demonstrate availability, and reading it as 1 meant any config
            # whose healthy cadence never accumulated covered time (or any run
            # whose events landed in one instant) passed §20.3 unconditionally.
            rate = None
        else:
            # No clamp needed: every second added to ``outage_total`` is added to
            # ``covered`` in the same branch, so the quotient is in [0, 1] by
            # construction.
            rate = (covered - outage_total) / covered
    return _KillSwitchTally(
        rate,
        refreshed,
        failed,
        fired,
        disarm_failed,
        outage_total,
        episodes,
        covered,
        suite_refreshed,
        suite_failed,
        ended_dirty,
        ended_in_outage,
    )


def _unresolved_reconciliation_mismatches(conn: sqlite3.Connection, run_id: str) -> int:
    """Count §12.3 cases still open — the §21.4 "no unresolved mismatch" metric.

    Mirrors the ``safe-mode --status`` open-case logic: a mismatch case is open
    until something stamps ``action_taken`` — an operator's ``--stamp-case``, or
    the sweep itself for the dispositions it can establish. A ``fill_unmapped``
    sighting is a backlog (resolved by booking the fill, not by stamping), not a
    mismatch, so it is excluded from the count.

    Rows, not distinct facts: a fact whose sweep disposition is provisional
    (``repo.PROVISIONAL_DISPOSITIONS``) records each recurrence as its own row,
    and only the newest of them is ever unresolved — so this still counts one
    per LIVE fault, which is what §21.4 asks, while the run's history keeps the
    episodes that were disposed of.
    """
    count = 0
    for row in repo.iter_exchange_reconciliation_events(conn, run_id):
        case = row["case_type"]
        if case in _MISMATCH_CASE_TYPES and row["action_taken"] is None:
            count += 1
    return count


@dataclass(frozen=True)
class LiveRunFacts:
    """Everything :func:`.validation.validate_live_run` reads from the store.

    The metrics of :class:`.validation_report.LiveValidationReport` before any
    verdict is drawn from them, plus the readings the verdict needs that the
    report does not carry: the stranded attempts, the smoke buckets and the
    text of a replay that raised. Where a field IS a report metric it carries
    the report's name, so the hand-off there reads as a copy, not a translation.
    """

    cycle_count: int
    api_failed_count: int
    invalid_output_count: int
    streaks: TrailingFailureStreaks
    stranded: _StrandedAttempts
    prompt_regimes: tuple[repo.PromptRegime, ...]
    live_order_count: int
    fill_count: int
    exchange_fill_dedupe_error_count: int
    orphan_exchange_order_count: int
    orphan_exchange_order_distinct_count: int
    duplicate_fill_apply_count: int
    local_exchange_position_mismatch_count: int
    # 1 when the replay itself raised; ``replay_raised`` then carries the
    # exception, and is None whenever the books were replayable.
    account_replay_mismatch_count: int
    replay_raised: str | None
    unprotected_position_seconds: Decimal
    unprotected_window_count: int
    unresolved_unprotected_window: bool
    kill_switch: _KillSwitchTally
    safe_mode: _SafeModeState
    unresolved_reconciliation_mismatch_count: int
    daily_loss_breached: bool
    daily_loss_active: bool
    emergency_close_event_count: int
    smoke: SmokeGateReport

    def __post_init__(self) -> None:
        # A replay that raised is counted as exactly ONE unverifiable book, and
        # ``replay_raised`` is the only place the exception survives: the report
        # carries the count but not the text, so nothing downstream re-checks
        # this pair the way the report re-checks the others. The reader cannot
        # build the mismatch; a hand-built instance (a test driving one gate)
        # would print "accounting replay raised" beside a count that says
        # otherwise.
        if self.replay_raised is not None and self.account_replay_mismatch_count != 1:
            raise ValueError(
                f"replay_raised is {self.replay_raised!r} but account_replay_mismatch_count "
                f"is {self.account_replay_mismatch_count} (a replay that raised is one "
                "unverifiable book)"
            )


def read_live_run_facts(
    conn: sqlite3.Connection, *, run_id: str, config_json: str | None, now: datetime
) -> LiveRunFacts:
    """Every store read behind the acceptance verdict, in one pass over ``conn``.

    ``config_json`` is the run's genesis record (the deadline the kill-switch
    tally starts from); ``now`` bounds an unprotected window still open at the
    end of the log. Read-only, and a store the reads cannot make sense of is
    reported THROUGH the facts (a stranded attempt with an unreadable stamp, a
    replay that raised) rather than raised past the caller.
    """
    cycle_count = repo.count_decision_attempts(conn, run_id, statuses=_COMPLETED_CYCLE_STATUSES)
    api_failed_count = repo.count_decision_attempts(conn, run_id, statuses=("api_failed",))
    invalid_output_count = repo.count_decision_attempts(
        conn, run_id, statuses=("invalid_output",)
    )
    streaks = trailing_failure_streaks(conn, run_id)
    stranded = _stranded_in_progress(conn, run_id)
    prompt_regimes = repo.prompt_regime_counts(conn, run_id, statuses=_COMPLETED_CYCLE_STATUSES)
    fill_count = repo.count_fills(conn, run_id)
    # Distinct acknowledged live orders: the exchange confirmed it holds
    # (or already held — a 'duplicate' ack) each cloid.
    live_order_count = repo.count_exchange_known_place_cloids(conn, run_id)
    # Structural dedupe assertion: the UNIQUE index makes this 0 unless the
    # store is corrupt.
    duplicate_fill_apply_count = repo.count_duplicate_exchange_fill_keys(conn, run_id)
    exchange_fill_dedupe_error_count = len(
        repo.iter_exchange_reconciliation_events(conn, run_id, case_type="fill_money_drift")
    )
    orphan_rows = repo.iter_exchange_reconciliation_events(
        conn, run_id, case_type="orphan_exchange_order"
    )
    orphan_exchange_order_count = len(orphan_rows)
    # Both numbers off ONE row list, so they cannot describe different
    # reads. Bucketed by CLOID, not by fact key: this case type writes a
    # bare cloid, ``<cloid>|local_terminal`` and
    # ``<cloid>|local_terminal_read_failed``, all reachable for the same
    # order in one run, so counting keys would report up to 3 orders where
    # there is 1 — the ``|`` split is what makes this "orders to go find".
    # Every orphan case this sweep constructs carries a key, so a NULL is a
    # store written by something else; it becomes its own bucket rather
    # than being dropped, which keeps "orders ≤ rows" true and cannot
    # under-state the orders THIS sweep records.
    orphan_exchange_order_distinct_count = len(
        {str(row["exchange_value"]).split("|", 1)[0] for row in orphan_rows}
    )
    local_exchange_position_mismatch_count = len(
        repo.iter_exchange_reconciliation_events(
            conn, run_id, case_type="exchange_position_mismatch"
        )
    )
    unprotected_position_seconds, unprotected_window_count, unresolved_unprotected_window = (
        _unprotected_windows(conn, run_id, now)
    )
    kill_switch = _kill_switch_tally(conn, run_id, config_json)
    safe_mode = _current_safe_mode(conn, run_id)
    unresolved_reconciliation_mismatch_count = _unresolved_reconciliation_mismatches(conn, run_id)

    # §10.3 daily-loss cap breach = a safe-mode episode entered for that
    # reason (the loss guard's only durable record of a breach). Two readings,
    # and §21.4 needs both (decided 2026-07-30):
    #   - EVER (store minimum as the since-instant, so it matches any episode):
    #     informational. §10.3's cap is a RECOVERABLE guard that auto-releases at
    #     the next UTC midnight, so gating on "ever" made one ordinary risk event
    #     on day 2 pin a real-money run at exit 5 permanently — 30 more real
    #     cycles under a fresh run-id, with no --stamp-case escape.
    #   - CURRENTLY unresolved: the gating condition, matching the sibling
    #     _unresolved_reconciliation_mismatches line it sits beside.
    # REASON_DAILY_LOSS, not the literal: has_safe_mode_reason_event takes a
    # free string, so a renamed constant would match zero rows forever and a
    # mainnet run that actually blew its §10.3 cap would report live_ready.
    daily_loss_breached = repo.has_safe_mode_reason_event(
        conn, run_id, reason=REASON_DAILY_LOSS, since_iso=_SINCE_BEGINNING
    )
    daily_loss_active = _daily_loss_still_active(conn, run_id)
    emergency_close_event_count = sum(
        1
        for row in repo.iter_protection_order_events(conn, run_id)
        if row["event_type"] == "emergency_close_triggered"
    )
    smoke = smoke_gate_report(conn, run_id)

    # Accounting replay inside THIS snapshot (an offline validator's one
    # point in time). A raise is an unverifiable-books outcome — counted, not
    # crashed — exactly as the paper validator treats it.
    replay_raised: str | None = None
    try:
        replayed = accounting.replay_within(conn, run_id=run_id)
    except Exception as exc:  # noqa: BLE001 — unverifiable books are an outcome
        replayed = None
        replay_raised = f"{type(exc).__name__}: {exc}"
    if replayed is None:
        account_replay_mismatch_count = 1
    else:
        account_replay_mismatch_count = len(replayed.position_mismatches) + (
            0 if replayed.account_matches else 1
        )

    return LiveRunFacts(
        cycle_count=cycle_count,
        api_failed_count=api_failed_count,
        invalid_output_count=invalid_output_count,
        streaks=streaks,
        stranded=stranded,
        prompt_regimes=prompt_regimes,
        live_order_count=live_order_count,
        fill_count=fill_count,
        exchange_fill_dedupe_error_count=exchange_fill_dedupe_error_count,
        orphan_exchange_order_count=orphan_exchange_order_count,
        orphan_exchange_order_distinct_count=orphan_exchange_order_distinct_count,
        duplicate_fill_apply_count=duplicate_fill_apply_count,
        local_exchange_position_mismatch_count=local_exchange_position_mismatch_count,
        account_replay_mismatch_count=account_replay_mismatch_count,
        replay_raised=replay_raised,
        unprotected_position_seconds=unprotected_position_seconds,
        unprotected_window_count=unprotected_window_count,
        unresolved_unprotected_window=unresolved_unprotected_window,
        kill_switch=kill_switch,
        safe_mode=safe_mode,
        unresolved_reconciliation_mismatch_count=unresolved_reconciliation_mismatch_count,
        daily_loss_breached=daily_loss_breached,
        daily_loss_active=daily_loss_active,
        emergency_close_event_count=emergency_close_event_count,
        smoke=smoke,
    )
