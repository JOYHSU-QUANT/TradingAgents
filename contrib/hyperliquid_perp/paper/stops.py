"""Stop-loss / take-profit price math (execution §2–§4) — pure, all-Decimal.

Both protection orders reduce or close only, never add exposure (execution §2).
This module owns the price formulas and the legality checks; the engine owns the
lifecycle (when to recompute, cancel, or fall back to a market close).

Stop loss (execution §3):

- a *risk gate* (§3.6): when the liquidation price sits so close to entry that no
  SL can be placed in the safe band and still trigger before liquidation, the
  engine must market-close instead of placing an SL — :func:`stop_loss_decision`
  returns ``CLOSE_NOW`` for that case;
- otherwise the SL price (§3.7) is the band-target capped by the liquidation
  buffer, tick-rounded conservatively (long **up**, short **down**, keeping the
  SL away from the liquidation level), then re-validated against the legal band
  (§3.3 / §3.4) — a rounding that leaves the band also yields ``CLOSE_NOW``;
- when there is no positive liquidation price (``estimated_liquidation_price =
  null``, execution §6.6.1), the SL uses the entry-based band only, with no
  liquidation buffer and no gate (the §6.6.1 null-estimate note).

Take profit (execution §4.2) is a flat percentage off entry.

All percentages come from :class:`StopConfig`, defaulting to the execution §3.5 /
§4.2 constants; they are surfaced as config so a future YAML block can override
them without touching this math.
"""

from __future__ import annotations

from dataclasses import InitVar, dataclass, field
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, localcontext
from enum import Enum

from ..domains.perp.margin import DECIMAL_CONTEXT
from ..persistence.models import Side

__all__ = [
    "StopAction",
    "StopConfig",
    "StopLossDecision",
    "round_to_tick",
    "stop_loss_decision",
    "take_profit_price",
]


@dataclass(frozen=True)
class StopConfig:
    """SL/TP band and buffer percentages (execution §3.5 / §4.2 defaults).

    Fractions, not whole percents: ``sl_min_pct = 0.05`` is 5%. ``sl_target_pct``
    is the band midpoint the SL formula aims for; it is *always* a ``Decimal`` after
    construction — derived from min/max, or from the ``sl_target_pct_override`` init
    parameter. Using ``field(init=False)`` (like ``AssetSpec``'s derived steps)
    keeps the stored field's static type honest (``Decimal``, never ``Decimal |
    None``) rather than leaving a nullable placeholder the code always fills in.
    """

    sl_min_pct: Decimal = Decimal("0.05")
    sl_max_pct: Decimal = Decimal("0.10")
    liq_buffer: Decimal = Decimal("0.10")
    tp_threshold: Decimal = Decimal("0.20")
    sl_target_pct_override: InitVar[Decimal | None] = None
    sl_target_pct: Decimal = field(init=False)

    def __post_init__(self, sl_target_pct_override: Decimal | None) -> None:
        for name in ("sl_min_pct", "sl_max_pct", "liq_buffer", "tp_threshold"):
            value = getattr(self, name)
            if value < 0:
                raise ValueError(f"StopConfig.{name} must be >= 0, got {value}")
        if not self.sl_min_pct < self.sl_max_pct:
            raise ValueError(
                f"StopConfig needs sl_min_pct < sl_max_pct, got "
                f"{self.sl_min_pct} / {self.sl_max_pct}"
            )
        if sl_target_pct_override is None:
            with localcontext(DECIMAL_CONTEXT):
                target = (self.sl_min_pct + self.sl_max_pct) / 2
        else:
            if not self.sl_min_pct <= sl_target_pct_override <= self.sl_max_pct:
                raise ValueError("sl_target_pct must lie within [sl_min_pct, sl_max_pct]")
            target = sl_target_pct_override
        object.__setattr__(self, "sl_target_pct", target)


class StopAction(str, Enum):
    """What the engine should do with the stop (execution §3.2 / §3.6)."""

    PLACE = "place"  # place / modify the SL at the returned price
    CLOSE_NOW = "close_now"  # no safe SL exists — market-close the position


@dataclass(frozen=True)
class StopLossDecision:
    """Outcome of the SL computation: a price to place, or a market-close signal.

    ``price`` is present exactly when ``action is PLACE``; ``reason`` names why on
    a ``CLOSE_NOW`` (liquidation too close, or the tick-rounded SL left the legal
    band).
    """

    action: StopAction
    price: Decimal | None
    reason: str | None

    def __post_init__(self) -> None:
        if (self.price is not None) != (self.action is StopAction.PLACE):
            raise ValueError("StopLossDecision.price is present exactly when action is PLACE")
        if self.price is not None and self.price <= 0:
            raise ValueError(f"StopLossDecision.price must be > 0, got {self.price}")
        if (self.reason is not None) != (self.action is StopAction.CLOSE_NOW):
            raise ValueError("StopLossDecision.reason is present exactly when action is CLOSE_NOW")


def round_to_tick(price: Decimal, tick: Decimal, *, up: bool) -> Decimal:
    """Round ``price`` to an exchange-legal price on the ``tick`` grid (up or down).

    Legality is Hyperliquid's px rule (DESIGN.md "Order 限制"), not just the
    tick: a non-integer price may carry at most **5 significant figures**
    (integer prices are always legal). Tick quantization alone leaves a
    six-figure BTC price with 7 significant figures — refused at the wire — so
    this rounds to the coarser of the tick and the 5-sig-fig step, the latter
    capped at 1 (the integer exemption keeps six-figure prices on the 1s grid
    rather than the 10s). Direction survives at the coarser quantum (CEILING
    when ``up`` else FLOOR) so every caller's invariant — marketable side for
    IOC limits, toward-entry for stops — is preserved, and the function stays
    pure and idempotent (a legal price returns unchanged in either direction —
    the §8.3 identical-resend contract depends on that determinism). A tick
    that does not evenly divide the sig-fig step (non-power-of-ten test grids)
    falls back to tick-only rounding.
    """
    if tick <= 0:
        raise ValueError(f"tick must be > 0, got {tick}")
    with localcontext(DECIMAL_CONTEXT):
        quantum = tick
        if price > 0:
            sig_step = min(Decimal(1).scaleb(price.adjusted() - 4), Decimal(1))
            if sig_step > tick and sig_step % tick == 0:
                quantum = sig_step
        steps = (price / quantum).to_integral_value(rounding=ROUND_CEILING if up else ROUND_FLOOR)
        # Rounding up across a power-of-ten boundary (99 999.96 → 100 000) lands
        # on a magnitude whose own step is coarser but still legal (the cap at 1
        # makes the step monotone in magnitude), so one pass suffices.
        return steps * quantum


def _long_sl(
    entry: Decimal, liquidation_price: Decimal | None, tick: Decimal, cfg: StopConfig
) -> StopLossDecision:
    with localcontext(DECIMAL_CONTEXT):
        band_hi = entry * (Decimal(1) - cfg.sl_min_pct)  # closest allowed to entry
        band_lo = entry * (Decimal(1) - cfg.sl_max_pct)  # furthest allowed from entry
        target = entry * (Decimal(1) - cfg.sl_target_pct)
        if liquidation_price is None:
            raw = target  # no liquidation reference: entry-based band only
        else:
            liq_floor = liquidation_price * (Decimal(1) + cfg.liq_buffer)
            # §3.6 risk gate: no SL can sit below band_hi and still clear the buffer.
            if liq_floor >= band_hi:
                return StopLossDecision(StopAction.CLOSE_NOW, None, "liquidation_too_close")
            raw = max(target, liq_floor)  # §3.7
    price = round_to_tick(raw, tick, up=True)  # long: round toward entry (away from liq)
    return _validate_long(price, band_lo, band_hi)


def _validate_long(price: Decimal, band_lo: Decimal, band_hi: Decimal) -> StopLossDecision:
    # Only the band can be violated post-rounding: §3.7's ``max(target, liq_floor)``
    # already puts the raw SL at/above ``liq*(1+buffer)``, and rounding a long SL
    # *up* only moves it further from liquidation — so the §3.3 buffer condition
    # holds by construction and needs no strict re-check (which would spuriously
    # fail when the SL sits exactly on the buffer floor). Rounding up can still
    # push a target that was near ``band_hi`` past it; that is the real out-of-band
    # case, and it must market-close (§3.2 / §3.6).
    if not band_lo <= price <= band_hi:
        return StopLossDecision(StopAction.CLOSE_NOW, None, "sl_out_of_range")
    return StopLossDecision(StopAction.PLACE, price, None)


def _short_sl(
    entry: Decimal, liquidation_price: Decimal | None, tick: Decimal, cfg: StopConfig
) -> StopLossDecision:
    with localcontext(DECIMAL_CONTEXT):
        band_lo = entry * (Decimal(1) + cfg.sl_min_pct)  # closest allowed to entry
        band_hi = entry * (Decimal(1) + cfg.sl_max_pct)  # furthest allowed from entry
        target = entry * (Decimal(1) + cfg.sl_target_pct)
        if liquidation_price is None:
            raw = target
        else:
            liq_ceil = liquidation_price * (Decimal(1) - cfg.liq_buffer)
            # §3.6 risk gate (short).
            if liq_ceil <= band_lo:
                return StopLossDecision(StopAction.CLOSE_NOW, None, "liquidation_too_close")
            raw = min(target, liq_ceil)  # §3.7
    price = round_to_tick(raw, tick, up=False)  # short: round toward entry (away from liq)
    return _validate_short(price, band_lo, band_hi)


def _validate_short(price: Decimal, band_lo: Decimal, band_hi: Decimal) -> StopLossDecision:
    # Symmetric with _validate_long: §3.7's ``min(target, liq_ceil)`` plus
    # rounding *down* keeps the short SL at/below ``liq*(1-buffer)``, so the §3.4
    # buffer holds by construction; only the band can break post-rounding.
    if not band_lo <= price <= band_hi:
        return StopLossDecision(StopAction.CLOSE_NOW, None, "sl_out_of_range")
    return StopLossDecision(StopAction.PLACE, price, None)


def stop_loss_decision(
    *,
    side: Side,
    entry_price: Decimal,
    liquidation_price: Decimal | None,
    tick_size: Decimal,
    config: StopConfig | None = None,
) -> StopLossDecision:
    """The SL to place for a position, or a market-close signal (execution §3).

    ``side`` is the *position* direction (``buy`` = long, ``sell`` = short). A
    ``None`` ``liquidation_price`` means the §6.6.1 estimate had no positive root:
    the SL falls back to the entry-based band with no liquidation buffer and no
    gate (execution §6.6.1 null-estimate note).
    """
    side = Side.parse(side)
    cfg = config or StopConfig()
    if entry_price <= 0:
        raise ValueError(f"entry_price must be > 0, got {entry_price}")
    if liquidation_price is not None and liquidation_price <= 0:
        raise ValueError(f"liquidation_price must be > 0 when given, got {liquidation_price}")
    if tick_size <= 0:
        raise ValueError(f"tick_size must be > 0, got {tick_size}")
    if side is Side.BUY:
        return _long_sl(entry_price, liquidation_price, tick_size, cfg)
    return _short_sl(entry_price, liquidation_price, tick_size, cfg)


def take_profit_price(
    *, side: Side, entry_price: Decimal, tick_size: Decimal, config: StopConfig | None = None
) -> Decimal:
    """The TP price for a position (execution §4.2): a flat threshold off entry.

    Long: ``entry * (1 + tp_threshold)``; short: ``entry * (1 - tp_threshold)``.
    Tick-rounded toward entry (long down, short up) so the simulated TP triggers
    no *later* than the exact threshold.
    """
    side = Side.parse(side)
    cfg = config or StopConfig()
    if entry_price <= 0:
        raise ValueError(f"entry_price must be > 0, got {entry_price}")
    if tick_size <= 0:
        raise ValueError(f"tick_size must be > 0, got {tick_size}")
    with localcontext(DECIMAL_CONTEXT):
        if side is Side.BUY:
            raw = entry_price * (Decimal(1) + cfg.tp_threshold)
            return round_to_tick(raw, tick_size, up=False)
        raw = entry_price * (Decimal(1) - cfg.tp_threshold)
        return round_to_tick(raw, tick_size, up=True)
