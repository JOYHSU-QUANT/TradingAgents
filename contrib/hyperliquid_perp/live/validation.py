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
  the ``orphan_exchange_order`` / ``exchange_position_mismatch`` §12.3 cases.
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
- ``kill_switch_refresh_success_rate`` — ``kill_switch_refreshed`` over
  (refreshed + ``kill_switch_refresh_failed``).
- the four ``*_test_passed`` booleans — the §20.2 smoke suite's latest verdicts
  (tests 15/16/17/18), read through :mod:`.smoke`.

Exit-code contract (mirrors ``paper.validation`` / the ``validate`` CLI): a hard
acceptance failure lands in ``failures`` → exit 5 ("investigate before going
live") — this covers both a store-integrity breach (dedupe errors, orphans,
duplicate applies, a position or replay mismatch) AND a safety-invariant breach
that is not itself store corruption (an unprotected window — §20.3: unprotected
seconds must be 0; a ``stop_loss_repair_blocked`` gate pause opens one unless a
COVERING stop-loss was still resting, since §17.4's modify-before-cancel can
leave the previous SL on the book — a refresh rate below
99% on EITHER profile, or — mainnet_tiny — an unresolved reconciliation case or a
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
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from ..paper import accounting
from ..paper.scheduler import parse_instant
from ..persistence import repository as repo
from ..persistence.db import Database
from .config import ExecutionMode
from .safe_mode import REASON_DAILY_LOSS
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
_UNPROTECTED_CLOSE_EVENTS = frozenset(
    {
        "stop_loss_placed",
        "stop_loss_modified",
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
if not {_KILL_SWITCH_REFRESH_OK, _KILL_SWITCH_REFRESH_FAILED} <= repo.KILL_SWITCH_EVENT_TYPES:
    raise AssertionError(
        "validation's kill-switch refresh event types drifted from "
        "repository.KILL_SWITCH_EVENT_TYPES"
    )

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

    def __post_init__(self) -> None:
        # A None rate exists exactly when there was no refresh evidence; a
        # present rate must be in [0, 1]. Either shape disagreeing with
        # ``kill_switch_refresh_total`` would let a fabricated rate slip the gate.
        if (self.kill_switch_refresh_success_rate is None) != (self.kill_switch_refresh_total == 0):
            raise ValueError(
                "kill_switch_refresh_success_rate is None iff there were no refreshes "
                f"(total={self.kill_switch_refresh_total})"
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
        if self.unprotected_position_seconds < 0:
            raise ValueError(
                f"unprotected_position_seconds must be >= 0, got "
                f"{self.unprotected_position_seconds}"
            )

    @property
    def live_ready(self) -> bool:
        """Acceptance passed: no integrity failure and nothing left to run."""
        return not self.failures and not self.shortfalls

    def summary_lines(self) -> list[str]:
        """The report as printable ``key: value`` lines (the CLI's output shape)."""

        def _rate(value: Decimal | None) -> str:
            return "n/a (no refresh yet)" if value is None else f"{value * 100:.2f}%"

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
            f"kill_switch_refresh_total: {self.kill_switch_refresh_total}",
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
    reduce-only; protection.py reads its single local row, so it is the stricter
    of the two). So the commonest blocked MODIFY, a RESIZE
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
    def _window_seconds(start: datetime, end: datetime) -> Decimal:
        secs = Decimal(str((end - start).total_seconds()))
        return secs if secs > 0 else Decimal(0)

    def _close_open_window(symbol: str, when: datetime) -> None:
        nonlocal total, windows
        total += _window_seconds(open_at.pop(symbol), when)
        windows += 1

    for row in repo.iter_protection_order_events(conn, run_id):
        symbol = row["symbol"]
        event = row["event_type"]
        when = parse_instant(row["timestamp"])
        if event in onset:
            if event == _SL_REPAIR_BLOCKED and row["order_id"]:
                # A gate-refused MODIFY over a still-COVERING SL (protection.py
                # stamps the id only then; see the docstring). Stale trigger,
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


def _kill_switch_refresh_rate(conn, run_id: str) -> tuple[Decimal | None, int]:
    """``(success_rate, total)`` over the kill-switch refresh events."""
    refreshed = 0
    failed = 0
    for row in repo.iter_kill_switch_events(conn, run_id):
        if row["event_type"] == _KILL_SWITCH_REFRESH_OK:
            refreshed += 1
        elif row["event_type"] == _KILL_SWITCH_REFRESH_FAILED:
            failed += 1
    total = refreshed + failed
    if total == 0:
        return None, 0
    return Decimal(refreshed) / Decimal(total), total


def _unresolved_reconciliation_mismatches(conn, run_id: str) -> int:
    """Count §12.3 cases still open — the §21.4 "no unresolved mismatch" metric.

    Mirrors the ``safe-mode --status`` open-case logic: every mismatch case is
    open until a human stamps ``action_taken``. A ``fill_unmapped`` sighting is a
    backlog (resolved by booking the fill, not by stamping), not a mismatch, so
    it is excluded from the count.
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
        refresh_rate, refresh_total = _kill_switch_refresh_rate(conn, run_id)
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
    # -- shortfalls (exit 4): not yet at the gate --------------------------
    if cycle_count < MIN_LIVE_CYCLES:
        shortfalls.append(f"cycle_count = {cycle_count} (need >= {MIN_LIVE_CYCLES})")

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
            refresh_rate=refresh_rate,
            refresh_total=refresh_total,
        )
    else:
        _apply_mainnet_gate(
            failures,
            shortfalls,
            unresolved_mismatch_count=unresolved_mismatch_count,
            daily_loss_active=daily_loss_active,
            run_execution_mode=run_execution_mode,
            refresh_rate=refresh_rate,
            refresh_total=refresh_total,
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
        kill_switch_refresh_total=refresh_total,
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


def _apply_refresh_gate(
    failures: list[str],
    shortfalls: list[str],
    *,
    refresh_rate: Decimal | None,
    refresh_total: int,
) -> None:
    """The kill-switch refresh-rate condition, shared by BOTH profiles.

    §20.3 names the >= 99% rate explicitly; §21.4 does not, but "the mechanism
    was proven on testnet" is not "the mechanism kept working on mainnet" — a
    real-money run whose dead man's switch quietly failed to refresh must not
    read ``live_ready`` (decision 2026-07-27). One encoding so the two gates
    cannot drift.
    """
    if refresh_total == 0:
        # No refresh evidence yet — a shortfall (keep running), not a 0% failure.
        shortfalls.append("no kill-switch refresh events yet (need a rate >= 99%)")
    elif refresh_rate is not None and refresh_rate < MIN_KILL_SWITCH_REFRESH_RATE:
        failures.append(
            f"kill_switch_refresh_success_rate = {refresh_rate * 100:.2f}% "
            f"(need >= {MIN_KILL_SWITCH_REFRESH_RATE * 100:.0f}%)"
        )


def _apply_testnet_gate(
    failures: list[str],
    shortfalls: list[str],
    *,
    live_order_count: int,
    refresh_rate: Decimal | None,
    refresh_total: int,
) -> None:
    """The §20.3 testnet_live conditions beyond the shared integrity set."""
    if live_order_count < MIN_LIVE_ORDERS:
        shortfalls.append(f"live_order_count = {live_order_count} (need >= {MIN_LIVE_ORDERS})")
    _apply_refresh_gate(
        failures, shortfalls, refresh_rate=refresh_rate, refresh_total=refresh_total
    )


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
    refresh_rate: Decimal | None,
    refresh_total: int,
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
        failures.append(
            "the run is still in a daily-loss safe-mode episode "
            "(§21.4: the cap must not be breached)"
        )
    _apply_refresh_gate(
        failures, shortfalls, refresh_rate=refresh_rate, refresh_total=refresh_total
    )
