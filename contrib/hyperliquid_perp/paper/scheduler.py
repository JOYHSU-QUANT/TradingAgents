"""Rolling 4h decision scheduler (phase2-spec §3 / §3.1) — the cycle driver.

The scheduler owns *when* a decision runs and the audit trail around it
(``decision_attempts`` / ``ai_inputs`` / ``ai_outputs``); the engine owns *what*
an approved decision does to the market (plans, fills, SL/TP). Like the engine,
it never sleeps: the caller (PR4's CLI loop in production, a test otherwise)
drives it one :meth:`PaperScheduler.poll` at a time against the injected clock,
so every retry wait and 4h boundary is a deterministic function of "now".

Cycle timing (spec §3):

- a run with no previous decision executes immediately; afterwards
  ``next_decision_at = actual decision_at + 4h`` — a rolling interval, never a
  fixed UTC candle boundary, and missed intervals are never back-filled;
- a delayed cycle (process down at 14:15, back at 16:00) runs once at 16:00
  under its *original* ``scheduled_at`` (the deterministic attempt id), and the
  next cycle is 20:00.

Retry (spec §3.1): one logical attempt per scheduled cycle, persisted **before**
each AI call so a restart resumes the same ``decision_attempt_id`` with the
counter intact. Up to three tries (10s, then 30s apart); the third failure
terminalizes the attempt as ``api_failed`` — position held, no target, next
cycle at ``scheduled_at + 4h`` (anchored on the terminal instant instead when
that stamp already lies in the past — a cycle that itself ran late must not
chain catch-up ladders). A response that parses but violates the
contract is ``invalid_output``: no re-ask, fail-closed maintain_current, the
cycle counts as done.

The AI/market seam is the injected :class:`DecisionProvider` (production wires
the TradingAgents engine; tests script outcomes), which signals a retryable
failure by raising :class:`RetryableDecisionError`. Anything else is a bug —
and a bug raised BEFORE the AI answers (the books read, the market fetch,
the ``ai_inputs`` row, the call itself) fails THAT CYCLE closed
(``api_failed`` with no §6.2 class, the traceback logged at ERROR, the
position and its SL/TP held, the next cycle on schedule) rather than the
daemon: the live driver's rule for the same stretch, adopted here in issue
#134 so the two lanes stop meaning different things by "failed". AFTER the
answer the policy follows what already durably exists (issue #163, aligned
with the live lane's persist-retry split): the two scheduler-owned persists
— the ``pending_raw_response`` store and ``_finalize``'s audit commit —
retry in-process on failure (a paid-for decision, or a committed plan the
audit trail must not contradict, exists; the daemon stays up and logs
ERROR each retry) until one lands or ``_MAX_PERSIST_FAILURES`` polls in a
row have failed, at which point the exception propagates after all — a
fault that outlives the bound is not the transient lock the lane exists
for, and unbounded containment would wedge the run invisibly. A bug
re-parsing the stored response on resume fails that cycle closed like any
other non-retryable error (a restart would only crash-loop into the same
parse). What still exits the daemon: an exception escaping the engine's
``start_plan`` — the ONE post-answer step given no containment at all,
because the engine fail-stops (a partially committed plan may exist and it
refuses every later call), so the position would sit unwatched inside a
live-looking process and only the supervisor's restart rebuilds it — plus
the persist escalation above, a failure of the terminal ``api_failed``
record itself, the cycle-boundary scheduling writes outside every guard
(``_execute``'s pre-call counter, ``poll``'s new-cycle insert), and a
non-DB error out of the best-effort cycle-end snapshot.

Restart safety of a half-finished cycle: the successful AI response is
persisted onto the attempt row (``pending_raw_response``) *before* the gate
runs, so ONCE THAT STORE LANDS a crash anywhere up to the audit commit — the
market-data-blocked gate phase, or the window between ``start_plan``'s
plan/order commit and the ``ai_outputs`` commit — resumes by re-parsing the
stored response (deterministic), never by re-asking the AI (spec §3.1). A
crash in the store-retry window itself (answered, nothing durable) fails
closed on restart and re-enters the §3.1 ladder — a never-stored decision is
never resumed, so that lane CAN spend a second AI call within budget. The
per-try ``output_id`` (``#out<n>``) means orders committed by a crashed try's
``start_plan`` reference the ai_outputs row the resumed gate eventually writes
for that same decision; restart reconciliation cancels the crashed plan either
way, so nothing trades on it twice.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, localcontext
from enum import Enum
from typing import Protocol, runtime_checkable

from ..common.constants import CYCLE_INTERVAL, ERROR_TYPES
from ..common.enum_guard import check_enum
from ..common.instants import parse_instant
from ..domains.perp.risk_gate import RiskConfig
from ..domains.perp.schema import PerpMarketContext
from ..domains.perp.target_decision import DecisionConfig, ParsedDecision, parse_target_decision
from ..persistence import audit_rows, repository as repo
from ..persistence.db import Database
from ..persistence.ids import decision_attempt_id as derive_attempt_id
from ..persistence.models import DECIMAL_CONTEXT
from . import accounting
from .clock import Clock
from .engine import AssetSpec, PaperExecutionEngine, PlanStartResult
from .position_facts import BookFacts, read_books

__all__ = [
    "CYCLE_INTERVAL",
    "CycleEvent",
    "DecisionInput",
    "DecisionProvider",
    "MAX_DECISION_ATTEMPTS",
    "PaperScheduler",
    "PollResult",
    "RETRY_DELAYS_SECONDS",
    "RetryableDecisionError",
    "parse_instant",
]

logger = logging.getLogger(__name__)

# The rolling decision interval (spec §3) is ``common.constants.CYCLE_INTERVAL``
# and the store's timestamp decoder is ``common.instants.parse_instant``; both
# stay in ``__all__`` for the callers that always imported them from here
# (issue #122 moved the definitions down so the freshness guard and the
# no-decision policy could read them without importing this module — and, with
# it, the whole paper engine). The §3.1 retry ladder: after the first failure
# wait 10s, after the second 30s, after the third → api_failed.
MAX_DECISION_ATTEMPTS = 3
RETRY_DELAYS_SECONDS = (10, 30)

# The persist-retry lanes' escalation bound (issue #163): a post-answer persist
# (the §3.1 response store, or the audit commit) is contained for this many
# consecutive failed polls — the LAST of them propagates instead, so the run
# gets N-1 in-process retries — as a daemon exit, the supervisor's restart
# signal. Counted per unbroken streak: one persist that lands resets it. The
# motivating transient (an operator's export/validate holding the SQLite write
# lock) heals within a poll or two; a fault that survives ten polls (a monitor
# interval apart, each already behind the connection's 5s busy_timeout —
# so the escape window is ten intervals, not ten seconds) is a fault that
# in-process retries cannot fix — unbounded containment would wedge the run
# invisibly (no terminal row, no §3.1 streak, a fresh lease heartbeat).
_MAX_PERSIST_FAILURES = 10


class RetryableDecisionError(Exception):
    """A market-data / AI API failure worth retrying (spec §3.1).

    ``error_type`` is a member of the §6.2 vocabulary,
    ``common.constants.ERROR_TYPES`` (the member-by-member rationale lives
    there); ``check_enum`` enforces it HERE, at construction — the same
    posture as ``ContextRefusal`` — so a producer's typo fails on the raise
    instead of when the daemon tries to record its failure at the repository
    write boundary (which checks the same set; issue #122). Anything the
    provider does not classify is a bug, not a retry: it fails the cycle
    closed with no class at all (see ``PaperScheduler._fail_untyped``).
    """

    def __init__(self, error_type: str, message: str) -> None:
        check_enum(error_type, ERROR_TYPES, name="RetryableDecisionError.error_type")
        super().__init__(f"{error_type}: {message}")
        self.error_type = error_type
        self.message = message


@dataclass(frozen=True)
class DecisionInput:
    """Everything one AI call sees, built by the provider before the call.

    ``context`` is the market side of the ``ai_inputs`` row; the account side
    rides along as ``books`` (the one read the position section was priced
    from), and the driver reads it itself only for an input that carries
    none. The payload
    path/hash point at the full JSON the provider persisted (phase2-data §5:
    SQLite keeps the summary + path + hash, never the whole prompt).
    """

    context: PerpMarketContext
    candle_start: datetime | None = None
    candle_end: datetime | None = None
    input_payload_path: str | None = None
    input_payload_hash: str | None = None
    prompt_version: str | None = None
    # The prompt's section structure (prompt_context.context_shape), the
    # second segmentation key beside prompt_version (issue #97).
    context_shape: str | None = None
    # The third: a content digest of the format block
    # (target_decision.format_fingerprint) — the half of the prompt the other
    # two keys do not cover, whose numbers move on a config edit (issue #129).
    format_fingerprint: str | None = None
    model: str | None = None
    # The books the position section was priced from — ledger, position, the
    # newest fill's stamp — so the ``ai_inputs`` row is written from the SAME
    # read rather than a second one (issue #134). ``None``: the provider
    # carries no books (a test double, a replay harness) and the driver reads
    # them itself.
    books: BookFacts | None = None

    def __post_init__(self) -> None:
        # Path and hash are two halves of one artifact (phase2-data §5: the
        # store keeps path + hash together); a provider supplying one without
        # the other would persist an audit row that can't be verified. The
        # candle window is the same kind of pair: one boundary without the
        # other is a malformed §5 audit row, not a narrower one.
        if (self.input_payload_path is None) != (self.input_payload_hash is None):
            raise ValueError(
                "DecisionInput.input_payload_path and input_payload_hash must be "
                "provided together (or both omitted)"
            )
        if (self.candle_start is None) != (self.candle_end is None):
            raise ValueError(
                "DecisionInput.candle_start and candle_end must be provided "
                "together (or both omitted)"
            )
        # The three segmentation keys are one set too: a row stamped with a
        # version but no shape (or no fingerprint) would be indistinguishable
        # from pre-v10 / pre-v11 history, which the review reads as "unknown".
        keys = (self.prompt_version, self.context_shape, self.format_fingerprint)
        if any(k is None for k in keys) and not all(k is None for k in keys):
            raise ValueError(
                "DecisionInput.prompt_version, context_shape and format_fingerprint "
                "must be provided together (or all omitted)"
            )
        # An inverted window (start after end) is a malformed §5 row the same way
        # a half-present pair is; the spec pair is one candle's [start, end].
        if (
            self.candle_start is not None
            and self.candle_end is not None
            and self.candle_start > self.candle_end
        ):
            raise ValueError(
                "DecisionInput.candle_start must not be after candle_end "
                f"({self.candle_start} > {self.candle_end})"
            )


@runtime_checkable
class DecisionProvider(Protocol):
    """The AI/market seam: build the input, then ask for one decision.

    Two phases so the ``ai_inputs`` row can be recorded *between* them (§5.1:
    每次呼叫 AI 前記錄一次 — the record must exist before the paid call).
    Both raise :class:`RetryableDecisionError` for §3.1-retryable failures.
    ``build_input`` must return a context that already passed the pre-LLM
    guards (``context_guards.context_refusal``); drivers do not re-check before
    spending the paid call.
    """

    def build_input(self, *, coin: str, as_of: datetime) -> DecisionInput: ...

    def request_decision(self, decision_input: DecisionInput) -> ParsedDecision: ...


class CycleEvent(str, Enum):
    """What one poll did to the current cycle — the caller's export/log trigger."""

    COMPLETED = "completed"
    INVALID_OUTPUT = "invalid_output"
    API_FAILED = "api_failed"
    RETRY_SCHEDULED = "retry_scheduled"
    PENDING_MARKET_DATA = "pending_market_data"

    @property
    def is_cycle_terminal(self) -> bool:
        """Whether this event finished the cycle (the CSV-export trigger)."""
        return self in (CycleEvent.COMPLETED, CycleEvent.INVALID_OUTPUT, CycleEvent.API_FAILED)


@dataclass(frozen=True)
class PollResult:
    """The outcome of one :meth:`PaperScheduler.poll` that touched a cycle."""

    event: CycleEvent
    decision_attempt_id: str
    scheduled_at: datetime
    attempt_count: int
    output_id: str | None = None
    plan: PlanStartResult | None = None
    retry_at: datetime | None = None
    next_decision_at: datetime | None = None
    # The §6.2 class the terminal api_failed row was written with, carried so
    # the loop does not have to read back a row it just wrote to learn WHY the
    # cycle failed (issue #50's escalation words its message from it). Only an
    # api_failed result may carry one — and an api_failed result may carry
    # NONE: a non-retryable bug fails the cycle closed with no §6.2 class
    # (``_fail_untyped``, issue #134), the same untyped row the live driver
    # writes, which the streak counter names "unclassified".
    error_type: str | None = None

    def __post_init__(self) -> None:
        # Shape guards, mirroring the sibling result dataclasses: each event
        # implies which follow-up fields exist, and a caller branches on them.
        if (self.retry_at is not None) != (self.event is CycleEvent.RETRY_SCHEDULED):
            raise ValueError("retry_at is present exactly on a retry_scheduled result")
        if self.error_type is not None and self.event is not CycleEvent.API_FAILED:
            raise ValueError("error_type is present only on an api_failed result")
        if (self.next_decision_at is None) == self.event.is_cycle_terminal:
            raise ValueError("next_decision_at is present exactly on a cycle-terminal result")
        completed = self.event in (CycleEvent.COMPLETED, CycleEvent.INVALID_OUTPUT)
        if (self.output_id is None) == completed:
            raise ValueError("output_id is present exactly when a decision was persisted")
        if (self.plan is None) == completed:
            raise ValueError("plan is present exactly when a decision was persisted")
        # Every instant this result carries is a UTC-aware breadcrumb/export
        # input (same convention as _PendingDecision.scheduled_at); a naive one
        # would compare wrong against the aware instants downstream consume.
        for name in ("scheduled_at", "retry_at", "next_decision_at"):
            value = getattr(self, name)
            if value is not None and value.tzinfo is None:
                raise ValueError(f"PollResult.{name} must be timezone-aware (UTC)")


@dataclass
class _PendingDecision:
    """A parsed AI decision waiting for the engine to gate it (market data due).

    Once ``raw_stored`` is set, the raw response is persisted on the attempt
    row (``pending_raw_response``) and this in-memory shape is just the working
    copy: a restart re-parses the stored text and rebuilds it — never re-asks
    the AI (spec §3.1). Until then the paid-for decision lives ONLY here, so a
    store failure must keep this object and retry the store (issue #163) —
    while a crash in that window still fails closed on restart, because a
    decision that was never durable is never resumed.
    """

    attempt_id: str
    scheduled_at: datetime
    attempt_count: int
    input_id: str
    output_id: str
    parsed: ParsedDecision
    # Whether ``pending_raw_response`` landed durably. Defaults to the
    # conservative state — gating is forbidden until the store lands, mirroring
    # the live driver's ``_InFlight.raw_stored``.
    raw_stored: bool = False
    # The engine's start_plan outcome, cached the moment it exists: a persist
    # failure after the gate ran must retry the PERSIST against THIS
    # registration — never re-gate (a second start_plan would supersede the
    # committed plan and register another; the live driver caches it the same
    # way on ``_InFlight.registration``).
    registration: PlanStartResult | None = None
    # Consecutive failed persist polls: incremented by
    # ``_persist_budget_spent``, cleared by ``_persist_reached`` the moment
    # one lands. At ``_MAX_PERSIST_FAILURES`` the retry lane stops containing
    # and lets the exception propagate (supervised-restart escalation). Both
    # post-answer persists share the counter — they never interleave, since
    # the store completes before the gate may run.
    persist_failures: int = 0

    def __post_init__(self) -> None:
        # Mutable-state guard, same convention as the engine's _Leg/_FlipState:
        # a malformed id here would key a decision_attempts/ai_outputs row with
        # no earlier failure point to diagnose it.
        for name in ("attempt_id", "input_id", "output_id"):
            if not getattr(self, name):
                raise ValueError(f"_PendingDecision.{name} must be non-empty")
        if self.attempt_count < 1:
            raise ValueError(
                f"_PendingDecision.attempt_count must be >= 1, got {self.attempt_count}"
            )
        if self.scheduled_at.tzinfo is None:
            raise ValueError("_PendingDecision.scheduled_at must be timezone-aware (UTC)")


class PaperScheduler:
    """Drives one run's rolling 4h decision cycles against the paper engine."""

    def __init__(
        self,
        *,
        db: Database,
        run_id: str,
        engine: PaperExecutionEngine,
        clock: Clock,
        provider: DecisionProvider,
        asset: AssetSpec,
        risk_config: RiskConfig,
        decision_config: DecisionConfig,
        mode: str = "paper",
    ) -> None:
        self._db = db
        self._run_id = run_id
        self._engine = engine
        self._clock = clock
        self._provider = provider
        self._asset = asset
        self._coin = asset.coin
        self._risk = risk_config
        # Needed to re-parse a persisted pending_raw_response on restart —
        # parse_target_decision is deterministic, so the resumed decision is
        # identical to the one the crashed process held in memory. If the
        # operator changed the decision config across the restart (accepted
        # parameter drift), the stored response is deliberately re-read under
        # the CURRENT config: drifted parameters apply from resume onward,
        # the same instant they would apply to any new cycle.
        self._decision_cfg = decision_config
        self._mode = mode
        self._pending: _PendingDecision | None = None

    # -- public surface ----------------------------------------------------

    def poll(self) -> PollResult | None:
        """Advance the cycle state machine one step; ``None`` when nothing is due.

        Order matters: a decision already parsed but not yet gated (market-data
        outage at plan start) is finished first; then a persisted in-progress
        attempt continues (restart-safe retry, spec §3.1); only then may a new
        cycle become due (spec §3). ``None`` with a pending decision standing
        means a scheduler persist failed and will be retried next poll
        (issue #163; the ERROR log carries the traceback) — ``next_due_at``
        answers "poll now" for that state, so the loop's cadence is the retry
        cadence.
        """
        now = self._clock.now()
        if self._pending is not None:
            return self._finalize(now)

        attempt = repo.find_in_progress_attempt(self._db.conn, self._run_id)
        if attempt is not None:
            count = attempt["attempt_count"]
            if attempt["pending_raw_response"] is not None:
                # The AI already answered this attempt (persisted before the
                # gate) — resume gating from the stored response, regardless of
                # the retry ladder or the try counter. Never re-ask (spec §3.1).
                return self._resume_pending(now, attempt)
            if count >= MAX_DECISION_ATTEMPTS:
                # A crash between persisting try 3's counter and recording its
                # outcome: the budget is spent (the third call may have fired),
                # so the only safe restart action is the §3.1 terminal state.
                return self._terminalize_api_failed(
                    now,
                    attempt_id=attempt["decision_attempt_id"],
                    scheduled_at=parse_instant(attempt["scheduled_at"]),
                    attempt_count=count,
                    error_type="interrupted",
                    error_message=(
                        "restart found the final attempt already started; "
                        "refusing to exceed the 3-try budget"
                    ),
                )
            if count > 0 and now < self._retry_at(attempt):
                return None  # between retries — not due yet
            return self._execute(now, attempt)

        scheduled_at = self._due_cycle(now)
        if scheduled_at is None:
            return None
        attempt_id = derive_attempt_id(self._run_id, scheduled_at)
        with self._db.transaction() as conn:
            repo.insert_decision_attempt(
                conn,
                decision_attempt_id=attempt_id,
                timestamp=now,
                mode=self._mode,
                run_id=self._run_id,
                scheduled_at=scheduled_at,
                attempt_count=0,
                status="in_progress",
            )
            repo.upsert_scheduler_state(
                conn, self._run_id, current_attempt_id=attempt_id, updated_at=now
            )
        attempt = repo.get_decision_attempt(self._db.conn, attempt_id)
        assert attempt is not None  # just inserted in a committed transaction
        return self._execute(now, attempt)

    def next_due_at(self) -> datetime | None:
        """When the scheduler next has work, for the caller's sleep sizing.

        ``None`` means "poll now" (a pending gate retry or an already-due
        cycle); a fresh run with no state is also due immediately (spec §3).
        """
        if self._pending is not None:
            return None
        attempt = repo.find_in_progress_attempt(self._db.conn, self._run_id)
        if attempt is not None:
            if attempt["attempt_count"] == 0 or attempt["pending_raw_response"] is not None:
                return None  # a fresh attempt / a stored decision awaiting its gate
            return self._retry_at(attempt)
        state = repo.get_scheduler_state(self._db.conn, self._run_id)
        if state is None or state["next_decision_at"] is None:
            return None
        return parse_instant(state["next_decision_at"])

    # -- cycle scheduling (spec §3) -----------------------------------------

    def _due_cycle(self, now: datetime) -> datetime | None:
        """The ``scheduled_at`` of a cycle due now, or ``None``.

        A missing ``next_decision_at`` is the "new run, no previous decision"
        shape → run immediately under ``scheduled_at = now``. A stored one in
        the past runs once under its *original* stamp (the deterministic
        attempt id) — never once per missed interval.
        """
        state = repo.get_scheduler_state(self._db.conn, self._run_id)
        raw = state["next_decision_at"] if state is not None else None
        if raw is None:
            return now
        next_at = parse_instant(raw)
        return next_at if now >= next_at else None

    def _retry_at(self, attempt) -> datetime:
        count = attempt["attempt_count"]
        delay = RETRY_DELAYS_SECONDS[min(count, len(RETRY_DELAYS_SECONDS)) - 1]
        return parse_instant(attempt["last_attempt_at"]) + timedelta(seconds=delay)

    # -- attempt execution (spec §3.1) ---------------------------------------

    def _execute(self, now: datetime, attempt) -> PollResult | None:
        attempt_id = attempt["decision_attempt_id"]
        scheduled_at = parse_instant(attempt["scheduled_at"])
        count = attempt["attempt_count"] + 1
        # Persist the try *before* any API call: a crash mid-call must resume
        # this attempt with the counter already spent (spec §3.1 — restart may
        # continue the same attempt, never reset it or double-decide).
        with self._db.transaction() as conn:
            repo.update_decision_attempt(
                conn,
                attempt_id,
                attempt_count=count,
                first_attempt_at=(
                    now
                    if attempt["first_attempt_at"] is None
                    else parse_instant(attempt["first_attempt_at"])
                ),
                last_attempt_at=now,
                timestamp=now,
            )
        input_id = f"{attempt_id}#in{count}"
        try:
            decision_input = self._provider.build_input(coin=self._coin, as_of=now)
            self._insert_ai_input(now, input_id, attempt_id, decision_input)
            parsed = self._provider.request_decision(decision_input)
        except RetryableDecisionError as exc:
            return self._record_failure(attempt_id, scheduled_at, count, exc)
        except Exception as exc:  # noqa: BLE001 — a bug fails the cycle closed, not the daemon
            return self._fail_untyped(attempt_id, scheduled_at, count, exc)
        # raw_stored=False: _finalize lands the §3.1 store before any gate.
        self._pending = self._pending_for(attempt_id, scheduled_at, count, parsed, raw_stored=False)
        return self._finalize(now)

    def _resume_pending(self, now: datetime, attempt) -> PollResult | None:
        """Rebuild the pending decision from the persisted response and gate it.

        ``parse_target_decision`` is deterministic, so this yields exactly the
        decision the crashed process held; the per-try ``input_id``/``output_id``
        are re-derived from the persisted try counter, so any rows the crashed
        try already committed line up with the ones written now.
        """
        count = attempt["attempt_count"]
        attempt_id = attempt["decision_attempt_id"]
        scheduled_at = parse_instant(attempt["scheduled_at"])
        raw = attempt["pending_raw_response"]
        try:
            parsed = parse_target_decision(raw, self._decision_cfg)
        except Exception as exc:  # noqa: BLE001 — a bug fails the cycle closed, not the daemon
            # parse_target_decision's contract is fail-closed (malformed
            # content returns an invalid ParsedDecision), so a raise here is a
            # bug or a corrupted store — and it is DETERMINISTIC: propagating
            # would crash the daemon and every supervised restart would resume
            # into the same parse (issue #163). _terminalize_api_failed clears
            # the poisoned response, so log the full text FIRST — the row was
            # its only durable copy and the post-mortem needs it.
            logger.error(
                "decision attempt %s: stored response failed to parse and is being "
                "cleared; preserving it here for diagnosis: %r",
                attempt_id,
                raw,
            )
            return self._fail_untyped(attempt_id, scheduled_at, count, exc)
        # raw_stored=True: resuming FROM the stored response — already durable.
        self._pending = self._pending_for(attempt_id, scheduled_at, count, parsed, raw_stored=True)
        return self._finalize(now)

    def _pending_for(
        self,
        attempt_id: str,
        scheduled_at: datetime,
        count: int,
        parsed: ParsedDecision,
        *,
        raw_stored: bool,
    ) -> _PendingDecision:
        """Build the in-memory pending decision, owning the per-try id scheme.

        The per-try ids (``#in<n>``/``#out<n>``) exist so a crashed try's
        committed orders reference the ai_outputs row of *its own* decision,
        never a later try's — derived HERE for both the fresh and the resumed
        lane, so the two can never drift apart.
        """
        return _PendingDecision(
            attempt_id=attempt_id,
            scheduled_at=scheduled_at,
            attempt_count=count,
            input_id=f"{attempt_id}#in{count}",
            output_id=f"{attempt_id}#out{count}",
            parsed=parsed,
            raw_stored=raw_stored,
        )

    def _record_failure(
        self,
        attempt_id: str,
        scheduled_at: datetime,
        count: int,
        exc: RetryableDecisionError,
    ) -> PollResult:
        logger.warning(
            "decision attempt %s try %d/%d failed (%s): %s",
            attempt_id,
            count,
            MAX_DECISION_ATTEMPTS,
            exc.error_type,
            exc.message,
        )
        # The §3.1 ladder counts from the *failure* instant ("attempt 1 失敗 →
        # wait 10 seconds"), not from when the try started: a timing-out call
        # burns its whole delay in flight, and basing retry_at on the pre-call
        # stamp would fire the whole ladder back-to-back in exactly the outage
        # the back-off exists for. ``last_attempt_at`` is re-stamped to the
        # failure instant so a restarted process waits the same delay.
        failed_at = self._clock.now()
        if count >= MAX_DECISION_ATTEMPTS:
            return self._terminalize_api_failed(
                failed_at,
                attempt_id=attempt_id,
                scheduled_at=scheduled_at,
                attempt_count=count,
                error_type=exc.error_type,
                error_message=exc.message,
            )
        with self._db.transaction() as conn:
            repo.update_decision_attempt(
                conn,
                attempt_id,
                last_attempt_at=failed_at,
                error_type=exc.error_type,
                error_message=exc.message,
                timestamp=failed_at,
            )
        return PollResult(
            event=CycleEvent.RETRY_SCHEDULED,
            decision_attempt_id=attempt_id,
            scheduled_at=scheduled_at,
            attempt_count=count,
            retry_at=failed_at + timedelta(seconds=RETRY_DELAYS_SECONDS[count - 1]),
        )

    def _fail_untyped(
        self, attempt_id: str, scheduled_at: datetime, count: int, exc: Exception
    ) -> PollResult:
        """A non-retryable error in the try: fail THIS cycle closed, keep the daemon.

        The live driver's rule (``LiveDecisionDriver._fail_closed`` with
        ``error_type=None``), adopted for parity (issue #134): before this the
        same bug let the exception climb out of ``_paper_loop`` and the daemon
        exited for systemd to restart — into the same bug, 60s later, with the
        position unwatched in between. Now the cycle is terminal at once — no
        §3.1 ladder, because a bug does not heal in 10s the way an outage may
        — with NO §6.2 class (none applies; the detail rides
        ``error_message`` under a ``non-retryable:`` prefix, and the streak
        counter names it "unclassified"), the traceback logged at ERROR, the
        position and its SL/TP held, and the next cycle on schedule. A bug
        that recurs every cycle therefore reads as the same no-decision streak
        an outage does: ERROR from the third, ``validate`` exit 4 — the
        signal that used to be systemd's restart count. Scope: the three
        statements of the try in ``_execute``, plus ``_resume_pending``'s
        re-parse of the stored response (issue #163 — a deterministic parse
        bug would otherwise crash-loop every restart). A failure of the fail
        record itself still propagates (here that exits the daemon; the live
        lane parks it in safe mode and retries the write), and so does a
        ``start_plan`` raise inside ``_finalize`` — the engine has fail-stopped
        by then and only a restart rebuilds it (module docstring). The two
        scheduler-owned persists after the answer retry in-process instead
        (``_store_pending_response`` / ``_finalize``'s audit commit).

        "Non-retryable" is not the same as "a bug": everything that is not a
        :class:`RetryableDecisionError` lands here, and that includes host
        trouble the provider does not classify — a ``sqlite3.OperationalError``
        past ``busy_timeout`` on a books read, a ``MemoryError``. (The payload
        write's ``OSError`` IS classified, as ``server_error``.) The repr in
        ``error_message`` is what tells the two apart; the RUNBOOK's row says so.
        """
        logger.exception(
            "decision attempt %s try %d hit a non-retryable error — failing the cycle closed",
            attempt_id,
            count,
        )
        return self._terminalize_api_failed(
            self._clock.now(),
            attempt_id=attempt_id,
            scheduled_at=scheduled_at,
            attempt_count=count,
            error_type=None,
            error_message=f"non-retryable: {exc!r}",
        )

    def _terminalize_api_failed(
        self,
        now: datetime,
        *,
        attempt_id: str,
        scheduled_at: datetime,
        attempt_count: int,
        error_type: str | None,
        error_message: str,
    ) -> PollResult:
        """Spec §3.1 terminal failure: hold position, no target, next = scheduled+4h.

        ``error_type`` is a §6.2 class, or ``None`` for a non-retryable bug
        (``_fail_untyped``) — the row then carries only ``error_message``.
        The previous AI output is deliberately not reused; existing SL/TP and
        the market monitor keep running untouched (the engine owns them).

        A cycle that itself ran late (process outage across schedule points)
        anchors on the terminal instant instead: the literal ``scheduled_at +
        4h`` would land in the past and fire the next cycle immediately, so a
        long outage over a failing API would chain one full retry ladder per
        missed interval — §3's "missed intervals are never backfilled" extended
        to failed cycles (the completed path already anchors on completion).
        """
        next_at = scheduled_at + CYCLE_INTERVAL
        if next_at <= now:
            next_at = now + CYCLE_INTERVAL
        with self._db.transaction() as conn:
            repo.update_decision_attempt(
                conn,
                attempt_id,
                status="api_failed",
                error_type=error_type,
                error_message=error_message,
                next_decision_at=next_at,
                timestamp=now,
            )
            repo.upsert_scheduler_state(
                conn,
                self._run_id,
                next_decision_at=next_at,
                current_attempt_id=None,
                updated_at=now,
            )
        # §11.1 best-effort cycle-end snapshot (after the terminal txn,
        # mirroring _finalize); see engine.try_write_cycle_snapshot.
        if not self._engine.try_write_cycle_snapshot():
            logger.warning(
                "api_failed cycle %s: cycle-end snapshot skipped (no market data or write failure)",
                attempt_id,
            )
        return PollResult(
            event=CycleEvent.API_FAILED,
            decision_attempt_id=attempt_id,
            scheduled_at=scheduled_at,
            attempt_count=attempt_count,
            next_decision_at=next_at,
            error_type=error_type,
        )

    # -- decision finalization (gate + audit rows) ---------------------------

    def _persist_budget_spent(self, pending: _PendingDecision, what: str) -> bool:
        """Count one persist failure; ``True`` on the ``_MAX_PERSIST_FAILURES``th.

        The caller then re-raises instead of containing: a fault that survives
        that many polls in a row is not the transient lock the retry lane
        exists for, and unbounded containment would wedge the run invisibly
        (the attempt row stays ``in_progress``, so the §3.1 no-decision streak
        and ``validate``'s exit 4 never fire, while the lease heartbeat keeps
        reporting a healthy daemon). The pre-#163 daemon exit — the
        supervisor's restart count — returns as the deliberate escalation
        signal.

        The count is CONSECUTIVE: any persist that lands clears it
        (:meth:`_persist_reached`), so the bound measures one unbroken streak
        rather than a cycle's lifetime total. Both post-answer persists share
        the counter, which is exact because they never interleave — the store
        completes before the gate is allowed to run — and a store that lands
        after failures hands the audit commit a fresh budget instead of the
        remainder of its own.
        """
        pending.persist_failures += 1
        if pending.persist_failures < _MAX_PERSIST_FAILURES:
            return False
        logger.error(
            "cycle %s: %s failed %d polls in a row — escalating to the supervisor (daemon exit)",
            pending.attempt_id,
            what,
            pending.persist_failures,
        )
        return True

    @staticmethod
    def _persist_reached(pending: _PendingDecision) -> None:
        """A persist landed: the failure streak is over (see the counter above)."""
        pending.persist_failures = 0

    def _store_pending_response(self, now: datetime, pending: _PendingDecision) -> bool:
        """Land the §3.1 store for the collected decision; ``False`` = retry.

        Persisting the response BEFORE gating is what makes the cycle
        resumable: a crash or a market-data-blocked gate resumes from the
        stored text instead of spending another AI call (spec §3.1). When the
        store itself fails — an operator's export/validate holding the SQLite
        lock is the motivating case — the paid-for decision lives only on
        ``pending``, so failing the cycle closed would discard it and the
        pre-#163 propagation exited the daemon over a transient miss. Instead
        the pending decision is kept and the NEXT poll retries only this store
        (never the AI call, never an unstored gate) — the live driver's
        ``_PendingResponsePersistError`` lane, expressed as a return value
        because paper's synchronous poll loop has no safe mode; its
        escalation is ``_persist_budget_spent``'s bounded re-raise. The ERROR
        log below is the operator's visibility; a crash while this retries
        fails closed on restart (the response was never durable).
        """
        if pending.raw_stored:
            return True
        if not pending.parsed.is_valid:
            # An invalid parse is no decision to resume, and its preserved text
            # is not guaranteed to re-parse to the same verdict (the live
            # driver's _store_pending_response carries the full reasoning: a
            # non-str answer is kept as its repr, which IS a str on resume).
            # "Nothing stored" IS the settled §3.1 state for an invalid answer
            # — a crash here retries the try, no order either way. The text is
            # durable nowhere else (ai_outputs records only the machine tag),
            # so preserve it in the log or a run that suddenly answers
            # invalid_output every cycle leaves the post-mortem only a counter.
            logger.warning(
                "decision attempt %s: the answer did not parse to a decision (%s) and is "
                "not resumable; preserving it here for diagnosis: %r",
                pending.attempt_id,
                pending.parsed.invalid_reason,
                pending.parsed.raw_response,
            )
            pending.raw_stored = True
            return True
        try:
            with self._db.transaction() as conn:
                # §3.1 store — see repo.store_pending_response (issue #181).
                repo.store_pending_response(
                    conn, pending.attempt_id, pending.parsed.raw_response, timestamp=now
                )
        except Exception:  # noqa: BLE001 — the decision only exists in memory; keep it
            if self._persist_budget_spent(pending, "the §3.1 response store"):
                raise
            logger.exception(
                "decision attempt %s: the AI answered but the §3.1 response store "
                "failed — holding the decision in memory and retrying the store "
                "next poll (never re-asking the AI)",
                pending.attempt_id,
            )
            return False
        pending.raw_stored = True
        self._persist_reached(pending)
        return True

    def _finalize(self, now: datetime) -> PollResult | None:
        """Gate the parsed decision and persist the cycle's terminal records.

        ``start_plan`` fetches its own fresh snapshot; with none available the
        gate never ran and nothing is persisted — the decision is held and this
        poll (and every later one) retries until data returns. §3.1's counter
        does not apply here: the AI already answered, so re-polling the gate
        must never burn a retry or re-ask the AI.

        ``None`` means one of the two scheduler-owned persists failed (the
        §3.1 response store, or the audit commit below): the pending decision
        — and, past the gate, its cached registration — is kept and the next
        poll retries only that persist; the ``_MAX_PERSIST_FAILURES``th
        failure in a row propagates instead (issue #163 — any persist that
        lands resets the streak). A ``start_plan`` raise is the one thing
        past the answer that gets no containment at all:
        the engine fail-stops on any escaped exception (a partially committed
        plan may exist), so every later tick would refuse and the position
        would sit unwatched inside a live-looking process — exiting for the
        supervisor to restart-and-rebuild is the recovery. That is the
        deliberate difference from the live lane, whose recoverable safe mode
        has no paper counterpart.

        A delayed audit retry still writes the GATE's mark/equity (the prices
        the plan was actually gated at — reusing the cached registration) under
        the retry instant's timestamp; the drift is bounded by the retry cap.
        """
        pending = self._pending
        assert pending is not None
        if not self._store_pending_response(now, pending):
            return None
        result = pending.registration
        if result is None:
            result = self._engine.start_plan(pending.parsed, output_id=pending.output_id)
            if result.gate is None:
                return PollResult(
                    event=CycleEvent.PENDING_MARKET_DATA,
                    decision_attempt_id=pending.attempt_id,
                    scheduled_at=pending.scheduled_at,
                    attempt_count=pending.attempt_count,
                )
            # Cache the outcome the moment it exists: the engine may have
            # COMMITTED (and armed) a plan, so a persist failure below must
            # retry the persist against THIS registration — never re-gate.
            pending.registration = result
        # Rolling boundary (spec §3): the next cycle keys off the instant the
        # cycle actually completed — the gate run, not the (possibly hours
        # earlier, market-data-delayed) AI answer — so two consecutive AI calls
        # are always >= 4h apart.
        decision_at = now
        next_at = decision_at + CYCLE_INTERVAL
        status = "completed" if pending.parsed.is_valid else "invalid_output"
        try:
            with self._db.transaction() as conn:
                self._insert_ai_output(conn, now, pending, result)
                repo.update_decision_attempt(
                    conn,
                    pending.attempt_id,
                    status=status,
                    output_id=pending.output_id,
                    next_decision_at=next_at,
                    # A completed cycle carries no live error state: clear any
                    # earlier retry's breadcrumbs (a "completed + timeout" row
                    # would misread as a failed cycle).
                    error_type=None,
                    error_message=None,
                    timestamp=now,
                )
                repo.upsert_scheduler_state(
                    conn,
                    self._run_id,
                    last_decision_at=decision_at,
                    next_decision_at=next_at,
                    last_input_id=pending.input_id,
                    last_output_id=pending.output_id,
                    current_attempt_id=None,
                    updated_at=now,
                )
        except Exception:  # noqa: BLE001 — a committed plan exists; api_failed would falsify it
            # Failing the cycle closed here would write "no action" into the
            # audit trail while the engine's just-committed plan fills real
            # (simulated) orders — the exact falsification the live lane's
            # _PlanRegisteredPersistError exists to prevent. Keep the pending
            # decision with its registration and retry ONLY this commit.
            if self._persist_budget_spent(pending, "the audit persist"):
                raise
            logger.exception(
                "cycle %s decided and gated, but the audit persist failed — "
                "retrying the persist next poll (the registered plan, if any, "
                "stands and keeps executing)",
                pending.attempt_id,
            )
            return None
        self._persist_reached(pending)
        # The cycle is now durably committed; drop the in-memory pending decision
        # BEFORE the best-effort snapshot. write_cycle_snapshot only swallows
        # (sqlite3.Error, OSError); a non-DB error (mark<=0 ValueError, a halted
        # engine, a corrupt stored Decimal) would otherwise escape with _pending
        # still set and re-enter _finalize next poll, re-running start_plan and
        # committing a duplicate live plan for the same output_id.
        self._pending = None
        # Cycle-end snapshot (§11.1/§12.1) at the gate's own mark — after the
        # audit transaction so a snapshot failure can't roll back the decision,
        # and best-effort so a snapshot write error can't tear down the live
        # SL/TP monitor (symmetric with the api_failed path; re-snapshots next
        # tick).
        assert result.mark_price is not None  # a gate ran (PlanStartResult invariant)
        if not self._engine.write_cycle_snapshot(result.mark_price):
            logger.warning(
                "completed cycle %s: cycle-end snapshot skipped; will re-snapshot next tick",
                pending.attempt_id,
            )
        return PollResult(
            event=CycleEvent.COMPLETED if pending.parsed.is_valid else CycleEvent.INVALID_OUTPUT,
            decision_attempt_id=pending.attempt_id,
            scheduled_at=pending.scheduled_at,
            attempt_count=pending.attempt_count,
            output_id=pending.output_id,
            plan=result,
            next_decision_at=next_at,
        )

    # -- audit rows (phase2-data §5 / §7) ------------------------------------

    def _insert_ai_input(
        self, now: datetime, input_id: str, attempt_id: str, decision_input: DecisionInput
    ) -> None:
        """Record what the AI is about to see: market context + paper account state.

        The account side comes from the books the provider read for the
        prompt's position section and carried on the input (issue #134): one
        read per cycle feeds both the prompt and this row, so the two cannot
        describe different books. A provider that carries none (a test double,
        a replay harness) gets the pre-#134 read here instead.
        """
        ctx = decision_input.context
        conn = self._db.conn
        books = decision_input.books or read_books(self._db, self._run_id, self._coin)
        if books is None:
            raise ValueError(
                f"run {self._run_id!r} has no account state; call accounting.initialize_run first"
            )
        ledger, position = books.ledger, books.position
        valuations = (
            []
            if position.is_flat
            else [
                accounting.PositionValuation(position, ctx.mark_price, self._asset.margin_schedule)
            ]
        )
        metrics = accounting.summarize_account(ledger, valuations, leverage=self._risk.leverage)
        active_plans = repo.iter_execution_plans(
            conn, self._run_id, statuses=repo.LIVE_PLAN_STATUSES
        )
        with localcontext(DECIMAL_CONTEXT):
            remaining_twap = sum(
                (Decimal(p["remaining_qty"]) for p in active_plans if p["remaining_qty"]),
                Decimal(0),
            )
        # The same estimate the engine trades on (one derivation, engine-owned).
        liq_price = self._engine.liquidation_price(position, ctx.mark_price)
        audit_rows.write_ai_input(
            self._db,
            now=now,
            input_id=input_id,
            attempt_id=attempt_id,
            decision_input=decision_input,
            mode=self._mode,
            run_id=self._run_id,
            symbol=self._coin,
            ledger=ledger,
            position=position,
            metrics=metrics,
            leverage=self._risk.leverage,
            max_target_margin_pct=self._risk.max_target_margin_pct,
            liquidation_price=liq_price,
            last_fill_time=books.last_fill_time,
            active_twap=bool(active_plans),
            remaining_twap_qty=remaining_twap if active_plans else None,
        )

    def _insert_ai_output(
        self, conn, now: datetime, pending: _PendingDecision, result: PlanStartResult
    ) -> None:
        """One §7 ``ai_outputs`` row from the gate's outcome (its own sizing inputs)."""
        gate = result.gate
        assert gate is not None  # _finalize only reaches here with a gated result
        audit_rows.write_ai_output(
            conn,
            now=now,
            output_id=pending.output_id,
            input_id=pending.input_id,
            decision_attempt_id=pending.attempt_id,
            mode=self._mode,
            run_id=self._run_id,
            symbol=self._coin,
            gate=gate,
            parsed=pending.parsed,
            mark_price=result.mark_price,
            account_equity=result.account_equity,
        )
