"""Tests for rendering a PerpMarketContext into prompt text.

This is the exact text ``--context-only`` prints, so the invariants under test
are: ``None`` renders as ``n/a`` (never ``NaN``), funding is shown in basis
points, the z-score keeps a sign, and the optional mid/premium lines drop out
when absent.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from contrib.hyperliquid_perp.domains.perp.prompt_context import render_market_context
from contrib.hyperliquid_perp.domains.perp.schema import (
    MarketRegime,
    PerpMarketContext,
    ProfileShape,
    VolumeProfile,
)

_AS_OF = datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


def _ctx(**overrides) -> PerpMarketContext:
    base = {
        "coin": "BTC",
        "as_of": _AS_OF,
        "candle_interval": "4h",
        "candle_count": 200,
        "mark_price": Decimal("60000.0"),
        "oracle_price": Decimal("60001.0"),
        "prev_day_price": Decimal("58000.0"),
        "mid_price": Decimal("60000.5"),
        "day_change_pct": 3.4482758620689653,
        "open_interest": Decimal("1234.5"),
        "day_ntl_volume": Decimal("987654.0"),
        "funding_rate": Decimal("0.0000125"),
        "funding_premium": Decimal("0.000002"),
        "funding_zscore_30d": 1.2345,
        "funding_window_days": 30,
        "funding_sample_count": 240,
        "indicators": {"rsi_14": 55.5, "ema_20": 60100.0, "macd": None},
        "market_regime": "trending",
    }
    base.update(overrides)
    return PerpMarketContext(**base)


def test_render_full_context_includes_all_sections():
    text = render_market_context(_ctx())
    assert "Coin: BTC (perpetual)" in text
    assert "As of: 2024-01-02T03:04:05+00:00 UTC" in text
    assert "Candles: 200 x 4h" in text
    for header in ("Price:", "Market:", "Funding:", "Indicators:"):
        assert header in text
    assert "Regime (computed): trending" in text
    assert "Regime note:" in text


@pytest.mark.parametrize("regime", list(MarketRegime))
def test_regime_note_covers_every_regime_without_directional_framing(regime):
    # The cost-awareness note is keyed to the computed regime (exhaustive over
    # MarketRegime) and must stay free of long/short directional framing —
    # asserted on the note line only, so unrelated context wording (e.g.
    # "longer window") can never trip this guard.
    text = render_market_context(_ctx(market_regime=regime.value))
    note = next(line for line in text.splitlines() if line.strip().startswith("Regime note:"))
    assert note.strip() != "Regime note:"
    assert "long" not in note
    assert "short" not in note


# One distinctive substring per regime: the shape checks above pass even if
# two _REGIME_NOTE entries are swapped or copy-pasted (each note individually
# is non-empty and direction-free), which would silently feed the model the
# wrong regime's cost advice. Indexing by regime keeps this exhaustive — a new
# MarketRegime member fails here with a KeyError until it gets its own marker.
_NOTE_MARKER = {
    MarketRegime.TRENDING: "with the trend",
    MarketRegime.RANGING: "rarely earns back its fees",
    MarketRegime.VOLATILE: "on conviction, not on noise",
}


@pytest.mark.parametrize("regime", list(MarketRegime))
def test_regime_note_renders_each_regimes_own_text(regime):
    text = render_market_context(_ctx(market_regime=regime.value))
    note = next(line for line in text.splitlines() if line.strip().startswith("Regime note:"))
    assert _NOTE_MARKER[regime] in note


def test_render_funding_in_basis_points_and_signed_zscore():
    text = render_market_context(_ctx())
    # 0.0000125 * 1e4 = 0.125 bps.
    assert "Current rate: 0.1250 bps (per hour)" in text
    # z-score keeps an explicit sign and two decimals, with the sample count.
    assert "30d z-score: +1.23 (n=240)" in text


def test_render_day_change_and_indicator_formatting():
    text = render_market_context(_ctx())
    assert "24h change: 3.45%" in text
    # Known indicators get their label; values use 4 decimals.
    assert "RSI(14): 55.5000" in text
    assert "EMA(20): 60,100.0000" in text


def test_render_every_known_indicator_gets_its_own_label():
    # Each _INDICATOR_LABEL entry paired with a distinct value: a swapped or
    # copy-pasted label (e.g. ema_50 rendered as "EMA(20)") passes any
    # subset/shape check but mislabels the indicator the model reasons over.
    text = render_market_context(
        _ctx(indicators={"rsi_14": 1.0, "ema_20": 2.0, "ema_50": 3.0, "atr_14": 4.0, "macd": 5.0})
    )
    assert "RSI(14): 1.0000" in text
    assert "EMA(20): 2.0000" in text
    assert "EMA(50): 3.0000" in text
    assert "ATR(14): 4.0000" in text
    assert "MACD: 5.0000" in text


def test_render_none_values_become_na_never_nan():
    text = render_market_context(
        _ctx(
            mid_price=None,
            day_change_pct=None,
            funding_premium=None,
            funding_zscore_30d=None,
            indicators={"macd": None},
        )
    )
    assert "NaN" not in text
    # Optional mid/premium lines are dropped entirely when absent.
    assert "Mid:" not in text
    assert "Premium:" not in text
    # n/a stand-ins for the missing scalars.
    assert "24h change: n/a%" in text
    assert "n/a (insufficient data)" in text
    assert "MACD: n/a" in text


def test_render_unknown_indicator_falls_back_to_raw_name():
    text = render_market_context(_ctx(indicators={"bollinger_99": 1.0}))
    assert "bollinger_99: 1.0000" in text


def test_market_regime_string_is_coerced_to_enum():
    # A plain string is accepted and coerced, so callers/fixtures stay terse.
    ctx = _ctx(market_regime="volatile")
    assert ctx.market_regime is MarketRegime.VOLATILE


def test_unknown_market_regime_raises_at_construction():
    # The point of the enum: an unknown regime fails here, not later at decision
    # time where it would burn an engine run before raising.
    with pytest.raises(ValueError, match="nonsense"):
        _ctx(market_regime="nonsense")


# --------------------------------------------------------------------------
# Volume profile — an OPTIONAL section that is present in full or not at all.
# --------------------------------------------------------------------------


def _profile(**overrides) -> VolumeProfile:
    base = {
        "shape": ProfileShape.P,
        "poc": Decimal("63450.0"),
        "value_area_low": Decimal("61900.0"),
        "value_area_high": Decimal("65100.0"),
        "range_low": Decimal("60100.0"),
        "range_high": Decimal("66800.0"),
        "poc_position": 0.62,
        "close_position": 0.71,
        "value_area_width_ratio": 0.4583333333333333,
        "candle_count": 30,
        "bucket_count": 24,
    }
    base.update(overrides)
    return VolumeProfile(**base)


def test_volume_profile_section_is_absent_when_the_profile_is():
    text = render_market_context(_ctx())
    assert _ctx().volume_profile is None
    # The WHOLE block drops out — not a header with n/a under it, which would
    # read as a measurement that came back empty rather than one never taken.
    for marker in ("Volume profile", "POC", "Value area", "Shape:"):
        assert marker not in text


def test_adding_a_volume_profile_changes_nothing_else_in_the_render():
    # Merging the feature must not perturb any existing line: the section is
    # appended, and everything above it stays byte-identical.
    without = render_market_context(_ctx())
    with_profile = render_market_context(_ctx(volume_profile=_profile()))
    assert with_profile.startswith(without)


def test_volume_profile_section_reports_every_level_and_the_window():
    text = render_market_context(_ctx(volume_profile=_profile()))
    assert "Volume profile (rolling window of 30 x 4h candles):" in text
    assert "Range: 60,100.00 - 66,800.00" in text
    assert "POC (most-traded price): 63,450.00 (62% up the range)" in text
    assert "Value area (70% of volume): 61,900.00 - 65,100.00 (46% of the range width)" in text
    assert "Latest close sits 71% up the range" in text
    assert "NaN" not in text


def test_volume_profile_states_the_coarse_candle_basis():
    # Design decision 2 in the plan: the levels are inferred from OHLCV bars,
    # not measured from ticks, and the prompt has to say so or the model will
    # reasonably read "POC: 63,450" as a measured volume peak.
    text = render_market_context(_ctx(volume_profile=_profile()))
    basis = next(line for line in text.splitlines() if line.strip().startswith("Basis:"))
    assert "spread evenly" in basis
    assert "24 price levels" in basis
    assert "not tick data" in basis


def test_volume_profile_window_label_follows_the_contexts_candle_interval():
    # The label must not hard-code "4h": a context built on another interval
    # would otherwise describe the window as a length it is not.
    text = render_market_context(
        _ctx(candle_interval="1h", volume_profile=_profile(candle_count=48))
    )
    assert "rolling window of 48 x 1h candles" in text


@pytest.mark.parametrize("shape", list(ProfileShape))
def test_every_shape_renders_its_letter_and_a_note(shape):
    text = render_market_context(_ctx(volume_profile=_profile(shape=shape)))
    line = next(line for line in text.splitlines() if line.strip().startswith("Shape:"))
    # ``.value``, never the (str, Enum) member — an f-string on the member
    # renders "ProfileShape.P" under 3.12 and corrupts the prompt.
    assert line.strip().startswith(f"Shape: {shape.value} —")
    assert "ProfileShape." not in text
    assert len(line.strip()) > len(f"Shape: {shape.value} —") + 10


# One distinctive substring per shape. The shape checks above pass even if two
# _SHAPE_NOTE entries are swapped (each is individually non-empty), which would
# feed the model the wrong description of where volume actually sat. Indexing
# by shape keeps this exhaustive — a new ProfileShape member fails with a
# KeyError until it gets its own marker.
_SHAPE_MARKER = {
    ProfileShape.D: "near the middle of the range",
    ProfileShape.P: "upper part of the range",
    ProfileShape.B: "lower part of the range",
    ProfileShape.THIN: "spread thinly across the range",
}


@pytest.mark.parametrize("shape", list(ProfileShape))
def test_each_shape_renders_its_own_note(shape):
    text = render_market_context(_ctx(volume_profile=_profile(shape=shape)))
    line = next(line for line in text.splitlines() if line.strip().startswith("Shape:"))
    assert _SHAPE_MARKER[shape] in line


def test_the_d_note_asserts_nothing_positive_about_the_distribution():
    # D is classify_shape's catch-all: it fires both for a centred POC and for
    # a POC skewed to one end whose close did not confirm it. So the note must
    # not claim centrality — a skewed-POC D is the MAJORITY case, and the claim
    # would sit one line under a POC reading that contradicts it. Rendered here
    # with a POC at 95% of the range, which is a legal D.
    text = render_market_context(
        _ctx(volume_profile=_profile(shape=ProfileShape.D, poc_position=0.95))
    )
    line = next(line for line in text.splitlines() if line.strip().startswith("Shape:"))
    assert "POC (most-traded price): 63,450.00 (95% up the range)" in text
    # No positive claim about where the volume sat...
    assert "volume is concentrated" not in line
    # ...it names itself as the catch-all, admits the skewed case, and points
    # the reader at the numbers rather than the letter.
    assert "catch-all" in line
    assert "skewed" in line
    assert "read the POC position above" in line
    assert "Does not test symmetry" in line
