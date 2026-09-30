"""The live acceptance report: its fields, their cross-field guards, its printed form.

The :class:`LiveValidationReport` that :func:`.validation.validate_live_run`
returns. Split out of that module (refactor plan v2, T3-a) so the SHAPE of the
report and how it renders sit apart from how its metrics are read
(:mod:`.validation_metrics`) and how the verdict is drawn (:mod:`.validation`).
The module docstring of :mod:`.validation` says where each metric comes from.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from ..paper.validation import prompt_regime_lines
from ..persistence import repository as repo
from ..runtime.no_decision import TrailingFailureStreaks
from .config import ExecutionMode

__all__ = ["LiveValidationReport"]

# What the clean-shutdown line prints for a run with no daemon rows at all. A
# named constant because RUNBOOK §20.3 quotes it verbatim for operators reading
# the summary by hand, and nothing tied the two: the literal could drift in
# either place with the suite green — the same gap round 16 closed for the
# marker token (2026-08-01 round-18 mutation probe).
_NO_DAEMON_ROWS_RENDER = "n/a (no daemon rows)"
# The one profile the summary renders differently (its four smoke booleans are
# a testnet_live signal). Derived from the enum, not re-typed as a literal, as
# :mod:`.validation` derives its pair.
_TESTNET_LIVE_MODE = ExecutionMode.TESTNET_LIVE.value


@dataclass(frozen=True)
class LiveValidationReport:
    """The §20.3 / §21.4 acceptance metrics plus the verdict for one live run."""

    run_id: str
    execution_mode: str  # testnet_live / mainnet_tiny (from runs.config_json)
    cycle_count: int
    api_failed_count: int
    # Non-gating: cycles the scheduler ran whose model output could not be
    # parsed. Excluded from cycle_count on purpose (see
    # ``validation_metrics._COMPLETED_CYCLE_STATUSES``) and reported so a run
    # that is "advancing" but producing nothing usable is visible rather than
    # merely absent from the count.
    invalid_output_count: int
    # Trailing cycles that reached no decision, and the stale-feed subset of
    # them (issue #50; see runtime.no_decision.trailing_failure_streaks). Past the
    # threshold — and while still recent — the run is holding a position on
    # SL/TP alone with nothing deciding for it, which is "not at the gate"
    # however many cycles came before; the validator turns that into a
    # ``shortfalls`` line, so this pair is reported, not gating on its own.
    # That line is STORED in ``shortfalls``, not derived from this pair in
    # ``__post_init__``: whether a streak still describes the run's CURRENT
    # state depends on ``now``, an input to the validator and not a field of
    # this report, so a streak past the threshold with no matching line is a
    # legal report (a stopped run's last hours) and not a contradiction the
    # cross-field guards below could assert against — the same reason the
    # paper report gives for its pair (issue #94, decided not to guard).
    streaks: TrailingFailureStreaks
    live_order_count: int
    fill_count: int
    exchange_fill_dedupe_error_count: int
    orphan_exchange_order_count: int
    # The row count above answers the gate; this one answers "how many orders
    # do I go looking for" — distinct CLOID, not distinct fact key (one order
    # has up to three of those). They differ whenever a cloid's orderStatus
    # flapped, or the same order was seen under two fault shapes (issue #84)
    # — see :mod:`.validation`'s module docstring.
    orphan_exchange_order_distinct_count: int
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
    # ``validation_metrics._KillSwitchTally.ended_in_outage``).
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
    # The run's cycles split by the three prompt segmentation keys, first seen
    # first — the paper report's field, for the same reason (issue #129); the
    # ``cycles`` sum to ``cycle_count`` (same statuses). Informational only.
    prompt_regimes: tuple[repo.PromptRegime, ...] = ()
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
        # The two orphan numbers count the SAME rows, one of them collapsed to
        # distinct cloids — so neither "more orders than rows" nor "rows but no
        # order" can be true, and either shape would print a summary telling
        # the operator to go find a number of orders the audit trail cannot
        # hold. Asserted because the report is also built by hand (tests, any
        # future caller); the tally derives both from one row list.
        # Skipped when the row count is itself negative: that is a fault of one
        # field, not of their relationship, and the shared non-negative sweep
        # below already names it — comparing first would answer "-3 rows" with
        # a message about orders exceeding rows.
        if (
            self.orphan_exchange_order_count >= 0
            and self.orphan_exchange_order_distinct_count > self.orphan_exchange_order_count
        ):
            raise ValueError(
                "orphan_exchange_order_distinct_count "
                f"({self.orphan_exchange_order_distinct_count}) exceeds "
                f"orphan_exchange_order_count ({self.orphan_exchange_order_count}) — "
                "they count the same rows"
            )
        if self.orphan_exchange_order_count > 0 and self.orphan_exchange_order_distinct_count == 0:
            raise ValueError(
                f"orphan_exchange_order_count is {self.orphan_exchange_order_count} but "
                "orphan_exchange_order_distinct_count is 0 — every recorded orphan row "
                "belongs to some order"
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
            "live_order_count",
            "fill_count",
            "exchange_fill_dedupe_error_count",
            "orphan_exchange_order_count",
            "orphan_exchange_order_distinct_count",
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
            f"no_decision_streak: {self.streaks.no_decision}",
            f"stale_feed_refusal_streak: {self.streaks.stale_feed}",
            f"live_order_count: {self.live_order_count}",
            f"fill_count: {self.fill_count}",
            f"exchange_fill_dedupe_error_count: {self.exchange_fill_dedupe_error_count}",
            f"orphan_exchange_order_count: {self.orphan_exchange_order_count}",
            # Its own line, and the row-count line above left byte-identical:
            # §21.4 and RUNBOOK-live both quote that line, and an operator
            # diffing runs greps it.
            f"orphan_exchange_order_distinct_count: {self.orphan_exchange_order_distinct_count}",
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
        ]
        lines.extend(prompt_regime_lines(self.prompt_regimes))
        lines.append(f"live_ready: {'yes' if self.live_ready else 'no'}")
        lines.extend(f"failure: {reason}" for reason in self.failures)
        lines.extend(f"shortfall: {reason}" for reason in self.shortfalls)
        lines.extend(f"warning: {reason}" for reason in self.warnings)
        return lines


def _yn(value: bool) -> str:
    return "yes" if value else "no"
