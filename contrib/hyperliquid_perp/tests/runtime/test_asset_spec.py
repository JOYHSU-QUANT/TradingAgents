"""The per-asset spec and the two precision steps behind it."""

from __future__ import annotations

from decimal import Decimal

import pytest

from contrib.hyperliquid_perp.domains.perp.margin import MarginSchedule, MarginTier
from contrib.hyperliquid_perp.runtime.asset_spec import (
    AssetSpec,
    price_tick_from_sz_decimals,
    qty_step_from_sz_decimals,
)

D = Decimal


def _schedule(coin: str = "BTC") -> MarginSchedule:
    return MarginSchedule(coin=coin, tiers=(MarginTier(D(0), D(50)),))


def test_qty_step_from_sz_decimals():
    assert qty_step_from_sz_decimals(0) == D(1)
    assert qty_step_from_sz_decimals(2) == D("0.01")
    assert qty_step_from_sz_decimals(5) == D("0.00001")


def test_price_tick_from_sz_decimals():
    assert price_tick_from_sz_decimals(5) == D("0.1")
    assert price_tick_from_sz_decimals(4) == D("0.01")


@pytest.mark.parametrize("step", [qty_step_from_sz_decimals, price_tick_from_sz_decimals])
def test_a_negative_sz_decimals_is_refused_by_both_steps(step):
    with pytest.raises(ValueError, match="szDecimals must be >= 0, got -1"):
        step(-1)


def test_asset_spec_derives_both_steps_from_sz_decimals():
    spec = AssetSpec(coin="BTC", sz_decimals=5, margin_schedule=_schedule())
    assert spec.qty_step == D("0.00001")
    assert spec.tick_size == D("0.1")


@pytest.mark.parametrize("coin", ["", "   "])
def test_asset_spec_refuses_a_blank_coin(coin):
    with pytest.raises(ValueError, match="AssetSpec.coin must be a non-empty string"):
        AssetSpec(coin=coin, sz_decimals=5, margin_schedule=_schedule())


def test_asset_spec_refuses_another_assets_margin_schedule():
    with pytest.raises(ValueError, match="AssetSpec.margin_schedule is for 'ETH', not 'BTC'"):
        AssetSpec(coin="BTC", sz_decimals=5, margin_schedule=_schedule("ETH"))
