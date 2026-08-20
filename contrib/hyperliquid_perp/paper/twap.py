"""TWAP / flip slice-planning math (execution §1.2 / §6.2) — pure, all-Decimal.

Turns an approved target into a concrete list of execution slices. Everything
here is a pure function of its inputs (no clock, no I/O), so a plan is fully
determined by the fresh market snapshot it is built against and a replay
reproduces the identical slice sizes.

The pipeline (execution §1.2):

    raw_total_qty  →  total_qty = floor_to_step(raw_total_qty)     # never round up
    min_order_qty  = ceil_to_step(min_notional / mid)
    max_legal      = floor(total_qty / min_order_qty)
    planned_slices = min(max_legal, 120)

        0 slices → REJECT (residual, never rounded up to a too-large position)
        1 slice  → PAPER_MARKET (Phase 3: Market / IOC)
        2–120    → TWAP, one slice every 30 s

Slice sizes are allocated in **integer quantity steps**, not by dividing a float,
so the sizes sum to *exactly* ``total_qty`` (execution §1.2 worked example:
1.03 / 0.01 over 4 slices → 0.26, 0.26, 0.26, 0.25). The dropped fraction
``raw_total_qty - total_qty`` is recorded as ``rounding_residual_qty``.

``raw_total_qty`` itself is recomputed at plan-build time from a fresh snapshot
(execution §6.2): the RiskGate's decision-time ``delta_notional`` is audit-only,
because price drifts between the 4h decision and ``active_from``. This module
owns that recompute (:func:`rebalance_delta`) and the flip-budget split
(:func:`split_flip_budget`); the engine decides *when* to build.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, localcontext
from enum import Enum

from ..common.decimal_context import DECIMAL_CONTEXT
from ..persistence.models import Side

__all__ = [
    "MAX_SLICES",
    "SLICE_INTERVAL_SECONDS",
    "PlanDisposition",
    "RebalanceDelta",
    "SlicePlan",
    "build_slice_plan",
    "ceil_to_step",
    "floor_to_step",
    "min_order_qty",
    "qty_step_from_sz_decimals",
    "rebalance_delta",
    "split_flip_budget",
]

# Execution §1.2: at most 120 slices, one every 30 s → a one-hour ceiling.
MAX_SLICES = 120
SLICE_INTERVAL_SECONDS = 30


class PlanDisposition(str, Enum):
    """How :func:`build_slice_plan` chose to execute (execution §1.2)."""

    REJECT = "reject"  # no legal slice fits — residual, never rounded up
    PAPER_MARKET = "paper_market"  # a single full-quantity fill
    TWAP = "twap"  # 2–120 slices, 30 s apart


def qty_step_from_sz_decimals(sz_decimals: int) -> Decimal:
    """The quantity step ``10 ** -szDecimals`` for an asset (never hardcoded)."""
    if sz_decimals < 0:
        raise ValueError(f"szDecimals must be >= 0, got {sz_decimals}")
    return Decimal(10) ** -sz_decimals


def floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    """Largest multiple of ``step`` that is ``<= value`` (value assumed >= 0)."""
    if step <= 0:
        raise ValueError(f"step must be > 0, got {step}")
    with localcontext(DECIMAL_CONTEXT):
        return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


def ceil_to_step(value: Decimal, step: Decimal) -> Decimal:
    """Smallest multiple of ``step`` that is ``>= value`` (value assumed >= 0)."""
    if step <= 0:
        raise ValueError(f"step must be > 0, got {step}")
    with localcontext(DECIMAL_CONTEXT):
        return (value / step).to_integral_value(rounding=ROUND_CEILING) * step


def min_order_qty(min_notional: Decimal, mid: Decimal, qty_step: Decimal) -> Decimal:
    """``ceil_to_step(min_notional / mid)`` — the smallest legal single slice (§1.2)."""
    if min_notional <= 0:
        raise ValueError(f"min_notional must be > 0, got {min_notional}")
    if mid <= 0:
        raise ValueError(f"mid must be > 0, got {mid}")
    with localcontext(DECIMAL_CONTEXT):
        return ceil_to_step(min_notional / mid, qty_step)


@dataclass(frozen=True)
class RebalanceDelta:
    """The fresh-snapshot recompute of the quantity to trade (execution §6.2).

    ``signed_delta_size`` is ``target_size - position_size`` at the fresh mark
    (positive → buy, negative → sell); ``raw_total_qty`` is its magnitude, the
    unrounded input to :func:`build_slice_plan`. ``side`` is ``None`` only when the
    delta is exactly zero (nothing to trade).
    """

    signed_delta_size: Decimal
    raw_total_qty: Decimal
    side: Side | None

    def __post_init__(self) -> None:
        if self.raw_total_qty != abs(self.signed_delta_size):
            raise ValueError("raw_total_qty must equal abs(signed_delta_size)")
        if self.signed_delta_size == 0:
            if self.side is not None:
                raise ValueError("a zero delta trades nothing and carries no side")
        elif self.side is None:
            raise ValueError("a non-zero delta must carry a side")
        elif (self.signed_delta_size > 0) != (self.side is Side.BUY):
            raise ValueError("side must be buy for a positive delta, sell for a negative one")


def rebalance_delta(
    *, target_signed_notional: Decimal, mark_price: Decimal, position_size: Decimal
) -> RebalanceDelta:
    """Recompute the signed quantity to trade at the fresh mark (execution §6.2).

    ``target_size = target_signed_notional / mark`` (sizing uses **mark**), then
    the delta against the current ``position_size``. The engine derives the order
    side and total quantity from this, never from the RiskGate's audit-only
    decision-time delta.
    """
    if mark_price <= 0:
        raise ValueError(f"mark_price must be > 0, got {mark_price}")
    with localcontext(DECIMAL_CONTEXT):
        target_size = target_signed_notional / mark_price
        signed_delta = target_size - position_size
    if signed_delta == 0:
        return RebalanceDelta(Decimal(0), Decimal(0), None)
    side = Side.BUY if signed_delta > 0 else Side.SELL
    return RebalanceDelta(signed_delta, abs(signed_delta), side)


@dataclass(frozen=True)
class SlicePlan:
    """A concrete execution plan: how to split ``raw_total_qty`` into slices.

    ``slice_sizes`` sums to exactly ``total_qty`` for an executable plan. A
    REJECT has no slices but keeps the floored ``total_qty`` (possibly > 0 when
    ``min_notional`` outlaws every slice) — that quantity is the audit trail's
    rejected/residual amount, never executed. Sizes are positive multiples of
    ``qty_step``; ``side`` is the fill direction for every slice.
    ``rounding_residual_qty`` is the fraction dropped by flooring the raw
    quantity to the grid — recorded, never rounded up (execution §1.2).
    """

    side: Side
    disposition: PlanDisposition
    total_qty: Decimal
    qty_step: Decimal
    min_order_qty: Decimal
    slice_sizes: tuple[Decimal, ...]
    rounding_residual_qty: Decimal

    def __post_init__(self) -> None:
        if self.total_qty < 0:
            raise ValueError(f"total_qty must be >= 0, got {self.total_qty}")
        if self.rounding_residual_qty < 0:
            raise ValueError("rounding_residual_qty must be >= 0")
        expected = {
            PlanDisposition.REJECT: 0,
            PlanDisposition.PAPER_MARKET: 1,
        }.get(self.disposition)
        if expected is not None and len(self.slice_sizes) != expected:
            raise ValueError(
                f"{self.disposition.value} plan must have {expected} slices, "
                f"got {len(self.slice_sizes)}"
            )
        if self.disposition is PlanDisposition.TWAP and not (
            2 <= len(self.slice_sizes) <= MAX_SLICES
        ):
            raise ValueError(
                f"a TWAP plan must have 2..{MAX_SLICES} slices, got {len(self.slice_sizes)}"
            )
        if any(s <= 0 for s in self.slice_sizes):
            raise ValueError("every slice size must be > 0")
        # The whole point of integer-step allocation: the sizes reconstitute the
        # floored total exactly, so execution can never drift above the approved
        # target or leave an unexplained gap. A REJECT is exempt: it executes
        # nothing, and its total_qty stays > 0 when min_notional outlawed every
        # slice for a non-zero floored quantity.
        if self.disposition is not PlanDisposition.REJECT:
            with localcontext(DECIMAL_CONTEXT):
                if sum(self.slice_sizes, Decimal(0)) != self.total_qty:
                    raise ValueError(
                        f"slice sizes sum to {sum(self.slice_sizes, Decimal(0))}, "
                        f"expected total_qty {self.total_qty}"
                    )

    @property
    def planned_slices(self) -> int:
        return len(self.slice_sizes)

    @property
    def is_executable(self) -> bool:
        return self.disposition is not PlanDisposition.REJECT


def _allocate_steps(
    total_qty: Decimal, qty_step: Decimal, planned_slices: int
) -> tuple[Decimal, ...]:
    """Split ``total_qty`` into ``planned_slices`` positive multiples of ``qty_step``.

    Integer-step allocation (execution §1.2): the first ``extra_steps`` slices get
    one extra step so the sizes sum to exactly ``total_qty`` with no float drift.
    """
    with localcontext(DECIMAL_CONTEXT):
        total_steps = int((total_qty / qty_step).to_integral_value())
        base_steps, extra_steps = divmod(total_steps, planned_slices)
        sizes = [
            (base_steps + 1) * qty_step if i < extra_steps else base_steps * qty_step
            for i in range(planned_slices)
        ]
    return tuple(sizes)


def build_slice_plan(
    raw_total_qty: Decimal,
    *,
    side: Side,
    qty_step: Decimal,
    min_notional: Decimal,
    mid: Decimal,
    max_slices: int = MAX_SLICES,
) -> SlicePlan:
    """Plan the execution of ``raw_total_qty`` into 30-second slices (execution §1.2).

    ``max_slices`` caps the plan (defaults to the 120 / one-hour budget; a flip
    leg passes its share from :func:`split_flip_budget`). A quantity too small to
    fund even one legal slice is REJECTed with the whole ``total_qty`` unfilled —
    never rounded up into a larger position.
    """
    side = Side.parse(side)
    if raw_total_qty < 0:
        raise ValueError(f"raw_total_qty must be >= 0, got {raw_total_qty}")
    if not 1 <= max_slices <= MAX_SLICES:
        raise ValueError(f"max_slices must be in 1..{MAX_SLICES}, got {max_slices}")

    moq = min_order_qty(min_notional, mid, qty_step)
    with localcontext(DECIMAL_CONTEXT):
        total_qty = floor_to_step(raw_total_qty, qty_step)
        rounding_residual = raw_total_qty - total_qty
        max_legal = int((total_qty / moq).to_integral_value(rounding=ROUND_FLOOR))
    planned = min(max_legal, max_slices)

    if planned <= 0:
        # Nothing legal fits — residual, do not round up (execution §1.2).
        return SlicePlan(
            side=side,
            disposition=PlanDisposition.REJECT,
            total_qty=total_qty,
            qty_step=qty_step,
            min_order_qty=moq,
            slice_sizes=(),
            rounding_residual_qty=rounding_residual,
        )
    if planned == 1:
        return SlicePlan(
            side=side,
            disposition=PlanDisposition.PAPER_MARKET,
            total_qty=total_qty,
            qty_step=qty_step,
            min_order_qty=moq,
            slice_sizes=(total_qty,),
            rounding_residual_qty=rounding_residual,
        )
    return SlicePlan(
        side=side,
        disposition=PlanDisposition.TWAP,
        total_qty=total_qty,
        qty_step=qty_step,
        min_order_qty=moq,
        slice_sizes=_allocate_steps(total_qty, qty_step, planned),
        rounding_residual_qty=rounding_residual,
    )


def split_flip_budget(
    close_qty: Decimal, open_qty: Decimal, *, total_budget: int = MAX_SLICES
) -> tuple[int, int]:
    """Split the shared slice budget between a flip's close and open legs (§1.3).

    Slices are apportioned by leg quantity, so the larger leg gets the larger
    share, and the two shares sum to exactly ``total_budget``. With a budget of
    at least 2, each leg is guaranteed one slice when its quantity is positive (a
    leg that must trade is never starved to zero budget); a zero-quantity leg
    gets zero, and a budget of 1 goes entirely to the close leg. Callers keep a
    genuine flip off that starved path themselves: the paper engine always
    passes the full 120, and the live engine passes its config-derived budget
    floored at 2 (``max(2, _max_slices)``). The per-leg :func:`build_slice_plan`
    still floors the share to what the leg's own ``min_order_qty`` can legally
    fund.
    """
    if not 1 <= total_budget <= MAX_SLICES:
        raise ValueError(f"total_budget must be in 1..{MAX_SLICES}, got {total_budget}")
    if close_qty < 0 or open_qty < 0:
        raise ValueError("flip leg quantities must be >= 0")
    if close_qty == 0 and open_qty == 0:
        return 0, 0
    if close_qty == 0:
        return 0, total_budget
    if open_qty == 0:
        return total_budget, 0
    with localcontext(DECIMAL_CONTEXT):
        close_share = (
            Decimal(total_budget) * close_qty / (close_qty + open_qty)
        ).to_integral_value(rounding=ROUND_FLOOR)
    close_budget = int(close_share)
    # Guarantee each positive leg at least one slice, and keep the sum exact.
    close_budget = max(1, min(close_budget, total_budget - 1))
    return close_budget, total_budget - close_budget
