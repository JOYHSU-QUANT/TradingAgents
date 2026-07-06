"""Tests for SL/TP price math (execution §2–§4)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from contrib.hyperliquid_perp.paper.stops import (
    StopAction,
    StopConfig,
    stop_loss_decision,
    take_profit_price,
)
from contrib.hyperliquid_perp.persistence.models import Side

D = Decimal
_TICK = D("0.1")


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
