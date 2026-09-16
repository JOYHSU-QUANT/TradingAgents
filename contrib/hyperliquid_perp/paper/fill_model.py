"""Simulated fill pricing (execution §5.2 / §6.4) — pure, all-Decimal.

Every Phase-2 fill (``paper_market``, TWAP slice, SL, TP, gap-stop) is an active
taker fill priced off the **mid** at execution time, plus a fixed slippage in the
adverse direction:

    buy  → mid * (1 + slippage_bps / 10_000)
    sell → mid * (1 - slippage_bps / 10_000)

Mid is the fill *reference* price (mark is only ever the SL/TP *trigger* basis —
execution §5.2), so this module takes mid alone. The slippage direction always
costs the taker: a buy fills above mid, a sell below. Deterministic — the same
(mid, side, bps) always yields the same price — so a replay reproduces fills
exactly.
"""

from __future__ import annotations

from decimal import Decimal, localcontext

from ..common.decimal_context import DECIMAL_CONTEXT
from ..persistence.models import Side
from .stops import round_to_tick

__all__ = ["fill_price", "maker_post_price", "maker_would_fill"]

_BPS_DENOMINATOR = Decimal(10_000)


def fill_price(mid_price: Decimal, side: Side | str, slippage_bps: Decimal) -> Decimal:
    """Simulated taker fill price at ``mid_price`` with adverse ``slippage_bps``.

    ``side`` is the fill direction (``buy`` fills above mid, ``sell`` below).
    Pinned to :data:`DECIMAL_CONTEXT` so the price is bit-for-bit reproducible on
    replay, like every other persisted money figure.
    """
    side = Side.parse(side)
    if mid_price <= 0:
        # Parity with every other price boundary: a non-positive mid is a corrupt
        # feed value, never a live quote — and the fill model must never fabricate
        # a fill from one (execution §5.2).
        raise ValueError(f"mid_price must be > 0, got {mid_price}")
    if slippage_bps < 0:
        raise ValueError(f"slippage_bps must be >= 0, got {slippage_bps}")
    with localcontext(DECIMAL_CONTEXT):
        factor = slippage_bps / _BPS_DENOMINATOR
        if side is Side.BUY:
            return mid_price * (Decimal(1) + factor)
        return mid_price * (Decimal(1) - factor)


def maker_post_price(
    mid_price: Decimal, side: Side | str, half_spread_bps: Decimal, tick: Decimal
) -> Decimal:
    """Where a simulated post-only slice rests (execution §5.2.1).

    The snapshot carries no book, so the touch is modelled as the mid less
    (buy) or plus (sell) an assumed half-spread, rounded to the tick on the
    PASSIVE side (a bid down, an ask up) — the same direction the live maker
    path rounds, so the simulated post can never sit inside the real spread.
    """
    side = Side.parse(side)
    if mid_price <= 0:
        raise ValueError(f"mid_price must be > 0, got {mid_price}")
    if half_spread_bps < 0:
        raise ValueError(f"half_spread_bps must be >= 0, got {half_spread_bps}")
    with localcontext(DECIMAL_CONTEXT):
        factor = half_spread_bps / _BPS_DENOMINATOR
        if side is Side.BUY:
            return round_to_tick(mid_price * (Decimal(1) - factor), tick, up=False)
        return round_to_tick(mid_price * (Decimal(1) + factor), tick, up=True)


def maker_would_fill(
    mid_price: Decimal, side: Side | str, post_price: Decimal, tick: Decimal
) -> bool:
    """Whether a slice posted at ``post_price`` fills on this snapshot (§5.2.1).

    Conservative on purpose: the mid must trade THROUGH the post by at least
    one tick (a buy fills when ``mid <= post - tick``, a sell when
    ``mid >= post + tick``). A mid merely touching the post is queue position
    the simulation cannot know, so it is not a fill.
    """
    side = Side.parse(side)
    if tick <= 0:
        raise ValueError(f"tick must be > 0, got {tick}")
    with localcontext(DECIMAL_CONTEXT):
        if side is Side.BUY:
            return mid_price <= post_price - tick
        return mid_price >= post_price + tick
