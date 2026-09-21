"""Tests for the typed ``market_data:`` block (issue #96).

The load-time contract — which YAML shapes the loader accepts and the exact
refusal wording — is pinned end to end in ``tests/test_config.py``. This file
covers the dataclass itself: the cross-field rule on direct construction (the
loader is not the only way to build one), the null rule, and the seam it
shares with the other four config blocks.
"""

from __future__ import annotations

import pytest

from contrib.hyperliquid_perp.common.constants import (
    MAX_MACRO_TREND_LOOKBACK,
    MIN_MACRO_TREND_LOOKBACK,
    MIN_VOLUME_PROFILE_WINDOW,
)
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


def test_the_macro_trend_band_is_the_shared_constants_on_both_sides():
    # Same layering rule as the profile floor above: the loader enforces the
    # band without importing the compute module that also reads it. Both ends
    # are pinned to the constants rather than to retyped numbers, and both
    # edges are exercised from INSIDE and OUT — the legal value at each bound
    # and the first illegal one past it.
    MarketDataConfig(macro_trend_daily_lookback=0)  # the off switch
    MarketDataConfig(macro_trend_daily_lookback=MIN_MACRO_TREND_LOOKBACK)
    MarketDataConfig(macro_trend_daily_lookback=MAX_MACRO_TREND_LOOKBACK)
    with pytest.raises(ValueError, match=f"at least {MIN_MACRO_TREND_LOOKBACK}"):
        MarketDataConfig(macro_trend_daily_lookback=MIN_MACRO_TREND_LOOKBACK - 1)
    with pytest.raises(ValueError, match="must be >= 0"):
        MarketDataConfig(macro_trend_daily_lookback=-1)
    # 1 is the other end of the sub-floor band, and the one the floor message
    # has to cover as well as 199.
    with pytest.raises(ValueError, match=f"at least {MIN_MACRO_TREND_LOOKBACK}"):
        MarketDataConfig(macro_trend_daily_lookback=1)
    # A ceiling, unlike the profile window — because both ways this one goes
    # wrong at the top end degrade SILENTLY rather than fail: measurable
    # Decimal time on the single-threaded live tick, and past ~20,700 a
    # negative computed ``startTime``. Neither raises, so an operator would
    # never learn of either.
    with pytest.raises(ValueError, match=f"at most {MAX_MACRO_TREND_LOOKBACK}"):
        MarketDataConfig(macro_trend_daily_lookback=MAX_MACRO_TREND_LOOKBACK + 1)


def test_the_macro_trend_lookback_is_not_cross_checked_against_the_candle_lookback():
    # The volume profile's window is cut from the SAME series candle_lookback
    # fetches, so a window wider than it can never be filled. The macro trend
    # fetches its own daily series, and the two count bars of different
    # lengths — so the recommended pairing (200 4h candles, 260 daily ones)
    # must be legal. A cross-check copied over from the profile would refuse
    # exactly the configuration the docs tell an operator to write.
    config = MarketDataConfig(candle_lookback=200, macro_trend_daily_lookback=260)
    assert config.macro_trend_daily_lookback == 260


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


def test_the_research_signal_switch_is_off_by_default():
    # Off is the empty string, not a missing key: merging PR C1 changes no
    # existing prompt until an operator writes a path.
    assert MarketDataConfig().autoresearch_signal == ""
    assert MarketDataConfig.from_dict({"autoresearch_signal": None}).autoresearch_signal == ""
    assert (
        MarketDataConfig.from_dict({"autoresearch_signal": "/srv/signal.json"}).autoresearch_signal
        == "/srv/signal.json"
    )


@pytest.mark.parametrize("written", [False, True])
def test_a_yaml_boolean_switch_is_refused_with_what_to_write_instead(written):
    # The trap this key has its own converter for: YAML 1.1 reads a bare
    # ``off`` / ``no`` / ``false`` as a BOOLEAN, and ``off`` is exactly what
    # an operator reaches for on a switch. The refusal has to say what the
    # two legal spellings are, not merely that a bool is not a string.
    with pytest.raises(ValueError, match="got a YAML boolean") as exc_info:
        MarketDataConfig.from_dict({"autoresearch_signal": written})
    message = str(exc_info.value)
    assert "autoresearch_signal" in message
    assert "quote the path" in message


def test_a_non_string_switch_is_refused_by_name():
    with pytest.raises(ValueError, match="expected a path to the research signal document"):
        MarketDataConfig.from_dict({"autoresearch_signal": 3})


@pytest.mark.parametrize("value", ["  ", " /srv/signal.json", "/srv/signal.json "])
def test_a_switch_that_is_only_whitespace_or_padded_is_refused(value):
    # Nothing will ever write to a path made of spaces, and its one runtime
    # symptom would be a missing prompt section — indistinguishable from the
    # feature being off on purpose. So it is a load-time failure, like every
    # other way of getting this block wrong.
    with pytest.raises(ValueError, match="leading or trailing whitespace"):
        MarketDataConfig(autoresearch_signal=value)
