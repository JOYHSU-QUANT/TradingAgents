"""§9 live self-managed sliced-TWAP execution engine (PR 5).

The live counterpart of :class:`~..paper.engine.PaperExecutionEngine`: same 30s
tick discipline and the same shared slice/stop math (:mod:`..paper.twap`,
:mod:`..paper.stops`), but it drives REAL exchange orders and never simulates a
fill — fills arrive asynchronously over the WebSocket and are booked by the
:class:`~.fills.LiveFillProcessor`, so the engine only ever READS the position
the fills produce.

One tick (§11.4 single-threaded, SQLite single-writer) does, in order:

1. drain the WS queue → ingest fills / order updates (books update here);
2. refresh the kill switch (§18.2 — must tick every loop, the AI decision runs
   off-thread so this cadence is never blocked);
3. run reconciliation at the §12.2 timings (post-fill, 5-minute heartbeat);
4. detect a settlement (position reached flat) → §10.4 loss guard;
5. sync §17 protection (recompute SL/TP; emergency-close on no-safe-SL);
6. evaluate the §10.3 daily-loss cap;
7. submit any due slice of the active plan (one aggressive-but-bounded IOC).

A decision cycle hands the engine a gated :class:`ParsedDecision` through
:meth:`start_plan`, which re-runs the shared RiskGate against fresh state,
applies the §10.1/§10.5 live checks, and registers a rebalance plan or a
sequential flip's close leg. Slice math is ``twap.build_slice_plan``; each slice
is an IOC limit ±``max_slippage_pct`` (§9.2); an IOC's unfilled remainder is not
re-sent (§9.2 rule 2) and the plan terminates ``expired`` with the residual at
its deadline. ``close`` / ``emergency_close`` never slice (§9.4): a single
aggressive reduce-only IOC.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal, localcontext
from enum import Enum

from ..domains.perp.margin import DECIMAL_CONTEXT
from ..domains.perp.risk_gate import (
    CurrentPositionState,
    RiskConfig,
    RiskGateResult,
    evaluate,
)
from ..domains.perp.target_decision import DecisionConfig, ParsedDecision, TargetSide
from ..paper.clock import Clock, WallClock
from ..paper.engine import AssetSpec
from ..paper.market_feed import SnapshotProvider
from ..paper.stops import round_to_tick
from ..paper.twap import (
    MAX_SLICES,
    SLICE_INTERVAL_SECONDS,
    PlanDisposition,
    Side,
    build_slice_plan,
    rebalance_delta,
    split_flip_budget,
)
from ..persistence import repository as repo
from ..persistence.cloid import cloid_logical
from ..persistence.db import Database
from ..persistence.models import PositionState
from .config import EXCHANGE_MIN_ORDER_NOTIONAL_USDC, LiveConfig
from .loss_guards import LossGuards
from .order_gate import RealOrderGate
from .orders import LiveOrderSubmitter
from .protection import ProtectionManager, ProtectionOutcome
from .safe_mode import REASON_EMERGENCY_CLOSE

__all__ = ["LiveExecutionEngine", "LiveTickResult", "PlanRegistration"]

logger = logging.getLogger(__name__)

# The §12.2 rule 7 heartbeat reconciliation cadence.
_HEARTBEAT = timedelta(minutes=5)
_PLAN_LIFETIME = timedelta(hours=1)

# §9.4: close / emergency_close is an AGGRESSIVE reduce-only IOC — its whole
# purpose is to cut risk immediately, so it must actually clear in the violent
# move that triggered it. A routine slice's ±0.5% band can fail to fill exactly
# then, leaving the position open and (post no-safe-SL) unprotected. This wider
# band keeps the order marketable while still price-bounded (§9.2 rule 1 forbids
# an UNbounded market order); 3% matches the exchange's own market-order slippage
# (§9.5). Never tighter than the routine band.
_EMERGENCY_SLIPPAGE_PCT = Decimal("0.03")

# Per-request market-data timeout. Bounded (never None) so a hung fetch cannot
# stall the tick thread and starve the §18.2 kill-switch refresh.
_MARKET_DATA_TIMEOUT_S = Decimal(10)


@dataclass
class _Leg:
    """The in-flight slice plan (one leg of a rebalance or flip)."""

    plan_id: str
    output_id: str | None
    side: Side
    slice_sizes: tuple[Decimal, ...]
    reduce_only: bool
    order_role: str
    flip_plan_id: str | None
    flip_leg: str | None
    active_from: datetime
    deadline: datetime
    submitted: int = 0  # slices submitted so far

    @property
    def planned(self) -> int:
        return len(self.slice_sizes)


@dataclass
class _PendingFlip:
    """A flip whose open leg waits for the close leg to reach flat (§9.1 rule 5)."""

    flip_plan_id: str
    parsed: ParsedDecision
    output_id: str | None
    deadline: datetime
    open_budget: int


@dataclass
class PlanRegistration:
    """The outcome of :meth:`start_plan` — a plan was built, or an order-less reason."""

    gate: RiskGateResult | None
    plan_id: str | None
    disposition: PlanDisposition | None
    reason: str | None  # no_order_reason / rejection / pending_market_data
    # The gate's sizing inputs, for the ai_outputs audit row — present iff the
    # gate ran (i.e. a fresh snapshot existed; ``None`` on pending_market_data).
    mark_price: Decimal | None = None
    account_equity: Decimal | None = None


class _TickStatus(str, Enum):
    OK = "ok"
    NO_MARKET_DATA = "no_market_data"


@dataclass
class LiveTickResult:
    at: datetime
    status: _TickStatus
    fills_ingested: int = 0
    slices_submitted: int = 0
    protection: ProtectionOutcome | None = None
    reconciled: bool = False
    events: tuple[str, ...] = field(default_factory=tuple)


class LiveExecutionEngine:
    """Drives one live run's §9 sliced execution, one 30s tick at a time."""

    def __init__(
        self,
        *,
        db: Database,
        run_id: str,
        asset: AssetSpec,
        live_config: LiveConfig,
        risk_config: RiskConfig,
        decision_config: DecisionConfig,
        provider: SnapshotProvider,
        submitter: LiveOrderSubmitter,
        gate: RealOrderGate,
        kill_switch,
        safe_mode,
        reconciler,
        protection: ProtectionManager,
        loss_guards: LossGuards,
        fill_processor,
        ws_stream,
        fetch_open_orders,
        fetch_clearinghouse,
        clock: Clock | None = None,
        timeout_seconds: Decimal = _MARKET_DATA_TIMEOUT_S,
    ) -> None:
        self._db = db
        self._run_id = run_id
        self._asset = asset
        self._coin = asset.coin
        self._live = live_config
        self._risk = risk_config
        self._decision = decision_config
        self._provider = provider
        self._submitter = submitter
        self._gate = gate
        self._kill_switch = kill_switch
        self._safe_mode = safe_mode
        self._reconciler = reconciler
        self._protection = protection
        self._loss_guards = loss_guards
        self._fill_processor = fill_processor
        self._ws = ws_stream
        self._fetch_open_orders = fetch_open_orders
        self._fetch_clearinghouse = fetch_clearinghouse
        self._clock = clock or WallClock()
        self._timeout: Decimal = timeout_seconds
        self._slippage = live_config.execution.max_slippage_pct
        self._prefix = live_config.order_owner_prefix
        self._leg: _Leg | None = None
        self._flip: _PendingFlip | None = None
        self._last_reconcile_at: datetime | None = None
        self._was_flat = self._read_position().is_flat
        # Set when a §17.2 emergency close is fired, cleared once the position
        # reaches flat — at which point the run escalates to MANUAL safe mode
        # (§13.5). Deferred to post-flat so the gate never blocks the close itself.
        # v1 LIMITATION (deferred to the PR 6 live-loop restart recovery): this
        # flag is in-memory only. A process restart in the narrow submit→fill
        # window drops the manual escalation for that episode; the reconciler's
        # §12.3 SL-missing check still enters RECOVERABLE safe mode as a partial
        # net. Persisting it (a scheduler_state column) lands with restart recovery.
        self._emergency_close_pending = False
        self._order_seq = repo.max_engine_seq(db.conn, run_id)
        # A restart abandons any dangling in-flight plan: without its in-memory
        # slice sizes the remaining slices cannot be resumed precisely, and the
        # position is protected regardless — the next cycle re-plans from the
        # current position. Mark it terminal so §9.3's "no active plan" holds.
        self._abandon_dangling_plans()
        self._gate.active_slice_plan = False

    # -- construction helpers -------------------------------------------------

    def _abandon_dangling_plans(self) -> None:
        for plan in repo.iter_execution_plans(self._db.conn, self._run_id):
            if plan["status"] == "active":
                with self._db.transaction() as conn:
                    repo.update_execution_plan(
                        conn,
                        plan["plan_id"],
                        status="expired",
                        status_reason="restart_abandoned",
                        updated_at=self._clock.now(),
                    )

    # -- reads ----------------------------------------------------------------

    def _read_position(self) -> PositionState:
        pos = repo.get_current_position(self._db.conn, self._run_id, self._coin)
        return pos if pos is not None else PositionState.flat(self._coin)

    def _wallet_balance(self) -> Decimal:
        ledger = repo.get_current_account_state(self._db.conn, self._run_id)
        return Decimal(0) if ledger is None else ledger.wallet_balance

    def _equity(self, position: PositionState, mark: Decimal) -> Decimal:
        with localcontext(DECIMAL_CONTEXT):
            unrealized = Decimal(0)
            if not position.is_flat and position.entry_price is not None:
                unrealized = position.size * (mark - position.entry_price)
            return self._wallet_balance() + unrealized

    def _liquidation_price(self, position: PositionState) -> Decimal | None:
        # The exchange-reported liq price is the live truth source; the reconciler
        # mirrors clearinghouse into current_positions. When absent (a fresh
        # position not yet snapshotted) the SL band falls back to the entry-based
        # band (stops.py handles ``liquidation_price=None``).
        pos = repo.get_current_position(self._db.conn, self._run_id, self._coin)
        return getattr(pos, "liquidation_price", None) if pos is not None else None

    def has_active_work(self) -> bool:
        """Whether the monitor must keep ticking (position, plan, or pending flip)."""
        return not self._read_position().is_flat or self._leg is not None or self._flip is not None

    def liquidation_price(self, position: PositionState, mark: Decimal) -> Decimal | None:
        """The exchange-reported liquidation estimate for the audit row (§12.1)."""
        return self._liquidation_price(position)

    def account_equity(self, position: PositionState, mark: Decimal) -> Decimal:
        """Reconciled account equity = wallet balance + unrealized PnL at ``mark``."""
        return self._equity(position, mark)

    # -- the tick -------------------------------------------------------------

    def tick(self) -> LiveTickResult:
        """One 30s live tick (§11.4). Never simulates a fill — books move on WS."""
        now = self._clock.now()
        events: list[str] = []

        fills = self._drain_ws(now, events)
        # §18.2: refresh the kill switch every loop; the AI decision is off-thread
        # so this cadence is never blocked by a multi-minute cycle.
        self._kill_switch.tick()

        # §12.2: reconcile after any fill ingest, and on the 5-minute heartbeat.
        reconciled = False
        if fills > 0:
            reconciled = self._reconcile("fill") or reconciled
        if self._last_reconcile_at is None or now - self._last_reconcile_at >= _HEARTBEAT:
            reconciled = self._reconcile("heartbeat") or reconciled

        # §10.4: a position that reached flat this tick settles one segment.
        self._detect_settlement(now)

        snap_result = self._provider.fetch(
            self._coin, requested_at=now, timeout_seconds=self._timeout
        )
        if not snap_result.is_valid:
            # No fresh mark: hold protection and slices this tick (§10.2 fail-closed
            # posture — the books and kill switch already advanced above).
            return LiveTickResult(
                at=now,
                status=_TickStatus.NO_MARKET_DATA,
                fills_ingested=fills,
                reconciled=reconciled,
                events=tuple(events),
            )
        snap = snap_result.snapshot
        assert snap is not None
        position = self._read_position()

        # §17 protection sync (recompute SL/TP; emergency close on no-safe-SL).
        protection = self._sync_protection(position, snap.mark_price, now, events)

        # §10.3 daily-loss cap (unrealized included).
        self._loss_guards.evaluate_daily_loss(
            account_equity=self._equity(position, snap.mark_price), now=now
        )

        # §9: submit any due slice(s) of the active plan.
        submitted = self._submit_due_slices(snap.mid_price, now, events)
        self._maybe_expire_plan(now, events)
        self._maybe_advance_flip(snap, now, events)

        return LiveTickResult(
            at=now,
            status=_TickStatus.OK,
            fills_ingested=fills,
            slices_submitted=submitted,
            protection=protection,
            reconciled=reconciled,
            events=tuple(events),
        )

    # -- tick steps -----------------------------------------------------------

    def _drain_ws(self, now: datetime, events: list[str]) -> int:
        """Empty the WS queue into the fill processor (§11.4 rule 2)."""
        drained = self._ws.drain()
        applied = 0
        for message in drained:
            try:
                results = self._fill_processor.ingest_message(message)
            except Exception:  # noqa: BLE001 — one bad message must not stall the loop
                logger.exception("WS message ingest failed; message dropped from this tick")
                continue
            applied += sum(1 for r in results if r.outcome.value == "applied")
        if applied:
            events.append(f"fills_applied:{applied}")
        return applied

    def _reconcile(self, trigger: str) -> bool:
        """Run a §12.2 reconciliation pass and drive the safe-mode machine."""
        self._last_reconcile_at = self._clock.now()
        report = self._reconciler.reconcile_and_apply(
            trigger,
            safe_mode=self._safe_mode,
            ws_restored=not self._ws.is_stale(),
            kill_switch_active=not self._kill_switch.stop_new_orders,
        )
        return report.clean

    def _detect_settlement(self, now: datetime) -> None:
        """§10.4: record a settlement when the position transitions to flat."""
        is_flat = self._read_position().is_flat
        if is_flat and not self._was_flat:
            self._loss_guards.record_settlement(wallet_balance=self._wallet_balance(), now=now)
            if self._emergency_close_pending:
                # §13.5: a §17.2 emergency close has now reached flat — escalate to
                # MANUAL safe mode so a human confirms before trading resumes (the
                # repeated SL failure that forced the close is a real problem).
                # Done HERE, post-flat, so the gate's manual-safe-mode block never
                # blocked the close order itself.
                self._safe_mode.enter(
                    "manual",
                    REASON_EMERGENCY_CLOSE,
                    detail="§17.2 SL-repair-exhausted emergency close reached flat (§13.5)",
                )
                self._emergency_close_pending = False
        self._was_flat = is_flat

    def _sync_protection(
        self, position: PositionState, mark: Decimal, now: datetime, events: list[str]
    ) -> ProtectionOutcome:
        outcome = self._protection.sync(
            position=position,
            liquidation_price=self._liquidation_price(position),
            mark=mark,
            plan_active=self._leg is not None,
        )
        if outcome is ProtectionOutcome.NEEDS_EMERGENCY_CLOSE:
            self._emergency_close(position, mark, now, events)
        return outcome

    def _submit_due_slices(self, mid: Decimal, now: datetime, events: list[str]) -> int:
        leg = self._leg
        if leg is None:
            return 0
        due = self._due_count(leg, now)
        submitted = 0
        while leg.submitted < due:
            idx = leg.submitted
            leg.submitted += 1  # one slice per tick advances the cursor even on reject
            size = leg.slice_sizes[idx]
            if self._submit_slice(leg, idx, size, mid, now):
                submitted += 1
                events.append(f"slice:{leg.plan_id}:{idx}")
        if leg.submitted >= leg.planned:
            self._terminate_leg(leg, now, "completed", events)
        return submitted

    def _due_count(self, leg: _Leg, now: datetime) -> int:
        if now < leg.active_from:
            return 0
        if leg.planned == 1:
            return 1  # a single-slice plan fires immediately (PAPER_MARKET shape)
        elapsed = (now - leg.active_from).total_seconds()
        return min(leg.planned, 1 + int(elapsed // SLICE_INTERVAL_SECONDS))

    def _submit_slice(
        self, leg: _Leg, idx: int, size: Decimal, mid: Decimal, now: datetime
    ) -> bool:
        limit_price = self._slice_limit(mid, leg.side)
        order_id = self._next_order_id(leg.order_role)
        logical = cloid_logical(
            prefix=self._prefix,
            run_id=self._run_id,
            symbol=self._coin,
            output_id=leg.output_id or "na",
            plan_id=leg.plan_id,
            leg=leg.flip_leg or "na",
            slice_index=idx,
            order_role=leg.order_role,
        )
        try:
            outcome = self._submitter.submit_ioc_limit(
                order_id=order_id,
                coin=self._coin,
                side=leg.side.value,
                size=size,
                limit_price=limit_price,
                cloid_logical=logical,
                order_role=leg.order_role,
                reduce_only=leg.reduce_only,
                output_id=leg.output_id,
                flip_plan_id=leg.flip_plan_id,
                flip_leg=leg.flip_leg,
            )
        except Exception:  # noqa: BLE001 — a submit failure is recorded by the submitter
            logger.exception("slice %s[%d] submit raised", leg.plan_id, idx)
            return False
        return outcome.outcome.value != "rejected"

    def _slice_limit(self, mid: Decimal, side: Side, *, slippage: Decimal | None = None) -> Decimal:
        """§9.2: an IOC limit ±slippage — marketable, slippage-bounded.

        Defaults to the routine ``max_slippage_pct``; ``_emergency_close`` passes
        the wider §9.4 aggressive band so a de-risking IOC actually clears.
        """
        slip = self._slippage if slippage is None else slippage
        is_buy = side is Side.BUY
        with localcontext(DECIMAL_CONTEXT):
            limit = mid * (1 + slip) if is_buy else mid * (1 - slip)
        return round_to_tick(limit, self._asset.tick_size, up=is_buy)

    # -- plan build (start_plan) ----------------------------------------------

    def start_plan(
        self,
        parsed: ParsedDecision,
        *,
        output_id: str | None = None,
        flip_open: _PendingFlip | None = None,
    ) -> PlanRegistration:
        """Gate a decision against fresh state and register its slice plan (§9).

        Re-runs the shared RiskGate, applies the §10.1/§10.5 live checks and the
        full §4.1 gate, then builds a rebalance plan or a sequential flip's close
        leg. Never re-asks the AI; returns an order-less reason when the gate or
        a live check declines. ``flip_open`` is set only when advancing a flip's
        second (open) leg — it makes the resulting plan share the flip's one
        §9.1-rule-5 envelope/budget and carry its audit linkage.
        """
        now = self._clock.now()
        snap_result = self._provider.fetch(
            self._coin, requested_at=now, timeout_seconds=self._timeout
        )
        if not snap_result.is_valid:
            return PlanRegistration(None, None, None, "pending_market_data")
        snap = snap_result.snapshot
        assert snap is not None
        position = self._read_position()
        equity = self._equity(position, snap.mark_price)
        current = self._current_position_state(position, snap.mark_price, equity)
        gate = evaluate(
            parsed,
            account_equity=equity,
            current=current,
            risk=self._risk,
            decision_cfg=self._decision,
        )
        self._gate.risk_gate_approved = gate.order_created
        # The gate's sizing inputs (mark / equity) ride EVERY post-snapshot
        # outcome onto the ai_outputs audit row — filled once, here.
        reg = self._gate_and_build(parsed, gate, position, snap, output_id, flip_open)
        return replace(reg, mark_price=snap.mark_price, account_equity=equity)

    def _gate_and_build(
        self, parsed, gate, position, snap, output_id, flip_open=None
    ) -> PlanRegistration:
        if not gate.order_created:
            return PlanRegistration(gate, None, None, gate.no_order_reason)
        assert gate.target_notional is not None and gate.target_signed_notional is not None

        # §10.1 hard notional cap.
        if self._loss_guards.notional_exceeds_cap(gate.target_notional):
            self._gate.risk_gate_approved = False
            return PlanRegistration(gate, None, None, "max_notional_usdc")

        # §10.5 max open orders: count bot-owned exchange orders and refuse a new
        # plan at/over the cap. A read failure is fail-closed — an unknowable count
        # must not wave a new order through — so treat it as at-cap. A genuine
        # breach also triggers a reconciliation (an abnormal signal on a
        # single-symbol run); a read failure does not (its own reads just failed).
        open_orders = self._safe_fetch_open_orders()
        if open_orders is None or self._loss_guards.max_open_orders_reached(open_orders):
            if open_orders is not None:
                self._reconcile("mismatch")
            self._gate.risk_gate_approved = False
            return PlanRegistration(gate, None, None, "max_open_orders")

        # §4.1 full new-target gate (wire + decision conditions in one place).
        reason = self._gate.check_new_target(self._coin)
        if reason is not None:
            return PlanRegistration(gate, None, None, reason)

        is_flip = (
            position.size != 0
            and gate.target_side in (TargetSide.LONG, TargetSide.SHORT)
            and gate.target_signed_notional * position.size < 0
        )
        if is_flip:
            return self._start_flip(parsed, gate, position, snap, output_id)
        return self._start_rebalance(gate, position, snap, output_id, flip_open)

    def _start_rebalance(self, gate, position, snap, output_id, flip_open=None) -> PlanRegistration:
        delta = rebalance_delta(
            target_signed_notional=gate.target_signed_notional,
            mark_price=snap.mark_price,
            position_size=position.size,
        )
        if delta.side is None:
            return PlanRegistration(gate, None, None, "zero_delta")
        reduce_only = position.size != 0 and delta.signed_delta_size * position.size < 0
        role = "rebalance" if position.size != 0 else "entry"
        # A flip's open leg shares the flip's ONE 1-hour envelope and slice budget
        # (§9.1 rule 5) and carries the flip audit linkage (flip_plan_id / leg);
        # an ordinary rebalance gets the full grid and its own fresh deadline.
        max_slices = flip_open.open_budget if flip_open is not None else MAX_SLICES
        plan = build_slice_plan(
            delta.raw_total_qty,
            side=delta.side,
            qty_step=self._asset.qty_step,
            min_notional=EXCHANGE_MIN_ORDER_NOTIONAL_USDC,
            mid=snap.mid_price,
            max_slices=max_slices,
        )
        return self._register_leg(
            plan,
            gate=gate,
            output_id=output_id,
            reduce_only=reduce_only,
            order_role=role,
            flip_plan_id=flip_open.flip_plan_id if flip_open is not None else None,
            flip_leg="open" if flip_open is not None else None,
            deadline=flip_open.deadline if flip_open is not None else None,
        )

    def _start_flip(self, parsed, gate, position, snap, output_id) -> PlanRegistration:
        close_qty = abs(position.size)
        with localcontext(DECIMAL_CONTEXT):
            open_qty = abs(gate.target_signed_notional / snap.mark_price)
        close_budget, open_budget = split_flip_budget(close_qty, open_qty)
        flip_plan_id = self._next_order_id("flip")
        deadline = self._clock.now() + _PLAN_LIFETIME
        self._flip = _PendingFlip(
            flip_plan_id=flip_plan_id,
            parsed=parsed,
            output_id=output_id,
            deadline=deadline,
            open_budget=open_budget,
        )
        close_side = Side.SELL if position.size > 0 else Side.BUY
        plan = build_slice_plan(
            close_qty,
            side=close_side,
            qty_step=self._asset.qty_step,
            min_notional=EXCHANGE_MIN_ORDER_NOTIONAL_USDC,
            mid=snap.mid_price,
            max_slices=close_budget,
        )
        return self._register_leg(
            plan,
            gate=gate,
            output_id=output_id,
            reduce_only=True,
            order_role="rebalance",
            flip_plan_id=flip_plan_id,
            flip_leg="close",
            deadline=deadline,
        )

    def _register_leg(
        self, plan, *, gate, output_id, reduce_only, order_role, flip_plan_id, flip_leg, deadline
    ) -> PlanRegistration:
        now = self._clock.now()
        plan_id = self._next_order_id("plan")
        if self._flip is not None and flip_plan_id != self._flip.flip_plan_id:
            self._flip = None  # a fresh non-flip decision supersedes a pending flip
        if not plan.is_executable:
            with self._db.transaction() as conn:
                repo.insert_execution_plan(
                    conn,
                    plan_id=plan_id,
                    run_id=self._run_id,
                    symbol=self._coin,
                    status="rejected",
                    created_at=now,
                    output_id=output_id,
                    flip_plan_id=flip_plan_id,
                    flip_leg=flip_leg,
                    planned_slices=0,
                    total_qty=plan.total_qty,
                    remaining_qty=plan.total_qty,
                    residual_qty=plan.total_qty,
                    rounding_residual_qty=plan.rounding_residual_qty,
                    status_reason="no_legal_slice",
                )
            if flip_leg == "close":
                self._flip = None
            return PlanRegistration(gate, plan_id, PlanDisposition.REJECT, "no_legal_slice")
        if deadline is None:
            deadline = now + _PLAN_LIFETIME
        with self._db.transaction() as conn:
            repo.insert_execution_plan(
                conn,
                plan_id=plan_id,
                run_id=self._run_id,
                symbol=self._coin,
                status="active",
                created_at=now,
                deadline_at=deadline,
                output_id=output_id,
                flip_plan_id=flip_plan_id,
                flip_leg=flip_leg,
                planned_slices=plan.planned_slices,
                total_qty=plan.total_qty,
                remaining_qty=plan.total_qty,
                residual_qty=Decimal(0),
                rounding_residual_qty=plan.rounding_residual_qty,
            )
        self._leg = _Leg(
            plan_id=plan_id,
            output_id=output_id,
            side=plan.side,
            slice_sizes=plan.slice_sizes,
            reduce_only=reduce_only,
            order_role=order_role,
            flip_plan_id=flip_plan_id,
            flip_leg=flip_leg,
            active_from=now,
            deadline=deadline,
        )
        self._gate.active_slice_plan = True
        return PlanRegistration(gate, plan_id, plan.disposition, None)

    # -- plan lifecycle -------------------------------------------------------

    def _terminate_leg(self, leg: _Leg, now: datetime, status: str, events: list[str]) -> None:
        if self._leg is not leg:
            return
        with self._db.transaction() as conn:
            repo.update_execution_plan(
                conn,
                leg.plan_id,
                status=status,  # completed / expired / canceled — all terminal
                status_reason=status,
                # v1 does NOT attribute live fills to their plan: fills.py ingests
                # without a plan_id (the orders table carries none), so nothing ever
                # decrements execution_plans.remaining_qty and the true unfilled
                # residual is not yet knowable here. Record NULL ("unknown") rather
                # than the frozen planned-total, which would falsely claim the whole
                # plan went unfilled. Authoritative per-plan fill tracking (and a real
                # residual) lands with the PR 6 WS/fill routing.
                residual_qty=None,
                updated_at=now,
            )
        self._leg = None
        self._gate.active_slice_plan = self._flip is not None
        events.append(f"plan_terminal:{leg.plan_id}:{status}")

    def _maybe_expire_plan(self, now: datetime, events: list[str]) -> None:
        leg = self._leg
        if leg is not None and now >= leg.deadline:
            self._terminate_leg(leg, now, "expired", events)

    def _maybe_advance_flip(self, snap, now: datetime, events: list[str]) -> None:
        """§9.1 rule 5: once the close leg is terminal and the position is flat,
        re-gate and open the flip's second leg — or abandon the flip at its
        envelope deadline if the close leg could not bring the position to flat."""
        if self._flip is None or self._leg is not None:
            return
        if not self._read_position().is_flat:
            # The close leg is terminal but the position is not flat — an IOC
            # underfill left residual. Wait within the flip envelope; at its
            # deadline, ABANDON the flip so a stuck pending flip can never block
            # new plans forever (its deadline was otherwise never checked — the
            # open leg would wait for a flat that never arrives). The position
            # stays as-is; the next cycle re-gates from current state.
            if now >= self._flip.deadline:
                flip_id = self._flip.flip_plan_id
                self._flip = None
                self._gate.active_slice_plan = False
                logger.warning(
                    "flip %s abandoned at deadline — close leg left the position "
                    "non-flat; next cycle re-gates from current state",
                    flip_id,
                )
                events.append(f"flip_abandoned:{flip_id}")
            return
        flip = self._flip
        if now >= flip.deadline:
            # The open leg could not be gated within the flip's envelope (a
            # transient decline every tick until here, or a persistent one) —
            # abandon it; the next cycle re-gates from current state.
            self._flip = None
            self._gate.active_slice_plan = False
            logger.warning(
                "flip %s abandoned at deadline — the open leg could not be gated "
                "in time; next cycle re-gates from current state",
                flip.flip_plan_id,
            )
            events.append(f"flip_abandoned:{flip.flip_plan_id}")
            return
        # Re-gate the open leg (fresh state, never re-ask the AI) with the flip's
        # shared budget/deadline and audit linkage (§9.1 rule 5). active_slice_plan
        # must be down for check_new_target to admit it; _register_leg raises it
        # again on success.
        self._gate.active_slice_plan = False
        reg = self.start_plan(flip.parsed, output_id=flip.output_id, flip_open=flip)
        if self._leg is not None:
            # The open leg registered — the flip is complete.
            self._flip = None
            events.append(f"flip_open:{flip.flip_plan_id}:{reg.plan_id}")
        elif reg.reason in ("no_legal_slice", "zero_delta"):
            # A STRUCTURAL decline won't change within the envelope (the open leg is
            # too small to slice, or there is nothing left to open) — abandon now
            # rather than re-inserting a rejected plan row every tick to the deadline.
            self._flip = None
            logger.warning(
                "flip %s open leg not executable (%s) — abandoned",
                flip.flip_plan_id,
                reg.reason,
            )
            events.append(f"flip_abandoned:{flip.flip_plan_id}")
        else:
            # A TRANSIENT decline (pending_market_data, a momentary cap / gate line):
            # HOLD the pending flip and retry next tick within the envelope rather
            # than abandoning the second leg on a single blip. Keep active_slice_plan
            # raised so a fresh AI target cannot slip in mid-flip.
            self._gate.active_slice_plan = True
            events.append(f"flip_open_pending:{flip.flip_plan_id}:{reg.reason}")

    # -- emergency close (§9.4 aggressive reduce-only IOC) --------------------

    def _emergency_close(self, position, mark: Decimal, now: datetime, events: list[str]) -> None:
        """§17.2 / §9.4: close the whole position with one aggressive reduce-only IOC.

        Cancels any in-flight plan AND any pending flip first (building — or
        re-opening — exposure while trying to exit is self-defeating), then
        submits a single full-size IOC. Clearing the pending flip is what stops
        ``_maybe_advance_flip`` from re-opening the flip's ORIGINAL target off a
        stale decision once this close reaches flat.
        """
        if self._leg is not None:
            self._terminate_leg(self._leg, now, "canceled", events)
        if self._flip is not None:
            flip_id = self._flip.flip_plan_id
            self._flip = None
            events.append(f"flip_abandoned:{flip_id}")
        self._gate.active_slice_plan = False
        with localcontext(DECIMAL_CONTEXT):
            size = abs(position.size)
        if size <= 0:
            return
        close_side = Side.SELL if position.size > 0 else Side.BUY
        # §9.4 aggressive band: at least the emergency slippage, never tighter
        # than the routine one — so it clears even in the move that triggered it.
        limit_price = self._slice_limit(
            mark, close_side, slippage=max(self._slippage, _EMERGENCY_SLIPPAGE_PCT)
        )
        order_id = self._next_order_id("emergency_close")
        logical = cloid_logical(
            prefix=self._prefix,
            run_id=self._run_id,
            symbol=self._coin,
            output_id="na",
            plan_id=order_id,
            leg="na",
            slice_index=0,
            order_role="emergency_close",
        )
        try:
            self._submitter.submit_ioc_limit(
                order_id=order_id,
                coin=self._coin,
                side=close_side.value,
                size=size,
                limit_price=limit_price,
                cloid_logical=logical,
                order_role="emergency_close",
                reduce_only=True,
            )
        except Exception:  # noqa: BLE001 — recorded by the submitter; reconciliation catches a miss
            logger.exception("emergency close submit raised for %s", self._run_id)
        # §13.5: escalate to MANUAL safe mode once this close reaches flat (handled
        # in _detect_settlement, post-flat, so the gate never blocks the close).
        # Set even if the submit raised — the submitter recorded it and the close is
        # retried next tick; the escalation waits for a confirmed flat. Set BEFORE the
        # (non-critical) audit write below, so a DB miss on that write can never drop
        # this safety escalation.
        self._emergency_close_pending = True
        events.append("emergency_close")
        # §17 audit (best-effort): a §17.2 close was triggered. Guarded so a write
        # miss logs but never aborts the §13.5 escalation set just above.
        try:
            with self._db.transaction() as conn:
                repo.insert_protection_order_event(
                    conn,
                    run_id=self._run_id,
                    event_type="emergency_close_triggered",
                    symbol=self._coin,
                    order_id=order_id,
                    detail=f"§17.2 aggressive reduce-only IOC size={size} limit={limit_price}",
                    timestamp=now,
                )
        except Exception:  # noqa: BLE001 — the audit write must not abort the §13.5 escalation
            logger.exception("failed to record emergency_close_triggered event")

    # -- helpers --------------------------------------------------------------

    def _current_position_state(
        self, position: PositionState, mark: Decimal, equity: Decimal
    ) -> CurrentPositionState:
        if position.is_flat:
            return CurrentPositionState.flat()
        with localcontext(DECIMAL_CONTEXT):
            signed = position.size * mark
            notional = abs(signed)
            margin_used = notional / self._risk.leverage
            margin_pct = margin_used / equity * 100 if equity > 0 else None
        return CurrentPositionState(
            side=TargetSide.LONG if position.size > 0 else TargetSide.SHORT,
            signed_notional=signed,
            margin_pct=margin_pct,
            leverage=self._risk.leverage,
        )

    def _safe_fetch_open_orders(self):
        """The bot's open orders, or ``None`` when the read failed (fail-closed).

        A fabricated over-cap list cannot express "unknown": ``count_bot_open_orders``
        only counts registry-known str cloids, so a sentinel list of ``{"cloid": None}``
        reads as count 0 (fail-OPEN — the opposite of the intent). Signal the failure
        with ``None`` and let the caller treat an unknowable count as at-cap.
        """
        try:
            result = self._fetch_open_orders()
        except Exception:  # noqa: BLE001 — a read failure must not size an order past the cap
            logger.exception("open-orders read failed; treating as at-cap (fail-closed)")
            return None
        if not isinstance(result, list):
            # A malformed, non-raising response (schema drift, an error envelope,
            # None) is just as unknowable as a raised failure: count_bot_open_orders
            # would read a non-list as 0 (fail-OPEN) and wave a new order past the
            # §10.5 cap. Signal the failure with None so the caller treats it as
            # at-cap, exactly like the exception path.
            logger.warning(
                "open-orders read returned a non-list (%s); treating as at-cap (fail-closed)",
                type(result).__name__,
            )
            return None
        return result

    def _next_order_id(self, tag: str) -> str:
        self._order_seq += 1
        return f"{self._run_id}:{tag}:{self._order_seq}"
