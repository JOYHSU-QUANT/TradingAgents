"""Live-run acceptance validator (phase3-spec §20.3 / §21.4, PR 6).

The live sibling of :mod:`..paper.validation`: read-only, it recomputes the
§20.3 (testnet_live) or §21.4 (mainnet_tiny) acceptance metrics entirely from
the live SQLite store and returns a verdict. Which profile applies is read from
the run's own genesis (``runs.config_json``'s ``live.mode``), never guessed —
a mainnet_tiny run is held to §21.4, a testnet_live run to §20.3.

Where the metrics come from (all from persisted PR 2–5 event logs):

- ``cycle_count`` — ``decision_attempts`` that reached ``completed``. STRICTER
  than the paper validator, which also counts ``invalid_output``: a live
  acceptance gate is asserting the bot can trade, and §21.4 has no order count
  to backstop it (``invalid_output_count`` is reported as a non-gating warning
  instead). ``api_failed`` never counts on either side.
- ``live_order_count`` — distinct acknowledged live ``place`` attempts
  (``live_order_attempts`` status in acknowledged/duplicate: exchange-confirmed).
- ``exchange_fill_dedupe_error_count`` — ``exchange_reconciliation_events``
  ``fill_money_drift`` (a redelivered fill whose identity fields disagreed).
- ``orphan_exchange_order_count`` / ``local_exchange_position_mismatch_count`` —
  the ``orphan_exchange_order`` / ``exchange_position_mismatch`` §12.3 case ROWS
  the run ever recorded, disposed of or not. Rows, not distinct orders: a
  ``|local_terminal_read_failed`` key records one row per unreadable→readable
  flap of the venue (see ``repo.PROVISIONAL_DISPOSITIONS``), so one order whose
  orderStatus flapped all morning can be most of this number. The gate is
  unaffected — it fails at one row either way — but read the count as episodes
  before going to look for that many orders.
- ``duplicate_fill_apply_count`` — live ``fills`` sharing an ``exchange_fill_key``
  (structurally impossible under the UNIQUE index — a store-integrity assertion).
- ``account_replay_mismatch_count`` — the §14/§15 accounting replay (reused
  verbatim from the paper layer; a raise is an unverifiable-books outcome).
- ``unprotected_position_seconds`` / ``unprotected_window_count`` — reconstructed
  from ``protection_order_events``: an unprotected window opens on
  ``stop_loss_repair_exhausted`` / ``stop_loss_repair_blocked`` (no SL rests that
  COVERS the position) and closes on the next
  ``stop_loss_placed`` / ``stop_loss_modified`` / ``protection_cleared`` /
  ``degraded_protection_cleared`` for that symbol — events that mean the episode
  ENDED. A §17.2 emergency close is a submission, not an end, and is excluded.
  Zero onsets → zero windows and zero seconds (the healthy case), and the gate
  reads the COUNT too so a window measured as 0 cannot impersonate that case.
- ``kill_switch_refresh_success_rate`` — covered TIME minus outage time, over
  covered time. An AVAILABILITY measure, and measured in seconds rather than in
  event counts: an outage opens at a ``kill_switch_refresh_failed`` and closes
  at the next ``kill_switch_armed``, ``kill_switch_refreshed`` OR
  ``kill_switch_disarmed`` — the three rows that prove a schedule stands on the
  exchange. Counting rows made the verdict depend on
  how often the manager happened to retry — a tuning parameter, not a property
  of the run — so rate-limiting the retry loop moved a 24h run with twelve
  90-second outages from 98.3% (correctly failing) to 99.6% (passing) with its
  exposure unchanged. A stretch of SILENCE longer than the deadline STANDING at
  that moment — re-read from each ``armed``/``refreshed`` row's own
  ``deadline=Ns``, with the run's ``schedule_cancel_seconds`` only as the
  starting value — is an outage too, whatever the process was doing:
  nothing renewed the schedule across it, so the exchange cancelled every order
  on the wallet partway through. That case used to be read as "the process was
  down, judge nothing", which threw away the worst outcome available and made the
  measure non-monotone — a 301s outage scoring 0s and passing while a 299s one
  scored 299s and failed. Exempt only when the stretch opens on a clean shutdown,
  which released the cover deliberately. Below ``MIN_KILL_SWITCH_REFRESH_SAMPLES`` events it is
  still not allowed to decide anything: a run needs to have exercised the switch
  before its availability means much.
- ``kill_switch_fired_count`` — ``kill_switch_cancel_triggered``: deadlines the
  exchange demonstrably acted on, cancelling every order on the wallet (SL/TP
  included). A separate question from the rate, and not answerable by it — the
  outage that lets a deadline lapse contributes a few failed refreshes that a
  multi-day run dilutes to inside the 99% bar.
- ``safe_mode_active_type`` / ``safe_mode_active_reason`` — the run's current
  episode from ``scheduler_state``. A MANUAL one is §10.4's "a human must
  confirm" latch, which leaves no other durable trace while §13.1 lets the
  cycle counts keep climbing.
- the four ``*_test_passed`` booleans — the §20.2 smoke suite's latest verdicts
  (tests 15/16/17/18), read through :mod:`.smoke`.

Exit-code contract (mirrors ``paper.validation`` / the ``validate`` CLI): a hard
acceptance failure lands in ``failures`` → exit 5 ("investigate before going
live") — this covers both a store-integrity breach (dedupe errors, orphans,
duplicate applies, a position or replay mismatch) AND a safety-invariant breach
that is not itself store corruption (an unprotected window — §20.3: unprotected
seconds must be 0; a ``stop_loss_repair_blocked`` pause — a §4.1 gate refusal OR
a venue throttle, the event ``detail`` says which — opens one unless a COVERING
stop-loss was still resting, since §17.4's modify-before-cancel can
leave the previous SL on the book — a refresh rate below
99% on EITHER profile, a dead man's switch that FIRED on either profile, a run
still latched in MANUAL safe mode on either profile, or — mainnet_tiny — an
unresolved reconciliation case or a
breached daily-loss cap). A run that is merely short of the gate (< 30 cycles /
orders, smoke tests or kill-switch refreshes not yet run, or a smoke test that
ran but FAILED/ERRORED — curable by a ``live-smoke --only`` re-run, so a
shortfall, not an integrity verdict; decision 2026-07-29) lands in
``shortfalls`` → exit 4 ("keep running / run the smoke suite"). All conditions
met → ``live_ready`` → exit 0. Non-gating
completeness signals — emergency closes during cycles (§21.4's "no emergency
close caused by bot bug" is not machine-decidable), daily-loss episodes and open
manual reconciliation cases on testnet, and the mainnet reminder that §21.4's
manual shutdown/restart item is operator-confirmed only — surface as
``warnings``, never gating.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, localcontext
from operator import itemgetter
from typing import NamedTuple

from ..common.config_coercion import int_from_yaml
from ..common.decimal_context import DECIMAL_CONTEXT
from ..paper import accounting
from ..paper.scheduler import parse_instant
from ..paper.validation import (
    STALE_FEED_STREAK_THRESHOLD,
    stale_feed_refusal_streak,
    stale_feed_shortfall,
)
from ..persistence import repository as repo
from ..persistence.db import Database
from .config import ExecutionMode
from .kill_switch import is_suite_authored
from .safe_mode import REASON_DAILY_LOSS, SAFE_MODE_MANUAL
from .smoke import SMOKE_TEST_KEYS, rerun_keys_for, smoke_gate_report

__all__ = [
    "MIN_KILL_SWITCH_REFRESH_RATE",
    "MIN_LIVE_CYCLES",
    "MIN_LIVE_ORDERS",
    "execution_mode",
    "LiveValidationReport",
    "validate_live_run",
]

MIN_LIVE_CYCLES = 30
MIN_LIVE_ORDERS = 30
# §20.3: kill_switch_refresh_success_rate >= 99%.
MIN_KILL_SWITCH_REFRESH_RATE = Decimal("0.99")
# Below this many refresh events the RATE carries no information and is not
# allowed to fail the run — it is reported as a shortfall (keep running) instead.
# Set at the point where one blip can no longer cross the bar: with a 99% bar,
# 99/100 passes but 9/10 does not, so any total under 100 makes a single network
# hiccup a verdict. At the default 30s cadence this is ~50 minutes of running,
# far inside the ≥30-cycle (~5 day) gate, so it never becomes the binding
# constraint on a real acceptance run.
MIN_KILL_SWITCH_REFRESH_SAMPLES = 100
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
# MUST equal live.config.KillSwitchConfig.schedule_cancel_seconds's default: a run
# that omits the key is measured against whatever THAT default armed it with, so a
# drift here silently measures every such run against the wrong deadline.
DEFAULT_SCHEDULE_CANCEL_SECONDS = Decimal(120)

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
# invalid_output cycles are still surfaced, as a non-gating warning below.
_COMPLETED_CYCLE_STATUSES = ("completed",)
# The registry stays partitioned into exactly what this file classifies: a NEW
# terminal attempt status must be counted or explicitly excluded HERE, at
# import, not silently dropped from a mainnet-facing gate.
if set(repo.TERMINAL_ATTEMPT_STATUSES) - set(_COMPLETED_CYCLE_STATUSES) != {
    "api_failed",
    "invalid_output",
}:
    raise AssertionError(
        "live cycle-count vocabulary drifted from repository.TERMINAL_ATTEMPT_STATUSES"
    )

# The four §20.3 ``*_test_passed`` acceptance booleans → their smoke test keys.
_RESTART_KEY = "restart_reconciliation"
_EMERGENCY_KEY = "emergency_close"
_EXISTING_POSITION_KEY = "startup_with_existing_position"
_STALE_ORDER_KEY = "startup_with_stale_open_order"
# These literals are validation's own copy of smoke-registry identities, and
# ``_passed()`` reads "absent from every non-passed bucket" as True — so a key
# renamed in SMOKE_TESTS without this file would silently report its acceptance
# boolean as passed forever. Fail at import, not in a mainnet-facing report.
if not {_RESTART_KEY, _EMERGENCY_KEY, _EXISTING_POSITION_KEY, _STALE_ORDER_KEY} <= set(
    SMOKE_TEST_KEYS
):
    raise AssertionError("validation's §20.3 smoke-test keys drifted from smoke.SMOKE_TESTS")

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
# the smoke-key assert above (same failure class, same guard).
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
# which _apply_refresh_gate reads as a shortfall rather than a failure — a
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
# What the clean-shutdown line prints for a run with no daemon rows at all. A
# named constant because RUNBOOK §20.3 quotes it verbatim for operators reading
# the summary by hand, and nothing tied the two: the literal could drift in
# either place with the suite green — the same gap round 16 closed for the
# marker token (2026-08-01 round-18 mutation probe).
_NO_DAEMON_ROWS_RENDER = "n/a (no daemon rows)"
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
# Same discipline for the safe-mode TYPE the manual gate keys on: a renamed
# member would make the gate match zero rows and pass every latched run.
if SAFE_MODE_MANUAL not in repo.SAFE_MODE_TYPES:
    raise AssertionError("safe_mode.SAFE_MODE_MANUAL drifted from repository.SAFE_MODE_TYPES")

# execution_mode() returns "unknown" for an unreadable genesis record, else
# whatever live.mode the genesis config named — one of ExecutionMode's members.
# Derived directly from the enum, not re-typed as a literal, so the profile
# split below can never drift the way the file's other hand-typed vocabulary
# guards defend against (2026-07-30 type-design pass).
_TESTNET_LIVE_MODE = ExecutionMode.TESTNET_LIVE.value
_MAINNET_TINY_MODE = ExecutionMode.MAINNET_TINY.value


@dataclass(frozen=True)
class LiveValidationReport:
    """The §20.3 / §21.4 acceptance metrics plus the verdict for one live run."""

    run_id: str
    execution_mode: str  # testnet_live / mainnet_tiny (from runs.config_json)
    cycle_count: int
    api_failed_count: int
    # Non-gating: cycles the scheduler ran whose model output could not be
    # parsed. Excluded from cycle_count on purpose (see _COMPLETED_CYCLE_STATUSES)
    # and reported so a run that is "advancing" but producing nothing usable is
    # visible rather than merely absent from the count.
    invalid_output_count: int
    # Trailing consecutive api_failed cycles refused as stale market data
    # (issue #50; see paper.validation.stale_feed_refusal_streak). A shortfall
    # at or past STALE_FEED_STREAK_THRESHOLD: the run is holding a position
    # on SL/TP alone with no decisions reaching it, which is "not at the gate"
    # however many cycles came before.
    stale_feed_refusal_streak: int
    live_order_count: int
    fill_count: int
    exchange_fill_dedupe_error_count: int
    orphan_exchange_order_count: int
    duplicate_fill_apply_count: int
    local_exchange_position_mismatch_count: int
    account_replay_mismatch_count: int
    unprotected_position_seconds: Decimal
    unprotected_window_count: int
    unresolved_unprotected_window: bool
    # None when the switch never refreshed (no evidence yet — a shortfall, not a
    # 0% failure): a fabricated 0 would misread as "every refresh failed".
    kill_switch_refresh_success_rate: Decimal | None
    kill_switch_refresh_total: int
    # The three numbers BEHIND the rate. They existed only inside the failure
    # string, so a run that passed at 99.2% gave the operator no way to see it had
    # been exposed at all, and the pre-2026-08-01 message it replaced did at least
    # print the raw counts.
    kill_switch_outage_seconds: Decimal
    kill_switch_outage_episodes: int
    kill_switch_covered_seconds: Decimal
    # The DAEMON's log stops without a shutdown row: killed, not stopped.
    # Non-gating, but it is the only trace that the cover standing at that
    # instant lapsed where no later event could measure it.
    # None when the run has no DAEMON kill-switch rows at all (a smoke-only
    # run-id): the flag reports on the daemon, so with no daemon it has
    # nothing to say and must not answer either way.
    kill_switch_ended_without_clean_shutdown: bool | None
    # A DIFFERENT question from the flag above, not a stronger form of it: the
    # RUN's tail is an outage nothing closed, so the availability figure is a
    # lower bound. Run-scoped on purpose where the flag above is daemon-scoped,
    # so the two can disagree in both directions (see
    # _KillSwitchTally.ended_in_outage).
    kill_switch_ended_in_outage: bool
    # Deadlines the exchange demonstrably acted on: it cancelled every order on
    # the wallet, SL/TP included. Gating in BOTH profiles — the refresh RATE is
    # an availability measure and cannot express this, because the outage that
    # lets a deadline lapse contributes a handful of failures that a multi-day
    # run dilutes to inside the 99% bar.
    kill_switch_fired_count: int
    # Non-gating: the run is over by then, but the wallet was left armed.
    kill_switch_disarm_failed_count: int
    # The CURRENT safe-mode episode, if any. A manual one is gating: §10.4's
    # consecutive-loss guard latches "a human must confirm" while §13.1 keeps
    # cycles running, so the counts climb on a run that cannot place an order.
    safe_mode_active_type: str | None
    safe_mode_active_reason: str | None
    restart_reconciliation_passed: bool
    emergency_close_test_passed: bool
    startup_with_existing_position_test_passed: bool
    startup_with_stale_open_order_test_passed: bool
    unresolved_reconciliation_mismatch_count: int
    # ``breached`` is "ever, anywhere in this run" (informational — §10.3 recovers at
    # the next UTC midnight); ``active`` is "still unresolved now", and is the one
    # §21.4 gates on. Reported separately so the operator can tell a released
    # episode from a live one instead of reading one bool for both.
    daily_loss_breached: bool
    daily_loss_active: bool
    emergency_close_event_count: int
    # Store-integrity failures → exit 5.
    failures: tuple[str, ...]
    # Not-yet-at-the-gate reasons → exit 4.
    shortfalls: tuple[str, ...]
    # Non-gating completeness signals (human review), never affect the verdict.
    warnings: tuple[str, ...] = ()
    # Refresh attempts written during live-smoke: excluded from
    # ``kill_switch_refresh_total`` above, and carried here ONLY so the summary
    # can say so. The three low-evidence shortfalls disclose the exclusion, but
    # they fire only below the floor — the summary's ``kill_switch_refresh_total``
    # line prints on EVERY run, including the ones that pass, and it was the
    # daemon-only number with nothing to explain the gap against the table an
    # operator can SELECT. Defaulted because it is a disclosure, never a verdict
    # (2026-08-01 round-21 review).
    kill_switch_suite_refresh_attempts: int = 0

    def __post_init__(self) -> None:
        # A None rate means "cannot say"; a present rate must be in [0, 1]. The
        # direction that still has to hold absolutely is that no refresh evidence
        # CANNOT produce a number — otherwise a fabricated rate slips the gate.
        # The converse is deliberately not an iff: a run whose rows exist but span
        # no wall time also cannot say, and used to answer that with a PERFECT
        # score (2026-08-01 round-13 review).
        if (
            self.kill_switch_refresh_success_rate is not None
            and self.kill_switch_refresh_total == 0
        ):
            raise ValueError(
                "kill_switch_refresh_success_rate must be None when there were no "
                f"refreshes (total={self.kill_switch_refresh_total})"
            )
        if self.kill_switch_refresh_success_rate is not None and not (
            0 <= self.kill_switch_refresh_success_rate <= 1
        ):
            raise ValueError(
                f"kill_switch_refresh_success_rate must be in [0, 1], got "
                f"{self.kill_switch_refresh_success_rate}"
            )
        # An unresolved (still-open) window IS one of the counted windows, so the
        # two must agree. Its measured seconds is normally > 0 (now is strictly
        # after a stored past onset) but can clamp to 0 on a corrupt store (a
        # future-timestamped onset), so no seconds guarantee is asserted here —
        # the gate keys off ``unprotected_window_count`` as well as the seconds and
        # the open flag, precisely so a clamped-to-zero window still fails.
        if self.unresolved_unprotected_window and self.unprotected_window_count < 1:
            raise ValueError(
                "unresolved_unprotected_window is set but unprotected_window_count is "
                f"{self.unprotected_window_count} (an open window is a counted window)"
            )
        # A reason without a type is a half-read episode: the gate keys on the
        # TYPE, so that shape would report the reason in the summary while
        # passing the run.
        if self.safe_mode_active_reason is not None and self.safe_mode_active_type is None:
            raise ValueError(
                f"safe_mode_active_reason {self.safe_mode_active_reason!r} without a "
                "safe_mode_active_type"
            )
        # "No daemon rows" and "daemon refresh evidence" cannot both hold: every
        # refresh counted in the total IS a daemon row. The tally never emits
        # that pair, but the report is also built by hand (tests, and any future
        # caller), and the pair would print "clean shutdown: n/a (no daemon
        # rows)" beside a daemon refresh rate — the same shape as the other
        # cross-field identities asserted here (2026-08-01 round-18 review).
        if (
            self.kill_switch_ended_without_clean_shutdown is None
            and self.kill_switch_refresh_total > 0
        ):
            raise ValueError(
                "kill_switch_ended_without_clean_shutdown is None (no daemon rows) but "
                f"kill_switch_refresh_total is {self.kill_switch_refresh_total}"
            )
        # Every count/measure here is non-negative by construction in the tally
        # (SQL counts; gaps between timestamp-ordered rows), but the report is
        # also built by hand (tests, any future caller) and a negative renders
        # raw into the summary — refresh_total and the three outage figures
        # print on EVERY run, the same render class as the "(+-3 during
        # live-smoke...)" shape this guard family exists for.
        for name in (
            "cycle_count",
            "api_failed_count",
            "invalid_output_count",
            "stale_feed_refusal_streak",
            "live_order_count",
            "fill_count",
            "exchange_fill_dedupe_error_count",
            "orphan_exchange_order_count",
            "duplicate_fill_apply_count",
            "local_exchange_position_mismatch_count",
            "account_replay_mismatch_count",
            "unprotected_position_seconds",
            "unprotected_window_count",
            "kill_switch_refresh_total",
            "kill_switch_outage_seconds",
            "kill_switch_outage_episodes",
            "kill_switch_covered_seconds",
            "kill_switch_fired_count",
            "kill_switch_disarm_failed_count",
            "unresolved_reconciliation_mismatch_count",
            "emergency_close_event_count",
            "kill_switch_suite_refresh_attempts",
        ):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be >= 0, got {getattr(self, name)}")

    @property
    def live_ready(self) -> bool:
        """Acceptance passed: no integrity failure and nothing left to run."""
        return not self.failures and not self.shortfalls

    def summary_lines(self) -> list[str]:
        """The report as printable ``key: value`` lines (the CLI's output shape)."""

        def _rate(value: Decimal | None) -> str:
            if value is not None:
                return f"{value * 100:.2f}%"
            # None has TWO causes now, and naming the wrong one printed
            # "n/a (no refresh yet)" directly above "kill_switch_refresh_total:
            # 150". Distinguish them off the count the operator can see.
            return (
                # "no DAEMON refresh": suite-authored rows may well exist, and
                # they are counted nowhere near this number.
                "n/a (no daemon refresh yet)"
                if self.kill_switch_refresh_total == 0
                else "n/a (rows span no elapsed time)"
            )

        def _smoke(value: bool) -> str:
            # The four smoke-derived booleans are a TESTNET_LIVE acceptance
            # signal; a mainnet_tiny run's live_smoke_tests is empty by design
            # (§21.3 proves smoke on the sibling testnet run), so a False here
            # means "not tracked for this profile", not "failed" — render it n/a
            # so the report can't be misread as a mainnet smoke failure.
            if self.execution_mode != _TESTNET_LIVE_MODE:
                return "n/a (proven on testnet run, §21.3)"
            return _yn(value)

        lines = [
            f"run_id: {self.run_id}",
            f"execution_mode: {self.execution_mode}",
            f"cycle_count: {self.cycle_count}",
            f"api_failed_count: {self.api_failed_count}",
            f"invalid_output_count: {self.invalid_output_count}",
            f"stale_feed_refusal_streak: {self.stale_feed_refusal_streak}",
            f"live_order_count: {self.live_order_count}",
            f"fill_count: {self.fill_count}",
            f"exchange_fill_dedupe_error_count: {self.exchange_fill_dedupe_error_count}",
            f"orphan_exchange_order_count: {self.orphan_exchange_order_count}",
            f"duplicate_fill_apply_count: {self.duplicate_fill_apply_count}",
            f"local_exchange_position_mismatch_count: {self.local_exchange_position_mismatch_count}",
            f"account_replay_mismatch_count: {self.account_replay_mismatch_count}",
            f"unprotected_position_seconds: {self.unprotected_position_seconds}",
            f"unprotected_window_count: {self.unprotected_window_count}",
            f"kill_switch_refresh_success_rate: {_rate(self.kill_switch_refresh_success_rate)}",
            f"kill_switch_refresh_total: {self.kill_switch_refresh_total}"
            + (
                f" (+{self.kill_switch_suite_refresh_attempts} during live-smoke, "
                "excluded from the sample floor)"
                if self.kill_switch_suite_refresh_attempts
                else ""
            ),
            # The numbers behind the rate, on the go/no-go summary rather than only
            # inside the failure string: a run that PASSES at 99.2% was still
            # exposed, and the operator could not see it.
            f"kill_switch_outage_seconds: {self.kill_switch_outage_seconds:.0f}"
            f" across {self.kill_switch_outage_episodes} outage(s)"
            f" of {self.kill_switch_covered_seconds:.0f}s covered",
            "kill_switch_clean_shutdown: "
            + (
                _NO_DAEMON_ROWS_RENDER
                if self.kill_switch_ended_without_clean_shutdown is None
                else _yn(not self.kill_switch_ended_without_clean_shutdown)
            ),
            # Printed, not only buried in the shortfall sentence: the RUNBOOK's
            # §20.3 table names this flag, so the summary has to show it.
            f"kill_switch_ended_in_outage: {_yn(self.kill_switch_ended_in_outage)}",
            f"kill_switch_fired_count: {self.kill_switch_fired_count}",
            f"kill_switch_disarm_failed_count: {self.kill_switch_disarm_failed_count}",
            f"safe_mode_active: {self.safe_mode_active_type or 'no'}"
            + (f" ({self.safe_mode_active_reason})" if self.safe_mode_active_reason else ""),
            f"restart_reconciliation_passed: {_smoke(self.restart_reconciliation_passed)}",
            f"emergency_close_test_passed: {_smoke(self.emergency_close_test_passed)}",
            "startup_with_existing_position_test_passed: "
            f"{_smoke(self.startup_with_existing_position_test_passed)}",
            "startup_with_stale_open_order_test_passed: "
            f"{_smoke(self.startup_with_stale_open_order_test_passed)}",
            f"unresolved_reconciliation_mismatch_count: {self.unresolved_reconciliation_mismatch_count}",
            f"daily_loss_breached: {_yn(self.daily_loss_breached)}",
            f"daily_loss_active: {_yn(self.daily_loss_active)}",
            f"emergency_close_event_count: {self.emergency_close_event_count}",
            f"live_ready: {'yes' if self.live_ready else 'no'}",
        ]
        lines.extend(f"failure: {reason}" for reason in self.failures)
        lines.extend(f"shortfall: {reason}" for reason in self.shortfalls)
        lines.extend(f"warning: {reason}" for reason in self.warnings)
        return lines


def _yn(value: bool) -> str:
    return "yes" if value else "no"


def _count(conn, sql: str, params: tuple) -> int:
    return conn.execute(sql, params).fetchone()[0]


def execution_mode(config_json: str | None) -> str:
    """Read ``live.mode`` from the run's genesis config; ``unknown`` if absent.

    Public on purpose: the CLI's ``--gate-status`` guard depends on it, so the
    cross-module contract is declared here (``__all__``), not smuggled through
    an underscore name a refactor would feel free to break.

    The verdict must never silently apply the wrong acceptance profile: a live
    run whose genesis config does not name its execution mode is reported as
    ``unknown`` and validated under the stricter (mainnet_tiny) gate — see
    :func:`validate_live_run`.
    """
    if not config_json:
        return "unknown"
    try:
        parsed = json.loads(config_json)
    except (ValueError, TypeError):
        return "unknown"
    # Valid JSON that is not an object (``"5"``, ``"[]"``) parses fine but has no
    # ``.get`` — a corrupt genesis record must degrade to "unknown", never crash
    # this read-only reporter (same discipline as cli._config_drift_report).
    live = parsed.get("live") if isinstance(parsed, dict) else None
    if isinstance(live, dict) and isinstance(live.get("mode"), str):
        return live["mode"]
    return "unknown"


def _schedule_cancel_seconds(config_json: str | None) -> Decimal:
    """How long one successful schedule covered THIS run, from its own genesis.

    The availability measure has to know the run's real deadline: it is the line
    between "silence we were still covered through" and "silence in which the
    exchange cancelled every order on the wallet". A module constant cannot know
    it — ``schedule_cancel_seconds`` is configurable (floored at 5, unbounded
    above), so any fixed threshold is simultaneously too strict for one run and
    too lax for another. Reading it per-run is what stops a legal config from
    moving the acceptance verdict.

    Same degrade discipline as :func:`execution_mode` — a corrupt or partial
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
    # stores the ``live:`` block VERBATIM, before coercion (cli._run_config_subset),
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
# (kill_switch.py: ``detail=f"deadline={...}s refresh={...}s"``). Taking the number
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


class _SafeModeState(NamedTuple):
    """The run's CURRENT safe-mode episode, if it is in one."""

    mode_type: str | None  # "manual" / "recoverable" / None
    reason: str | None


def _current_safe_mode(conn, run_id: str) -> _SafeModeState:
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


def _daily_loss_still_active(conn, run_id: str) -> bool:
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


def _unprotected_windows(conn, run_id: str, now: datetime) -> tuple[Decimal, int, bool]:
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


def _kill_switch_tally(conn, run_id: str, config_json: str | None) -> _KillSwitchTally:
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
            # enough for its availability figure to mean anything, and six
            # back-to-back smoke suites answer it at 114 refreshes and 100% with
            # the daemon never started. That figure would describe the smoke phase,
            # not the thing §20.3 certifies for real money (2026-08-01 round-15).
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


def _unresolved_reconciliation_mismatches(conn, run_id: str) -> int:
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


def validate_live_run(
    db: Database, *, run_id: str, now: datetime | None = None
) -> LiveValidationReport:
    """Compute the §20.3 / §21.4 acceptance report for a live ``run_id`` (read-only).

    ``now`` (default: wall clock) is used only to bound an unprotected window
    still open at the end of the log — every gating count is otherwise derived
    purely from the store, so the verdict is reproducible.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    replay_raised: str | None = None
    with db.read_transaction() as conn:
        run_row = repo.get_run(conn, run_id)
        if run_row is None:
            raise ValueError(f"run {run_id!r} does not exist; nothing to validate")
        if run_row["mode"] != "live":
            raise ValueError(
                f"run {run_id!r} is a {run_row['mode']} run — use the paper "
                "acceptance validator (validate reads live metrics only for live runs)"
            )
        run_execution_mode = execution_mode(run_row["config_json"])

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
        invalid_output_count = _count(
            conn,
            "SELECT COUNT(*) FROM decision_attempts WHERE run_id = ? AND status = 'invalid_output'",
            (run_id,),
        )
        stale_streak = stale_feed_refusal_streak(conn, run_id)
        fill_count = _count(conn, "SELECT COUNT(*) FROM fills WHERE run_id = ?", (run_id,))
        # Distinct acknowledged live orders: the exchange confirmed it holds
        # (or already held — a 'duplicate' ack) each cloid.
        known = repo.EXCHANGE_KNOWN_ATTEMPT_STATUSES
        known_placeholders = ", ".join("?" for _ in known)
        live_order_count = _count(
            conn,
            "SELECT COUNT(DISTINCT cloid_hex) FROM live_order_attempts WHERE run_id = ?"
            f" AND action = 'place' AND status IN ({known_placeholders})",
            (run_id, *known),
        )
        # Structural dedupe assertion: the UNIQUE index makes this 0 unless the
        # store is corrupt. Filter to live fills (paper fills carry NULL keys).
        duplicate_fill_apply_count = _count(
            conn,
            "SELECT COUNT(*) FROM (SELECT exchange_fill_key FROM fills"
            " WHERE run_id = ? AND exchange_fill_key IS NOT NULL"
            " GROUP BY exchange_fill_key HAVING COUNT(*) > 1)",
            (run_id,),
        )
        dedupe_error_count = len(
            repo.iter_exchange_reconciliation_events(conn, run_id, case_type="fill_money_drift")
        )
        orphan_count = len(
            repo.iter_exchange_reconciliation_events(
                conn, run_id, case_type="orphan_exchange_order"
            )
        )
        position_mismatch_count = len(
            repo.iter_exchange_reconciliation_events(
                conn, run_id, case_type="exchange_position_mismatch"
            )
        )
        unprotected_seconds, unprotected_windows, unprotected_open = _unprotected_windows(
            conn, run_id, now
        )
        kill_switch = _kill_switch_tally(conn, run_id, run_row["config_json"])
        refresh_rate, refresh_total = kill_switch.refresh_rate, kill_switch.refresh_total
        safe_mode = _current_safe_mode(conn, run_id)
        unresolved_mismatch_count = _unresolved_reconciliation_mismatches(conn, run_id)

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
        _gate_passed, smoke_missing, smoke_failed, smoke_errored = smoke_gate_report(conn, run_id)

        # Accounting replay inside THIS snapshot (an offline validator's one
        # point in time). A raise is an unverifiable-books outcome — counted, not
        # crashed — exactly as the paper validator treats it.
        try:
            replayed = accounting.replay_within(conn, run_id=run_id)
        except Exception as exc:  # noqa: BLE001 — unverifiable books are an outcome
            replayed = None
            replay_raised = f"{type(exc).__name__}: {exc}"

    if replayed is None:
        replay_mismatch_count = 1
    else:
        replay_mismatch_count = len(replayed.position_mismatches) + (
            0 if replayed.account_matches else 1
        )

    def _passed(key: str) -> bool:
        return key not in smoke_missing and key not in smoke_failed and key not in smoke_errored

    restart_passed = _passed(_RESTART_KEY)
    emergency_passed = _passed(_EMERGENCY_KEY)
    existing_passed = _passed(_EXISTING_POSITION_KEY)
    stale_passed = _passed(_STALE_ORDER_KEY)

    # testnet_live is §20.3; everything else (mainnet_tiny, or an unreadable
    # mode) is held to the stricter §21.4 gate.
    is_testnet = run_execution_mode == _TESTNET_LIVE_MODE

    failures: list[str] = []
    shortfalls: list[str] = []
    warnings: list[str] = []

    # -- integrity failures (exit 5), common to both profiles --------------
    if dedupe_error_count:
        failures.append(f"exchange_fill_dedupe_error_count = {dedupe_error_count} (want 0)")
    if orphan_count:
        failures.append(f"orphan_exchange_order_count = {orphan_count} (want 0)")
    if duplicate_fill_apply_count:
        failures.append(
            f"duplicate_fill_apply_count = {duplicate_fill_apply_count} (want 0 — "
            "the exchange_fill_key UNIQUE index was violated; the store is corrupt)"
        )
    if position_mismatch_count:
        failures.append(
            f"local_exchange_position_mismatch_count = {position_mismatch_count} (want 0)"
        )
    if replay_mismatch_count:
        if replayed is None:
            failures.append(f"accounting replay raised: {replay_raised} — books unverifiable")
        else:
            failures.append(f"account_replay_mismatch_count = {replay_mismatch_count} (want 0)")
    # The window COUNT gates too, not just the seconds. A window that measured 0 —
    # out-of-order stamps on a corrupt store, or an onset and its close inside one
    # clock tick — is still a position that had no stop loss, and keying on seconds
    # alone made that case read byte-identical to a run that was never unprotected.
    if unprotected_seconds > 0 or unprotected_open or unprotected_windows:
        suffix = " (still open at end of log)" if unprotected_open else ""
        # Say WHY a zero-second window still fails, or the line reads as a
        # self-contradiction ("= 0 ... (want 0)") to the operator who has to act.
        measured = (
            f"unprotected_position_seconds = {unprotected_seconds}"
            if unprotected_seconds > 0
            else "unprotected_position_seconds measured 0 (onset and close within one "
            "clock tick, or out-of-order stamps) but the window is real"
        )
        failures.append(f"{measured} across {unprotected_windows} window(s){suffix} (want 0)")
    # The dead man's switch actually firing is an integrity failure in BOTH
    # profiles, for the same reason the refresh rate gates both: "the mechanism
    # was proven on testnet" is not "the mechanism held on this run". While it
    # was fired the exchange carried none of our orders — the SL included — and
    # the engine went on placing against a book it believed was still there.
    if kill_switch.fired_count:
        failures.append(
            f"kill_switch_fired_count = {kill_switch.fired_count} (want 0 — the "
            "scheduled-cancel deadline lapsed and the exchange cancelled every order "
            "on the wallet, SL/TP included; the positions held over that window had "
            "no stop on the book). This is CUMULATIVE and cannot be cleared: the run "
            "traded unprotected and no later state makes that untrue. Investigate the "
            "cause (an API outage spanning the deadline, or a host clock that jumped "
            "forward past it), then accumulate the acceptance cycles under a NEW run-id"
        )
    # A run sitting in MANUAL safe mode is, by §13.1, locked out of adding risk
    # until a human confirms — it cannot be "ready to trade live" whatever its
    # counts say, and §10.4's consecutive-loss latch reaches this state leaving
    # no other durable trace.
    if safe_mode.mode_type == SAFE_MODE_MANUAL:
        because = f" ({safe_mode.reason})" if safe_mode.reason else ""
        failures.append(
            f"the run is in MANUAL safe mode{because} and cannot place new orders "
            "until a human releases it; cycle and order counts accumulated before "
            "the latch do not make it live-ready. This is a LATCH, not a permanent "
            "verdict: investigate the reason, then `safe-mode --release --run-id "
            "<id>` and re-run validate — the run does NOT have to be abandoned"
        )
    # -- shortfalls (exit 4): not yet at the gate --------------------------
    if cycle_count < MIN_LIVE_CYCLES:
        shortfalls.append(f"cycle_count = {cycle_count} (need >= {MIN_LIVE_CYCLES})")
    # The feed has refused the last N cycles (issue #50): the accumulated
    # cycle count says nothing about a run that cannot decide RIGHT NOW. A
    # shortfall, not a failure — the store is sound and the streak clears by
    # itself at the next decided cycle (an exchange maintenance window must
    # not become a permanent verdict). Same wording as the paper report.
    if stale_streak >= STALE_FEED_STREAK_THRESHOLD:
        shortfalls.append(stale_feed_shortfall(stale_streak))

    # The §20.2 smoke suite (and the four §20.3 *_test_passed booleans it feeds)
    # is a TESTNET_LIVE acceptance condition only: §21.4 omits it, and a
    # mainnet_tiny run's smoke was proven on the separate testnet run (§21.3), so
    # its own live_smoke_tests table is empty by design. Gating mainnet on it
    # would make §21.4 unreachable (decided 2026-07-27).
    if is_testnet:
        # All three smoke buckets are SHORTFALLS (exit 4), not integrity
        # failures (exit 5): a red smoke item is curable by one
        # `live-smoke --only <key>` re-run (latest-per-key supersedes), so it
        # belongs in "not yet at the gate" — exit 5 stays reserved for the
        # permanent conditions whose RUNBOOK remedy is "investigate before
        # trusting results / consider a fresh run-id" (decision 2026-07-29).
        # The errored/failed triage split is preserved in the wording.
        if smoke_errored:
            shortfalls.append(
                f"smoke test(s) ERRORED (harness/code bug — not an exchange refusal): "
                f"{', '.join(smoke_errored)} (fix the harness, then re-run "
                f"`live-smoke --only {' '.join(rerun_keys_for(smoke_errored))}`)"
            )
        if smoke_failed:
            shortfalls.append(
                f"smoke test(s) FAILED (exchange refused): {', '.join(smoke_failed)} "
                f"(fix config/market state, then re-run `live-smoke --only "
                f"{' '.join(rerun_keys_for(smoke_failed))}`)"
            )
        if smoke_missing:
            shortfalls.append(
                f"smoke test(s) not yet run for real: {', '.join(smoke_missing)} "
                "(run `live-smoke` before entering cycles)"
            )
        _apply_testnet_gate(
            failures,
            shortfalls,
            live_order_count=live_order_count,
            kill_switch=kill_switch,
        )
    else:
        _apply_mainnet_gate(
            failures,
            shortfalls,
            unresolved_mismatch_count=unresolved_mismatch_count,
            daily_loss_active=daily_loss_active,
            run_execution_mode=run_execution_mode,
            kill_switch=kill_switch,
        )

    # -- non-gating warnings ----------------------------------------------
    if invalid_output_count:
        warnings.append(
            f"{invalid_output_count} cycle(s) produced unparseable model output and do "
            "NOT count toward the ≥30 gate — the scheduler advanced but the run "
            "produced no usable decision; check the model/prompt contract"
        )
    if emergency_close_event_count:
        warnings.append(
            f"{emergency_close_event_count} emergency close(s) occurred during the run — "
            "§21.4 'no emergency close caused by bot bug' is not machine-decidable; "
            "review the preceding stop_loss_repair evidence to rule out a bot bug"
        )
    if kill_switch.disarm_failed_count:
        warnings.append(
            f"{kill_switch.disarm_failed_count} kill-switch disarm failure(s) — a "
            "daemon shutdown OR a live-smoke exit could not clear the wallet-wide "
            "scheduleCancel, and the trigger cancels every resting order at its "
            "deadline. Which orders are at risk depends on which one it was: a "
            "§18.2 keep_protective shutdown leaves SL/TP resting, a smoke exit "
            "leaves probe orders. Confirm the wallet is disarmed either way "
            "(the count is cumulative for the run-id and never clears)"
        )
    if is_testnet and daily_loss_breached:
        warnings.append(
            "a daily-loss safe-mode episode is on record (informational on testnet; "
            "a mainnet_tiny gate condition per §21.4)"
        )
    elif daily_loss_breached and not daily_loss_active:
        # Mainnet, breached earlier, already released. Not gating (§10.3 recovers at
        # the next UTC midnight) but never silent: the operator has to know the 30
        # cycles were not homogeneous before reading the acceptance verdict.
        warnings.append(
            "a daily-loss safe-mode episode occurred earlier in this run and has since "
            "released (§10.3 recovers at the next UTC midnight) — not a §21.4 gate "
            "condition once resolved, but review why the cap was hit before going live"
        )
    if is_testnet and unresolved_mismatch_count:
        warnings.append(
            f"{unresolved_mismatch_count} unresolved reconciliation case(s) open "
            "(§12.3 manual lane) — informational on testnet; a mainnet_tiny gate "
            "condition per §21.4. Resolve them before preparing the mainnet run."
        )
    if not is_testnet:
        # §21.4's "manual shutdown/restart tested" entry criterion is not
        # machine-decidable (a deliberate operator exercise leaves no
        # distinguishable store signature) — surface it on every mainnet report
        # so an operator reading exit 0 as the §21.4 checklist cannot silently
        # skip a listed item.
        warnings.append(
            "§21.4 'manual shutdown/restart tested' is operator-confirmed only — "
            "not machine-verifiable from the store; confirm it before go-live"
        )

    return LiveValidationReport(
        run_id=run_id,
        execution_mode=run_execution_mode,
        cycle_count=cycle_count,
        api_failed_count=api_failed_count,
        invalid_output_count=invalid_output_count,
        stale_feed_refusal_streak=stale_streak,
        live_order_count=live_order_count,
        fill_count=fill_count,
        exchange_fill_dedupe_error_count=dedupe_error_count,
        orphan_exchange_order_count=orphan_count,
        duplicate_fill_apply_count=duplicate_fill_apply_count,
        local_exchange_position_mismatch_count=position_mismatch_count,
        account_replay_mismatch_count=replay_mismatch_count,
        unprotected_position_seconds=unprotected_seconds,
        unprotected_window_count=unprotected_windows,
        unresolved_unprotected_window=unprotected_open,
        kill_switch_refresh_success_rate=refresh_rate,
        kill_switch_outage_seconds=kill_switch.outage_seconds,
        kill_switch_outage_episodes=kill_switch.outage_episodes,
        kill_switch_covered_seconds=kill_switch.covered_seconds,
        kill_switch_ended_without_clean_shutdown=kill_switch.ended_without_clean_shutdown,
        kill_switch_ended_in_outage=kill_switch.ended_in_outage,
        kill_switch_refresh_total=refresh_total,
        kill_switch_suite_refresh_attempts=(kill_switch.suite_refreshed + kill_switch.suite_failed),
        kill_switch_fired_count=kill_switch.fired_count,
        kill_switch_disarm_failed_count=kill_switch.disarm_failed_count,
        safe_mode_active_type=safe_mode.mode_type,
        safe_mode_active_reason=safe_mode.reason,
        restart_reconciliation_passed=restart_passed,
        emergency_close_test_passed=emergency_passed,
        startup_with_existing_position_test_passed=existing_passed,
        startup_with_stale_open_order_test_passed=stale_passed,
        unresolved_reconciliation_mismatch_count=unresolved_mismatch_count,
        daily_loss_breached=daily_loss_breached,
        daily_loss_active=daily_loss_active,
        emergency_close_event_count=emergency_close_event_count,
        failures=tuple(failures),
        shortfalls=tuple(shortfalls),
        warnings=tuple(warnings),
    )


def _suite_exclusion_note(kill_switch: _KillSwitchTally) -> str:
    """Why the count above is smaller than the row count the operator can SELECT.

    Every one of these branches prints a DAEMON-only number. Round 18 fixed that
    for the zero-evidence branch alone, and the branch one line over kept the
    same defect: a run with 121 live-smoke rows and ten daemon refreshes said
    "kill_switch_refresh_total = 10" beside a table holding 131 refresh rows —
    a claim the operator checks against the table and finds false, which is the
    exact thing round 18 set out to stop (2026-08-01 round-20 review).
    """
    attempts = kill_switch.suite_refreshed + kill_switch.suite_failed
    if not attempts:
        return ""
    return (
        f" — a further {attempts} refresh attempt(s) on record were written during "
        "live-smoke and do not count toward the §20.3 sample floor"
    )


def _apply_refresh_gate(
    failures: list[str],
    shortfalls: list[str],
    *,
    kill_switch: _KillSwitchTally,
) -> None:
    """The kill-switch refresh-rate condition, shared by BOTH profiles.

    §20.3 names the >= 99% rate explicitly; §21.4 does not, but "the mechanism
    was proven on testnet" is not "the mechanism kept working on mainnet" — a
    real-money run whose dead man's switch quietly failed to refresh must not
    read ``live_ready`` (decision 2026-07-27). One encoding so the two gates
    cannot drift.
    """
    refresh_rate, refresh_total = kill_switch.refresh_rate, kill_switch.refresh_total
    # NOTE ON ``ended_without_clean_shutdown``: reported, never gated — not even
    # as a shortfall. The normal way to run ``validate`` is against a run that is
    # STILL GOING, and an in-flight run has no shutdown row by definition, so
    # gating on it would refuse to certify every healthy run for the duration of
    # its own life. The flag distinguishes a killed run from a stopped one for a
    # human reading the summary; it cannot distinguish either from a running one,
    # so it does not get a vote (2026-08-01 round-13 review).
    # ``ended_in_outage`` is REPORTED, never gated — for exactly the reason spelled
    # out above for ``ended_without_clean_shutdown``, which it took a second
    # mistake to see. It briefly appended a shortfall here, on the argument that
    # an unclosed outage is unambiguous where a missing shutdown row is not. It is
    # not: an in-flight run's log ends wherever ``validate`` happened to read it,
    # so a HEALTHY daemon that hit a transient blip 15s ago failed at exit 4 while
    # the SAME run 30s later — with strictly MORE measured exposure — passed. That
    # is the verdict moving with the clock, the one property this measure exists to
    # deny (2026-08-01 round-13 exit check).
    if refresh_total == 0:
        # No refresh evidence yet — a shortfall (keep running), not a 0% failure.
        # Counted AND named as refresh attempts. The counter only ever held
        # refresh-class rows, while the sentence called them "kill-switch row(s)
        # on record" — a claim the operator can check against the table, and one
        # that was false the moment the daemon had written an ``armed`` row of
        # its own: two rows on record, one of them the daemon's, described as
        # "the 1 kill-switch row(s) ... written during live-smoke". Attempts are
        # also the unit the §20.3 floor counts, which is what this sentence
        # exists to explain the absence of (2026-08-01 round-18 review).
        suite_attempts = kill_switch.suite_refreshed + kill_switch.suite_failed
        if suite_attempts:
            shortfalls.append(
                f"no DAEMON kill-switch refresh events yet — the {suite_attempts} "
                "refresh attempt(s) on record were written during live-smoke and do "
                "not count toward the §20.3 sample floor (need a rate >= 99% over "
                "daemon evidence)"
            )
        else:
            shortfalls.append("no kill-switch refresh events yet (need a rate >= 99%)")
    elif refresh_total < MIN_KILL_SWITCH_REFRESH_SAMPLES:
        # Some evidence, but not enough to judge availability. Zero was always
        # handled above; 1..N-1 was not, and at a 30s cadence a run five minutes
        # old has ten samples, so ONE network blip pinned a healthy run at exit 5
        # — which RUNBOOK §5 answers with "stop and investigate" and §7 with
        # "start a fresh run-id". Still counted in EVENTS rather than seconds:
        # the question here is "has the switch been exercised enough to judge",
        # and a run can sit idle for hours without proving anything.
        shortfalls.append(
            f"kill_switch_refresh_total = {refresh_total} — too few to judge availability "
            f"(need >= {MIN_KILL_SWITCH_REFRESH_SAMPLES}; below that one blip decides it)"
            + _suite_exclusion_note(kill_switch)
        )
    elif refresh_rate is None:
        # Enough rows to judge, but they span no wall time, so availability has no
        # denominator. Ordered AFTER the sample floor because that message is the
        # more useful one whenever both apply. Used to answer with a perfect score
        # (2026-08-01 round-13 review).
        shortfalls.append(
            f"kill_switch events span no elapsed time ({refresh_total} daemon refresh "
            "row(s) at a single instant) — availability has no denominator to "
            "measure against" + _suite_exclusion_note(kill_switch)
        )
    elif refresh_rate < MIN_KILL_SWITCH_REFRESH_RATE:
        # Report the TIME, not only the rounded percentage. At two decimals a
        # genuine 0.98998 renders as "99.00% (need >= 99%)" — a go/no-go line that
        # reads as a self-contradiction to the operator who has to act on it — and
        # "17.9 minutes across 12 outages" is the sentence that tells them what
        # actually happened to this run.
        failures.append(
            f"kill_switch_refresh_success_rate = {refresh_rate * 100:.2f}% "
            f"({kill_switch.outage_seconds:.0f}s unrefreshed across "
            f"{kill_switch.outage_episodes} outage(s), of "
            f"{kill_switch.covered_seconds:.0f}s covered) "
            f"(need >= {MIN_KILL_SWITCH_REFRESH_RATE * 100:.0f}%)"
        )


def _apply_testnet_gate(
    failures: list[str],
    shortfalls: list[str],
    *,
    live_order_count: int,
    kill_switch: _KillSwitchTally,
) -> None:
    """The §20.3 testnet_live conditions beyond the shared integrity set."""
    if live_order_count < MIN_LIVE_ORDERS:
        shortfalls.append(f"live_order_count = {live_order_count} (need >= {MIN_LIVE_ORDERS})")
    _apply_refresh_gate(failures, shortfalls, kill_switch=kill_switch)


def _apply_mainnet_gate(
    failures: list[str],
    shortfalls: list[str],
    *,
    unresolved_mismatch_count: int,
    daily_loss_active: bool,
    # Not ``execution_mode``: that is this module's own public function, and
    # shadowing it inside a helper is exactly what the matching rename in
    # validate_live_run already avoided.
    run_execution_mode: str,
    kill_switch: _KillSwitchTally,
) -> None:
    """The §21.4 mainnet_tiny conditions beyond the shared integrity set.

    Deliberately stricter than §21.4's letter on one point: the kill-switch
    refresh rate gates here too (see :func:`_apply_refresh_gate`). The order
    count does NOT — §21.4 omits it and the user kept that (2026-07-27).
    """
    # "testnet_live" is unreachable from the one caller (this helper runs only in
    # the else of `is_testnet`); it stays as defence-in-depth so a future second
    # caller cannot turn a legitimate mode into the "unreadable genesis" failure.
    if run_execution_mode not in (_MAINNET_TINY_MODE, _TESTNET_LIVE_MODE):
        # An unreadable genesis mode is validated under this stricter gate, and
        # named so the operator fixes the record rather than trusting a verdict
        # computed under a guessed profile.
        failures.append(
            f"execution_mode is {run_execution_mode!r} — genesis config does not name a "
            "live.mode; validated under the stricter §21.4 gate (fix the run record)"
        )
    if unresolved_mismatch_count:
        failures.append(
            f"unresolved_reconciliation_mismatch_count = {unresolved_mismatch_count} (want 0)"
        )
    # CURRENTLY unresolved gates; a breach the run already recovered from does not
    # (it is carried as a warning instead). §10.3's cap auto-releases at the next
    # UTC midnight, so "ever" would make one ordinary risk event terminal for a
    # real-money run — while the line right above it, unresolved_mismatch_count,
    # is already current-state and human-clearable. Decided 2026-07-30.
    if daily_loss_active:
        # NAME THE REMEDY. The latch is released by safe_mode.try_auto_recover,
        # whose only caller is the reconciler's tick — so it advances while the
        # DAEMON runs and never while `validate` runs. The documented flow is
        # "finish 30 cycles, stop the daemon, validate", which lands exactly on a
        # latch nothing can now clear; and `safe-mode --release` refuses a
        # recoverable episode by design, so the operator sees a dead end and
        # reads the neighbouring RUNBOOK warning as "burn the run and redo 30
        # real-money cycles". Restarting the daemon for one clean reconciliation
        # is all it takes, once the UTC day has turned.
        failures.append(
            "the run is still in a daily-loss safe-mode episode "
            "(§21.4: the cap must not be breached) — this is a LATCH, not a "
            "permanent verdict: §10.3 releases it via a clean reconciliation once "
            "the UTC day has turned, so restart `live --run-id <id> --loop` long "
            "enough for one reconcile tick and re-run validate before abandoning "
            "the run (`safe-mode --release` cannot clear a recoverable episode)"
        )
    _apply_refresh_gate(failures, shortfalls, kill_switch=kill_switch)
