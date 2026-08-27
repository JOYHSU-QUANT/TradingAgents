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

from contrib.hyperliquid_perp.domains.perp.marginal_cost import build_position_context
from contrib.hyperliquid_perp.domains.perp.prompt_context import (
    context_shape,
    render_market_context,
)
from contrib.hyperliquid_perp.domains.perp.schema import (
    MarketRegime,
    PerpMarketContext,
    ProfileShape,
    VolumeProfile,
)
from contrib.hyperliquid_perp.domains.perp.volume_profile import compute_volume_profile

from .test_volume_profile import _candle, _classify, _shaped

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
            # None only WITH no reference price (a freshly listed coin): the
            # DTO ties the two together.
            prev_day_price=Decimal("0"),
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
# hand-set ``shape=`` field: ``_shaped`` (imported from test_volume_profile,
# where the candle builders live) classifies a builder's candles and asserts
# the letter. Writing the geometry out by hand instead would need a second
# copy of classify_shape's rule LADDER here to check itself, and that mirror
# would drift silently the first time the production rules were reordered.
# Every parametrized case below reads only the rendered NOTE, so the
# builders' own 100-110 price range (rather than ``_profile``'s
# 60,000-66,000) is immaterial here.


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
                # the rule calls that a P — VolumeProfile re-derives the
                # letter at construction, so ``shape=D`` would be refused.
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
    # ``thin`` fires on value_area_width_ratio >= THIN_VALUE_AREA_RATIO, which measures
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
    # ``P`` fires on poc_position >= POC_UPPER_BAND, i.e. on where the single
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


# --------------------------------------------------------------------------
# context_shape — the prompt's STRUCTURE, the second segmentation key (#97)
# --------------------------------------------------------------------------


_HEADER_TO_SHAPE = {
    "Price:": "price",
    "Market:": "market",
    "Funding:": "funding",
    "Indicators:": "indicators",
    "Position:": "position",
}


def _rendered_headers(text: str) -> list[str]:
    """The top-level section headers of a render, in order.

    A header is a non-indented, non-blank line after the three-line preamble
    (Coin / As of / Candles): every section opens with one and everything
    under it is indented two spaces.
    """
    return [line for line in text.split("\n")[3:] if line and not line.startswith("  ")]


def _shape_name(header: str) -> str:
    if header.startswith("Volume profile ("):
        return "volume_profile"
    return _HEADER_TO_SHAPE[header]


@pytest.mark.parametrize("position", [None, "flat", "open"])
@pytest.mark.parametrize("with_profile", [False, True])
def test_context_shape_names_the_rendered_headers_in_order_both_ways(with_profile, position):
    # The shape and the render are two functions with no code in common, so
    # this is the lock between them, in BOTH directions: every header the
    # render prints has a shape entry at the same position, and the shape
    # names nothing the render does not print. A section added to one and
    # forgotten in the other fails here instead of silently filing two prompt
    # regimes under one shape.
    overrides = {}
    if with_profile:
        overrides["volume_profile"] = _profile()
    if position == "flat":
        overrides["position"] = _flat_position()
    elif position == "open":
        overrides["position"] = _open_position()
    ctx = _ctx(**overrides)
    headers = _rendered_headers(render_market_context(ctx))
    assert headers[:4] == ["Price:", "Market:", "Funding:", "Indicators:"]
    assert len(headers) == 4 + int(with_profile) + int(position is not None)
    parts = context_shape(ctx).split("|")
    assert [part.split("(")[0] for part in parts] == [_shape_name(h) for h in headers]
    # The indicator rows are the fixture's names, in the fixture's order.
    assert parts[3] == "indicators(rsi_14,ema_20,macd)"
    if position is not None:
        assert parts[-1] == "position"


# --------------------------------------------------------------------------
# Position — the prompt-v4 section: the account's own position, priced.
# --------------------------------------------------------------------------

_FILL_AT = datetime(2024, 1, 1, 15, 4, 5, tzinfo=timezone.utc)  # 12h before _AS_OF


def _position(**overrides):
    """A position priced at the fixture's mark (60,000): 0.005 BTC long from
    50,000 = notional 300, uPnL +50 on a 950 wallet, so equity 1,000 and
    margin exactly 30% — round numbers, so every line below is hand-checkable
    and no figure sits on a float rounding tie."""
    base = {
        "size": Decimal("0.005"),
        "entry_price": Decimal("50000"),
        "wallet_balance": Decimal("950"),
        "mark": Decimal("60000.0"),
        "leverage": Decimal(1),
        "funding_rate": Decimal("0.0000125"),
        "grid_min": 0,
        "grid_max": 60,
        "grid_step": 1,
        "taker_fee_rate": Decimal("0.00045"),
        "slippage_bps": Decimal(5),
        "last_fill_at": _FILL_AT,
    }
    base.update(overrides)
    return build_position_context(**base)


def _open_position(**overrides):
    return _position(**overrides)


def _flat_position(**overrides):
    # Flat: no unrealized PnL, so the wallet IS the equity (1,000 here).
    return _position(size=Decimal(0), entry_price=None, wallet_balance=Decimal("1000"), **overrides)


def _position_block(text: str) -> str:
    """The Position: section alone — the header and everything under it."""
    return text[text.index("Position:") :]


def test_the_open_position_section_states_the_facts_and_the_priced_moves():
    block = _position_block(render_market_context(_ctx(position=_open_position())))
    assert "Side: long, size 0.005 BTC, notional 300.00 USDC at mark" in block
    assert "Entry: 50,000.00 (unrealized PnL +50.00 USDC)" in block
    # 300 / 1000 * 100 = 30%
    assert "Committed margin: 30.00% of account equity 1,000.00 USDC" in block
    assert "configured 1x leverage" in block
    assert (
        "Last fill: 2024-01-01T15:04:05+00:00 UTC (12.0 hours before the as-of time above)" in block
    )
    # 0.0000125 * 8 * 300 = 0.03 USDC per 8h, paid by the long.
    assert "Holding cost at the current funding rate: pays 0.0300 USDC per 8h" in block
    # The rate is spelled out once, per fill and as the round-trip total.
    assert (
        "taker fee 0.0450% and 5.00 bps slippage per fill, 19.00 bps of the traded notional"
        in block
    )
    # Flat from 30% trades the whole 300 notional: 300 * 0.0019 = 0.57.
    assert (
        "-> 0%: trades 300.00 USDC notional, round-trip cost 0.57 USDC, breakeven 19.00 bps"
        in block
    )
    # 35% trades 5 / 100 * 1000 = 50.00 -> 0.095 -> 0.10 (the nearest row
    # above the current margin, which itself gets no row).
    assert (
        "-> 35%: trades 50.00 USDC notional, round-trip cost 0.10 USDC, breakeven 19.00 bps"
        in block
    )
    assert "-> 30%:" not in block
    # 60% trades 30 / 100 * 1000 = 300.00 -> 0.57.
    assert (
        "-> 60%: trades 300.00 USDC notional, round-trip cost 0.57 USDC, breakeven 19.00 bps"
        in block
    )
    # The per-point rate that prices every legal target the rows skip:
    # 1000 / 100 = 10.00 USDC per point, * 0.0019 = 0.0190.
    assert (
        "Every 1 percentage point of margin moved trades 10.00 USDC and costs 0.0190 USDC round trip"
        in block
    )


def test_the_flat_position_section_is_one_line_with_the_equity():
    block = _position_block(render_market_context(_ctx(position=_flat_position())))
    lines = block.split("\n")
    assert lines[0] == "Position:"
    assert lines[1] == "  flat, no open position (account equity 1,000.00 USDC)"
    assert len(lines) == 2
    # No cost table, no holding cost, no last-fill line on a flat account.
    assert "->" not in block
    assert "Holding cost" not in block


def test_a_short_receives_positive_funding_and_says_so():
    block = _position_block(
        render_market_context(_ctx(position=_open_position(size=Decimal("-0.005"))))
    )
    assert "Side: short, size 0.005 BTC" in block
    # uPnL for a short from 50,000 marked 60,000: -50.
    assert "unrealized PnL -50.00 USDC" in block
    assert "receives 0.0300 USDC per 8h" in block


def test_zero_funding_prints_a_zero_holding_cost_not_pays_or_receives():
    block = _position_block(
        render_market_context(_ctx(position=_open_position(funding_rate=Decimal(0))))
    )
    holding = next(line for line in block.split("\n") if line.startswith("  Holding cost"))
    assert "0.0000 USDC (funding rate is zero) per 8h" in holding
    assert "pays" not in holding
    assert "receives" not in holding


def test_a_fill_after_the_as_of_is_said_not_shown_as_a_negative_age():
    later = _AS_OF.replace(hour=5)
    block = _position_block(
        render_market_context(_ctx(position=_open_position(last_fill_at=later)))
    )
    assert f"Last fill: {later.isoformat()} UTC (after the as-of time above)" in block
    assert "-" + "1." not in block  # no "-1.9 hours"


def test_a_position_with_no_recorded_fill_says_so():
    block = _position_block(render_market_context(_ctx(position=_open_position(last_fill_at=None))))
    assert "Last fill: none recorded for this run" in block


_GATE_WORDS = ("confidence", "deadband", "exempt", "threshold", "resize", "min_confidence")


@pytest.mark.parametrize("word", _GATE_WORDS)
@pytest.mark.parametrize("side", [Decimal("0.005"), Decimal("-0.005"), Decimal(0)])
def test_the_position_section_never_names_a_gate_threshold(word, side):
    # The 2026-07 ruling, now load-bearing: with its own position in view the
    # model CAN tell which targets are resizes, so any sentence about which
    # bar a target faces (flat/open only need the base bar) would funnel
    # mid-confidence de-risking into full closes. The section is facts and
    # prices only; the words the format block uses for its rules never
    # appear here. Both sides and flat, since the flat line is its own text.
    entry = None if side == 0 else Decimal("50000")
    block = _position_block(
        render_market_context(_ctx(position=_position(size=side, entry_price=entry)))
    )
    assert word not in block.lower()


def test_the_position_section_renders_last_after_the_volume_profile():
    text = render_market_context(_ctx(volume_profile=_profile(), position=_open_position()))
    assert text.index("Volume profile (") < text.index("Position:")
    assert text.rstrip().endswith("distance from the current margin.")


def test_context_shape_files_open_and_flat_and_both_sides_as_one_position_section():
    # Open vs flat is the account's STATE, alternating within a run cycle by
    # cycle — a shape that split on it would split one run into two buckets
    # on nothing the operator configured, and the paper review reads exactly
    # one shape per run. The review tells open from flat on
    # ai_inputs.current_position_side; the shape says only that the section
    # was there.
    base = context_shape(_ctx(position=_open_position()))
    assert base.endswith("|position")
    assert context_shape(_ctx(position=_flat_position())) == base
    assert context_shape(_ctx(position=_open_position(size=Decimal("-0.005")))) == base
    # And a moved mark (different numbers, same rows) does not move the shape.
    assert context_shape(_ctx(position=_open_position(mark=Decimal("60000.0")))) == context_shape(
        _ctx(position=_open_position())
    )


def test_context_shape_changes_when_the_volume_profile_section_appears():
    # The #97 case: flipping market_data.volume_profile_window_candles adds a
    # section with no code change, so the shape — not prompt_version — is
    # what separates the two populations.
    without = context_shape(_ctx())
    with_profile = context_shape(_ctx(volume_profile=_profile()))
    assert without == "price|market|funding|indicators(rsi_14,ema_20,macd)"
    assert with_profile == without + "|volume_profile"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        # The oracle rather than the mark: the mark is cross-checked against
        # day_change_pct, so moving it alone no longer builds.
        ("oracle_price", Decimal("1.0")),
        ("candle_count", 50),
        ("funding_window_days", 14),
        ("funding_zscore_30d", None),
        # Data-availability lines drop out per cycle; they must not split a regime.
        ("mid_price", None),
        ("funding_premium", None),
        # A dead indicator renders n/a — same row, same shape.
        ("indicators", {"rsi_14": None, "ema_20": None, "macd": None}),
    ],
)
def test_context_shape_ignores_content_and_data_driven_lines(field, value):
    base = _ctx()
    variant = _ctx(**{field: value})
    # The premise: the render really did change, so an over-eager shape that
    # hashed the text would have split here.
    assert render_market_context(variant) != render_market_context(base)
    assert context_shape(variant) == context_shape(base)


def test_context_shape_follows_the_indicator_set_and_its_order():
    base = context_shape(_ctx())
    fewer = context_shape(_ctx(indicators={"rsi_14": 55.5, "ema_20": 60100.0}))
    reordered = context_shape(_ctx(indicators={"macd": None, "rsi_14": 55.5, "ema_20": 60100.0}))
    assert fewer == "price|market|funding|indicators(rsi_14,ema_20)"
    assert reordered == "price|market|funding|indicators(macd,rsi_14,ema_20)"
    assert len({base, fewer, reordered}) == 3
