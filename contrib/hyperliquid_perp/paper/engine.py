"""Paper execution engine (execution §1–§5) — the tick-driven orchestrator.

Everything else in ``paper/`` is pure math or a thin persistence seam; this module
is where they compose into the actual paper-trading behaviour. It is driven one
**tick** at a time by its caller (PR4's 30-second monitor loop in production, a
:class:`~.clock.ManualClock`-advancing test otherwise) — the engine never sleeps,
so its logic stays a deterministic function of the injected clock, snapshot
provider, and funding source. A 120-slice / one-hour plan is therefore fully
reproducible in microseconds, and a replay of the same tick sequence rebuilds the
same fills.

Each tick processes the fixed event order of execution §5.3 against **one** shared
snapshot (the monitor and the due slice never double-fetch — §5.5):

    1. update mark / mid from this snapshot
    2. post any funding that came due at/under this snapshot
    3. liquidation / emergency close
    3.5. after a funding post, refresh the SL (the liquidation estimate moved —
         §6.6.1 lists funding among the must-recompute events)
    4. active stop loss
    5. active take profit
    6. if still executable, the due TWAP slice / paper_market fill
    7. (position / PnL / fees / margin move with every fill above)
    8. recompute SL/TP lifecycle, then write the account snapshot

A liquidation / SL / TP that flattens the position immediately cancels the plan's
remaining slices and marks it terminal — the same snapshot never both closes and
re-opens a position (§5.3).

Market-data freshness (§1.1): a scheduled slice needs a fresh snapshot within the
timeout carrying both mid and mark; a failure leaves that slice unexecuted, three
consecutive failures pause the plan (``paused_market_data``), a resumed snapshot
whose mark has crossed the SL trigger fills a ``gap_stop_fill``, and missed slices
are never re-run. A leg whose remaining slices are all exhausted by misses
terminates as ``residual`` immediately (at/past the deadline the ``expired``
label wins instead).

Persistence (PR3 requirement 8): every change caused by one fill — the fill row,
the position, the ledger, the order's filled/remaining/status, and the plan's
remaining/residual — commits in **one** ``db.transaction()`` via
:func:`~.accounting.apply_fill` plus the repository writers. Protection (SL/TP)
lives on ``current_positions`` (the live trigger state); an SL/TP/emergency fill
creates its order row in that same transaction. Funding posts in its own
exactly-once transaction (:func:`~.accounting.record_funding`).
"""

from __future__ import annotations

import functools
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, localcontext
from enum import Enum
from typing import Protocol, runtime_checkable

from ..domains.perp.margin import (
    DECIMAL_CONTEXT,
    MarginSchedule,
    position_notional,
    unrealized_pnl,
)
from ..domains.perp.risk_gate import (
    CurrentPositionState,
    RiskConfig,
    RiskGateResult,
    evaluate,
)
from ..domains.perp.target_decision import DecisionConfig, ParsedDecision, TargetSide
from ..persistence import repository as repo
from ..persistence.db import Database
from ..persistence.ids import fill_id as derive_fill_id, slice_id as derive_slice_id
from ..persistence.models import PositionState, Side
from . import accounting
from .clock import Clock
from .config import PaperTradingConfig
from .fill_model import fill_price
from .liquidation import (
    LIQUIDATION_MODEL_VERSION,
    estimated_liquidation_price,
    maintenance_snapshot,
    price_tick_from_sz_decimals,
)
from .market_feed import SnapshotOutcome, SnapshotProvider, SnapshotResult
from .stops import StopAction, StopConfig, stop_loss_decision, take_profit_price
from .twap import (
    MAX_SLICES,
    SLICE_INTERVAL_SECONDS,
    PlanDisposition,
    build_slice_plan,
    qty_step_from_sz_decimals,
    rebalance_delta,
    split_flip_budget,
)

__all__ = [
    "AssetSpec",
    "EngineHaltedError",
    "FundingSource",
    "PaperExecutionEngine",
    "PlanStartResult",
    "TickEvent",
    "TickResult",
]

logger = logging.getLogger(__name__)


class EngineHaltedError(RuntimeError):
    """The engine fail-stopped after an earlier public call raised.

    An exception escaping mid-write can leave in-memory state (leg counters,
    protection, flip bookkeeping) ahead of the rolled-back DB. Rather than
    auditing every mutation site for commit-ordering, the engine refuses to
    run again after any escaped exception: rebuild it over the run — the
    constructor re-hydrates from the DB, which is the source of truth.
    """


def _fail_stop(method):
    """Halt the engine permanently when a public entry point raises."""

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        self._ensure_not_halted()
        try:
            return method(self, *args, **kwargs)
        except Exception:
            self._halted = True
            raise

    return wrapper


# The one-hour terminal deadline every plan must reach (execution §1.2).
_PLAN_LIFETIME = timedelta(hours=1)
_MODE = "paper"


@runtime_checkable
class FundingSource(Protocol):
    """Supplies the Hyperliquid funding rate for a settlement hour (execution §6.5).

    ``rate_at`` returns the rate as a fraction, or ``None`` when it is not yet
    available (the engine then records a ``pending`` funding event to backfill).
    """

    def rate_at(self, coin: str, funding_timestamp: datetime) -> Decimal | None: ...


@dataclass(frozen=True)
class AssetSpec:
    """The per-asset metadata the engine needs, never hardcoded (execution §1.2 / §6.6.1).

    ``sz_decimals`` fixes the quantity step and price tick; ``margin_schedule`` is
    the exchange's maintenance-margin table (from the ``meta`` response). The two
    derived steps are computed once at construction.
    """

    coin: str
    sz_decimals: int
    margin_schedule: MarginSchedule
    qty_step: Decimal = field(init=False)
    tick_size: Decimal = field(init=False)

    def __post_init__(self) -> None:
        if not self.coin or not self.coin.strip():
            raise ValueError("AssetSpec.coin must be a non-empty string")
        if self.margin_schedule.coin != self.coin:
            raise ValueError(
                f"AssetSpec.margin_schedule is for {self.margin_schedule.coin!r}, not {self.coin!r}"
            )
        object.__setattr__(self, "qty_step", qty_step_from_sz_decimals(self.sz_decimals))
        object.__setattr__(self, "tick_size", price_tick_from_sz_decimals(self.sz_decimals))


class TickEvent(str, Enum):
    """A thing that happened during one tick — the auditable tick outcome."""

    PRICE_UPDATED = "price_updated"
    FUNDING_POSTED = "funding_posted"
    FUNDING_PENDING = "funding_pending"
    LIQUIDATION_CLOSE = "liquidation_close"
    STOP_LOSS_FILL = "stop_loss_fill"
    GAP_STOP_FILL = "gap_stop_fill"
    TAKE_PROFIT_FILL = "take_profit_fill"
    SLICE_FILL = "slice_fill"
    PAPER_MARKET_FILL = "paper_market_fill"
    SLICE_MISSED = "slice_missed"
    PENDING_MARKET_DATA = "pending_market_data"
    PAUSED = "paused_market_data"
    RESUMED = "resumed"
    PLAN_TERMINAL = "plan_terminal"
    PROTECTION_UPDATED = "protection_updated"
    IDLE = "idle"


class PlanStatus(str, Enum):
    """The live/terminal disposition of the engine's current leg (for TickResult)."""

    ACTIVE = "active"
    TERMINAL = "terminal"


@dataclass(frozen=True)
class TickResult:
    """What one :meth:`PaperExecutionEngine.tick` did — for the caller / tests."""

    at: datetime
    outcome: SnapshotOutcome
    events: tuple[TickEvent, ...]
    position_size: Decimal
    plan_status: PlanStatus | None

    def has(self, event: TickEvent) -> bool:
        return event in self.events


@dataclass(frozen=True)
class PlanStartResult:
    """Outcome of :meth:`PaperExecutionEngine.start_plan`.

    Three legal shapes (guarded below, like the sibling result dataclasses):
    no plan built (``disposition is None`` → ``plan_id is None``, a ``reason``);
    a rejected plan (``disposition is REJECT`` → a ``plan_id`` *and* a ``reason``);
    an executing plan (``PAPER_MARKET``/``TWAP`` → a ``plan_id``, no ``reason``).

    ``gate`` is ``None`` exactly when no gate ran at all — the
    ``pending_market_data`` shape: without a fresh snapshot the RiskGate is never
    evaluated (its notionals would be priced at a fabricated mark), the decision
    is not persisted, and the caller retries when data returns.

    ``mark_price`` / ``account_equity`` are the gate's own sizing inputs (the
    fresh snapshot's mark and the equity valued at it), carried out so PR4's
    scheduler can persist the ``ai_outputs`` audit row (phase2-data §7:
    決策當下 sizing 輸入) with exactly the numbers the gate used — never a
    re-fetched approximation. Present exactly when the gate ran.
    """

    gate: RiskGateResult | None
    plan_id: str | None
    disposition: PlanDisposition | None
    reason: str | None  # no_order_reason / rejected / pending_market_data
    mark_price: Decimal | None = None
    account_equity: Decimal | None = None

    def __post_init__(self) -> None:
        if self.gate is None and (self.disposition is not None or self.reason is None):
            raise ValueError(
                "a gateless PlanStartResult is the pending_market_data shape: "
                "no disposition, a reason"
            )
        if ((self.mark_price is None) != (self.gate is None)) or (
            (self.account_equity is None) != (self.gate is None)
        ):
            raise ValueError(
                "PlanStartResult.mark_price / account_equity must be present "
                "exactly when a gate ran (they are the gate's sizing inputs)"
            )
        if self.disposition is None:
            if self.plan_id is not None or self.reason is None:
                raise ValueError("a no-plan PlanStartResult carries no plan_id and a reason")
        elif self.disposition is PlanDisposition.REJECT:
            if self.plan_id is None or self.reason is None:
                raise ValueError("a rejected PlanStartResult carries both a plan_id and a reason")
        else:  # PAPER_MARKET / TWAP — an executing plan
            if self.plan_id is None or self.reason is not None:
                raise ValueError("an executing PlanStartResult carries a plan_id and no reason")


# --------------------------------------------------------------------------
# Internal plan / protection state
# --------------------------------------------------------------------------


@dataclass
class _Leg:
    """One executing leg: a rebalance plan, or a flip's close / open leg."""

    plan_id: str
    order_id: str
    output_id: str | None
    side: Side
    disposition: PlanDisposition
    slice_sizes: tuple[Decimal, ...]
    reduce_only: bool
    order_role: str
    flip_plan_id: str | None
    flip_leg: str | None
    active_from: datetime
    deadline: datetime
    consumed: int = 0  # slices whose scheduled time has passed (executed or missed)
    executed: int = 0
    filled_qty: Decimal = Decimal(0)
    terminal: bool = False

    def __post_init__(self) -> None:
        self.validate()  # construction obeys the same invariants as every mutation

    @property
    def planned(self) -> int:
        return len(self.slice_sizes)

    @property
    def remaining_qty(self) -> Decimal:
        with localcontext(DECIMAL_CONTEXT):
            return sum(self.slice_sizes, Decimal(0)) - self.filled_qty

    def validate(self) -> None:
        """Assert the leg's counters/quantities stay consistent after a mutation.

        The mutable analogue of the frozen dataclasses' ``__post_init__`` guards:
        ``_Leg`` is mutated across several call sites, so this backstops a double
        fill or a miscounted slice (which would otherwise silently corrupt
        ``remaining_qty`` / ``residual_qty``) rather than trusting every site.
        (``terminal`` with ``remaining_qty > 0`` is legal — a risk exit cancels a
        partly-filled plan — so it is intentionally not asserted here.)
        """
        if not 0 <= self.executed <= self.consumed <= self.planned:
            raise ValueError(
                f"_Leg counters out of order: executed {self.executed}, "
                f"consumed {self.consumed}, planned {self.planned}"
            )
        with localcontext(DECIMAL_CONTEXT):
            total = sum(self.slice_sizes, Decimal(0))
        if not 0 <= self.filled_qty <= total:
            raise ValueError(f"_Leg.filled_qty {self.filled_qty} not in [0, total {total}]")


@dataclass
class _FlipState:
    """A sequential flip in progress (execution §1.3): close leg then open leg.

    ``deadline`` is the flip's single one-hour envelope — the two legs share the
    execution budget *and* the wall clock (§1.3: 合計 120 slices / 一小時), so the
    open leg inherits this deadline instead of restarting its own hour.
    """

    flip_plan_id: str
    output_id: str | None
    parsed: ParsedDecision
    open_budget: int
    deadline: datetime
    open_started: bool = False

    def __post_init__(self) -> None:
        if not self.flip_plan_id:
            raise ValueError("_FlipState.flip_plan_id must be non-empty")
        if self.open_budget < 1:  # split_flip_budget guarantees each leg >= 1 slice
            raise ValueError(f"_FlipState.open_budget must be >= 1, got {self.open_budget}")


@dataclass
class _Protection:
    """The live SL / TP trigger prices (execution §2 invariants)."""

    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None

    def __post_init__(self) -> None:
        for name, price in (("stop_loss", self.stop_loss), ("take_profit", self.take_profit)):
            if price is not None and price <= 0:
                raise ValueError(f"_Protection.{name} must be > 0, got {price}")


# --------------------------------------------------------------------------
# Engine
# --------------------------------------------------------------------------


class PaperExecutionEngine:
    """Drives one run's paper execution, one tick at a time (execution §1–§5)."""

    def __init__(
        self,
        *,
        db: Database,
        run_id: str,
        asset: AssetSpec,
        clock: Clock,
        provider: SnapshotProvider,
        risk_config: RiskConfig,
        decision_config: DecisionConfig,
        paper_config: PaperTradingConfig,
        stop_config: StopConfig | None = None,
        funding_source: FundingSource | None = None,
    ) -> None:
        self._db = db
        self._run_id = run_id
        self._asset = asset
        self._coin = asset.coin
        self._clock = clock
        self._provider = provider
        self._risk = risk_config
        self._decision = decision_config
        self._paper = paper_config
        self._stop = stop_config or StopConfig()
        self._funding = funding_source

        self._leg: _Leg | None = None
        self._flip: _FlipState | None = None
        self._halted = False
        self._consecutive_md_failures = 0
        self._paused = False
        self._resume_pending_gap_check = False
        self._last_funding_hour: datetime | None = None
        # Constructed over an existing run (crash / restart), the engine must
        # never mint an id that collides with a persisted row, and must not
        # forget a live SL/TP (execution §2). Full restart reconciliation
        # (§1.2 steps — canceled_restart, residual handling) is PR4's; these two
        # reads are the silent-corruption guards a bare rebuild needs regardless.
        self._order_seq = repo.max_engine_seq(db.conn, run_id)
        protection = repo.get_position_protection(db.conn, run_id, asset.coin)
        self._protection = _Protection(*protection) if protection is not None else _Protection()

    # -- config-derived shortcuts -----------------------------------------

    @property
    def _fee_rate(self) -> Decimal:
        return self._paper.execution.taker_fee_rate

    @property
    def _slippage_bps(self) -> Decimal:
        return self._paper.execution.fill_model.slippage_bps

    @property
    def _min_notional(self) -> Decimal:
        return self._paper.execution.min_notional_usdc

    @property
    def _timeout(self) -> Decimal:
        return Decimal(self._paper.execution.market_monitor.request_timeout_seconds)

    @property
    def _leverage(self) -> Decimal:
        return self._risk.leverage

    # -- state reads -------------------------------------------------------

    def _read_position(self) -> PositionState:
        return repo.get_current_position(
            self._db.conn, self._run_id, self._coin
        ) or PositionState.flat(self._coin)

    def _read_ledger(self):
        ledger = repo.get_current_account_state(self._db.conn, self._run_id)
        if ledger is None:
            raise ValueError(
                f"run {self._run_id!r} has no account state; call accounting.initialize_run first"
            )
        return ledger

    def _equity(self, position: PositionState, mark: Decimal) -> Decimal:
        ledger = self._read_ledger()
        with localcontext(DECIMAL_CONTEXT):
            un = (
                Decimal(0)
                if position.is_flat
                else unrealized_pnl(position.size, mark, position.entry_price)
            )
            return ledger.wallet_balance + un

    def _next_order_id(self, tag: str) -> str:
        self._order_seq += 1
        return f"{self._run_id}:{tag}:{self._order_seq}"

    def _ensure_not_halted(self) -> None:
        if self._halted:
            raise EngineHaltedError(
                f"engine for run {self._run_id!r} halted after an earlier error; "
                "rebuild it over the run to continue"
            )

    def flag_restart_gap(self) -> None:
        """Arm the gap-SL check for the next tick (execution §1.2 step 6).

        A restart is the same blind window as a market-data outage: the mark may
        have crossed the active SL while no process was watching. PR4's restart
        reconciliation calls this after rebuilding the engine over the run, so
        the first tick's SL fill is labelled ``gap_stop_fill`` rather than
        claiming the position closed at the (long-passed) trigger price. The
        flag is one-shot and tick-scoped, exactly like the pause-resume path.
        """
        self._ensure_not_halted()
        self._resume_pending_gap_check = True

    def _best_effort_cycle_snapshot(self, now: datetime, mark: Decimal) -> bool:
        """Write the post-commit cycle-end snapshot, tolerating a write failure.

        These §11.1/§12.1 rows are audit records written AFTER the decision's own
        transaction has committed, so a snapshot write error must NOT halt the
        engine and strand the live SL/TP monitor — it is logged and swallowed and
        the next tick re-snapshots. Deliberately NOT ``@_fail_stop`` (unlike the
        tick-driven/trading writes): a genuine store failure still resurfaces
        fatally at the next trading write, so nothing is hidden for long.
        Returns whether the snapshot was written.

        The guard catches the STORE errors AND the value errors, because both are
        reachable and either one escaping defeats the entire point of this method.
        ``_read_position`` / ``_read_ledger`` rebuild Decimals and dataclasses out of
        stored TEXT — a missing account state raises ``ValueError``, a corrupt cell
        raises ``InvalidOperation`` — and ``_write_snapshots`` runs the account math
        before it writes anything, which can raise either. Caught, the tick loses one
        audit row. Escaping, it kills the process that is the only thing watching the
        live position's SL/TP: exactly the outcome the paragraph above promises cannot
        happen. (A genuine store failure still resurfaces fatally at the next trading
        write, so nothing stays hidden for long.)
        """
        try:
            self._write_snapshots(now, mark, self._read_position())
        except (sqlite3.Error, OSError, ValueError, InvalidOperation):
            logger.warning(
                "cycle-end snapshot write failed at mark %s; skipped, will re-snapshot next tick",
                mark,
                exc_info=True,
            )
            return False
        return True

    def write_cycle_snapshot(self, mark: Decimal) -> bool:
        """Best-effort cycle-end account/position snapshot at the gate's own mark.

        Tick-driven snapshots only fire on material changes; a decision cycle
        that produced no fill (maintain_current, deadband, rejection) would
        otherwise leave no per-cycle record. PR4's scheduler calls this at each
        completed cycle with the decision's own mark price, AFTER the audit
        transaction — a write failure here is non-fatal (see
        :meth:`_best_effort_cycle_snapshot`) so it can never tear down live SL/TP
        monitoring. Returns whether the snapshot was written.
        """
        self._ensure_not_halted()
        if mark <= 0:
            raise ValueError(f"cycle snapshot mark must be > 0, got {mark}")
        return self._best_effort_cycle_snapshot(self._clock.now(), mark)

    def try_write_cycle_snapshot(self) -> bool:
        """Best-effort cycle-end snapshot when no gate mark exists (§11.1).

        An ``api_failed`` cycle terminates without a gate run, so there is no
        decision mark to snapshot at — but data §11.1 still wants a per-cycle
        record. Fetches a fresh snapshot and returns whether one was written:
        skipped (``False``) when market data is unavailable (commonly the same
        outage that failed the decision — never fabricate a mark, execution §6.5)
        or when the write itself fails (non-fatal, as above).
        """
        self._ensure_not_halted()
        now = self._clock.now()
        result = self._provider.fetch(self._coin, requested_at=now, timeout_seconds=self._timeout)
        if not result.is_valid or result.snapshot is None:
            return False
        return self._best_effort_cycle_snapshot(now, result.snapshot.mark_price)

    def has_active_work(self) -> bool:
        """Whether the monitor must keep polling (execution §5.5)."""
        self._ensure_not_halted()  # a halted engine's view may be stale — never poll on it
        if self._read_position().size != 0:
            return True
        if self._leg is not None and not self._leg.terminal:
            return True
        if self._flip is not None:
            return True
        return self._protection.stop_loss is not None or self._protection.take_profit is not None

    @_fail_stop
    def cancel_active_plans(self) -> bool:
        """Cancel the in-flight leg and any pending flip (mid-run halt sweep).

        The mid-run analogue of restart reconciliation's cancel sweep (§1.2
        steps 1–3): entering protection-only mode must also stop the plan the
        halting cycle just started — otherwise it keeps filling for up to an
        hour on books that just failed to verify, and a flip caught between
        legs would register a brand-new order after the halt. SL/TP and the
        monitor stay live; a surviving position regains its TP exactly like
        every other terminal path. Returns whether anything was canceled.
        """
        now = self._clock.now()
        canceled = False
        leg = self._leg
        if leg is not None and not leg.terminal:
            with self._db.transaction() as conn:
                self._write_leg_terminal(
                    conn,
                    leg,
                    now,
                    status="canceled",
                    plan_reason="trading_halted",
                    order_reason="trading_halted",
                )
            canceled = True
        if self._flip is not None:
            # A flip that has not opened its reverse leg never will now —
            # record it exactly like the expiry path (which also restores a
            # surviving position's TP).
            self._record_flip_incomplete("trading_halted")
            canceled = True
        elif canceled:
            self._reconcile_protection_after_terminal()
        return canceled

    # -- liquidation estimate ---------------------------------------------

    def liquidation_price(self, position: PositionState, mark: Decimal) -> Decimal | None:
        """Estimated liquidation price for a non-flat position, or ``None`` (execution §6.6.1).

        Public: the scheduler's ``ai_inputs`` audit row reports the same estimate
        the engine trades on, so both callers share this one derivation.
        """
        if position.is_flat:
            return None
        est = estimated_liquidation_price(
            size=position.size,
            entry_price=position.entry_price,
            mark_price=mark,
            wallet_balance=self._read_ledger().wallet_balance,
            schedule=self._asset.margin_schedule,
            tick_size=self._asset.tick_size,
        )
        return est.price if not est.already_liquidatable else None

    def _is_liquidatable(self, position: PositionState, mark: Decimal) -> bool:
        if position.is_flat:
            return False
        est = estimated_liquidation_price(
            size=position.size,
            entry_price=position.entry_price,
            mark_price=mark,
            wallet_balance=self._read_ledger().wallet_balance,
            schedule=self._asset.margin_schedule,
            tick_size=self._asset.tick_size,
        )
        return est.already_liquidatable

    # -- plan start (execution §1.2 / §1.3 / §6.2) ------------------------

    @_fail_stop
    def start_plan(
        self, parsed: ParsedDecision, *, output_id: str | None = None
    ) -> PlanStartResult:
        """Gate a decision against fresh state and build its execution plan.

        Re-runs the deterministic RiskGate against the current account/position and
        a fresh snapshot (never re-asks the AI), then builds a rebalance plan, a
        sequential flip, or records a no-order outcome. Returns without a plan when
        the gate declines the order or no fresh snapshot is available yet.
        """
        now = self._clock.now()
        result = self._provider.fetch(self._coin, requested_at=now, timeout_seconds=self._timeout)
        if not result.is_valid:
            # No fresh snapshot: run no gate (it would price its notionals at a
            # fabricated mark) and persist nothing; the caller retries when data
            # returns (PR4's scheduler owns the cadence).
            return PlanStartResult(
                gate=None, plan_id=None, disposition=None, reason="pending_market_data"
            )
        snap = result.snapshot
        assert snap is not None  # is_valid guarantees a snapshot (SnapshotResult invariant)
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
        if not gate.order_created:
            return PlanStartResult(
                gate,
                None,
                None,
                gate.no_order_reason,
                mark_price=snap.mark_price,
                account_equity=equity,
            )

        assert gate.target_signed_notional is not None
        is_flip = (
            position.size != 0
            and gate.target_side in (TargetSide.LONG, TargetSide.SHORT)
            and gate.target_signed_notional * position.size < 0
        )
        if is_flip:
            return self._start_flip(
                parsed, gate, position, snap.mark_price, snap.mid_price, output_id, equity
            )
        return self._start_rebalance(
            gate, position, snap.mark_price, snap.mid_price, output_id, equity
        )

    def _current_position_state(
        self, position: PositionState, mark: Decimal, equity: Decimal
    ) -> CurrentPositionState:
        return CurrentPositionState.from_signed_size(
            position.size,
            mark=mark,
            equity=equity,
            leverage=self._leverage,  # paper positions always match configured leverage
        )

    def _start_rebalance(
        self,
        gate: RiskGateResult,
        position: PositionState,
        mark: Decimal,
        mid: Decimal,
        output_id: str | None,
        equity: Decimal,
    ) -> PlanStartResult:
        delta = rebalance_delta(
            target_signed_notional=gate.target_signed_notional,
            mark_price=mark,
            position_size=position.size,
        )
        if delta.side is None:
            return PlanStartResult(
                gate, None, None, "zero_delta", mark_price=mark, account_equity=equity
            )
        reduce_only = position.size != 0 and delta.signed_delta_size * position.size < 0
        role = "rebalance" if position.size != 0 else "entry"
        plan = build_slice_plan(
            delta.raw_total_qty,
            side=delta.side,
            qty_step=self._asset.qty_step,
            min_notional=self._min_notional,
            mid=mid,
            max_slices=MAX_SLICES,
        )
        return self._register_leg(
            plan,
            gate=gate,
            output_id=output_id,
            reduce_only=reduce_only,
            order_role=role,
            flip_plan_id=None,
            flip_leg=None,
            mark=mark,
            equity=equity,
        )

    def _start_flip(
        self,
        parsed: ParsedDecision,
        gate: RiskGateResult,
        position: PositionState,
        mark: Decimal,
        mid: Decimal,
        output_id: str | None,
        equity: Decimal,
    ) -> PlanStartResult:
        close_qty = abs(position.size)
        with localcontext(DECIMAL_CONTEXT):
            open_qty = abs(gate.target_signed_notional / mark)
        close_budget, open_budget = split_flip_budget(close_qty, open_qty)
        flip_plan_id = self._next_order_id("flip")
        # One envelope for the whole flip (§1.3): both legs share the 120-slice
        # budget AND the wall clock, so the open leg inherits this deadline
        # rather than restarting its own hour at close-leg terminal.
        flip_deadline = self._clock.now() + _PLAN_LIFETIME
        self._flip = _FlipState(
            flip_plan_id=flip_plan_id,
            output_id=output_id,
            parsed=parsed,
            open_budget=open_budget,
            deadline=flip_deadline,
        )
        close_side = Side.SELL if position.size > 0 else Side.BUY
        plan = build_slice_plan(
            close_qty,
            side=close_side,
            qty_step=self._asset.qty_step,
            min_notional=self._min_notional,
            mid=mid,
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
            deadline=flip_deadline,
            mark=mark,
            equity=equity,
        )

    def _register_leg(
        self,
        plan,
        *,
        gate: RiskGateResult,
        output_id: str | None,
        reduce_only: bool,
        order_role: str,
        flip_plan_id: str | None,
        flip_leg: str | None,
        deadline: datetime | None = None,
        mark: Decimal,
        equity: Decimal,
    ) -> PlanStartResult:
        now = self._clock.now()
        plan_id = self._next_order_id("plan")
        # A plan from a NEW decision (not a leg of the in-flight flip) replaces
        # that flip's bookkeeping the moment it registers — never earlier: a
        # decision that registers nothing (zero_delta / pending) leaves the flip
        # running, matching the supersede-on-registration rule for the leg itself.
        if self._flip is not None and flip_plan_id != self._flip.flip_plan_id:
            self._flip = None
        if not plan.is_executable:
            # No legal slice fits: record a rejected plan with the whole qty residual
            # (execution §1.2 — never round up to a too-large position).
            rejected_order_id = self._next_order_id("ord")
            with self._db.transaction() as conn:
                self._supersede_active_leg(conn, now)
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
                # §5.2: a validation failure before the fill flow still writes an
                # orders row (status="rejected") — execution_plans is internal,
                # while orders is the exported audit trail. The row never enters
                # fill simulation (no active_from); paper_market is the degenerate
                # shape the order would have taken had one legal slice existed.
                repo.insert_order(
                    conn,
                    order_id=rejected_order_id,
                    mode=_MODE,
                    run_id=self._run_id,
                    symbol=self._coin,
                    order_role=order_role,
                    side=plan.side.value,
                    order_type="paper_market",
                    qty=plan.total_qty,
                    status="rejected",
                    output_id=output_id,
                    flip_plan_id=flip_plan_id,
                    flip_leg=flip_leg,
                    remaining_qty=plan.total_qty,
                    status_reason="no_legal_slice",
                    reduce_only=reduce_only,
                    timestamp=now,
                )
            if flip_leg == "close":
                self._flip = None  # a flip whose close leg cannot execute never opens
            if self._flip is None:
                # Nothing replaces the plan this REJECT displaced, and no flip is
                # pending — a pending flip being the only legal TP deferral (§4.1)
                # — so restore the steady state now: a surviving position carries
                # its TP. Covers a just-superseded live leg, an already-terminal
                # leg whose stale flip died above (a miss-exhausted close leg),
                # and the close-leg-REJECT case; idempotent otherwise (the TP is
                # a pure function of entry price and tick size).
                self._reconcile_protection_after_terminal()
            return PlanStartResult(
                gate,
                plan_id,
                PlanDisposition.REJECT,
                "no_legal_slice",
                mark_price=mark,
                account_equity=equity,
            )
        order_id = self._next_order_id("ord")
        order_type = (
            "paper_market"
            if plan.disposition is PlanDisposition.PAPER_MARKET
            else "paper_twap_slice"
        )
        if deadline is None:
            deadline = now + _PLAN_LIFETIME
        with self._db.transaction() as conn:
            self._supersede_active_leg(conn, now)
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
            repo.insert_order(
                conn,
                order_id=order_id,
                mode=_MODE,
                run_id=self._run_id,
                symbol=self._coin,
                order_role=order_role,
                side=plan.side.value,
                order_type=order_type,
                qty=plan.total_qty,
                status="open",
                output_id=output_id,
                flip_plan_id=flip_plan_id,
                flip_leg=flip_leg,
                remaining_qty=plan.total_qty,
                reduce_only=reduce_only,
                active_from=now,
                timestamp=now,
            )
        self._leg = _Leg(
            plan_id=plan_id,
            order_id=order_id,
            output_id=output_id,
            side=plan.side,
            disposition=plan.disposition,
            slice_sizes=plan.slice_sizes,
            reduce_only=reduce_only,
            order_role=order_role,
            flip_plan_id=flip_plan_id,
            flip_leg=flip_leg,
            active_from=now,
            deadline=deadline,
        )
        # Cancel any active TP for the duration of the plan (execution §4.1 step 1),
        # keeping the SL.
        if self._protection.take_profit is not None:
            self._set_protection(self._protection.stop_loss, None)
        return PlanStartResult(
            gate=gate,
            plan_id=plan_id,
            disposition=plan.disposition,
            reason=None,
            mark_price=mark,
            account_equity=equity,
        )

    # -- tick loop (execution §5.3) ---------------------------------------

    @_fail_stop
    def tick(self) -> TickResult:
        """Process one 30-second logical tick against a single shared snapshot."""
        now = self._clock.now()
        result = self._provider.fetch(self._coin, requested_at=now, timeout_seconds=self._timeout)
        events: list[TickEvent] = []

        if not result.is_valid:
            return self._handle_no_data(now, result, events)

        snap = result.snapshot
        assert snap is not None  # is_valid guarantees a snapshot (SnapshotResult invariant)
        events.append(TickEvent.PRICE_UPDATED)
        self._consecutive_md_failures = 0
        if self._paused:
            self._paused = False
            # A resumed snapshot must check for an SL the mark crossed during the gap
            # (execution §1.1: gap_stop_fill) before anything re-opens.
            self._resume_pending_gap_check = True
            events.append(TickEvent.RESUMED)
            if self._leg is not None and not self._leg.terminal:
                self._patch_plan(self._leg.plan_id, status="active")

        position = self._read_position()

        # 2. funding due at/under this snapshot
        self._process_funding(now, snap.mark_price, position, events)
        position = self._read_position()

        # 3. liquidation / emergency close
        if self._maybe_close_all(now, snap, position, events):
            return self._finish_tick(now, result, events)

        # 3.5 funding moved the wallet, so the liquidation estimate moved
        # (§6.6.1 lists funding posting among the must-recompute events):
        # refresh the SL so the §3.6 no-safe-SL gate stays live between fills
        # instead of drifting until outright liquidatability. At most hourly.
        if TickEvent.FUNDING_POSTED in events and not position.is_flat:
            if self._recompute_stop_loss(now, snap, position, events):
                return self._finish_tick(now, result, events)
            events.append(TickEvent.PROTECTION_UPDATED)

        # 4. stop loss (incl. gap_stop after a resume; the gap flag is consumed
        # inside _maybe_stop_loss on every exit path).
        if self._maybe_stop_loss(now, snap, position, events):
            return self._finish_tick(now, result, events)

        # 5. take profit
        if self._maybe_take_profit(now, snap, position, events):
            return self._finish_tick(now, result, events)

        # 6. the due slice / paper_market
        self._maybe_execute_slice(now, snap, events)

        # deadline + flip advancement
        self._maybe_expire_plan(now, events)
        self._maybe_advance_flip(now, snap, events)

        if not events or events == [TickEvent.PRICE_UPDATED]:
            events.append(TickEvent.IDLE)
        return self._finish_tick(now, result, events)

    def _handle_no_data(
        self, now: datetime, result: SnapshotResult, events: list[TickEvent]
    ) -> TickResult:
        """A failed snapshot: skip the due slice, count toward the 3-strike pause (§1.1)."""
        events.append(TickEvent.PENDING_MARKET_DATA)
        self._consecutive_md_failures += 1
        # Any slice whose scheduled time passed during the outage is missed, never
        # re-run (execution §1.1). Advance the leg's cursor over it without filling.
        if self._leg is not None and not self._leg.terminal:
            due = self._due_count(self._leg, now)
            if due > self._leg.consumed:
                self._leg.consumed = due
                self._leg.validate()
                events.append(TickEvent.SLICE_MISSED)
                # Every slice consumed (all missed): the leg can never fill again,
                # so terminate it as residual now — the same exhaustion rule the
                # fill path applies — instead of sitting "active" until the 1h
                # deadline. TP reconciliation is snapshot-independent (§4.1), so
                # it is safe during the outage. At/past the deadline the expiry
                # below owns the terminal instead (label "expired"/"deadline").
                if self._leg.consumed >= self._leg.planned and now < self._leg.deadline:
                    self._terminate_leg(now, events)
        if not self._paused and self._consecutive_md_failures >= 3:
            self._paused = True
            events.append(TickEvent.PAUSED)
            if self._leg is not None and not self._leg.terminal:
                self._patch_plan(self._leg.plan_id, status="paused_market_data")
        self._maybe_expire_plan(now, events)
        return self._finish_tick(now, result, events)

    def _finish_tick(
        self, now: datetime, result: SnapshotResult, events: list[TickEvent]
    ) -> TickResult:
        # The gap flag is strictly tick-scoped. _maybe_stop_loss is its normal
        # consumer, but steps 3 / 3.5 (liquidation, post-funding emergency close)
        # can finish the tick before step 4 ever runs — the flag must not leak
        # into a later tick and mislabel an unrelated SL fill as a gap stop.
        self._resume_pending_gap_check = False
        position = self._read_position()
        # Record account/position snapshots when this tick materially changed state
        # (a fill or funding); idle polling ticks don't bloat the store (§11.1/§12.1).
        material = {
            TickEvent.FUNDING_POSTED,
            TickEvent.SLICE_FILL,
            TickEvent.PAPER_MARKET_FILL,
            TickEvent.STOP_LOSS_FILL,
            TickEvent.GAP_STOP_FILL,
            TickEvent.TAKE_PROFIT_FILL,
            TickEvent.LIQUIDATION_CLOSE,
            TickEvent.PLAN_TERMINAL,
        }
        if result.is_valid and material.intersection(events):
            self._write_snapshots(now, result.snapshot.mark_price, position)
        plan_status = (
            None
            if self._leg is None
            else (PlanStatus.TERMINAL if self._leg.terminal else PlanStatus.ACTIVE)
        )
        return TickResult(
            at=now,
            outcome=result.outcome,
            events=tuple(events),
            position_size=position.size,
            plan_status=plan_status,
        )

    # -- funding (execution §6.5) -----------------------------------------

    def _process_funding(
        self, now: datetime, mark: Decimal, position: PositionState, events: list[TickEvent]
    ) -> None:
        """Post funding for each whole hour crossed since the last processed hour.

        PR3 scope (deferred to PR4 by decision): this posts funding forward from the
        engine's first tick and records a ``pending`` event when the rate is
        unavailable, but it does **not** retry a pending hour, backfill hours before
        start, or value a multi-hour catch-up at each hour's own historical mark
        (a catch-up spanning >1 hour uses this tick's mark for all of them). PR4's
        daemon loop/reconciliation owns pending-retry (cycle boundaries, the
        halted-mode hourly timer, and the shutdown exports) and historical backfill
        (the exactly-once key and stored-basis machinery in ``record_funding``
        already make that safe to add). A ``None`` source disables funding entirely.
        """
        if self._funding is None:
            return
        current_hour = now.replace(minute=0, second=0, microsecond=0)
        if self._last_funding_hour is None:
            # First tick establishes the baseline; funding accrues from here forward
            # (no backfill of hours before the engine started — PR4 owns backfill).
            self._last_funding_hour = current_hour
            return
        h = self._last_funding_hour + timedelta(hours=1)
        while h <= now:
            rate = self._funding.rate_at(self._coin, h)
            res = accounting.record_funding(
                self._db,
                run_id=self._run_id,
                mode=_MODE,
                symbol=self._coin,
                funding_timestamp=h,
                position_size=position.size,
                funding_rate=rate,
                mark_price=mark,
                source="live_public_data",
                recorded_at=now,
            )
            if res.status == "posted":
                events.append(TickEvent.FUNDING_POSTED)
            elif res.status == "pending":
                events.append(TickEvent.FUNDING_PENDING)
            self._last_funding_hour = h
            h += timedelta(hours=1)

    # -- close-all paths (liquidation / emergency, execution §5.3 step 3) --

    def _maybe_close_all(self, now, snap, position: PositionState, events: list[TickEvent]) -> bool:
        if position.is_flat or not self._is_liquidatable(position, snap.mark_price):
            return False
        terminated = self._close_position(
            now,
            snap,
            position,
            fill_reason="liquidation",
            order_role="rebalance",
            plan_terminal_status="canceled",
            plan_terminal_reason="risk_exit",
            end_flip=True,
        )
        events.append(TickEvent.LIQUIDATION_CLOSE)
        if terminated:
            events.append(TickEvent.PLAN_TERMINAL)
        return True

    def _maybe_stop_loss(self, now, snap, position: PositionState, events) -> bool:
        sl = self._protection.stop_loss
        # Consume the one-shot gap flag here, on *every* exit path — a plain early
        # return that left it set would mislabel the next normal SL fill as a gap
        # stop for the rest of the process's life.
        is_gap = self._resume_pending_gap_check
        self._resume_pending_gap_check = False
        if position.is_flat or sl is None:
            return False
        mark = snap.mark_price
        triggered = (position.size > 0 and mark <= sl) or (position.size < 0 and mark >= sl)
        if not triggered:
            return False
        reason = "gap_stop_fill" if is_gap else "stop_loss"
        terminated = self._close_position(
            now,
            snap,
            position,
            fill_reason=reason,
            order_role="stop_loss",
            trigger_price=sl,
            plan_terminal_status="canceled",
            plan_terminal_reason="stop_loss",
            end_flip=True,
        )
        events.append(TickEvent.GAP_STOP_FILL if is_gap else TickEvent.STOP_LOSS_FILL)
        if terminated:
            events.append(TickEvent.PLAN_TERMINAL)
        return True

    def _maybe_take_profit(self, now, snap, position: PositionState, events) -> bool:
        tp = self._protection.take_profit
        if position.is_flat or tp is None:
            return False
        mark = snap.mark_price
        triggered = (position.size > 0 and mark >= tp) or (position.size < 0 and mark <= tp)
        if not triggered:
            return False
        # TP fires only after a plan is terminal (§4.1), so there is normally no
        # active plan to cancel; pass no terminal status.
        self._close_position(
            now,
            snap,
            position,
            fill_reason="take_profit",
            order_role="take_profit",
            trigger_price=tp,
        )
        events.append(TickEvent.TAKE_PROFIT_FILL)
        return True

    # -- slice execution (execution §1.1 / §5.1 / §5.2) -------------------

    def _due_count(self, leg: _Leg, now: datetime) -> int:
        if now < leg.active_from:
            return 0
        if leg.disposition is PlanDisposition.PAPER_MARKET:
            return 1
        elapsed = (now - leg.active_from).total_seconds()
        return min(leg.planned, int(elapsed // SLICE_INTERVAL_SECONDS))

    def _maybe_execute_slice(self, now, snap, events) -> None:
        leg = self._leg
        if leg is None or leg.terminal or self._paused:
            return
        due = self._due_count(leg, now)
        if leg.consumed >= due:
            return  # nothing new due this tick
        idx = leg.consumed
        leg.consumed += 1  # one slice per tick (execution §5.5)
        size = leg.slice_sizes[idx]
        price = fill_price(snap.mid_price, leg.side, self._slippage_bps)
        slice_id = derive_slice_id(self._run_id, leg.plan_id, leg.flip_leg, idx)
        fid = derive_fill_id(self._run_id, leg.order_id, idx)
        self._post_slice_fill(
            now, leg, size=size, price=price, slice_id=slice_id, fill_id=fid, slice_index=idx
        )
        leg.executed += 1
        with localcontext(DECIMAL_CONTEXT):
            leg.filled_qty += size
        leg.validate()
        events.append(
            TickEvent.PAPER_MARKET_FILL
            if leg.disposition is PlanDisposition.PAPER_MARKET
            else TickEvent.SLICE_FILL
        )
        position = self._read_position()
        # Recompute SL after every position-changing fill (execution §3.2 / §4.1:
        # during a plan only the SL moves; TP is (re)created at terminal).
        if not position.is_flat:
            if self._recompute_stop_loss(now, snap, position, events):
                return  # no safe SL -> emergency-closed this tick; plan already terminal
            events.append(TickEvent.PROTECTION_UPDATED)
        else:
            self._set_protection(None, None)
        if leg.consumed >= leg.planned or leg.remaining_qty <= 0:
            self._terminate_leg(now, events)

    def _post_slice_fill(
        self, now, leg: _Leg, *, size, price, slice_id, fill_id, slice_index
    ) -> None:
        with self._db.transaction() as conn:
            accounting.apply_fill(
                conn,
                run_id=self._run_id,
                mode=_MODE,
                fill_id=fill_id,
                order_id=leg.order_id,
                symbol=self._coin,
                side=leg.side,
                qty=size,
                price=price,
                fee_rate=self._fee_rate,
                slice_id=slice_id,
                plan_id=leg.plan_id,
                flip_leg=leg.flip_leg,
                slice_index=slice_index,
                timestamp=now,
            )
            with localcontext(DECIMAL_CONTEXT):
                new_filled = leg.filled_qty + size
                total = sum(leg.slice_sizes, Decimal(0))
                remaining = total - new_filled
            repo.update_order(
                conn,
                leg.order_id,
                filled_qty=new_filled,
                remaining_qty=remaining,
                status="filled" if remaining <= 0 else "partially_filled",
                updated_at=now,
            )
            repo.update_execution_plan(conn, leg.plan_id, remaining_qty=remaining, updated_at=now)

    # -- close a whole position (SL/TP/liquidation, execution §2/§5.2) ----

    def _close_position(
        self,
        now,
        snap,
        position: PositionState,
        *,
        fill_reason: str,
        order_role: str,
        trigger_price: Decimal | None = None,
        plan_terminal_status: str | None = None,
        plan_terminal_reason: str | None = None,
        end_flip: bool = False,
    ) -> bool:
        """Reduce-only close of the whole position, atomically (execution §2 / §5.2).

        The order, the fill (position + ledger via ``apply_fill``), the protection
        clear, and — when the caller passes a terminal status — the active plan's
        termination all commit in **one** ``db.transaction()``. This is what keeps a
        flattening fill from leaving, on a crash, a flat position with a stale SL/TP
        (violating §2's "position_size == 0 → no active SL/TP") or an "active" plan.
        ``trigger_price`` is recorded only when this close *was* a triggered stop —
        an SL/TP fill passes its trigger; liquidation and emergency closes pass
        ``None`` (an emergency close's just-invalidated SL never triggered and must
        not be exported as if it had). Returns whether it terminated an active plan
        (so the caller can emit ``PLAN_TERMINAL`` only when a plan was cancelled).
        """
        if plan_terminal_reason is not None and plan_terminal_status is None:
            # The reason would be silently dropped (termination is gated on the
            # status alone) — reject the half-specified call instead.
            raise ValueError("plan_terminal_reason requires plan_terminal_status")
        side = Side.SELL if position.size > 0 else Side.BUY
        qty = abs(position.size)
        price = fill_price(snap.mid_price, side, self._slippage_bps)
        order_id = self._next_order_id(order_role)
        fid = derive_fill_id(self._run_id, order_id, 0)
        order_type = {
            "stop_loss": "stop_market",
            "take_profit": "take_market",
        }.get(order_role, "paper_market")
        leg = self._leg
        terminated = plan_terminal_status is not None and leg is not None and not leg.terminal
        with self._db.transaction() as conn:
            repo.insert_order(
                conn,
                order_id=order_id,
                mode=_MODE,
                run_id=self._run_id,
                symbol=self._coin,
                order_role=order_role,
                side=side.value,
                order_type=order_type,
                qty=qty,
                status="filled",
                price=None,
                trigger_price=trigger_price,
                filled_qty=qty,
                remaining_qty=Decimal(0),
                reduce_only=True,
                active_from=now,
                timestamp=now,
            )
            accounting.apply_fill(
                conn,
                run_id=self._run_id,
                mode=_MODE,
                fill_id=fid,
                order_id=order_id,
                symbol=self._coin,
                side=side,
                qty=qty,
                price=price,
                fee_rate=self._fee_rate,
                fill_reason=fill_reason,
                timestamp=now,
            )
            # A flat position carries no SL/TP (§2) — clear it in the same transaction
            # as the fill so a crash can never leave protection on a closed position.
            repo.set_position_protection(
                conn,
                self._run_id,
                self._coin,
                stop_loss_price=None,
                take_profit_price=None,
                updated_at=now,
            )
            if terminated:
                assert leg is not None
                assert plan_terminal_status is not None  # `terminated` implies it
                self._write_leg_terminal(
                    conn,
                    leg,
                    now,
                    status=plan_terminal_status,
                    plan_reason=plan_terminal_reason,
                    order_reason=plan_terminal_reason,
                )
        self._protection = _Protection(None, None)
        if end_flip:
            self._flip = None  # a risk exit ends any in-flight flip
        return terminated

    # -- protection lifecycle (execution §2–§4) ---------------------------

    def _recompute_stop_loss(self, now, snap, position: PositionState, events) -> bool:
        """Recompute and persist the SL after a position-changing fill (§3.2).

        Returns ``True`` when no safe SL exists (§3.6) and the position was
        emergency-closed **synchronously in this tick** — the user-chosen behaviour:
        the close happens on the same snapshot that detected it, so the position is
        never left unprotected for a tick.
        """
        side = Side.BUY if position.size > 0 else Side.SELL
        liq = self.liquidation_price(position, snap.mark_price)
        decision = stop_loss_decision(
            side=side,
            entry_price=position.entry_price,
            liquidation_price=liq,
            tick_size=self._asset.tick_size,
            config=self._stop,
        )
        if decision.action is StopAction.CLOSE_NOW:
            # trigger_price stays None: the old SL was just judged unsafe and
            # never triggered — recording it would fabricate a trigger in the
            # audit trail (fills.fill_reason="emergency_close" carries the why).
            terminated = self._close_position(
                now,
                snap,
                position,
                fill_reason="emergency_close",
                order_role="stop_loss",
                plan_terminal_status="canceled",
                plan_terminal_reason="no_safe_sl",
                end_flip=True,
            )
            events.append(TickEvent.LIQUIDATION_CLOSE)
            if terminated:
                events.append(TickEvent.PLAN_TERMINAL)
            return True
        self._set_protection(decision.price, self._protection.take_profit)
        return False

    def _set_protection(self, sl: Decimal | None, tp: Decimal | None) -> None:
        self._protection = _Protection(stop_loss=sl, take_profit=tp)
        # Persist only when a position row exists (protection with no position is a
        # no-op — a just-flattened close already cleared to None).
        if repo.get_current_position(self._db.conn, self._run_id, self._coin) is not None:
            with self._db.transaction() as conn:
                repo.set_position_protection(
                    conn,
                    self._run_id,
                    self._coin,
                    stop_loss_price=sl,
                    take_profit_price=tp,
                    updated_at=self._clock.now(),
                )

    # -- plan / flip terminal handling ------------------------------------

    def _finalize_leg_order(self, conn, leg: _Leg, now, *, reason: str | None) -> None:
        """Terminal-close the leg's order row alongside its plan (phase2-data §8).

        A fully-filled order is already ``filled`` (stamped by its last slice's
        update); anything still unfilled when the plan dies becomes ``canceled``
        with the plan's reason, in the same transaction — the orders table must
        never keep an ``open``/``partially_filled`` row under a terminal plan
        (PR4's CSV export and Phase 3 reconciliation read ``orders.status``).
        """
        if leg.remaining_qty > 0:
            repo.update_order(
                conn, leg.order_id, status="canceled", status_reason=reason, updated_at=now
            )

    def _write_leg_terminal(
        self,
        conn,
        leg: _Leg,
        now,
        *,
        status: str,
        plan_reason: str | None,
        order_reason: str | None,
    ) -> None:
        """Persist one leg's terminal transition: plan row and order row together.

        Every terminal path shares this write pair — the plan patched terminal
        with its residual recorded, the unfilled order canceled — inside the
        caller's transaction; only the status/reason vocabulary differs per path.
        """
        leg.terminal = True
        remaining = leg.remaining_qty
        repo.update_execution_plan(
            conn,
            leg.plan_id,
            status=status,
            remaining_qty=remaining,
            residual_qty=max(remaining, Decimal(0)),
            status_reason=plan_reason,
            updated_at=now,
        )
        self._finalize_leg_order(conn, leg, now, reason=order_reason)

    def _reconcile_protection_after_terminal(self) -> None:
        """§4.1 terminal reconciliation: a surviving position regains its TP.

        Plan start cancels the TP for the plan's duration, so every TP-reconciled
        terminal (completed / residual / canceled / expired / flip_incomplete)
        funnels here. TP math needs only entry price + tick size — never this
        tick's market data. A flat position instead clears any lingering
        protection (§2: no position → no SL/TP).
        """
        position = self._read_position()
        if not position.is_flat:
            self._create_take_profit(position)
        else:
            self._set_protection(None, None)

    def _supersede_active_leg(self, conn, now) -> bool:
        """Cancel a still-live leg before a new plan registers (one live plan max).

        A new decision replaces the old target (§1.2: no old plan may keep
        slicing alongside a newer ``output_id``), so the old plan terminates as
        ``canceled``/``superseded`` with its residual recorded — in the same
        transaction that inserts the new plan, never as an orphaned ``active``
        row. Normal 4h cadence never triggers this (plans die within the hour);
        a scheduler retry or an operator restart of the decision flow does.
        Returns whether a leg was actually cancelled. (The REJECT path no longer
        keys TP reconciliation on this — an already-terminal leg whose stale flip
        died at registration needs the same reconcile — it reconciles whenever no
        flip is left pending.)
        """
        leg = self._leg
        if leg is None or leg.terminal:
            return False
        self._write_leg_terminal(
            conn, leg, now, status="canceled", plan_reason="superseded", order_reason="superseded"
        )
        return True

    def _terminate_leg(self, now, events) -> None:
        leg = self._leg
        if leg is None or leg.terminal:
            return
        status = "completed" if leg.remaining_qty <= 0 else "residual"
        with self._db.transaction() as conn:
            self._write_leg_terminal(
                conn, leg, now, status=status, plan_reason=None, order_reason="residual"
            )
        events.append(TickEvent.PLAN_TERMINAL)
        # Non-flip rebalance and a flip's open leg create the TP now (§4.1 step 3);
        # a flip's close leg defers TP to the open leg's terminal (§4.1 flip note).
        if leg.flip_leg == "close":
            return
        self._reconcile_protection_after_terminal()

    def _create_take_profit(self, position: PositionState) -> None:
        side = Side.BUY if position.size > 0 else Side.SELL
        tp = take_profit_price(
            side=side,
            entry_price=position.entry_price,
            tick_size=self._asset.tick_size,
            config=self._stop,
        )
        self._set_protection(self._protection.stop_loss, tp)

    def _maybe_expire_plan(self, now, events) -> None:
        leg = self._leg
        if leg is None or leg.terminal:
            return
        if now >= leg.deadline:
            with self._db.transaction() as conn:
                self._write_leg_terminal(
                    conn,
                    leg,
                    now,
                    status="expired",
                    plan_reason="deadline",
                    order_reason="deadline",
                )
            events.append(TickEvent.PLAN_TERMINAL)
            # An expired flip close leg that never fully closed cannot open (§1.3).
            if leg.flip_leg == "close" and self._flip is not None:
                self._record_flip_incomplete("close_leg_expired")
            elif leg.flip_leg != "close":
                # A deadline hit during an outage must not strand the position
                # without its TP — reconciliation is snapshot-independent.
                self._reconcile_protection_after_terminal()

    def _maybe_advance_flip(self, now, snap, events) -> None:
        flip = self._flip
        leg = self._leg
        if flip is None or leg is None or leg.flip_leg != "close" or not leg.terminal:
            return
        if flip.open_started:
            return
        position = self._read_position()
        if not position.is_flat:
            # Close leg finished without reaching flat: do not open the reverse
            # position (execution §1.3).
            self._record_flip_incomplete("close_leg_incomplete")
            return
        # Re-run the deterministic RiskGate for the open leg (never re-ask the AI).
        # Reuse this tick's snapshot — the monitor and any same-tick step share one
        # snapshot and never double-call the API (execution §5.5).
        flip.open_started = True
        if snap is None:
            self._record_flip_incomplete("open_leg_no_market_data")
            return
        osnap = snap
        equity = self._equity(position, osnap.mark_price)
        current = self._current_position_state(position, osnap.mark_price, equity)
        gate = evaluate(
            flip.parsed,
            account_equity=equity,
            current=current,
            risk=self._risk,
            decision_cfg=self._decision,
        )
        if not gate.order_created or gate.target_signed_notional is None:
            self._record_flip_incomplete("open_leg_gate_rejected")
            return
        delta = rebalance_delta(
            target_signed_notional=gate.target_signed_notional,
            mark_price=osnap.mark_price,
            position_size=position.size,
        )
        if delta.side is None:
            self._record_flip_incomplete("open_leg_zero_delta")
            return
        plan = build_slice_plan(
            delta.raw_total_qty,
            side=delta.side,
            qty_step=self._asset.qty_step,
            min_notional=self._min_notional,
            mid=osnap.mid_price,
            max_slices=flip.open_budget,
        )
        res = self._register_leg(
            plan,
            gate=gate,
            output_id=flip.output_id,
            reduce_only=False,
            order_role="entry",
            flip_plan_id=flip.flip_plan_id,
            flip_leg="open",
            deadline=flip.deadline,
            mark=osnap.mark_price,
            equity=equity,
        )
        if res.disposition is PlanDisposition.REJECT:
            self._record_flip_incomplete("open_leg_no_legal_slice")
        else:
            # The open leg is now a normal executing leg; the flip bookkeeping is
            # done, so drop it (otherwise ``has_active_work`` would never go quiet
            # once the position later closes).
            self._flip = None

    def _record_flip_incomplete(self, reason: str) -> None:
        leg = self._leg
        if leg is not None and self._flip is not None:
            with self._db.transaction() as conn:
                repo.insert_execution_plan(
                    conn,
                    plan_id=self._next_order_id("flipincomplete"),
                    run_id=self._run_id,
                    symbol=self._coin,
                    status="flip_incomplete",
                    created_at=self._clock.now(),
                    output_id=self._flip.output_id,
                    flip_plan_id=self._flip.flip_plan_id,
                    status_reason=reason,
                )
        self._flip = None
        # §4.1 lists flip_incomplete among the TP-reconciled terminal states: a
        # surviving (partially-closed) position must regain its TP here — no
        # other path would ever recreate it. The open-leg failure reasons
        # arrive with a flat position, which reconciles to cleared protection.
        self._reconcile_protection_after_terminal()

    # -- persistence helpers ----------------------------------------------

    def _patch_plan(self, plan_id: str, **fields) -> None:
        with self._db.transaction() as conn:
            repo.update_execution_plan(conn, plan_id, updated_at=self._clock.now(), **fields)

    def _write_snapshots(self, now, mark: Decimal, position: PositionState) -> None:
        ledger = self._read_ledger()
        valuations = (
            []
            if position.is_flat
            else [accounting.PositionValuation(position, mark, self._asset.margin_schedule)]
        )
        metrics = accounting.summarize_account(ledger, valuations, leverage=self._leverage)
        with localcontext(DECIMAL_CONTEXT):
            total_pnl = (
                ledger.realized_pnl
                + metrics.unrealized_pnl
                - ledger.total_fees
                + ledger.net_funding_pnl
            )
        with self._db.transaction() as conn:
            repo.insert_account_snapshot(
                conn,
                timestamp=now,
                mode=_MODE,
                run_id=self._run_id,
                wallet_balance=ledger.wallet_balance,
                account_equity=metrics.account_equity,
                available_balance=metrics.available_balance,
                realized_pnl=ledger.realized_pnl,
                unrealized_pnl=metrics.unrealized_pnl,
                total_pnl=total_pnl,
                total_fees=ledger.total_fees,
                net_funding_pnl=ledger.net_funding_pnl,
                total_position_notional=metrics.total_position_notional,
                effective_leverage=metrics.effective_leverage,
                used_initial_margin=metrics.used_initial_margin,
                total_maintenance_margin=metrics.total_maintenance_margin,
                margin_ratio=metrics.margin_ratio,
            )
            if not position.is_flat:
                maint = maintenance_snapshot(self._asset.margin_schedule, position.size, mark)
                liq = self.liquidation_price(position, mark)
                with localcontext(DECIMAL_CONTEXT):
                    notional = position_notional(position.size, mark)
                    exposure = (
                        notional / metrics.account_equity * 100
                        if metrics.account_equity > 0
                        else None
                    )
                repo.insert_position_snapshot(
                    conn,
                    timestamp=now,
                    mode=_MODE,
                    run_id=self._run_id,
                    symbol=self._coin,
                    position_size=position.size,
                    side="long" if position.size > 0 else "short",
                    entry_price=position.entry_price,
                    mark_price=mark,
                    position_notional=notional,
                    exposure_pct=exposure,
                    unrealized_pnl=unrealized_pnl(position.size, mark, position.entry_price),
                    realized_pnl=position.realized_pnl,
                    maintenance_margin=maint.maintenance_margin,
                    estimated_liquidation_price=liq,
                    margin_tier_id=maint.margin_tier_id,
                    maintenance_margin_rate=maint.maintenance_margin_rate,
                    maintenance_deduction=maint.maintenance_deduction,
                    liquidation_model_version=LIQUIDATION_MODEL_VERSION,
                    stop_loss_price=self._protection.stop_loss,
                    take_profit_price=self._protection.take_profit,
                )
