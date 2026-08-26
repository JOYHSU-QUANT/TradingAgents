"""Tests for the typed ``market_data:`` block (issue #96).

The load-time contract — which YAML shapes the loader accepts and the exact
refusal wording — is pinned end to end in ``tests/test_config.py``. This file
covers the dataclass itself: the cross-field rule on direct construction (the
loader is not the only way to build one), the null rule, and the seam it
shares with the other four config blocks.
"""

from __future__ import annotations

import pytest

from contrib.hyperliquid_perp.common.constants import MIN_VOLUME_PROFILE_WINDOW
from contrib.hyperliquid_perp.domains.perp.market_data_config import MarketDataConfig
from contrib.hyperliquid_perp.domains.perp.schema import CandleInterval


def test_from_dict_treats_absent_blank_and_empty_alike():
    assert MarketDataConfig.from_dict(None) == MarketDataConfig()
    assert MarketDataConfig.from_dict({}) == MarketDataConfig()
    assert (
        MarketDataConfig.from_dict({"candle_lookback": None, "candle_interval": None})
        == MarketDataConfig()
    )


def test_the_cross_field_rule_holds_on_direct_construction():
    # The window/lookback comparison lives in __post_init__, not in the
    # loader, so a caller building the object by hand gets the same refusal.
    MarketDataConfig(candle_lookback=30, volume_profile_window_candles=30)  # equal is legal
    with pytest.raises(ValueError, match=r"\(31\) exceeds 'market_data.candle_lookback' \(30\)"):
        MarketDataConfig(candle_lookback=30, volume_profile_window_candles=31)


def test_the_profile_floor_is_the_shared_constant():
    # The floor is read from common.constants (the loader must not import the
    # compute module that also reads it); pin that the refusal band is
    # exactly [1, floor) and not a retyped number.
    MarketDataConfig(volume_profile_window_candles=MIN_VOLUME_PROFILE_WINDOW)
    with pytest.raises(ValueError, match=f"at least {MIN_VOLUME_PROFILE_WINDOW}"):
        MarketDataConfig(volume_profile_window_candles=MIN_VOLUME_PROFILE_WINDOW - 1)


@pytest.mark.parametrize("interval", [i.value for i in CandleInterval])
def test_every_supported_interval_is_accepted(interval):
    assert MarketDataConfig(candle_interval=interval).candle_interval == interval


def test_an_unsupported_interval_is_refused_naming_the_key_and_the_legal_set():
    with pytest.raises(ValueError, match="'market_data.candle_interval'") as exc_info:
        MarketDataConfig(candle_interval="4H")
    # The legal set is interval_to_ms's message, not a second copy here.
    assert "'4H'" in str(exc_info.value)
    assert "'1d'" in str(exc_info.value)


def test_unknown_keys_are_refused_by_the_shared_seam():
    # The reason the class exists: the block used to have no parser, and a
    # typo'd key silently fell back to its default (issue #96).
    with pytest.raises(ValueError, match="unknown config key\\(s\\): 'candle_lookbak'"):
        MarketDataConfig.from_dict({"candle_lookbak": 50})
