"""§18.2 background AI decision worker — the Option-A concurrency seam (PR 5).

The live loop must refresh the kill switch every 30s (§18.2): its dead man's
switch cancels every order on the wallet if a refresh lands late. But the AI
decision (:meth:`DecisionProvider.request_decision` — a multi-agent LLM graph)
runs for MINUTES. Running it synchronously on the tick thread, as the paper loop
does, would block the kill-switch refresh and let the switch fire during normal
operation.

So the LLM call — and ONLY the LLM call, which touches no SQLite — runs on a
background worker thread while the main loop keeps ticking (draining the WS
queue, refreshing the kill switch, reconciling, protecting). Everything with a
side effect stays on the main thread (§11.4 single writer): the caller builds
the :class:`DecisionInput` and persists the input/attempt BEFORE
:meth:`submit`, and consumes the parsed decision through :meth:`poll` to gate
and persist the output AFTER — so the worker is a pure function-in-a-thread with
no store access of its own.

A :class:`RetryableDecisionError` raised inside ``request_decision`` is captured
and re-raised out of :meth:`poll` on the main thread, so the caller's §10.2
fail-closed handling (hold the position, never act on a stale target, retry next
cycle) runs exactly where its DB writes belong.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from ..common.constants import CYCLE_INTERVAL
from ..common.instants import parse_instant
from ..common.no_decision import note_cycle_outcome
from ..domains.perp.risk_gate import RiskConfig
from ..domains.perp.target_decision import (
    DecisionConfig,
    ParsedDecision,
    parse_target_decision,
)
from ..paper import accounting
from ..paper.clock import Clock, WallClock
from ..paper.engine import AssetSpec
from ..paper.position_facts import read_books
from ..paper.scheduler import DecisionInput, DecisionProvider, RetryableDecisionError
from ..persistence import audit_rows, ids, repository as repo
from ..persistence.db import Database

if TYPE_CHECKING:
    from .engine import PlanRegistration

__all__ = ["LiveDecisionDriver", "LiveDecisionWorker"]

logger = logging.getLogger(__name__)

# §18.2 stall visibility (decided 2026-07-22 — warn, don't force): a wedged LLM
# call (an SDK hang with no effective timeout) leaves the run silently
# protection-only while tick / kill switch / SL all look healthy. v1 only makes
# the condition VISIBLE on a cadence — forcing a deadline would abandon the
# stuck thread and submit a fresh worker beside it, and two concurrent
# request_decision calls on one provider instance break the busy-gate
# serialization its thread-safety relies on (provider purity, deferred PR 6).
_STALL_WARN_AFTER = timedelta(minutes=30)
_STALL_REWARN_EVERY = timedelta(minutes=5)


class LiveDecisionWorker:
    """Runs one AI decision at a time on a background thread (§18.2 Option A)."""

    def __init__(self, *, provider: DecisionProvider) -> None:
        self._provider = provider
        self._thread: threading.Thread | None = None
        self._result: ParsedDecision | None = None
        self._error: BaseException | None = None
        self._lock = threading.Lock()

    @property
    def busy(self) -> bool:
        """Whether a decision is currently computing on the worker thread."""
        return self._thread is not None and self._thread.is_alive()

    @property
    def has_result(self) -> bool:
        """Whether a finished decision (or its error) is waiting to be polled."""
        return not self.busy and (self._result is not None or self._error is not None)

    def submit(self, decision_input: DecisionInput) -> None:
        """Start computing ``request_decision(decision_input)`` off the tick thread.

        The caller must have persisted the input/attempt already (main thread)
        and must not submit while :attr:`busy` — one decision runs at a time.
        """
        if self.busy:
            raise RuntimeError(
                "a decision is already in flight — poll it before submitting another"
            )
        self._result = None
        self._error = None

        def _run() -> None:
            try:
                parsed = self._provider.request_decision(decision_input)
            except BaseException as exc:  # noqa: BLE001 — surfaced verbatim on the main thread
                with self._lock:
                    self._error = exc
            else:
                with self._lock:
                    self._result = parsed

        self._thread = threading.Thread(target=_run, name="hl-live-decision", daemon=True)
        self._thread.start()

    def poll(self) -> ParsedDecision | None:
        """The finished decision, or ``None`` while it is still computing.

        Re-raises on the main thread whatever ``request_decision`` raised (a
        :class:`RetryableDecisionError` or otherwise), so the caller's
        fail-closed retry handling — and its DB writes — run where they belong.
        Consuming a result clears it: the next :meth:`submit` starts fresh.
        """
        if self.busy:
            return None
        with self._lock:
            error, result = self._error, self._result
            self._error = None
            self._result = None
        self._thread = None
        if error is not None:
            raise error
        return result

    def join(self, timeout: float | None = None) -> None:
        """Block until the in-flight decision finishes (for shutdown / tests)."""
        thread = self._thread
        if thread is not None:
            thread.join(timeout)


class _PlanRegisteredPersistError(RuntimeError):
    """A plan WAS registered but its audit persist failed (§18.2 / phase2-data §5).

    Raised out of :meth:`LiveDecisionDriver._gate` INSTEAD of failing the cycle:
    marking it ``api_failed`` would leave the audit trail claiming "no action"
    while the engine's just-committed plan sends real slices next tick. The
    driver keeps ``_inflight`` (with the registration cached) and retries ONLY
    the persist on subsequent pumps; the exception propagates to the loop's
    tick guard, which enters recoverable safe mode — pausing the plan's slices
    at the wire gate until the store heals and a clean reconcile releases it.
    """


class _PendingResponsePersistError(RuntimeError):
    """The decision WAS collected but its §3.1 store failed (2026-07-23).

    ``poll()`` is one-shot: by the time ``_store_pending_response`` runs, the
    paid-for decision lives only on ``_inflight``. Failing the cycle closed
    here (the generic pump guard) would discard it over a transient DB miss —
    an operator's export/validate holding the SQLite lock — and idle the run
    up to 4h. The third retry-only-the-persist lane, mirroring
    :class:`_PlanRegisteredPersistError` and ``pending_fail``: the driver keeps
    ``_inflight`` (``parsed`` set, ``raw_stored`` False) and retries ONLY the
    store on subsequent pumps — never re-polling the worker, never gating an
    unstored decision (a crash before the store lands must fail closed on
    restart, §3.1, not resume a cycle whose raw response was never durable).
    The exception propagates to the loop's tick guard (recoverable safe mode).
    """


@dataclass
class _InFlight:
    attempt_id: str
    input_id: str
    output_id: str
    scheduled_at: datetime
    parsed: ParsedDecision | None = None  # set once the worker returns, gate pending
    # Whether ``pending_raw_response`` landed durably. ``parsed`` is set the
    # moment poll() hands over the one-shot result — BEFORE the fallible §3.1
    # store — so "collected but not yet stored" is a real state, and gating is
    # forbidden in it (see _PendingResponsePersistError).
    raw_stored: bool = False
    # The engine's start_plan outcome, cached the moment it returns: a persist
    # failure after registration must retry the PERSIST, never re-gate (a
    # second start_plan would re-run the RiskGate and could register a second
    # plan).
    registration: PlanRegistration | None = None
    # Armed when the cycle has FAILED but the api_failed record could not be
    # written (a double fault: the failure handler's own DB write raised).
    # While set, pump() retries ONLY that write — it never re-polls the
    # worker (poll() is one-shot; a second call reads as "not ready" forever,
    # the C1 wedge) and never re-asks the AI. Mirrors the
    # _PlanRegisteredPersistError pattern: keep _inflight, retry the persist.
    pending_fail: tuple[str | None, str] | None = None  # (error_type, message)
    # Stall visibility: when the LLM call was handed to the worker thread — NOT
    # scheduled_at, which for an overdue cycle (downtime, latch) lies hours in
    # the past and would trip the warning on the first busy tick.
    submitted_at: datetime | None = None
    last_stall_log_at: datetime | None = None


class LiveDecisionDriver:
    """Drives the 4h AI decision cycle around the off-thread worker (§3 / §18.2).

    The main-thread half of Option A: all cycle bookkeeping (attempt rows, the
    ai_inputs / ai_outputs audit trail, the rolling ``next_decision_at``) runs
    here on the tick thread; only the LLM call rides
    :class:`LiveDecisionWorker`. :meth:`pump` advances the cycle one step per
    tick and never blocks — while the worker computes it returns immediately, so
    the loop keeps refreshing the kill switch.

    A retryable failure (§6.2 vocabulary) or a stale response is fail-closed
    (§10.2): the attempt is marked ``api_failed``, the position and its SL/TP are
    held, and the cycle re-anchors to the next 4h boundary. The full within-cycle
    retry ladder (paper §3.1) is deliberately NOT replicated for v1 — a live
    failure holds and retries at the next scheduled cycle.
    """

    def __init__(
        self,
        *,
        db: Database,
        run_id: str,
        coin: str,
        asset: AssetSpec,
        risk_config: RiskConfig,
        decision_config: DecisionConfig,
        engine,
        worker: LiveDecisionWorker,
        provider: DecisionProvider,
        clock: Clock | None = None,
        cycle_interval: timedelta = CYCLE_INTERVAL,
        mode: str = "live",
    ) -> None:
        self._db = db
        self._run_id = run_id
        self._coin = coin
        self._asset = asset
        self._risk = risk_config
        # Needed to re-parse a persisted pending_raw_response on restart —
        # parse_target_decision is deterministic, so the resumed decision is
        # identical to the one the crashed process held in memory (same
        # accepted-drift semantics as PaperScheduler: a config change across
        # the restart applies from resume onward).
        self._decision_cfg = decision_config
        self._engine = engine
        self._worker = worker
        self._provider = provider
        self._clock = clock or WallClock()
        self._cycle_interval = cycle_interval
        self._mode = mode
        self._inflight: _InFlight | None = None
        self._paused_for_latch = False  # one log line per manual-latch pause episode
        self._no_decision_streak = 0  # issue #50: consecutive cycles with no decision

    def pump(self) -> str | None:
        """Advance the decision cycle one non-blocking step; return an event tag."""
        now = self._clock.now()
        if self._worker.busy:
            self._maybe_warn_stalled(now)
            return None
        if self._inflight is not None:
            # One guard for the whole in-flight lifecycle (collect + gate): ANY error
            # advancing an already-started cycle fails it CLOSED, so a bug in poll() /
            # start_plan / persist can never wedge the driver — a stuck _inflight
            # either stalls the loop forever (the empty poll slot reads as "not
            # ready", C1) or crash-loops the gate every tick (C2). A retryable (§6.2)
            # failure keeps its error_type; anything else is a non-retryable bug that
            # still fails closed. The fail record itself is a DB write that can ALSO
            # fail — that double fault arms ``pending_fail`` instead of losing the
            # cycle, and this branch retries only that write until it lands (never
            # re-polling the one-shot worker slot: the original C1 wedge). _start
            # owns its own pre-inflight failure the same way.
            if self._inflight.pending_fail is not None:
                return self._flush_pending_fail()
            try:
                if self._inflight.parsed is None and not self._collect():
                    return None  # worker still settling
                if not self._inflight.raw_stored:
                    # Fresh-collected, or a prior store missed: ensure the §3.1
                    # raw response is durable (the worker slot is spent — never
                    # re-poll) before gating.
                    self._persist_pending_response(now)
                return self._gate(now)
            except (_PlanRegisteredPersistError, _PendingResponsePersistError):
                # A durable fact (registered plan / collected decision) exists
                # that failing the cycle closed would falsify or discard. Keep
                # _inflight (registration / parsed cached) so the next pump
                # retries only the persist; propagate so the tick guard alarms
                # (safe mode).
                raise
            except RetryableDecisionError as exc:
                return self._fail_closed(exc.error_type, exc.message)
            except Exception as exc:  # noqa: BLE001 — a bug must still fail the cycle closed
                return self._fail_closed(None, f"non-retryable: {exc!r}")
        if not self._due(now):
            return None
        # A standing §13.5 manual latch means a human must intervene and every
        # target would be refused at the manual_safe_mode gate line — skip the
        # multi-agent LLM spend entirely (decided 2026-07-22). Only NEW starts
        # are skipped: an already-in-flight decision above still collects and
        # gates (its refusal records the §4.1 rejected row). next_decision_at
        # never advances while paused, so release starts a FRESH cycle at once.
        if self._manual_latched():
            if self._paused_for_latch:
                return None
            self._paused_for_latch = True
            logger.info(
                "decision cycle due, but a manual safe-mode latch is standing — "
                "skipping the LLM call until a human releases it (§13.6)"
            )
            return "decision_paused"
        if self._paused_for_latch:
            self._paused_for_latch = False
            logger.info("manual safe-mode latch released — decision cycles resume")
        return self._start(now)

    def resume_startup(self) -> str | None:
        """§3.1 restart adoption of a stranded ``in_progress`` attempt.

        Without this, a process killed mid-cycle (Ctrl-C during the minutes-long
        LLM call included) wedges the driver PERMANENTLY: ``next_decision_at``
        never advanced, so ``_start`` re-derives the same deterministic attempt
        id and ``insert_decision_attempt`` raises on its UNIQUE — every tick,
        forever, while the run looks alive. Mirrors ``PaperScheduler.poll``'s
        adoption: a persisted raw response resumes at the gate (never a second
        AI call, §3.1); an attempt the AI never answered fails closed to the
        next cycle. Call once at loop start, before the first :meth:`pump`.
        """
        row = repo.find_in_progress_attempt(self._db.conn, self._run_id)
        if row is None:
            return None
        attempt_id = row["decision_attempt_id"]
        scheduled_at = parse_instant(row["scheduled_at"])
        raw = row["pending_raw_response"]
        if raw is not None:
            # Live attempts are always try 1 (no within-cycle ladder in v1), so
            # the per-try ids are re-derived the same way _start minted them.
            inflight = self._inflight = _InFlight(
                attempt_id, f"{attempt_id}#in1", f"{attempt_id}#out1", scheduled_at
            )
            try:
                inflight.parsed = parse_target_decision(raw, self._decision_cfg)
            except Exception as exc:  # noqa: BLE001 — a bug fails the cycle closed, not the daemon
                # A raise here is a parser bug or a corrupted store (the parse
                # is fail-closed by contract — see PaperScheduler._resume_pending,
                # the same guard), and DETERMINISTIC: this runs before the
                # loop's tick guard exists, so propagating would exit the daemon
                # into a supervised crash-loop with the position and its SL/TP
                # unwatched between restarts (issue #180). _fail_closed clears
                # the poisoned response, so log the full text FIRST — the row
                # was its only durable copy. The bare in-flight above is what
                # _fail_closed asserts on; it buys the traceback and the streak
                # count, NOT its pending_fail retry — a store miss on the fail
                # record still propagates out of this pre-loop call (a
                # transient fault a restart retries, unlike the parse).
                logger.error(
                    "decision attempt %s: stored response failed to parse and is being "
                    "cleared; preserving it here for diagnosis: %r",
                    attempt_id,
                    raw,
                )
                return self._fail_closed(None, f"non-retryable: {exc!r}")
            inflight.raw_stored = True  # the parse SOURCE is the store — durable by definition
            logger.info("resuming in-progress decision %s from its stored response", attempt_id)
            return "resumed"
        # The AI never answered (the LLM call died with the process): fail the
        # cycle closed — §10.2 hold — and re-anchor, exactly like an in-process
        # non-retryable failure. The paid-for call is gone either way.
        logger.warning(
            "in-progress decision %s had no stored response after restart — failing it closed",
            attempt_id,
        )
        self._fail_cycle(
            attempt_id,
            scheduled_at,
            error_type=None,
            error_message="process restart interrupted this cycle before the AI answered",
        )
        # This process's first terminal outcome: the counter is per-process and
        # starts at zero anyway, so this is the honest 1 rather than a reset.
        self._note_cycle_outcome("api_failed", None)
        return "api_failed"

    def salvage_shutdown(self) -> bool:
        """Persist a completed-but-undurable decision at shutdown (best-effort).

        Ctrl-C lands whenever it likes; a worker that finished DURING the
        shutdown window holds a paid-for decision that only ``_collect`` would
        have persisted (and a collected decision whose §3.1 store missed is in
        the same boat). Storing its raw response here lets ``resume_startup``
        resume the cycle from stored text after restart (§3.1 — never a second
        AI call) instead of failing it closed and idling up to 4h. Every
        failure is contained: shutdown proceeds regardless, and the un-salvaged
        cycle simply fails closed on restart exactly as before. Call after
        ``worker.join`` and never on the live tick path.
        """
        inflight = self._inflight
        if inflight is None or inflight.pending_fail is not None:
            return False
        parsed = inflight.parsed
        if inflight.raw_stored:
            return False  # already durable — restart resumes from the store as-is
        if parsed is None:
            try:
                parsed = self._worker.poll()
            except Exception:  # noqa: BLE001 — the worker failed; restart fails the cycle closed anyway
                logger.exception(
                    "shutdown salvage: worker raised — the cycle fails closed on restart"
                )
                return False
            if parsed is None:
                return False  # still computing (join timed out) — nothing to store
        # Two ways to get here: the worker finished during the shutdown window
        # (never polled), or _collect polled it and the §3.1 store missed
        # (parsed set, raw_stored False) — one last try either way.
        try:
            self._store_pending_response(inflight, parsed, self._clock.now())
        except Exception:  # noqa: BLE001 — a store miss must not block shutdown
            logger.exception("shutdown salvage: persist failed — the cycle fails closed on restart")
            return False
        inflight.raw_stored = True
        logger.info(
            "shutdown salvage: stored the completed decision for %s — restart resumes it",
            inflight.attempt_id,
        )
        return True

    # -- cycle steps ----------------------------------------------------------

    def _maybe_warn_stalled(self, now: datetime) -> None:
        """Warn on a cadence while the in-flight LLM call outlives all expectation.

        Visibility only (decided 2026-07-22): after ``_STALL_WARN_AFTER`` of
        worker-busy, log every ``_STALL_REWARN_EVERY`` so a wedged SDK call is
        distinguishable from a quiet market without inspecting
        ``decision_attempts``. Never forces the cycle — see the module-level
        constants for why v1 must not abandon-and-respawn the worker.
        """
        inflight = self._inflight
        if inflight is None or inflight.submitted_at is None:
            return
        elapsed = now - inflight.submitted_at
        if elapsed < _STALL_WARN_AFTER:
            return
        last = inflight.last_stall_log_at
        if last is not None and now - last < _STALL_REWARN_EVERY:
            return
        inflight.last_stall_log_at = now
        logger.warning(
            "live decision %s has been in flight for %.0f minutes — the LLM call "
            "may be hung; the run makes no new trading decisions until it returns "
            "(ticks, kill switch and SL/TP keep running). v1 does not force a "
            "deadline (PR 6).",
            inflight.attempt_id,
            elapsed.total_seconds() / 60,
        )

    def _manual_latched(self) -> bool:
        """Whether a §13.5 manual safe-mode latch is standing (scheduler_state)."""
        state = repo.get_scheduler_state(self._db.conn, self._run_id)
        return state is not None and state["safe_mode_type"] == "manual"

    def _due(self, now: datetime) -> bool:
        state = repo.get_scheduler_state(self._db.conn, self._run_id)
        if state is None or state["next_decision_at"] is None:
            return True  # a fresh run decides immediately (§3: run at once, then roll)
        return now >= parse_instant(state["next_decision_at"])

    def _scheduled_at(self, now: datetime) -> datetime:
        state = repo.get_scheduler_state(self._db.conn, self._run_id)
        raw = None if state is None else state["next_decision_at"]
        if raw is None:
            return now
        stored = parse_instant(raw)
        return stored if stored <= now else now

    def _start(self, now: datetime) -> str | None:
        scheduled_at = self._scheduled_at(now)
        attempt_id = ids.decision_attempt_id(self._run_id, scheduled_at)
        input_id = f"{attempt_id}#in1"
        output_id = f"{attempt_id}#out1"
        with self._db.transaction() as conn:
            repo.insert_decision_attempt(
                conn,
                decision_attempt_id=attempt_id,
                timestamp=now,
                mode=self._mode,
                run_id=self._run_id,
                scheduled_at=scheduled_at,
                attempt_count=1,
                status="in_progress",
                first_attempt_at=now,
                last_attempt_at=now,
            )
            repo.upsert_scheduler_state(
                conn, self._run_id, current_attempt_id=attempt_id, updated_at=now
            )
        try:
            decision_input = self._provider.build_input(coin=self._coin, as_of=now)
            self._persist_ai_input(now, input_id, attempt_id, decision_input)
        except RetryableDecisionError as exc:
            # The attempt row is already `in_progress`; leaving it unresolved with
            # next_decision_at in the past crash-loops every tick on the duplicate
            # attempt_id (C2). Adopt it as a failed in-flight so even a double
            # fault (the fail record's own write raising) retries the record
            # instead of re-INSERTing the same attempt id forever.
            self._inflight = _InFlight(attempt_id, input_id, output_id, scheduled_at)
            return self._fail_closed(exc.error_type, exc.message)
        except Exception as exc:  # noqa: BLE001 — a bug must fail the cycle CLOSED, never wedge it
            self._inflight = _InFlight(attempt_id, input_id, output_id, scheduled_at)
            return self._fail_closed(None, f"non-retryable: {exc!r}")
        self._inflight = _InFlight(attempt_id, input_id, output_id, scheduled_at)
        try:
            self._worker.submit(decision_input)
        except Exception as exc:  # noqa: BLE001 — Thread.start() can raise under pressure
            # Uncontained, this is the one wedge the guards above cannot catch:
            # the attempt row is in_progress, no thread ever ran, and poll()
            # would answer None forever — the cycle never completes and never
            # fails. _inflight is already set, so fail it CLOSED like any other
            # start failure; the next due cycle submits a fresh worker thread.
            return self._fail_closed(None, f"non-retryable: {exc!r}")
        self._inflight.submitted_at = now
        return "cycle_started"

    def _collect(self) -> bool:
        """Poll the one-shot worker; True once the decision is in hand.

        Failure handling lives in pump()'s in-flight guard: poll() re-raising a
        retryable (§6.2) or a non-retryable worker error both land there. On a
        ready result the parsed decision is cached on ``_inflight`` BEFORE the
        fallible §3.1 store (see :class:`_PendingResponsePersistError`); pump
        then makes it durable and gates — so the persist→gate tail lives in one
        place and ``_gate`` is reached only from pump.
        """
        inflight = self._inflight
        assert inflight is not None
        parsed = self._worker.poll()
        if parsed is None:
            return False  # not ready (worker still settling)
        inflight.parsed = parsed
        return True

    def _persist_pending_response(self, now: datetime) -> None:
        """Land the §3.1 store for the collected decision; retryable on failure."""
        inflight = self._inflight
        assert inflight is not None and inflight.parsed is not None
        try:
            self._store_pending_response(inflight, inflight.parsed, now)
        except Exception as exc:
            raise _PendingResponsePersistError(
                f"decision {inflight.attempt_id} collected but its store failed: {exc!r}"
            ) from exc
        inflight.raw_stored = True

    def _store_pending_response(
        self, inflight: _InFlight, parsed: ParsedDecision, now: datetime
    ) -> None:
        """What makes a decision resumable (§3.1): the stored raw response.

        Both callers — ``_persist_pending_response`` (the normal path) and
        ``salvage_shutdown`` (the shutdown window) — land the one record shape
        through ``repo.store_pending_response`` (issue #181).
        """
        with self._db.transaction() as conn:
            repo.store_pending_response(
                conn, inflight.attempt_id, parsed.raw_response, timestamp=now
            )

    def _gate(self, now: datetime) -> str | None:
        inflight = self._inflight
        assert inflight is not None and inflight.parsed is not None
        reg = inflight.registration
        if reg is None:
            reg = self._engine.start_plan(inflight.parsed, output_id=inflight.output_id)
            if reg.gate is None:
                # No fresh snapshot: the gate never ran. Hold the parsed decision and
                # retry the gate next tick — never re-ask the AI (§3.1).
                return "pending_market_data"
            # Cache the outcome the moment it exists: from here on the engine may
            # have COMMITTED (and armed) a plan, so a persist failure below must
            # retry the persist against THIS registration — never re-gate.
            inflight.registration = reg
        decision_at = now
        next_at = decision_at + self._cycle_interval
        status = "completed" if inflight.parsed.is_valid else "invalid_output"
        try:
            with self._db.transaction() as conn:
                self._persist_ai_output(conn, now, inflight, reg)
                repo.update_decision_attempt(
                    conn,
                    inflight.attempt_id,
                    status=status,
                    output_id=inflight.output_id,
                    next_decision_at=next_at,
                    error_type=None,
                    error_message=None,
                    timestamp=now,
                )
                repo.upsert_scheduler_state(
                    conn,
                    self._run_id,
                    last_decision_at=decision_at,
                    next_decision_at=next_at,
                    last_input_id=inflight.input_id,
                    last_output_id=inflight.output_id,
                    current_attempt_id=None,
                    updated_at=now,
                )
        except Exception as exc:
            raise _PlanRegisteredPersistError(
                f"audit persist failed after start_plan for {inflight.attempt_id} "
                f"(plan_id={reg.plan_id}); retrying the persist next pump"
            ) from exc
        self._inflight = None
        # A decided cycle — target or unparseable answer — breaks the streak,
        # exactly as it breaks the store query the escalation mirrors. Fed on
        # the success path too, or the log would keep counting across cycles
        # that did decide and claim a blackout ``validate`` cannot see.
        self._note_cycle_outcome(status, None)
        return status

    def _note_cycle_outcome(self, status: str, error_type: str | None) -> None:
        """Feed this cycle's terminal outcome to the issue #50 streak counter."""
        self._no_decision_streak = note_cycle_outcome(
            self._no_decision_streak, status, error_type, run_id=self._run_id
        )

    def _fail_closed(self, error_type: str | None, message: str) -> str:
        """§10.2 fail-closed: record the in-flight cycle as ``api_failed``.

        A retryable (§6.2) failure keeps its ``error_type``; a non-retryable bug
        passes ``None`` (not a §6.2 vocabulary word — the detail rides
        ``error_message``). Either way the position and its SL/TP are held and
        the cycle re-anchors to the next 4h boundary. The fail record is armed
        on ``_inflight.pending_fail`` BEFORE the write, so if the write itself
        raises (double fault), the next pump retries only the write — the cycle
        outcome is already decided and the worker slot is never re-polled.
        """
        assert self._inflight is not None
        if error_type is None:
            # A non-retryable bug, not a §6.2 API failure: log the traceback
            # LOUDLY (a real problem to fix) — callers invoke this from inside
            # their except block, so the active exception rides along.
            logger.exception(
                "live decision cycle %s hit a non-retryable error — failing closed",
                self._inflight.attempt_id,
            )
        # Counted HERE, where the fail record is ARMED — once per cycle —
        # rather than in ``_fail_cycle``, which ``_flush_pending_fail``
        # re-enters on every pump while a failing write keeps it armed. Counted
        # there, one refused cycle whose write kept hitting a locked store
        # would have reached the ERROR threshold within three ticks and claimed
        # "3 consecutive (~12h with no decision)" about a single cycle.
        self._note_cycle_outcome("api_failed", error_type)
        self._inflight.pending_fail = (error_type, message)
        return self._flush_pending_fail()

    def _flush_pending_fail(self) -> str:
        """Write the armed ``api_failed`` record; clear ``_inflight`` on success.

        Raises (to the loop's tick guard — visible safe mode) when the write
        fails, keeping ``_inflight.pending_fail`` armed for the next pump.
        """
        inflight = self._inflight
        assert inflight is not None and inflight.pending_fail is not None
        error_type, message = inflight.pending_fail
        self._fail_cycle(
            inflight.attempt_id, inflight.scheduled_at, error_type=error_type, error_message=message
        )
        self._inflight = None
        return "api_failed"

    def _fail_cycle(
        self,
        attempt_id: str,
        scheduled_at: datetime,
        *,
        error_type: str | None,
        error_message: str,
    ) -> None:
        now = self._clock.now()
        next_at = scheduled_at + self._cycle_interval
        if next_at <= now:
            next_at = now + self._cycle_interval
        logger.warning(
            "live decision cycle %s failed (%s): %s — holding position, retry at %s",
            attempt_id,
            error_type,
            error_message,
            next_at.isoformat(),
        )
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

    # -- audit rows (phase2-data §5 / §7) — assembly shared with PaperScheduler
    # via persistence.audit_rows; the deliberate live/paper differences stay at
    # these call sites (ledger acquisition, remaining_twap_qty, and the output
    # row's mark/equity source), alongside a prologue that is deliberately kept
    # duplicated with the paper side (folding it in would need a runtime
    # persistence → paper import). -------------------------------------------

    def _persist_ai_input(
        self, now: datetime, input_id: str, attempt_id: str, decision_input: DecisionInput
    ) -> None:
        ctx = decision_input.context
        conn = self._db.conn
        # The books the prompt was built from, carried on the input (issue
        # #134) — one read per cycle, shared with the position section. A
        # provider that carries none (a test double, a replay harness) gets the
        # pre-#134 read here, and a missing ledger still fails the way this
        # lane always did.
        books = decision_input.books or read_books(self._db, self._run_id, self._coin)
        if books is None:
            repo.require_current_account_state(conn, self._run_id)  # raises
            raise AssertionError("read_books answered None over a seeded ledger")
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
            # v1 does not yet attribute live fills to their plan, so a running
            # plan's remaining quantity is not truthfully known here; report it as
            # unknown (None) rather than the plan's frozen original total, which
            # would over-state outstanding TWAP exposure to the AI every cycle.
            # Authoritative tracking lands with the PR 6 WS/fill routing.
            remaining_twap_qty=None,
        )

    def _persist_ai_output(self, conn, now: datetime, inflight: _InFlight, reg) -> None:
        gate = reg.gate
        assert gate is not None
        assert inflight.parsed is not None  # _gate only reaches here with a parsed decision
        audit_rows.write_ai_output(
            conn,
            now=now,
            output_id=inflight.output_id,
            input_id=inflight.input_id,
            decision_attempt_id=inflight.attempt_id,
            mode=self._mode,
            run_id=self._run_id,
            symbol=self._coin,
            gate=gate,
            parsed=inflight.parsed,
            mark_price=reg.mark_price,
            account_equity=reg.account_equity,
        )
