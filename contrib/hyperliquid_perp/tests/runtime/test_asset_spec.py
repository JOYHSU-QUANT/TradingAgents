"""The per-asset spec and the two precision steps behind it."""

from __future__ import annotations

from decimal import Decimal

import pytest

from contrib.hyperliquid_perp.domains.perp.margin import MarginSchedule, MarginTier
from contrib.hyperliquid_perp.exchanges.hyperliquid.errors import ExchangeError
from contrib.hyperliquid_perp.runtime.asset_spec import (
    AssetSpec,
    build_asset_spec,
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


class _Market:
    """The one read ``build_asset_spec`` makes, scripted."""

    def __init__(self, meta):
        self._meta = meta
        self.calls: list[str] = []

    def get_asset_meta(self, coin):
        self.calls.append(coin)
        if isinstance(self._meta, Exception):
            raise self._meta
        return self._meta


def test_build_asset_spec_reads_one_meta_and_builds_the_spec():
    market = _Market((5, _schedule()))
    spec = build_asset_spec(market, "BTC")
    assert spec == AssetSpec(coin="BTC", sz_decimals=5, margin_schedule=_schedule())
    assert spec.qty_step == D("0.00001")
    assert market.calls == ["BTC"]


def test_build_asset_spec_lets_the_venue_failure_through_unchanged():
    # Nothing here catches or rewraps it; each caller decides what a venue
    # failure at this read means.
    market = _Market(ExchangeError("meta endpoint down"))
    with pytest.raises(ExchangeError, match="meta endpoint down"):
        build_asset_spec(market, "BTC")


def test_build_asset_spec_refuses_a_schedule_for_another_coin():
    market = _Market((5, _schedule("ETH")))
    with pytest.raises(ValueError, match="AssetSpec.margin_schedule is for 'ETH', not 'BTC'"):
        build_asset_spec(market, "BTC")
