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

import json
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, localcontext

from ..domains.perp.margin import DECIMAL_CONTEXT
from ..domains.perp.risk_gate import RiskConfig
from ..domains.perp.target_decision import ParsedDecision
from ..paper import accounting
from ..paper.clock import Clock, WallClock
from ..paper.engine import AssetSpec
from ..paper.scheduler import (
    CYCLE_INTERVAL,
    DecisionInput,
    DecisionProvider,
    RetryableDecisionError,
    parse_instant,
)
from ..persistence import ids, repository as repo
from ..persistence.db import Database
from ..persistence.models import PositionState

__all__ = ["LiveDecisionDriver", "LiveDecisionWorker"]

logger = logging.getLogger(__name__)


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


@dataclass
class _InFlight:
    attempt_id: str
    input_id: str
    output_id: str
    scheduled_at: datetime
    parsed: ParsedDecision | None = None  # set once the worker returns, gate pending


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
        self._engine = engine
        self._worker = worker
        self._provider = provider
        self._clock = clock or WallClock()
        self._cycle_interval = cycle_interval
        self._mode = mode
        self._inflight: _InFlight | None = None

    def pump(self) -> str | None:
        """Advance the decision cycle one non-blocking step; return an event tag."""
        now = self._clock.now()
        if self._worker.busy:
            return None
        if self._inflight is not None:
            # One guard for the whole in-flight lifecycle (collect + gate): ANY error
            # advancing an already-started cycle fails it CLOSED and clears _inflight,
            # so a bug in poll() / start_plan / persist can never wedge the driver — a
            # stuck _inflight either stalls the loop forever (the empty poll slot reads
            # as "not ready", C1) or crash-loops the gate every tick (C2). A retryable
            # (§6.2) failure keeps its error_type; anything else is a non-retryable bug
            # that still fails closed. _start owns its own pre-inflight failure (it has
            # no _inflight yet, and its attempt id/schedule are still local).
            try:
                if self._inflight.parsed is None:
                    return self._collect(now)
                return self._gate(now)
            except RetryableDecisionError as exc:
                self._fail(self._inflight.attempt_id, self._inflight.scheduled_at, exc)
                self._inflight = None
                return "api_failed"
            except Exception as exc:  # noqa: BLE001 — a bug must still fail the cycle closed
                self._fail_internal(self._inflight.attempt_id, self._inflight.scheduled_at, exc)
                self._inflight = None
                return "api_failed"
        if self._due(now):
            return self._start(now)
        return None

    # -- cycle steps ----------------------------------------------------------

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
            self._fail(attempt_id, scheduled_at, exc)
            return "api_failed"
        except Exception as exc:  # noqa: BLE001 — a bug must fail the cycle CLOSED, never wedge it
            # The attempt row is already `in_progress`; leaving it unresolved with
            # next_decision_at in the past crash-loops every tick on the duplicate
            # attempt_id. Fail it closed so the driver recovers next cycle.
            self._fail_internal(attempt_id, scheduled_at, exc)
            return "api_failed"
        self._inflight = _InFlight(attempt_id, input_id, output_id, scheduled_at)
        self._worker.submit(decision_input)
        return "cycle_started"

    def _collect(self, now: datetime) -> str | None:
        # Failure handling lives in pump()'s in-flight guard: poll() re-raising a
        # retryable (§6.2) or a non-retryable worker error both land there, so this
        # step is pure happy-path.
        inflight = self._inflight
        assert inflight is not None
        parsed = self._worker.poll()
        if parsed is None:
            return None  # not ready (worker still settling)
        # Persist the response before gating (§3.1: a crash resumes from stored
        # text, never a second AI call).
        with self._db.transaction() as conn:
            repo.update_decision_attempt(
                conn, inflight.attempt_id, pending_raw_response=parsed.raw_response, timestamp=now
            )
        inflight.parsed = parsed
        return self._gate(now)

    def _gate(self, now: datetime) -> str | None:
        inflight = self._inflight
        assert inflight is not None and inflight.parsed is not None
        reg = self._engine.start_plan(inflight.parsed, output_id=inflight.output_id)
        if reg.gate is None:
            # No fresh snapshot: the gate never ran. Hold the parsed decision and
            # retry the gate next tick — never re-ask the AI (§3.1).
            return "pending_market_data"
        decision_at = now
        next_at = decision_at + self._cycle_interval
        status = "completed" if inflight.parsed.is_valid else "invalid_output"
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
                pending_raw_response=None,
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
        self._inflight = None
        return status

    def _fail(self, attempt_id: str, scheduled_at: datetime, exc: RetryableDecisionError) -> None:
        """§10.2 fail-closed for a retryable (§6.2) failure — hold, re-anchor, retry."""
        self._fail_cycle(
            attempt_id, scheduled_at, error_type=exc.error_type, error_message=exc.message
        )

    def _fail_internal(self, attempt_id: str, scheduled_at: datetime, exc: BaseException) -> None:
        """Fail the cycle CLOSED after a NON-retryable error (a bug, not a §6.2 failure).

        The synchronous paper scheduler lets such an exception propagate and crash;
        the live loop cannot. Bare propagation wedges the driver — a lost
        ``_inflight`` (the loop silently stops deciding, C1) or an unresolved
        ``in_progress`` row that duplicate-crash-loops every tick (C2) — and a hard
        teardown would strip the position's SL/TP. So log it LOUDLY (a real problem
        to fix) but still hold the position and re-anchor to the next cycle, exactly
        like the §10.2 retryable path. ``error_type`` stays NULL: it is not a §6.2
        vocabulary word, and the detail rides ``error_message``.
        """
        logger.exception(
            "live decision cycle %s hit a non-retryable error — failing closed", attempt_id
        )
        self._fail_cycle(
            attempt_id, scheduled_at, error_type=None, error_message=f"non-retryable: {exc!r}"
        )

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

    # -- audit rows (phase2-data §5 / §7) — mirror of PaperScheduler ----------

    def _persist_ai_input(
        self, now: datetime, input_id: str, attempt_id: str, decision_input: DecisionInput
    ) -> None:
        ctx = decision_input.context
        conn = self._db.conn
        ledger = repo.require_current_account_state(conn, self._run_id)
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
        protection = repo.get_position_protection(conn, self._run_id, self._coin)
        stop_loss, take_profit = protection if protection is not None else (None, None)
        active_plans = repo.iter_execution_plans(
            conn, self._run_id, statuses=repo.LIVE_PLAN_STATUSES
        )
        with localcontext(DECIMAL_CONTEXT):
            notional = abs(position.size * ctx.mark_price)
            margin_pct = (
                notional / self._risk.leverage / metrics.account_equity * 100
                if not position.is_flat and metrics.account_equity > 0
                else None
            )
        liq_price = self._engine.liquidation_price(position, ctx.mark_price)
        last_fill = conn.execute(
            "SELECT MAX(timestamp) FROM fills WHERE run_id = ?", (self._run_id,)
        ).fetchone()[0]
        side = "flat" if position.is_flat else ("long" if position.is_long else "short")
        with self._db.transaction() as txn:
            repo.insert_ai_input(
                txn,
                input_id=input_id,
                timestamp=now,
                mode=self._mode,
                run_id=self._run_id,
                symbol=self._coin,
                candle_start=decision_input.candle_start,
                candle_end=decision_input.candle_end,
                mark_price=ctx.mark_price,
                mid_price=ctx.mid_price,
                funding_rate=ctx.funding_rate,
                wallet_balance=ledger.wallet_balance,
                account_equity=metrics.account_equity,
                available_balance=metrics.available_balance,
                realized_pnl=ledger.realized_pnl,
                unrealized_pnl=metrics.unrealized_pnl,
                total_fees=ledger.total_fees,
                net_funding_pnl=ledger.net_funding_pnl,
                effective_leverage=metrics.effective_leverage,
                margin_ratio=metrics.margin_ratio,
                current_position_side=side,
                current_position_size=position.size,
                entry_price=position.entry_price,
                position_notional=notional,
                current_margin_pct=margin_pct,
                configured_leverage=self._risk.leverage,
                estimated_liquidation_price=liq_price,
                stop_loss_price=stop_loss,
                take_profit_price=take_profit,
                active_twap=bool(active_plans),
                # v1 does not yet attribute live fills to their plan, so a running
                # plan's remaining quantity is not truthfully known here; report it as
                # unknown (None) rather than the plan's frozen original total, which
                # would over-state outstanding TWAP exposure to the AI every cycle.
                # Authoritative tracking lands with the PR 6 WS/fill routing.
                remaining_twap_qty=None,
                last_fill_time=last_fill,
                max_target_margin_pct=Decimal(self._risk.max_target_margin_pct),
                input_payload_path=decision_input.input_payload_path,
                input_payload_hash=decision_input.input_payload_hash,
                prompt_version=decision_input.prompt_version,
                model=decision_input.model,
            )
            repo.update_decision_attempt(txn, attempt_id, input_id=input_id, timestamp=now)

    def _persist_ai_output(self, conn, now: datetime, inflight: _InFlight, reg) -> None:
        gate = reg.gate
        assert gate is not None
        assert inflight.parsed is not None  # _gate only reaches here with a parsed decision
        decision = inflight.parsed.decision
        reason = decision.rationale or inflight.parsed.invalid_reason or "(no rationale)"
        repo.insert_ai_output(
            conn,
            output_id=inflight.output_id,
            timestamp=now,
            mode=self._mode,
            run_id=self._run_id,
            input_id=inflight.input_id,
            decision_attempt_id=inflight.attempt_id,
            symbol=self._coin,
            decision_mode=gate.decision_mode.value,
            target_side=None if gate.target_side is None else gate.target_side.value,
            requested_target_margin_pct=gate.requested_target_margin_pct,
            approved_target_margin_pct=gate.approved_target_margin_pct,
            risk_action=gate.risk_action.value,
            risk_reason=gate.risk_reason,
            target_margin=gate.target_margin,
            configured_leverage=gate.configured_leverage,
            target_notional=gate.target_notional,
            target_signed_notional=gate.target_signed_notional,
            current_signed_notional=gate.current_signed_notional,
            delta_notional=gate.delta_notional,
            mark_price=reg.mark_price,
            account_equity=reg.account_equity,
            confidence=gate.confidence,
            decision_reason=reason,
            key_risks=json.dumps(list(decision.key_risks), ensure_ascii=False),
            order_created=gate.order_created,
            no_order_reason=gate.no_order_reason,
        )
