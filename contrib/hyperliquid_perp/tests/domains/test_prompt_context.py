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
from contrib.hyperliquid_perp.domains.perp.volume_profile import compute_volume_profile

from .test_volume_profile import (
    _b_shape,
    _candle,
    _classify,
    _d_shape,
    _p_shape,
    _thin_shape,
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
    """A SELF-CONSISTENT profile: the fractions agree with the prices.

    ``VolumeProfile`` cross-checks ``poc_position`` and
    ``value_area_width_ratio`` against the prices they claim to be derived
    from, so a fixture can no longer render "63,450.00 (62% up the range)" for
    a POC that actually sits at 50% of its range — which the first version of
    this fixture did, and which is precisely the self-contradicting line the
    guards exist to keep out of a prompt.

    Range 60,000-66,000 over 24 buckets makes a bucket 250 wide, so every level
    here is a round number and each percentage is hand-checkable. Overriding a
    fraction means overriding its price too.
    """
    base = {
        "shape": ProfileShape.P,
        "poc": Decimal("63625.0"),  # bucket 14's midpoint
        "value_area_low": Decimal("62250.0"),  # bucket 9's lower edge
        "value_area_high": Decimal("65000.0"),  # bucket 19's upper edge
        "range_low": Decimal("60000.0"),
        "range_high": Decimal("66000.0"),
        "poc_position": 29 / 48,  # == (63625 - 60000) / 6000
        "close_position": 0.71,
        "value_area_width_ratio": 11 / 24,  # == (65000 - 62250) / 6000
        "poc_volume_share": 0.18,
        "value_area_volume_share": 0.74,
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
    assert "Volume profile (rolling window of 30 x 4h candles, as of the last closed candle):" in (
        text
    )
    assert "Range: 60,000.00 - 66,000.00" in text
    # Both labels name what was MEASURED — a bucket, and a band that holds "at
    # least" the share. Neither may promote itself back into "most-traded
    # price" (a price the window may never have traded) or a bare "70% of
    # volume" (which the walk overshoots by construction).
    assert "POC (midpoint of the heaviest price bucket): 63,625.00 (60% up the range)" in text
    assert (
        "Value area (band holding at least 70% of volume): "
        "62,250.00 - 65,000.00 (46% of the range width)" in text
    )
    assert "Latest close sits 71% up the range" in text
    assert "NaN" not in text


def test_the_block_states_that_its_levels_come_from_closed_candles():
    # Every level in the block is cut from CLOSED candles, so on a 4h interval
    # it can trail the live mark printed further up by a whole interval. The
    # vintage is stated rather than left to be inferred — same disclosure rule
    # the rest of the context follows. Pinned separately from the label test
    # above so deleting the phrase fails on its own reason.
    text = render_market_context(_ctx(volume_profile=_profile()))
    header = next(line for line in text.splitlines() if line.startswith("Volume profile"))
    assert "as of the last closed candle" in header


def test_the_value_area_share_is_taken_from_the_constant_not_written_out(monkeypatch):
    # The rendered "70%" must BE VALUE_AREA_FRACTION, not a literal that agrees
    # with it today. Written out, it would keep telling the model 70% after the
    # convention moved, and every other assertion in this file would still pass
    # — which is the whole failure mode. monkeypatch (not a bare rebind) so the
    # module is restored even if the assertions below fail.
    from contrib.hyperliquid_perp.domains.perp import prompt_context as pc

    monkeypatch.setattr(pc, "VALUE_AREA_FRACTION", Decimal("0.55"))
    text = render_market_context(_ctx(volume_profile=_profile()))
    assert "Value area (band holding at least 55% of volume):" in text
    assert "70% of volume" not in text


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


# Fixtures whose letter comes from the PRODUCTION classifier, not from a
# hand-set ``shape=`` field. The candle builders live next door in
# test_volume_profile and already produce exactly these four letters.
#
# Writing the geometry out by hand instead would need a second copy of
# classify_shape's rule LADDER here to check itself, and that mirror would
# drift silently the first time the production rules were reordered — leaving
# the guard certifying fixtures the classifier no longer agrees with, the very
# defect it exists to catch. Going through ``_classify`` avoids the mirror.
_SHAPE_CANDLES = {
    ProfileShape.D: _d_shape,
    ProfileShape.P: _p_shape,
    ProfileShape.B: _b_shape,
    ProfileShape.THIN: _thin_shape,
}


def _shaped(shape: ProfileShape) -> VolumeProfile:
    """A profile the production classifier really labels ``shape``.

    Every parametrized case below reads only the rendered NOTE, so the
    builders' own 100-110 price range (rather than ``_profile``'s
    60,000-66,000) is immaterial here.
    """
    profile = _classify(_SHAPE_CANDLES[shape]())
    # A builder that stopped producing its letter would otherwise let every
    # test below assert against a block contradicting its own label.
    assert profile.shape is shape, f"{shape} builder now classifies as {profile.shape}"
    return profile


@pytest.mark.parametrize("shape", list(ProfileShape))
def test_every_shape_renders_its_letter_and_a_note(shape):
    # Asserts on the WHOLE render, not just the Shape line: the enum-leak check
    # below has to cover every line the profile contributes, not one of them.
    text = render_market_context(_ctx(volume_profile=_shaped(shape)))
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
    ProfileShape.THIN: "value area spans most of the range",
}


@pytest.mark.parametrize("shape", list(ProfileShape))
def test_each_shape_renders_its_own_note(shape):
    line = _shape_line(_shaped(shape))
    assert _SHAPE_MARKER[shape] in line


# Wordings retired because each asserted something its rule never measured.
# Swept over EVERY shape rather than asserted inside the one test whose fixture
# happens to produce that letter: "volume built up" was reworded out of the P
# AND b notes in the same change, so a per-case assertion would let b's wording
# be reverted alone with the suite still green.
_RETIRED_CLAIMS = (
    "volume built up",  # P/b test the heaviest BUCKET, not where the bulk sat
    "spread thinly",  # thin tests value-area WIDTH, not flatness
    "no price level held",  # thin: a two-cluster window lands here too
    "most-traded price",  # the POC is a bucket midpoint, possibly never traded
)


@pytest.mark.parametrize("shape", list(ProfileShape))
def test_no_retired_overclaim_comes_back(shape):
    text = render_market_context(_ctx(volume_profile=_shaped(shape)))
    for claim in _RETIRED_CLAIMS:
        assert claim not in text, f"{shape}: retired wording {claim!r} is back"


@pytest.mark.parametrize("shape", list(ProfileShape))
def test_no_shape_note_attributes_the_volume_to_buyers_or_sellers(shape):
    # The notes state geometry and stop. Two reasons they must: this file keeps
    # directional framing out of its strings (see _REGIME_NOTE), and the same
    # geometry carries opposite readings — a P is absorption to one school and
    # short covering to another. Nothing here measured which, so nothing here
    # may name one. Without this test the phrasing drifts back the moment
    # someone finds the geometric wording dry: every other assertion in this
    # file passes with "— buyers absorbed the move up" appended.
    line = _shape_line(_shaped(shape))
    for attribution in ("buyer", "seller", "absorb", "covering", "liquidation", "bull", "bear"):
        assert attribution not in line.lower()


def test_the_d_note_asserts_nothing_positive_about_the_distribution():
    # D is classify_shape's catch-all: it fires both for a centred POC and for
    # a POC skewed to one end whose close did not confirm it. So the note must
    # not claim centrality — a skewed POC whose close failed to confirm it is a
    # legal D, so the claim can sit one line under a POC reading that
    # contradicts it. That follows from classify_shape's rule order alone; no
    # claim about how OFTEN it happens is made or needed. Rendered here with a
    # POC at 95% of the range, which is such a D.
    #
    # The POC price and the value area move WITH poc_position: the fixture is
    # cross-checked, and a 95% POC left at the default 63,625 would be the very
    # self-contradicting line this file is trying to keep out.
    text = render_market_context(
        _ctx(
            volume_profile=_profile(
                shape=ProfileShape.D,
                poc_position=0.95,
                # REQUIRED for this to be the case the comment describes. Left
                # at the default 0.71 the close CONFIRMS the upward skew, and
                # classify_shape would call this a P — the fixture would only
                # look like a D because ``shape`` is not re-derived, which is
                # the very gap VolumeProfile's own comment warns about.
                close_position=0.29,
                poc=Decimal("65700.0"),  # 60000 + 0.95 * 6000
                value_area_low=Decimal("63250.0"),  # keeps the 11/24 width...
                value_area_high=Decimal("66000.0"),  # ...and contains the POC
            )
        )
    )
    line = next(line for line in text.splitlines() if line.strip().startswith("Shape:"))
    assert "POC (midpoint of the heaviest price bucket): 65,700.00 (95% up the range)" in text
    # No positive claim about where the volume sat...
    assert "volume is concentrated" not in line
    # ...it names itself as the catch-all, admits the skewed case, and points
    # the reader at the numbers rather than the letter.
    assert "catch-all" in line
    assert "skewed" in line
    assert "read the POC position above" in line
    assert "Does not test symmetry" in line


# --- The notes may claim only what their rule tested. -----------------------
#
# The two cases below are NOT hand-built VolumeProfile fixtures: they are real
# candles pushed through the real ``compute_volume_profile``, because the claim
# under test is precisely that the production classifier assigns these letters
# to these windows. A fixture with ``shape=`` set by hand would assert nothing
# about the classifier and would be free to violate the very rule it illustrates
# — the defect already caught once in the D fixture above.


def _shape_line(profile) -> str:
    text = render_market_context(_ctx(volume_profile=profile))
    return next(line for line in text.splitlines() if line.strip().startswith("Shape:"))


def test_the_thin_note_does_not_claim_volume_is_evenly_spread():
    # ``thin`` fires on value_area_width_ratio >= _THIN_VA_RATIO, which measures
    # how WIDE the outward walk got — not how flat the distribution is. A
    # two-cluster window (price ranges at one level, traverses, ranges at
    # another — ordinary over five days of 4h bars) makes the walk cross a
    # near-empty middle and come out wide while the volume is in fact extremely
    # concentrated. The old note said "volume is spread thinly across the range
    # ... so no price level held the activity"; both halves are false here.
    candles = (
        [_candle(i, 100, 102, 101, 50) for i in range(14)]
        + [_candle(14 + i, 102, 122, 112, 1) for i in range(2)]
        + [_candle(16 + i, 122, 124, 123, 50) for i in range(14)]
    )
    profile = compute_volume_profile(candles, 30)
    assert profile is not None
    # The label really is thin...
    assert profile.shape is ProfileShape.THIN
    # ...while a single bucket holds several times the uniform 1/24 share, which
    # is what makes "spread thinly" a false description of this window.
    assert profile.poc_volume_share > 4 * (1 / profile.bucket_count)

    line = _shape_line(profile)
    # It states the geometry it tested, and names this exact counterexample so
    # the model is not left to infer flatness from the letter.
    assert "value area spans most of the range" in line
    assert "two separate clusters" in line


def test_the_thin_note_does_not_claim_the_volume_needed_the_range():
    # The note may describe the WALK; it may not assert the range was required.
    # Width is partly an artefact of the upward tie-break: between equal
    # neighbours the walk always climbs, so it can cross a run of near-empty
    # buckets before turning back to collect the heavy low ones.
    #
    # Three wide low-volume bars put a floor of volume in EVERY bucket, so the
    # POC's two neighbours tie at that floor and the walk climbs to the top
    # before turning down. Result: a value area covering 100% of the range
    # while 71% of the volume sits in the bottom 12.5% of it. A note saying the
    # walk "had to cross most of the range" is false here — which is what the
    # first version of this reworded note claimed.
    candles = (
        [_candle(i, 100, 124, 112, 24) for i in range(3)]
        + [_candle(3 + i, 100, 101, 100.5, 29) for i in range(10)]
        + [_candle(13 + i, 102, 103, 102.5, 30) for i in range(11)]
        + [_candle(24 + i, 114, 115, 114.5, 30) for i in range(6)]
    )
    profile = _classify(candles)
    assert profile.shape is ProfileShape.THIN
    assert profile.value_area_width_ratio == 1.0

    total = sum(c.volume for c in candles)
    concentrated = sum(c.volume for c in candles if c.high <= Decimal("103"))
    # The premise: the range was NOT needed to hold the volume.
    assert concentrated / total > Decimal("0.7")

    line = _shape_line(profile)
    assert "had to cross" not in line  # no causal claim...
    assert "not proof the volume needed the range" in line  # ...an explicit denial
    assert "property of the walk" in line


def test_the_p_note_does_not_claim_where_the_bulk_of_the_volume_sat():
    # ``P`` fires on poc_position >= _POC_UPPER, i.e. on where the single
    # heaviest BUCKET sits. That is not a claim about where the bulk of the
    # volume sat, and the two can point opposite ways: below, the POC bucket is
    # 60% up the range while most of the window's volume sat BELOW the midpoint,
    # and the value-area line rendered one row above the note spans the lower
    # part of the range. The old note ("volume built up in the upper part of the
    # range") contradicted the line directly above it.
    candles = (
        [_candle(i, 100.0, 100.9, 100.45, 0.75) for i in range(12)]
        + [_candle(12 + i, 100 + i + 1, 100 + i + 1.9, 100 + i + 1.45, 1) for i in range(13)]
        + [_candle(25, 114.1, 114.9, 114.5, 10)]
        + [_candle(26 + i, 123.0, 124.0, 123.5, 0.0001) for i in range(4)]
    )
    profile = compute_volume_profile(candles, 30)
    assert profile is not None
    assert profile.shape is ProfileShape.P

    midpoint = (profile.range_low + profile.range_high) / 2
    total = sum(c.volume for c in candles)
    below = sum(c.volume for c in candles if (c.low + c.high) / 2 < midpoint)
    # The premise of the case: most of the volume sat below the midpoint even
    # though the profile is labelled P. Without this the test would still pass
    # against a window where the old wording happened to be true.
    assert below / total > Decimal("0.5")
    # And the value area printed above the note reaches into the lower half.
    assert profile.value_area_low < midpoint

    line = _shape_line(profile)
    assert "heaviest single price bucket" in line
    assert "where the bulk of the volume sat" in line
