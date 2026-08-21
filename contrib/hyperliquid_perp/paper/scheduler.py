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
failure by raising :class:`RetryableDecisionError` — anything else propagates
as a bug.

Restart safety of a half-finished cycle: the successful AI response is
persisted onto the attempt row (``pending_raw_response``) *before* the gate
runs, so a crash anywhere between the AI answering and the audit commit — the
market-data-blocked gate phase, or the window between ``start_plan``'s
plan/order commit and the ``ai_outputs`` commit — resumes by re-parsing the
stored response (deterministic), never by re-asking the AI (spec §3.1). The
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

from ..domains.perp.risk_gate import RiskConfig
from ..domains.perp.schema import PerpMarketContext
from ..domains.perp.target_decision import DecisionConfig, ParsedDecision, parse_target_decision
from ..persistence import audit_rows, repository as repo
from ..persistence.db import Database
from ..persistence.ids import decision_attempt_id as derive_attempt_id
from ..persistence.models import DECIMAL_CONTEXT, PositionState
from . import accounting
from .clock import Clock
from .engine import AssetSpec, PaperExecutionEngine, PlanStartResult

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

# The rolling decision interval (spec §3) and the §3.1 retry ladder: after the
# first failure wait 10s, after the second 30s, after the third → api_failed.
CYCLE_INTERVAL = timedelta(hours=4)
MAX_DECISION_ATTEMPTS = 3
RETRY_DELAYS_SECONDS = (10, 30)


def parse_instant(text: str) -> datetime:
    """Decode a stored ISO-8601 UTC timestamp (the repository's storage form)."""
    value = datetime.fromisoformat(text)
    if value.tzinfo is None:  # the write boundary never stores naive stamps
        raise ValueError(f"stored timestamp {text!r} is naive; the store is corrupt")
    return value


class RetryableDecisionError(Exception):
    """A market-data / AI API failure worth retrying (spec §3.1).

    ``error_type`` uses the §6.2 vocabulary (``timeout`` / ``rate_limit`` /
    ``connection`` / ``malformed_response`` / ``server_error``); anything the
    provider does not classify should propagate as a normal exception (a bug,
    not a retry). The authoritative list is ``repository._vocab._ERROR_TYPES``,
    which ``check_enum`` enforces at the write boundary — this sentence is a
    convenience copy, so extend the two together.
    """

    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(f"{error_type}: {message}")
        self.error_type = error_type
        self.message = message


@dataclass(frozen=True)
class DecisionInput:
    """Everything one AI call sees, built by the provider before the call.

    ``context`` is the market side of the ``ai_inputs`` row; the paper-account
    side is read from the store by the scheduler at insert time. The payload
    path/hash point at the full JSON the provider persisted (phase2-data §5:
    SQLite keeps the summary + path + hash, never the whole prompt).
    """

    context: PerpMarketContext
    candle_start: datetime | None = None
    candle_end: datetime | None = None
    input_payload_path: str | None = None
    input_payload_hash: str | None = None
    prompt_version: str | None = None
    model: str | None = None

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
    guards (``engine_bridge._context_refusal_error``); drivers do not re-check before
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

    def __post_init__(self) -> None:
        # Shape guards, mirroring the sibling result dataclasses: each event
        # implies which follow-up fields exist, and a caller branches on them.
        if (self.retry_at is not None) != (self.event is CycleEvent.RETRY_SCHEDULED):
            raise ValueError("retry_at is present exactly on a retry_scheduled result")
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

    The raw response is already persisted on the attempt row
    (``pending_raw_response``), so this in-memory shape is just the working
    copy: a restart re-parses the stored text and rebuilds it — never re-asks
    the AI (spec §3.1).
    """

    attempt_id: str
    scheduled_at: datetime
    attempt_count: int
    input_id: str
    output_id: str
    parsed: ParsedDecision

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
        cycle become due (spec §3).
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

    def _execute(self, now: datetime, attempt) -> PollResult:
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
        # Persist the response BEFORE gating: from here on, a crash or a
        # market-data-blocked gate resumes from this stored text instead of
        # spending another AI call (spec §3.1 — no duplicate decision).
        with self._db.transaction() as conn:
            repo.update_decision_attempt(
                conn, attempt_id, pending_raw_response=parsed.raw_response, timestamp=now
            )
        self._pending = _PendingDecision(
            attempt_id=attempt_id,
            scheduled_at=scheduled_at,
            attempt_count=count,
            input_id=input_id,
            # Per-try id: a crashed try's committed orders must reference the
            # ai_outputs row of *its own* decision, never a later try's.
            output_id=f"{attempt_id}#out{count}",
            parsed=parsed,
        )
        return self._finalize(now)

    def _resume_pending(self, now: datetime, attempt) -> PollResult:
        """Rebuild the pending decision from the persisted response and gate it.

        ``parse_target_decision`` is deterministic, so this yields exactly the
        decision the crashed process held; the per-try ``input_id``/``output_id``
        are re-derived from the persisted try counter, so any rows the crashed
        try already committed line up with the ones written now.
        """
        count = attempt["attempt_count"]
        attempt_id = attempt["decision_attempt_id"]
        self._pending = _PendingDecision(
            attempt_id=attempt_id,
            scheduled_at=parse_instant(attempt["scheduled_at"]),
            attempt_count=count,
            input_id=f"{attempt_id}#in{count}",
            output_id=f"{attempt_id}#out{count}",
            parsed=parse_target_decision(attempt["pending_raw_response"], self._decision_cfg),
        )
        return self._finalize(now)

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

    def _terminalize_api_failed(
        self,
        now: datetime,
        *,
        attempt_id: str,
        scheduled_at: datetime,
        attempt_count: int,
        error_type: str,
        error_message: str,
    ) -> PollResult:
        """Spec §3.1 terminal failure: hold position, no target, next = scheduled+4h.

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
        )

    # -- decision finalization (gate + audit rows) ---------------------------

    def _finalize(self, now: datetime) -> PollResult:
        """Gate the parsed decision and persist the cycle's terminal records.

        ``start_plan`` fetches its own fresh snapshot; with none available the
        gate never ran and nothing is persisted — the decision is held and this
        poll (and every later one) retries until data returns. §3.1's counter
        does not apply here: the AI already answered, so re-polling the gate
        must never burn a retry or re-ask the AI.
        """
        pending = self._pending
        assert pending is not None
        result = self._engine.start_plan(pending.parsed, output_id=pending.output_id)
        if result.gate is None:
            return PollResult(
                event=CycleEvent.PENDING_MARKET_DATA,
                decision_attempt_id=pending.attempt_id,
                scheduled_at=pending.scheduled_at,
                attempt_count=pending.attempt_count,
            )
        # Rolling boundary (spec §3): the next cycle keys off the instant the
        # cycle actually completed — the gate run, not the (possibly hours
        # earlier, market-data-delayed) AI answer — so two consecutive AI calls
        # are always >= 4h apart.
        decision_at = now
        next_at = decision_at + CYCLE_INTERVAL
        status = "completed" if pending.parsed.is_valid else "invalid_output"
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
                # would misread as a failed cycle) and the consumed response.
                error_type=None,
                error_message=None,
                pending_raw_response=None,
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
        """Record what the AI is about to see: market context + paper account state."""
        ctx = decision_input.context
        conn = self._db.conn
        ledger = repo.get_current_account_state(conn, self._run_id)
        if ledger is None:
            raise ValueError(
                f"run {self._run_id!r} has no account state; call accounting.initialize_run first"
            )
        position = repo.get_current_position(conn, self._run_id, self._coin) or PositionState.flat(
            self._coin
        )
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
