"""Paper estimated liquidation price (execution §6.6.1) — pure, all-Decimal.

Solves for the adverse price at which the cross account can no longer maintain
its positions: the root of

    f(p) = account_equity(p) - total_maintenance_margin(p)

for a candidate symbol, holding the other cross positions' contributions fixed at
their current marks (their unrealized PnL and maintenance margin do not depend on
the candidate's price). The maintenance margin is re-evaluated at every candidate
price via the :class:`~..domains.perp.margin.MarginSchedule`, because a price move
can cross a tier boundary.

Direction and bracketing follow the spec:

- **long** — f is increasing in ``p``; search *down* from mark toward ``0``. If
  ``f`` stays positive all the way to ``0`` there is no positive liquidation
  price (``None``).
- **short** — f is decreasing in ``p``; expand a bracket *up* from mark until
  ``f <= 0``, then bisect.
- if ``f(mark) <= 0`` the account is already liquidatable — report that (the
  caller enters the liquidation / emergency-close flow instead of placing an
  ordinary SL).

The root is found by deterministic bisection (a fixed iteration count, so the
same inputs always give the same price) and then rounded conservatively to the
asset tick: long **up**, short **down**.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, localcontext

from ..domains.perp.margin import (
    MarginSchedule,
    account_equity,
    position_notional,
    unrealized_pnl,
)
from ..persistence.models import DECIMAL_CONTEXT

__all__ = [
    "LIQUIDATION_MODEL_VERSION",
    "LiquidationEstimate",
    "MaintenanceSnapshot",
    "estimated_liquidation_price",
    "maintenance_snapshot",
    "price_tick_from_sz_decimals",
]

# Bump when the estimation math changes so recorded snapshots stay attributable.
LIQUIDATION_MODEL_VERSION = "1"

# Enough halvings that the bracket collapses far below any real tick size; fixed
# (not tolerance-based) so the result is bit-for-bit reproducible on replay.
_BISECT_ITERATIONS = 200

# Perp price precision on Hyperliquid: a price may carry up to (6 - szDecimals)
# decimal places, so the tick is 10 ** -(6 - szDecimals).
_PERP_PRICE_MAX_DECIMALS = 6


def price_tick_from_sz_decimals(sz_decimals: int) -> Decimal:
    """The perp price tick implied by an asset's ``szDecimals`` (never hardcoded)."""
    if sz_decimals < 0:
        raise ValueError(f"szDecimals must be >= 0, got {sz_decimals}")
    return Decimal(10) ** -(_PERP_PRICE_MAX_DECIMALS - sz_decimals)


@dataclass(frozen=True)
class MaintenanceSnapshot:
    """The margin-tier figures recorded alongside a position snapshot (§6.6.1)."""

    margin_tier_id: str
    maintenance_margin_rate: Decimal
    maintenance_deduction: Decimal
    maintenance_margin: Decimal

    def __post_init__(self) -> None:
        # Reproducibility fields persisted alongside a position snapshot (§6.6.1):
        # guard the non-negativity a valid tier always yields — like the sibling
        # LiquidationEstimate below — so a hand-built or deserialized snapshot
        # can't carry a negative rate/deduction/margin or an empty tier id.
        if not self.margin_tier_id:
            raise ValueError("MaintenanceSnapshot.margin_tier_id must be non-empty")
        if self.maintenance_margin_rate < 0:
            raise ValueError(
                "MaintenanceSnapshot.maintenance_margin_rate must be >= 0, "
                f"got {self.maintenance_margin_rate}"
            )
        if self.maintenance_deduction < 0:
            raise ValueError(
                "MaintenanceSnapshot.maintenance_deduction must be >= 0, "
                f"got {self.maintenance_deduction}"
            )
        if self.maintenance_margin < 0:
            raise ValueError(
                "MaintenanceSnapshot.maintenance_margin must be >= 0, "
                f"got {self.maintenance_margin}"
            )


def maintenance_snapshot(
    schedule: MarginSchedule, size: Decimal, mark_price: Decimal
) -> MaintenanceSnapshot:
    """Tier / rate / deduction / maintenance margin at the *current* mark notional.

    These are the reproducibility fields phase2-data §12.2 requires on every
    position snapshot; the tier is selected from the notional at the current mark
    (the liquidation search re-selects per candidate price separately).
    """
    with localcontext(DECIMAL_CONTEXT):  # §12.2 reproducibility fields — pin the math
        notional = position_notional(size, mark_price)
        tier_index, tier, deduction, margin = schedule.tier_details(notional)
        return MaintenanceSnapshot(
            margin_tier_id=str(tier_index),
            maintenance_margin_rate=tier.maintenance_margin_rate,
            maintenance_deduction=deduction,
            maintenance_margin=margin,
        )


@dataclass(frozen=True)
class LiquidationEstimate:
    """Result of the estimated-liquidation search.

    ``price`` is the tick-rounded estimate, or ``None`` when no positive
    liquidation price exists (a well-funded long). ``already_liquidatable`` is
    ``True`` when ``f(mark) <= 0`` — the position is at/over its maintenance
    threshold now, so ``price`` is ``None`` and the caller must not place an
    ordinary SL.
    """

    price: Decimal | None
    already_liquidatable: bool

    def __post_init__(self) -> None:
        # The documented contract: already_liquidatable means there is no
        # forward-looking price — a caller matching on ``price is not None`` to
        # place an SL must never see both.
        if self.already_liquidatable and self.price is not None:
            raise ValueError("an already-liquidatable LiquidationEstimate carries no price")


def _round_to_tick(price: Decimal, tick: Decimal, *, up: bool) -> Decimal:
    # tick validity is enforced at the estimated_liquidation_price boundary.
    steps = (price / tick).to_integral_value(rounding=ROUND_CEILING if up else ROUND_FLOOR)
    return steps * tick


def _bisect(f: Callable[[Decimal], Decimal], lo: Decimal, hi: Decimal) -> Decimal:
    """Root of a monotonic ``f`` in ``[lo, hi]`` where ``f(lo)`` and ``f(hi)`` differ in sign."""
    f_lo = f(lo)
    for _ in range(_BISECT_ITERATIONS):
        mid = (lo + hi) / 2
        f_mid = f(mid)
        if f_mid == 0:
            return mid
        # Keep the half that still brackets the sign change.
        if (f_mid > 0) == (f_lo > 0):
            lo, f_lo = mid, f_mid
        else:
            hi = mid
    return (lo + hi) / 2


def estimated_liquidation_price(
    *,
    size: Decimal,
    entry_price: Decimal,
    mark_price: Decimal,
    wallet_balance: Decimal,
    schedule: MarginSchedule,
    tick_size: Decimal,
    other_positions_unrealized_pnl: Decimal = Decimal(0),
    other_positions_maintenance_margin: Decimal = Decimal(0),
) -> LiquidationEstimate:
    """Estimate the candidate position's liquidation price (execution §6.6.1).

    ``wallet_balance`` must already include posted fees / funding / realized PnL
    (they are not re-applied inside ``f``). ``other_positions_*`` fold the rest of
    a cross account in as constants evaluated at their current marks.
    """
    if size == 0:
        raise ValueError("liquidation price is undefined for a flat position")
    if entry_price <= 0 or mark_price <= 0:
        raise ValueError("entry_price and mark_price must be > 0")
    if tick_size <= 0:
        # Fail loud (like every other malformed input here): a zero/negative tick
        # from a bad szDecimals lookup must not silently yield an off-grid price.
        raise ValueError(f"tick_size must be > 0, got {tick_size}")

    def f(p: Decimal) -> Decimal:
        equity = account_equity(
            wallet_balance,
            unrealized_pnl(size, p, entry_price) + other_positions_unrealized_pnl,
        )
        total_maint = (
            schedule.maintenance_margin(position_notional(size, p))
            + other_positions_maintenance_margin
        )
        return equity - total_maint

    with localcontext(DECIMAL_CONTEXT):
        if f(mark_price) <= 0:
            return LiquidationEstimate(price=None, already_liquidatable=True)

        if size > 0:
            # Long: f increases with p. If it is still positive at p -> 0 there is no
            # positive liquidation price.
            if f(Decimal(0)) > 0:
                return LiquidationEstimate(price=None, already_liquidatable=False)
            root = _bisect(f, Decimal(0), mark_price)
            price = _round_to_tick(root, tick_size, up=True)
        else:
            # Short: f decreases with p. Expand the upper bound until f <= 0, then bisect.
            hi = mark_price * 2
            for _ in range(_BISECT_ITERATIONS):
                if f(hi) <= 0:
                    break
                hi *= 2
            else:  # pragma: no cover - short f -> -inf, so a bracket is always found
                return LiquidationEstimate(price=None, already_liquidatable=False)
            root = _bisect(f, mark_price, hi)
            price = _round_to_tick(root, tick_size, up=False)

    return LiquidationEstimate(price=price, already_liquidatable=False)
