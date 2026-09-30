"""Live-run acceptance validator (phase3-spec §20.3 / §21.4, PR 6).

The live sibling of :mod:`..paper.validation`: read-only, it recomputes the
§20.3 (testnet_live) or §21.4 (mainnet_tiny) acceptance metrics entirely from
the live SQLite store and returns a verdict. Which profile applies is read from
the run's own genesis (``runs.config_json``'s ``live.mode``), never guessed —
a mainnet_tiny run is held to §21.4, a testnet_live run to §20.3.

Three modules (refactor plan v2, T3-a): this one holds the thresholds, the
verdict and the public entry points; :mod:`.validation_metrics` reads the
store; :mod:`.validation_report` is the report and its printed form.

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
  unaffected — it fails at one row either way.
- ``orphan_exchange_order_distinct_count`` — those same rows counted by DISTINCT
  CLOID: the number of orders to go looking for at the exchange (issue #84).
  Not by fact key — this case type writes THREE key shapes for one order (a
  bare cloid, ``|local_terminal``, ``|local_terminal_read_failed``), and they
  are deliberately distinct so a re-settle does not dedupe against a plain
  orphan sighting, so a fact-key count would still over-state the orders by up
  to 3×. Reported BESIDE the row count rather than replacing it — the row count
  is what the §20.3 threshold list and the exit-5 gate key off — and the gap
  between the two is the venue flapping, not more orders. Non-gating.
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
from datetime import datetime, timezone
from decimal import Decimal

from ..common.constants import CYCLE_INTERVAL
from ..common.instants import delta_ms, gap_label, whole_hours_label
from ..persistence import repository as repo
from ..persistence.db import Database
from ..runtime.no_decision import NO_DECISION_STREAK_THRESHOLD, no_decision_shortfall
from .config import ExecutionMode
from .safe_mode import SAFE_MODE_MANUAL
from .smoke import SMOKE_TEST_KEYS, SmokeGateReport, rerun_keys_for
from .validation_metrics import LiveRunFacts, _KillSwitchTally, read_live_run_facts
from .validation_report import LiveValidationReport

__all__ = [
    "MIN_KILL_SWITCH_REFRESH_RATE",
    "MIN_LIVE_CYCLES",
    "MIN_LIVE_ORDERS",
    "execution_mode",
    "validate_live_run",
]

# §20.3's testnet_live acceptance floors. ``MIN_LIVE_CYCLES`` happens to equal
# ``paper.validation.MIN_CYCLES_FOR_PHASE3`` — a coincidence of two specs, not
# one rule spelled twice: that one is the phase2→phase3 entry gate on a PAPER
# run, this one is the phase3 acceptance gate on a live run, and either spec
# may move its number without the other's argument changing. Deliberately not
# tied together (issue #102).
MIN_LIVE_CYCLES = 30
MIN_LIVE_ORDERS = 30
# §20.3: kill_switch_refresh_success_rate >= 99%.
MIN_KILL_SWITCH_REFRESH_RATE = Decimal("0.99")
# The bar as the operator reads it, rendered once for every branch that quotes
# it. A rounded threshold is the failure this file already names one screen
# below, from the other side: there two decimals turn a genuine 0.98998 into
# "99.00% (need >= 99%)", a go/no-go line reading as a self-contradiction, and
# that branch is rescued by printing the outage time beside it. Here the hazard
# is the BAR: at ``:.0f`` a 0.995 bar prints as "100%", so the gate would refuse
# at 99.5% while telling the operator it needed a hundred. ``normalize`` drops
# the trailing zeros the Decimal multiply leaves — it is context-sensitive, but
# rounds nothing a rate constant could carry at prec 28 — and ``:f`` spells out
# the exponent form it can return (100% normalizes to 1E+2).
_REFRESH_BAR = f"{(MIN_KILL_SWITCH_REFRESH_RATE * 100).normalize():f}%"
# Below this many refresh events the RATE carries no information and is not
# allowed to fail the run — it is reported as a shortfall (keep running) instead.
# Set at the point where one blip can no longer cross the bar: with a 99% bar,
# 99/100 passes but 9/10 does not, so any total under 100 makes a single network
# hiccup a verdict. At the default 30s cadence this is ~50 minutes of running,
# far inside the ≥30-cycle (~5 day) gate, so it never becomes the binding
# constraint on a real acceptance run.
MIN_KILL_SWITCH_REFRESH_SAMPLES = 100

# The four §20.3 ``*_test_passed`` acceptance booleans → their smoke test keys.
_RESTART_KEY = "restart_reconciliation"
_EMERGENCY_KEY = "emergency_close"
_EXISTING_POSITION_KEY = "startup_with_existing_position"
_STALE_ORDER_KEY = "startup_with_stale_open_order"
# These literals are validation's own copy of smoke-registry identities, and
# ``_smoke_test_passed()`` reads "absent from every non-passed bucket" as True
# — so a key renamed in SMOKE_TESTS without this file would silently report
# its acceptance boolean as passed forever. Fail at import, not in a
# mainnet-facing report.
if not {_RESTART_KEY, _EMERGENCY_KEY, _EXISTING_POSITION_KEY, _STALE_ORDER_KEY} <= set(
    SMOKE_TEST_KEYS
):
    raise AssertionError("validation's §20.3 smoke-test keys drifted from smoke.SMOKE_TESTS")
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
    # this read-only reporter (same discipline as cli._drift._config_drift_report).
    live = parsed.get("live") if isinstance(parsed, dict) else None
    if isinstance(live, dict) and isinstance(live.get("mode"), str):
        return live["mode"]
    return "unknown"


# How long one attempt may sit ``in_progress`` before the run is treated as
# wedged rather than mid-cycle (issue #205). Derived from the no-decision
# escalation, not chosen beside it: a wedge IS a no-decision run — the driver
# adopted nothing, so no terminal row is ever written and
# ``trailing_failure_streaks`` (which skips ``in_progress`` rows, correctly)
# counts nothing new, leaving the streak FROZEN at its pre-wedge value however
# long the wedge lasts — and the two must not be able to drift into disagreeing
# about how long "cannot decide" is allowed to last. Three cycles at the 4h
# cadence, so a cycle whose LLM call is legitimately running cannot reach it.
# The comparison is ``>=``, so exactly this long already counts as wedged.
_ADOPTION_WEDGE_AFTER = NO_DECISION_STREAK_THRESHOLD * CYCLE_INTERVAL
# The bound as the shortfall states it ("12h"); refused at import if the
# cadence stops being whole hours, rather than rendered truncated.
_ADOPTION_WEDGE_LABEL = whole_hours_label(
    _ADOPTION_WEDGE_AFTER, what="live.validation._ADOPTION_WEDGE_AFTER"
)


def _seconds_to_ms(seconds: Decimal) -> int:
    """A tally's Decimal seconds as the whole milliseconds ``gap_label`` takes."""
    return int(seconds * 1000)


def validate_live_run(
    db: Database, *, run_id: str, now: datetime | None = None
) -> LiveValidationReport:
    """Compute the §20.3 / §21.4 acceptance report for a live ``run_id`` (read-only).

    ``now`` (default: wall clock) bounds an unprotected window still open at
    the end of the log, and dates the no-decision streak against the shared
    recency window (issue #50). That second use makes ``now`` a GATING input:
    the same store validated hours apart can move from exit 4 to exit 0 as a
    stopped run ages out of the window. The verdict is reproducible given the
    same ``now``, not from the store alone.
    """
    if now is None:
        now = datetime.now(timezone.utc)
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
        facts = read_live_run_facts(
            conn, run_id=run_id, config_json=run_row["config_json"], now=now
        )

    # testnet_live is §20.3; everything else (mainnet_tiny, or an unreadable
    # mode) is held to the stricter §21.4 gate.
    is_testnet = run_execution_mode == _TESTNET_LIVE_MODE

    failures: list[str] = []
    shortfalls: list[str] = []
    warnings: list[str] = []
    _apply_integrity_gate(failures, facts=facts)
    _apply_cycle_gate(shortfalls, facts=facts, now=now)
    if is_testnet:
        _apply_testnet_gate(failures, shortfalls, facts=facts)
    else:
        _apply_mainnet_gate(
            failures, shortfalls, facts=facts, run_execution_mode=run_execution_mode
        )
    _note_warnings(warnings, facts=facts, is_testnet=is_testnet)

    return LiveValidationReport(
        run_id=run_id,
        execution_mode=run_execution_mode,
        cycle_count=facts.cycle_count,
        api_failed_count=facts.api_failed_count,
        invalid_output_count=facts.invalid_output_count,
        streaks=facts.streaks,
        live_order_count=facts.live_order_count,
        fill_count=facts.fill_count,
        exchange_fill_dedupe_error_count=facts.exchange_fill_dedupe_error_count,
        orphan_exchange_order_count=facts.orphan_exchange_order_count,
        orphan_exchange_order_distinct_count=facts.orphan_exchange_order_distinct_count,
        duplicate_fill_apply_count=facts.duplicate_fill_apply_count,
        local_exchange_position_mismatch_count=facts.local_exchange_position_mismatch_count,
        account_replay_mismatch_count=facts.account_replay_mismatch_count,
        unprotected_position_seconds=facts.unprotected_position_seconds,
        unprotected_window_count=facts.unprotected_window_count,
        unresolved_unprotected_window=facts.unresolved_unprotected_window,
        kill_switch_refresh_success_rate=facts.kill_switch.refresh_rate,
        kill_switch_outage_seconds=facts.kill_switch.outage_seconds,
        kill_switch_outage_episodes=facts.kill_switch.outage_episodes,
        kill_switch_covered_seconds=facts.kill_switch.covered_seconds,
        kill_switch_ended_without_clean_shutdown=facts.kill_switch.ended_without_clean_shutdown,
        kill_switch_ended_in_outage=facts.kill_switch.ended_in_outage,
        kill_switch_refresh_total=facts.kill_switch.refresh_total,
        kill_switch_suite_refresh_attempts=(
            facts.kill_switch.suite_refreshed + facts.kill_switch.suite_failed
        ),
        kill_switch_fired_count=facts.kill_switch.fired_count,
        kill_switch_disarm_failed_count=facts.kill_switch.disarm_failed_count,
        safe_mode_active_type=facts.safe_mode.mode_type,
        safe_mode_active_reason=facts.safe_mode.reason,
        restart_reconciliation_passed=_smoke_test_passed(_RESTART_KEY, facts.smoke),
        emergency_close_test_passed=_smoke_test_passed(_EMERGENCY_KEY, facts.smoke),
        startup_with_existing_position_test_passed=_smoke_test_passed(
            _EXISTING_POSITION_KEY, facts.smoke
        ),
        startup_with_stale_open_order_test_passed=_smoke_test_passed(
            _STALE_ORDER_KEY, facts.smoke
        ),
        unresolved_reconciliation_mismatch_count=facts.unresolved_reconciliation_mismatch_count,
        daily_loss_breached=facts.daily_loss_breached,
        daily_loss_active=facts.daily_loss_active,
        emergency_close_event_count=facts.emergency_close_event_count,
        failures=tuple(failures),
        shortfalls=tuple(shortfalls),
        warnings=tuple(warnings),
        prompt_regimes=facts.prompt_regimes,
    )


def _smoke_test_passed(key: str, smoke: SmokeGateReport) -> bool:
    """One §20.3 ``*_test_passed`` boolean: absent from every non-passed bucket."""
    return key not in smoke.missing and key not in smoke.failed and key not in smoke.errored


def _apply_integrity_gate(failures: list[str], *, facts: LiveRunFacts) -> None:
    """The integrity failures (exit 5), common to both profiles.

    Store-integrity breaches and the safety invariants that are not themselves
    store corruption; the module docstring's exit-code contract lists them.
    Each appends its own line, in this order; the profile gates append theirs
    after.
    """
    if facts.exchange_fill_dedupe_error_count:
        failures.append(
            f"exchange_fill_dedupe_error_count = {facts.exchange_fill_dedupe_error_count} (want 0)"
        )
    if facts.orphan_exchange_order_count:
        # The gate is the ROW count and stays so (one row is already a
        # failure). The order count rides along because this string is where an
        # operator reads the number before going to the exchange to look for
        # that many orders (issue #84).
        failures.append(
            f"orphan_exchange_order_count = {facts.orphan_exchange_order_count} (want 0) across "
            f"{facts.orphan_exchange_order_distinct_count} distinct order(s)"
        )
    if facts.duplicate_fill_apply_count:
        failures.append(
            f"duplicate_fill_apply_count = {facts.duplicate_fill_apply_count} (want 0 — "
            "the exchange_fill_key UNIQUE index was violated; the store is corrupt)"
        )
    if facts.local_exchange_position_mismatch_count:
        failures.append(
            "local_exchange_position_mismatch_count = "
            f"{facts.local_exchange_position_mismatch_count} (want 0)"
        )
    if facts.account_replay_mismatch_count:
        if facts.replay_raised is not None:
            failures.append(
                f"accounting replay raised: {facts.replay_raised} — books unverifiable"
            )
        else:
            failures.append(
                f"account_replay_mismatch_count = {facts.account_replay_mismatch_count} (want 0)"
            )
    # The window COUNT gates too, not just the seconds. A window that measured 0 —
    # out-of-order stamps on a corrupt store, or an onset and its close inside one
    # clock tick — is still a position that had no stop loss, and keying on seconds
    # alone made that case read byte-identical to a run that was never unprotected.
    if (
        facts.unprotected_position_seconds > 0
        or facts.unresolved_unprotected_window
        or facts.unprotected_window_count
    ):
        suffix = " (still open at end of log)" if facts.unresolved_unprotected_window else ""
        # Say WHY a zero-second window still fails, or the line reads as a
        # self-contradiction ("= 0 ... (want 0)") to the operator who has to act.
        measured = (
            f"unprotected_position_seconds = {facts.unprotected_position_seconds}"
            if facts.unprotected_position_seconds > 0
            else "unprotected_position_seconds measured 0 (onset and close within one "
            "clock tick, or out-of-order stamps) but the window is real"
        )
        failures.append(f"{measured} across {facts.unprotected_window_count} window(s){suffix} (want 0)")
    # The dead man's switch actually firing is an integrity failure in BOTH
    # profiles, for the same reason the refresh rate gates both: "the mechanism
    # was proven on testnet" is not "the mechanism held on this run". While it
    # was fired the exchange carried none of our orders — the SL included — and
    # the engine went on placing against a book it believed was still there.
    if facts.kill_switch.fired_count:
        failures.append(
            f"kill_switch_fired_count = {facts.kill_switch.fired_count} (want 0 — the "
            "scheduled-cancel deadline lapsed and the exchange cancelled every order "
            "on the wallet, SL/TP included; the positions held over that window had "
            "no stop on the book). This is CUMULATIVE and cannot be cleared: the run "
            "traded unprotected and no later state makes that untrue. Investigate the "
            "cause (an API outage spanning the deadline, or a host clock that jumped "
            "forward past it), then accumulate the acceptance cycles under a NEW run-id"
        )
    # More than one non-terminal attempt is the decision state machine broken:
    # a new cycle is only scheduled once the previous one reached a terminal
    # status, so two live rows cannot both be legitimate. It is also what
    # WEDGES the daemon — repo.find_in_progress_attempt fails loud on exactly
    # this shape, so §3.1 startup adoption raises on every re-read and the run
    # never decides again (issue #205). An integrity failure rather than a
    # shortfall: no later state makes the store consistent, and unlike a locked
    # store it will not clear itself. ``validation_metrics`` reads the rows
    # through ``repo.iter_in_progress_attempts`` rather than that helper,
    # precisely because the helper raises.
    if facts.stranded.count > 1:
        failures.append(
            f"in_progress_decision_attempts = {facts.stranded.count} (want <= 1): the run has "
            "more than one non-terminal decision attempt, which the scheduler cannot "
            "produce — the decision state machine is broken. A live daemon on this "
            "store cannot adopt past it either (§3.1 startup adoption fails loud on "
            "this shape and the run latches into MANUAL safe mode without ever "
            "deciding again). Inspect the rows in decision_attempts and terminalize "
            "the ones that are not the live cycle before restarting"
        )
    # An in-progress row whose stamp will not parse. Its AGE is the only thing
    # separating a cycle in flight from a wedged run, so an unreadable stamp
    # makes the shortfall below unanswerable — and nothing else in this report
    # reads an IN_PROGRESS row's timestamp (trailing_failure_streaks parses the
    # same column, but only on terminal rows), so withholding both verdicts
    # would let this store — strictly more broken — pass a gate the merely
    # stale one fails.
    if facts.stranded.count and facts.stranded.oldest_at is None:
        failures.append(
            f"stranded_decision_cycle = {facts.stranded.oldest_id} carries a timestamp that "
            "cannot be read as an instant, so how long the run has been unable to "
            "finish that cycle cannot be computed. The column is NOT NULL and only "
            "the repository writes it, so this is a corrupt row rather than a young "
            "one: read it in decision_attempts and terminalize it"
        )
    # A run sitting in MANUAL safe mode is, by §13.1, locked out of adding risk
    # until a human confirms — it cannot be "ready to trade live" whatever its
    # counts say, and §10.4's consecutive-loss latch reaches this state leaving
    # no other durable trace.
    if facts.safe_mode.mode_type == SAFE_MODE_MANUAL:
        because = f" ({facts.safe_mode.reason})" if facts.safe_mode.reason else ""
        failures.append(
            f"the run is in MANUAL safe mode{because} and cannot place new orders "
            "until a human releases it; cycle and order counts accumulated before "
            "the latch do not make it live-ready. This is a LATCH, not a permanent "
            "verdict: investigate the reason, then `safe-mode --release --run-id "
            "<id>` and re-run validate — the run does NOT have to be abandoned"
        )


def _apply_cycle_gate(shortfalls: list[str], *, facts: LiveRunFacts, now: datetime) -> None:
    """The decision-cycle shortfalls (exit 4): not yet at the gate.

    The cycle floor, the trailing no-decision streak and the wedged attempt: three
    readings of "this run is not deciding", each cleared by the run going on, so
    none of them is a verdict.
    """
    if facts.cycle_count < MIN_LIVE_CYCLES:
        shortfalls.append(f"cycle_count = {facts.cycle_count} (need >= {MIN_LIVE_CYCLES})")
    # The last N cycles reached no decision (issue #50): the accumulated cycle
    # count says nothing about a run that cannot decide RIGHT NOW. A shortfall,
    # not a failure — the store is sound and the streak clears by itself at the
    # next decided cycle (an exchange maintenance window must not become a
    # permanent verdict). Same wording and same recency window as the paper
    # report, which is why both call the shared helper rather than re-deriving.
    no_decision_line = no_decision_shortfall(facts.streaks, now=now)
    if no_decision_line is not None:
        shortfalls.append(no_decision_line)
    # One attempt stuck ``in_progress`` far past the cadence: the run is not
    # mid-cycle, it is wedged, and the no-decision streak above CANNOT say so —
    # its query skips ``in_progress`` rows (rightly: an unfinished cycle has
    # not said anything yet), so a daemon whose §3.1 adoption keeps failing
    # writes no terminal row at all and its streak stays frozen at whatever it
    # was before the wedge. That is issue #205's blind spot: a run stuck here
    # for days looked exactly like a run that had just started, while the real
    # position rode its resting SL/TP alone.
    #
    # A shortfall (exit 4), the same bucket and the same constant source as the
    # streak beside it: the store is sound and the commonest cause — an
    # operator's export or validate holding the SQLite lock through boot —
    # clears by itself, so this must not become a permanent verdict. The
    # causes that do NOT clear latch MANUAL safe mode from the daemon, which is
    # an exit-5 failure above; this line stays the one that catches the wedge
    # even when no daemon is running to latch anything.
    #
    # Skipped when the stamp will not parse, because there is no age to compare
    # — that row is reported as an integrity failure above instead, which is
    # the stronger verdict of the two.
    #
    # No recency window, unlike the streak beside it, and that is deliberate. A
    # stopped run reaches this line too — Ctrl-C during a cycle leaves the row
    # in_progress on purpose, so ``salvage_shutdown`` can hand the paid-for
    # answer to the next process — and it stays exit 4 until a daemon adopts
    # it. That is the honest reading: the cycle is unfinished business either
    # way, the remedy is one restart, and a window keyed on age would suppress
    # precisely the wedge this line exists to catch, since a wedge is old by
    # definition.
    if facts.stranded.count == 1 and facts.stranded.oldest_at is not None:
        stuck_for = now - facts.stranded.oldest_at
        if stuck_for >= _ADOPTION_WEDGE_AFTER:
            # The measured span through ``gap_label`` (23h59m used to floor to
            # "~23h") and the bound as its whole-hours label.
            shortfalls.append(
                f"stranded_decision_cycle = {facts.stranded.oldest_id} (in_progress and "
                f"unchanged for {gap_label(delta_ms(now, facts.stranded.oldest_at))}, past the "
                f"{_ADOPTION_WEDGE_LABEL} this gate allows): "
                "the daemon has written no terminal row for it, so no_decision_streak "
                "cannot see it and the accumulated cycle counts describe a run that "
                "may not have decided anything since. Either no daemon is driving "
                "this run, or §3.1 startup adoption keeps failing — check the run "
                "log for `startup adoption` and `safe mode` lines, and see "
                "RUNBOOK-live. The shortfall clears by itself once the cycle reaches "
                "a terminal status"
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
                f"not count toward the §20.3 sample floor (need a rate >= {_REFRESH_BAR} "
                "over daemon evidence)"
            )
        else:
            shortfalls.append(f"no kill-switch refresh events yet (need a rate >= {_REFRESH_BAR})")
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
        # "17.9 min unrefreshed across 12 outage(s)" is the sentence that tells
        # them what actually happened to this run. Both spans go through
        # ``gap_label`` so a run whose whole outage is sub-second cannot report
        # "0s unrefreshed" beside a non-zero episode count (issue #290); the
        # summary's ``kill_switch_outage_seconds:`` line keeps whole seconds
        # because that key names its unit.
        failures.append(
            f"kill_switch_refresh_success_rate = {refresh_rate * 100:.2f}% "
            f"({gap_label(_seconds_to_ms(kill_switch.outage_seconds))} unrefreshed across "
            f"{kill_switch.outage_episodes} outage(s), of "
            f"{gap_label(_seconds_to_ms(kill_switch.covered_seconds))} covered) "
            f"(need >= {_REFRESH_BAR})"
        )


def _apply_testnet_gate(
    failures: list[str], shortfalls: list[str], *, facts: LiveRunFacts
) -> None:
    """The §20.3 testnet_live conditions beyond the shared integrity set."""
    # The §20.2 smoke suite (and the four §20.3 *_test_passed booleans it feeds)
    # is a TESTNET_LIVE acceptance condition only: §21.4 omits it, and a
    # mainnet_tiny run's smoke was proven on the separate testnet run (§21.3), so
    # its own live_smoke_tests table is empty by design. Gating mainnet on it
    # would make §21.4 unreachable (decided 2026-07-27).
    #
    # All three smoke buckets are SHORTFALLS (exit 4), not integrity
    # failures (exit 5): a red smoke item is curable by one
    # `live-smoke --only <key>` re-run (latest-per-key supersedes), so it
    # belongs in "not yet at the gate" — exit 5 stays reserved for the
    # permanent conditions whose RUNBOOK remedy is "investigate before
    # trusting results / consider a fresh run-id" (decision 2026-07-29).
    # The errored/failed triage split is preserved in the wording.
    if facts.smoke.errored:
        shortfalls.append(
            f"smoke test(s) ERRORED (harness/code bug — not an exchange refusal): "
            f"{', '.join(facts.smoke.errored)} (fix the harness, then re-run "
            f"`live-smoke --only {' '.join(rerun_keys_for(facts.smoke.errored))}`)"
        )
    if facts.smoke.failed:
        shortfalls.append(
            f"smoke test(s) FAILED (exchange refused): {', '.join(facts.smoke.failed)} "
            f"(fix config/market state, then re-run `live-smoke --only "
            f"{' '.join(rerun_keys_for(facts.smoke.failed))}`)"
        )
    if facts.smoke.missing:
        shortfalls.append(
            f"smoke test(s) not yet run for real: {', '.join(facts.smoke.missing)} "
            "(run `live-smoke` before entering cycles)"
        )
    if facts.live_order_count < MIN_LIVE_ORDERS:
        shortfalls.append(
            f"live_order_count = {facts.live_order_count} (need >= {MIN_LIVE_ORDERS})"
        )
    _apply_refresh_gate(failures, shortfalls, kill_switch=facts.kill_switch)


def _apply_mainnet_gate(
    failures: list[str],
    shortfalls: list[str],
    *,
    facts: LiveRunFacts,
    # Not ``execution_mode``: that is this module's own public function, and
    # shadowing it inside a helper is exactly what the matching rename in
    # validate_live_run already avoided.
    run_execution_mode: str,
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
    if facts.unresolved_reconciliation_mismatch_count:
        failures.append(
            "unresolved_reconciliation_mismatch_count = "
            f"{facts.unresolved_reconciliation_mismatch_count} (want 0)"
        )
    # CURRENTLY unresolved gates; a breach the run already recovered from does not
    # (it is carried as a warning instead). §10.3's cap auto-releases at the next
    # UTC midnight, so "ever" would make one ordinary risk event terminal for a
    # real-money run — while the line right above it, unresolved_mismatch_count,
    # is already current-state and human-clearable. Decided 2026-07-30.
    if facts.daily_loss_active:
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
    _apply_refresh_gate(failures, shortfalls, kill_switch=facts.kill_switch)


def _note_warnings(warnings: list[str], *, facts: LiveRunFacts, is_testnet: bool) -> None:
    """The non-gating completeness signals (human review); they never move the verdict."""
    if facts.invalid_output_count:
        warnings.append(
            f"{facts.invalid_output_count} cycle(s) produced unparseable model output and do "
            "NOT count toward the ≥30 gate — the scheduler advanced but the run "
            "produced no usable decision; check ai_outputs.risk_reason: truncated_output "
            "means the completion cap bound (raise engine.max_completion_tokens), anything "
            "else is the model/prompt contract"
        )
    if facts.emergency_close_event_count:
        warnings.append(
            f"{facts.emergency_close_event_count} emergency close(s) occurred during the run — "
            "§21.4 'no emergency close caused by bot bug' is not machine-decidable; "
            "review the preceding stop_loss_repair evidence to rule out a bot bug"
        )
    if facts.kill_switch.disarm_failed_count:
        warnings.append(
            f"{facts.kill_switch.disarm_failed_count} kill-switch disarm failure(s) — a "
            "daemon shutdown OR a live-smoke exit could not clear the wallet-wide "
            "scheduleCancel, and the trigger cancels every resting order at its "
            "deadline. Which orders are at risk depends on which one it was: a "
            "§18.2 keep_protective shutdown leaves SL/TP resting, a smoke exit "
            "leaves probe orders. Confirm the wallet is disarmed either way "
            "(the count is cumulative for the run-id and never clears)"
        )
    if is_testnet and facts.daily_loss_breached:
        warnings.append(
            "a daily-loss safe-mode episode is on record (informational on testnet; "
            "a mainnet_tiny gate condition per §21.4)"
        )
    elif facts.daily_loss_breached and not facts.daily_loss_active:
        # Mainnet, breached earlier, already released. Not gating (§10.3 recovers at
        # the next UTC midnight) but never silent: the operator has to know the 30
        # cycles were not homogeneous before reading the acceptance verdict.
        warnings.append(
            "a daily-loss safe-mode episode occurred earlier in this run and has since "
            "released (§10.3 recovers at the next UTC midnight) — not a §21.4 gate "
            "condition once resolved, but review why the cap was hit before going live"
        )
    if is_testnet and facts.unresolved_reconciliation_mismatch_count:
        warnings.append(
            f"{facts.unresolved_reconciliation_mismatch_count} unresolved reconciliation case(s) open "
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

    # Same self-check as the paper report: the buckets are counted through
    # ``decision_attempts.input_id`` and must sum to ``cycle_count``.
    regime_total = sum(r.cycles for r in facts.prompt_regimes)
    if regime_total != facts.cycle_count:
        warnings.append(
            f"prompt_regime buckets cover {regime_total} of {facts.cycle_count} cycles — "
            "decided attempt(s) without an ai_inputs row; the split is partial"
        )
