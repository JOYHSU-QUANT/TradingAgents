"""Tests for SL/TP price math (execution §2–§4)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from contrib.hyperliquid_perp.paper.stops import (
    StopAction,
    StopConfig,
    round_to_tick,
    stop_loss_decision,
    take_profit_price,
)
from contrib.hyperliquid_perp.persistence.models import Side

D = Decimal
_TICK = D("0.1")


# --------------------------------------------------------------------------
# round_to_tick — the wire-price legalizer (5 sig figs + tick, 2026-07-28)
# --------------------------------------------------------------------------


def test_round_to_tick_clamps_to_five_significant_figures():
    # DESIGN.md "Order 限制": a non-integer px with >5 significant figures is
    # refused at the wire; integers are always legal — so six-figure prices
    # land on the 1s grid (integer exemption), not the 10s.
    assert round_to_tick(D("119416.845"), D("0.1"), up=True) == D("119417")
    assert round_to_tick(D("119416.845"), D("0.1"), up=False) == D("119416")
    assert round_to_tick(D("59117.25"), D("0.1"), up=False) == D("59117")
    # Four-figure price: the 5-sig-fig step (0.1) is coarser than the tick.
    assert round_to_tick(D("3456.78"), D("0.01"), up=False) == D("3456.7")
    assert round_to_tick(D("3456.78"), D("0.01"), up=True) == D("3456.8")


def test_round_to_tick_small_prices_keep_full_tick_precision():
    # ≤5 sig figs on the tick grid: the clamp must be a no-op.
    assert round_to_tick(D("100.05"), D("0.01"), up=True) == D("100.05")
    assert round_to_tick(D("92.53"), D("0.1"), up=False) == D("92.5")


def test_round_to_tick_is_idempotent_in_both_directions():
    # §8.3 identical-resend depends on determinism: a legal price re-rounded in
    # EITHER direction must return unchanged.
    for px, tick in ((D("119417"), D("0.1")), (D("3456.8"), D("0.01")), (D("92.5"), D("0.1"))):
        assert round_to_tick(px, tick, up=True) == px
        assert round_to_tick(px, tick, up=False) == px


def test_round_to_tick_direction_survives_the_coarser_quantum():
    # CEILING stays a ceiling on the sig-fig grid (never rounds toward the
    # nearer but wrong side), and crossing a power boundary is fine.
    assert round_to_tick(D("119416.05"), D("0.1"), up=True) == D("119417")
    assert round_to_tick(D("99999.96"), D("0.1"), up=True) == D("100000")


def test_round_to_tick_non_power_of_ten_tick_falls_back_to_tick_only():
    # A test grid the sig-fig step does not evenly divide keeps old behaviour.
    assert round_to_tick(D("119416.85"), D("0.3"), up=False) == D("119416.8")


# --------------------------------------------------------------------------
# StopConfig
# --------------------------------------------------------------------------


def test_stop_config_derives_target_midpoint():
    cfg = StopConfig()
    assert cfg.sl_target_pct == D("0.075")


def test_stop_config_rejects_min_ge_max():
    with pytest.raises(ValueError):
        StopConfig(sl_min_pct=D("0.10"), sl_max_pct=D("0.05"))


# --------------------------------------------------------------------------
# Long SL (§3.3 / §3.6 / §3.7)
# --------------------------------------------------------------------------


def test_long_sl_entry_based_no_liquidation():
    # target = 100*(1-0.075) = 92.5, in band [90, 95]
    d = stop_loss_decision(
        side=Side.BUY, entry_price=D(100), liquidation_price=None, tick_size=_TICK
    )
    assert d.action is StopAction.PLACE
    assert d.price == D("92.5")


def test_long_sl_target_binds_when_liq_far():
    d = stop_loss_decision(
        side=Side.BUY, entry_price=D(100), liquidation_price=D(80), tick_size=_TICK
    )
    # liq_floor = 88 < target 92.5 -> SL = 92.5
    assert d.action is StopAction.PLACE
    assert d.price == D("92.5")


def test_long_sl_liq_buffer_binds():
    d = stop_loss_decision(
        side=Side.BUY, entry_price=D(100), liquidation_price=D(85), tick_size=_TICK
    )
    # liq_floor = 93.5 > target 92.5 -> SL = 93.5 (still in band [90, 95])
    assert d.action is StopAction.PLACE
    assert d.price == D("93.5")


def test_long_sl_risk_gate_closes_when_liq_too_close():
    d = stop_loss_decision(
        side=Side.BUY, entry_price=D(100), liquidation_price=D(88), tick_size=_TICK
    )
    # liq_floor = 96.8 >= band_hi 95 -> market close
    assert d.action is StopAction.CLOSE_NOW
    assert d.reason == "liquidation_too_close"
    assert d.price is None


# --------------------------------------------------------------------------
# Short SL (§3.4 / §3.6 / §3.7)
# --------------------------------------------------------------------------


def test_short_sl_entry_based_no_liquidation():
    d = stop_loss_decision(
        side=Side.SELL, entry_price=D(100), liquidation_price=None, tick_size=_TICK
    )
    # target = 100*(1+0.075) = 107.5, band [105, 110]
    assert d.action is StopAction.PLACE
    assert d.price == D("107.5")


def test_short_sl_liq_buffer_binds():
    d = stop_loss_decision(
        side=Side.SELL, entry_price=D(100), liquidation_price=D(118), tick_size=_TICK
    )
    # liq_ceil = 106.2 < target 107.5 -> SL = 106.2
    assert d.action is StopAction.PLACE
    assert d.price == D("106.2")


def test_short_sl_risk_gate_closes_when_liq_too_close():
    d = stop_loss_decision(
        side=Side.SELL, entry_price=D(100), liquidation_price=D(115), tick_size=_TICK
    )
    # liq_ceil = 103.5 <= band_lo 105 -> market close
    assert d.action is StopAction.CLOSE_NOW
    assert d.reason == "liquidation_too_close"


# --------------------------------------------------------------------------
# post-rounding out-of-band (§3.2 / §3.6: the rounded SL leaves the legal band)
# --------------------------------------------------------------------------


def test_long_sl_out_of_range_after_rounding():
    # tick 8: target 92.5 rounds up to 96 > band_hi 95 -> market close, never an
    # illegal SL placement.
    d = stop_loss_decision(
        side=Side.BUY, entry_price=D(100), liquidation_price=None, tick_size=D(8)
    )
    assert d.action is StopAction.CLOSE_NOW
    assert d.reason == "sl_out_of_range"


def test_short_sl_out_of_range_after_rounding():
    # tick 8: target 107.5 rounds down to 104 < band_lo 105 -> market close.
    d = stop_loss_decision(
        side=Side.SELL, entry_price=D(100), liquidation_price=None, tick_size=D(8)
    )
    assert d.action is StopAction.CLOSE_NOW
    assert d.reason == "sl_out_of_range"


# --------------------------------------------------------------------------
# Take profit (§4.2)
# --------------------------------------------------------------------------


def test_long_take_profit():
    assert take_profit_price(side=Side.BUY, entry_price=D(100), tick_size=_TICK) == D(120)


def test_short_take_profit():
    assert take_profit_price(side=Side.SELL, entry_price=D(100), tick_size=_TICK) == D(80)


# --------------------------------------------------------------------------
# guards
# --------------------------------------------------------------------------


def test_rejects_bad_inputs():
    with pytest.raises(ValueError):
        stop_loss_decision(side=Side.BUY, entry_price=D(0), liquidation_price=None, tick_size=_TICK)
    with pytest.raises(ValueError):
        stop_loss_decision(
            side=Side.BUY, entry_price=D(100), liquidation_price=D(-1), tick_size=_TICK
        )
    with pytest.raises(ValueError):
        take_profit_price(side=Side.BUY, entry_price=D(100), tick_size=D(0))
