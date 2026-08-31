"""Hyperliquid maintenance-margin tier model (pure, all-Decimal).

Phase 2's paper liquidation model (``paper/liquidation.py``, execution §6.6.1)
needs the exchange's maintenance-margin schedule to answer "how much margin
must this notional keep?". Hyperliquid publishes a tiered schedule per asset:
larger notional is allowed less leverage, so the maintenance-margin *rate*
steps **up** as notional crosses each tier boundary. The rate for a tier is
``1 / (2 * tier_max_leverage)`` (execution §6.6.1) — half the tier's initial
margin fraction.

A naive per-tier ``notional * rate`` would jump discontinuously at every
boundary. Hyperliquid keeps ``maintenance_margin(notional)`` continuous with a
per-tier *deduction* accumulated across boundaries, so this module precomputes
each tier's rate and deduction once and exposes:

- :meth:`MarginSchedule.tier_for_notional` — the tier a candidate notional sits
  in (the liquidation search re-selects the tier at every candidate price,
  because a price move can cross a boundary);
- :meth:`MarginSchedule.maintenance_margin` — the continuous maintenance margin.

The mapper (:mod:`...exchanges.hyperliquid.mapper`) builds a schedule from the
``meta`` response; nothing here knows Hyperliquid's raw field names.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, localcontext

# The one decimal context for money math now lives in the dependency-free
# common layer (see its module docstring for the pin rationale). Re-exported
# from its original home for importers deliberately left on this path:
# paper/engine stays untouched under the phase3-spec §2.1 freeze, and
# out-of-tree consumers may still read it from here.
from ...common.decimal_context import DECIMAL_CONTEXT

__all__ = [
    "DECIMAL_CONTEXT",
    "MarginTier",
    "MarginSchedule",
    "account_equity",
    "funding_cost",
    "position_notional",
    "unrealized_pnl",
]


# Base pure formulas (execution §6), defined here — not in ``paper/accounting``
# — because the liquidation search needs them too and must stay free of the
# accounting layer's persistence imports. One definition each: an inline
# re-derivation that drifted (say, a future contract multiplier applied to one
# copy) would silently disagree between account metrics and the liquidation
# estimate. Callers pin ``DECIMAL_CONTEXT``; these stay plain arithmetic.


def position_notional(size: Decimal, mark_price: Decimal) -> Decimal:
    """``abs(size * mark_price)`` — a single position's notional (§6.1)."""
    return abs(size * mark_price)


def unrealized_pnl(size: Decimal, mark_price: Decimal, entry_price: Decimal) -> Decimal:
    """``size * (mark - entry)`` — signed, long positive on a rise (§6.3)."""
    return size * (mark_price - entry_price)


def account_equity(wallet_balance: Decimal, total_unrealized_pnl: Decimal) -> Decimal:
    """``wallet_balance + total_unrealized_pnl`` (§6.1)."""
    return wallet_balance + total_unrealized_pnl


def funding_cost(signed_position_notional: Decimal, funding_rate: Decimal) -> Decimal:
    """``signed_position_notional * funding_rate`` — one hour's funding, COST-signed (§6.5).

    Positive means the position PAYS: a long (positive notional) at a positive
    rate pays, a short receives. The ledger states the same quantity the other
    way round — income positive — so ``paper.accounting.funding_pnl`` is this
    negated, and the prompt's holding cost (``marginal_cost``) is this times
    the horizon it states. One formula, three readers: a sign that drifted in
    one copy would have the prompt calling a rebate a cost while the books
    credited it (issue #134).
    """
    return signed_position_notional * funding_rate


@dataclass(frozen=True)
class MarginTier:
    """One maintenance-margin bracket: applies to notional ``>= lower_bound``.

    ``max_leverage`` is the exchange's max leverage allowed at this notional; the
    maintenance rate derives from it as ``1 / (2 * max_leverage)``. ``lower_bound``
    is an absolute notional value (USDC), not a percentage.
    """

    lower_bound: Decimal
    max_leverage: Decimal

    def __post_init__(self) -> None:
        if self.lower_bound < 0:
            raise ValueError(f"MarginTier.lower_bound must be >= 0, got {self.lower_bound}")
        if self.max_leverage <= 0:
            raise ValueError(f"MarginTier.max_leverage must be > 0, got {self.max_leverage}")

    @property
    def maintenance_margin_rate(self) -> Decimal:
        """``1 / (2 * max_leverage)`` — half the tier's initial-margin fraction."""
        with localcontext(DECIMAL_CONTEXT):  # recomputed per access; pin the division
            return Decimal(1) / (Decimal(2) * self.max_leverage)


@dataclass(frozen=True)
class MarginSchedule:
    """An asset's ordered maintenance-margin tiers with continuity deductions.

    ``coin`` names the asset this schedule belongs to, so a position can be
    verified against its *own* asset's schedule (see ``PositionValuation``) rather
    than being silently valued against another asset's tier table.

    Tiers must be sorted by ascending ``lower_bound``, start at ``0`` (every
    positive notional maps to a tier), and never *increase* max leverage as
    notional grows (the schedule only ever tightens). The per-tier deduction that
    makes ``maintenance_margin`` continuous across boundaries is derived once at
    construction and cached in ``_deductions``.
    """

    coin: str
    tiers: tuple[MarginTier, ...]
    # Derived: cumulative deduction per tier, index-parallel to ``tiers`` (never
    # caller-supplied — always recomputed in __post_init__).
    _deductions: tuple[Decimal, ...] = field(init=False, default=(), compare=False, repr=False)

    def __post_init__(self) -> None:
        if not self.coin:
            raise ValueError("MarginSchedule.coin must be non-empty")
        if not self.tiers:
            raise ValueError("MarginSchedule needs at least one tier")
        if self.tiers[0].lower_bound != 0:
            raise ValueError(
                f"the first margin tier must start at lower_bound 0, got {self.tiers[0].lower_bound}"
            )
        for prev, cur in zip(self.tiers, self.tiers[1:], strict=False):
            if cur.lower_bound <= prev.lower_bound:
                raise ValueError(
                    "margin tiers must have strictly ascending lower_bound, got "
                    f"{prev.lower_bound} then {cur.lower_bound}"
                )
            if cur.max_leverage > prev.max_leverage:
                raise ValueError(
                    "margin tiers must not raise max_leverage as notional grows, got "
                    f"{prev.max_leverage} then {cur.max_leverage}"
                )
        # Continuity deduction: at each boundary the maintenance margin computed
        # with the lower and upper tier must agree, so
        # ded_i = ded_{i-1} + lower_bound_i * (rate_i - rate_{i-1}). ded_0 = 0.
        # Pinned: these are baked in at construction (often outside any pinned
        # accounting scope) and later compared against pinned arithmetic.
        with localcontext(DECIMAL_CONTEXT):
            deductions: list[Decimal] = [Decimal(0)]
            for prev, cur in zip(self.tiers, self.tiers[1:], strict=False):
                deductions.append(
                    deductions[-1]
                    + cur.lower_bound * (cur.maintenance_margin_rate - prev.maintenance_margin_rate)
                )
        object.__setattr__(self, "_deductions", tuple(deductions))

    def _tier_index(self, notional: Decimal) -> int:
        """Index of the tier a non-negative ``notional`` falls in (highest bound <= n)."""
        if notional < 0:
            raise ValueError(f"notional must be >= 0 to select a margin tier, got {notional}")
        index = 0
        for i, tier in enumerate(self.tiers):
            if notional >= tier.lower_bound:
                index = i
            else:
                break
        return index

    def tier_for_notional(self, notional: Decimal) -> MarginTier:
        """The tier a candidate notional sits in (re-selected at every candidate price)."""
        return self.tiers[self._tier_index(notional)]

    def maintenance_margin_rate(self, notional: Decimal) -> Decimal:
        """The maintenance-margin *rate* applicable at ``notional``."""
        return self.tier_for_notional(notional).maintenance_margin_rate

    def maintenance_deduction(self, notional: Decimal) -> Decimal:
        """The continuity deduction applicable at ``notional``."""
        return self._deductions[self._tier_index(notional)]

    def tier_details(self, notional: Decimal) -> tuple[int, MarginTier, Decimal, Decimal]:
        """``(index, tier, deduction, maintenance margin)`` at ``notional`` — one search.

        The snapshot writer needs all four figures for the same notional;
        deriving them here from a single ``_tier_index`` lookup keeps them
        mutually consistent by construction (and spares three re-scans).
        """
        i = self._tier_index(notional)
        tier = self.tiers[i]
        with localcontext(DECIMAL_CONTEXT):
            margin = notional * tier.maintenance_margin_rate - self._deductions[i]
        return i, tier, self._deductions[i], margin

    def maintenance_margin(self, notional: Decimal) -> Decimal:
        """Continuous maintenance margin: ``notional * rate - deduction`` at its tier.

        Never negative: at a tier's own ``lower_bound`` the deduction can never
        exceed ``notional * rate`` (that is exactly what the continuity
        construction guarantees), and the value only grows above the bound.
        """
        return self.tier_details(notional)[3]
