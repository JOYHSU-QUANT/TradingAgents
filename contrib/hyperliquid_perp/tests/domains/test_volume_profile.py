"""Tests for the volume profile — bucketing, value area, and shape rules.

The module is pure and deterministic, so every case here is a synthetic candle
fixture with a hand-checkable answer. Three families of invariant:

- **Fail-closed.** Every degenerate window returns ``None`` for the WHOLE
  profile. A half-built profile would render as confident price levels.
- **Conservation.** Bucketed volume tracks the volume that went in; a candle
  whose range straddles a bucket edge must not lose a sliver. Asserted to a
  tolerance, not bit-exactly — the per-candle ``overlap / span`` division
  rounds at 28 significant digits, so the totals agree to roughly a 1e-24
  relative error. Some fixtures happen to come out exact; that is luck, and a
  test written as ``==`` would pin the luck rather than the invariant.
- **Discrimination.** Each shape rule is pinned by a fixture that flips to a
  DIFFERENT shape when that rule's own condition is removed — so a rule that
  silently stopped being consulted would fail here, which a "some shape came
  back" assertion would not.
"""

from __future__ import annotations

import logging
from decimal import Decimal, localcontext

import pytest

from contrib.hyperliquid_perp.common.constants import MIN_VOLUME_PROFILE_WINDOW
from contrib.hyperliquid_perp.common.decimal_context import DECIMAL_CONTEXT
from contrib.hyperliquid_perp.domains.perp.schema import Candle, ProfileShape, VolumeProfile
from contrib.hyperliquid_perp.domains.perp.volume_profile import (
    BUCKET_COUNT,
    DEFAULT_WINDOW_CANDLES,
    MIN_WINDOW_CANDLES,
    VALUE_AREA_FRACTION,
    PriceDistribution,
    _bucket_edges,
    _bucket_width,
    build_profile,
    classify_shape,
    compute_volume_profile,
)

_START_MS = 1_700_000_000_000
_INTERVAL_MS = 4 * 3600_000


def _candle(index: int, low, high, close, volume) -> Candle:
    """A candle spanning ``low``-``high`` that closes at ``close``.

    ``open`` is pinned to ``low`` so the OHLC ordering invariant always holds
    regardless of what the case is exercising.
    """
    return Candle(
        open_time=_START_MS + index * _INTERVAL_MS,
        close_time=_START_MS + (index + 1) * _INTERVAL_MS,
        open=Decimal(str(low)),
        high=Decimal(str(high)),
        low=Decimal(str(low)),
        close=Decimal(str(close)),
        volume=Decimal(str(volume)),
    )


def _wide_tails(count: int, close, volume=5) -> list[Candle]:
    """Low-volume bars covering the whole 100-110 range — the profile's tails."""
    return [_candle(i, 100, 110, close, volume) for i in range(count)]


def _d_shape() -> list[Candle]:
    """Fat node in the MIDDLE of the range, close at mid-range."""
    return _wide_tails(4, 105) + [_candle(i, 104, 106, 105, 60) for i in range(4, 16)]


def _p_shape() -> list[Candle]:
    """Fat node at the TOP of the range, close high — the geometry P tests for."""
    return _wide_tails(4, 108, volume=4) + [_candle(i, 108, 110, 109, 60) for i in range(4, 16)]


def _b_shape() -> list[Candle]:
    """Fat node at the BOTTOM of the range, close low — the mirror of P."""
    return _wide_tails(4, 101, volume=4) + [_candle(i, 100, 102, 101, 60) for i in range(4, 16)]


def _thin_shape() -> list[Candle]:
    """Every bar covers the whole range with equal volume — nothing held."""
    return [_candle(i, 100, 110, 109, 10) for i in range(16)]


# One builder per letter, keyed by the letter the PRODUCTION classifier gives
# it — shared with test_prompt_context and test_schema, so a fixture whose
# letter must be real goes through ``_shaped`` rather than hand-writing the
# geometry (a second copy of the rule ladder that drifts the first time a
# threshold moves). Exhaustive over ProfileShape by construction: a fifth
# member fails ``test_classify_shape_labels_with_the_dtos_own_rule`` here.
_SHAPE_CANDLES = {
    ProfileShape.D: _d_shape,
    ProfileShape.P: _p_shape,
    ProfileShape.B: _b_shape,
    ProfileShape.THIN: _thin_shape,
}


def _shaped(shape: ProfileShape) -> VolumeProfile:
    """A profile the production classifier really labels ``shape``."""
    profile = _classify(_SHAPE_CANDLES[shape]())
    # A builder that stopped producing its letter would otherwise let every
    # consumer assert against a block contradicting its own label.
    assert profile.shape is shape, f"{shape} builder now classifies as {profile.shape}"
    return profile


# --------------------------------------------------------------------------
# build_profile — fail-closed on every degenerate window.
# --------------------------------------------------------------------------


def test_window_of_zero_disables_the_profile_without_a_log_line():
    # 0 is the documented OFF switch, not a degradation: it must not warn, or a
    # daemon with the feature deliberately off would log every single cycle.
    candles = _d_shape()
    logger = logging.getLogger("contrib.hyperliquid_perp.domains.perp.volume_profile")
    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append  # type: ignore[method-assign]
    logger.addHandler(handler)
    try:
        assert build_profile(candles, 0) is None
        assert compute_volume_profile(candles, 0) is None
    finally:
        logger.removeHandler(handler)
    assert records == []


@pytest.mark.parametrize(
    ("candles", "window", "fragment"),
    [
        pytest.param(_d_shape(), 5, "below the 12-candle minimum", id="window-under-floor"),
        pytest.param(
            _d_shape()[:10], 12, "asks for 12 candles but only 10 are available", id="short-history"
        ),
        pytest.param(
            [_candle(i, 100, 100, 100, 5) for i in range(12)],
            12,
            "zero price width",
            id="zero-width",
        ),
        pytest.param(
            [_candle(i, 100, 110, 105, 0) for i in range(12)],
            12,
            "traded zero volume",
            id="zero-volume",
        ),
    ],
)
def test_every_runtime_skip_says_why_on_the_log(candles, window, fragment, caplog):
    # The mirror of the window-of-zero test above: 0 is the OFF switch and must
    # stay silent, but every skip that is NOT the off switch must say why. This
    # matters beyond tidiness — SETUP.md names the short-history line as the
    # operator's ONLY diagnostic for the one failure mode config load cannot
    # catch (a legal window with too little history), and until now deleting any
    # of these four ``logger.warning`` calls left the suite green while breaking
    # that documented path.
    with caplog.at_level(
        logging.WARNING, logger="contrib.hyperliquid_perp.domains.perp.volume_profile"
    ):
        assert build_profile(candles, window) is None
    assert fragment in caplog.text
    # And the reason names the profile, so a reader grepping the daemon log can
    # tell which feature went quiet.
    assert "volume-profile" in caplog.text or "volume profile" in caplog.text


@pytest.mark.parametrize("window", [-5, 1, MIN_WINDOW_CANDLES - 1])
def test_window_below_the_minimum_returns_none(window):
    # The literal "rolling 24h" window at this project's 4h candles is 6 bars,
    # which lands in this band — hence the config-load rejection.
    assert build_profile(_d_shape(), window) is None


def test_the_producers_constants_are_the_ones_the_loader_and_the_dto_enforce():
    # Several enforcement points, one constant each. The window floor: if it
    # forked, load_config would accept a window this module then refuses on
    # every cycle — and the only symptom is a prompt section that never
    # appears. The grid and the value-area convention (issue #100):
    # VolumeProfile pins ``bucket_count`` and its volume-share floors against
    # them, so a fork would have the producer emit a profile the DTO refuses,
    # and the only symptom a cycle dying on a ValueError out of a pure
    # function. All live in common/constants.py so neither the loader nor the
    # DTO module imports this compute module. Identity, not equality.
    from contrib.hyperliquid_perp.common import constants

    assert MIN_WINDOW_CANDLES is MIN_VOLUME_PROFILE_WINDOW
    assert BUCKET_COUNT is constants.VOLUME_PROFILE_BUCKET_COUNT
    assert VALUE_AREA_FRACTION is constants.VALUE_AREA_FRACTION


def test_the_thin_threshold_tracks_the_value_area_convention():
    # Same shape of pin as the floor-constant check above. The thin
    # threshold's whole justification is that it sits at the
    # uniform-distribution mark: volume spread perfectly evenly puts
    # VALUE_AREA_FRACTION of itself inside that same fraction of the range.
    # Written out as its own 0.70 the two would come apart the first time the
    # value-area convention moved, leaving a threshold whose comment described
    # a mark it no longer sat on. Both now live in common.constants (issue
    # #100 moved the shape rule into schema, which cannot import this module).
    from contrib.hyperliquid_perp.common.constants import THIN_VALUE_AREA_RATIO

    assert float(VALUE_AREA_FRACTION) == THIN_VALUE_AREA_RATIO


def test_classify_shape_labels_with_the_dtos_own_rule():
    # The letter classify_shape assigns must be what derive_profile_shape says
    # for the fractions it stored — otherwise the DTO's re-derivation would
    # refuse the producer's own output. One fixture per letter, through the
    # real producer; ``_SHAPE_CANDLES`` must cover every member for this to
    # mean anything, so that is pinned too.
    from contrib.hyperliquid_perp.domains.perp.schema import derive_profile_shape

    assert set(_SHAPE_CANDLES) == set(ProfileShape)
    for letter in _SHAPE_CANDLES:
        profile = _shaped(letter)
        assert profile.shape is derive_profile_shape(
            profile.value_area_width_ratio, profile.poc_position, profile.close_position
        )


def test_window_wider_than_the_available_history_is_refused_not_narrowed():
    # A profile labelled "30 candles" cut from 16 would read as a wider, steadier
    # structure than the data supports. Refuse instead of quietly narrowing.
    candles = _d_shape()
    assert len(candles) == 16
    assert build_profile(candles, DEFAULT_WINDOW_CANDLES) is None
    # ...and the boundary itself is inclusive: exactly enough history works.
    assert build_profile(candles, 16) is not None


def test_empty_candles_returns_none():
    assert build_profile([], MIN_WINDOW_CANDLES) is None
    assert compute_volume_profile([], MIN_WINDOW_CANDLES) is None


# --------------------------------------------------------------------------
# build_profile — bucketing correctness.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", [_d_shape, _p_shape, _b_shape, _thin_shape])
def test_bucketed_volume_tracks_the_volume_that_went_in(fixture):
    # Conservation: the per-candle spread is a redistribution, not a sampling.
    # A bucket-edge gap would drop a sliver of every straddling bar — which at
    # these volumes would show up far above the 1e-24 rounding floor.
    candles = fixture()
    dist = build_profile(candles, len(candles))
    assert dist is not None
    went_in = sum(c.volume for c in candles)
    assert abs(sum(dist.bucket_volumes) - went_in) < went_in * Decimal("1e-20")
    assert len(dist.bucket_volumes) == BUCKET_COUNT
    assert dist.candle_count == len(candles)


def test_range_comes_from_the_window_extremes_not_the_closes():
    candles = [_candle(i, 100, 110, 105, 5) for i in range(12)]
    candles[3] = _candle(3, 90, 110, 105, 5)  # a lower low
    candles[7] = _candle(7, 100, 130, 105, 5)  # a higher high
    dist = build_profile(candles, 12)
    assert dist is not None
    assert dist.range_low == Decimal("90")
    assert dist.range_high == Decimal("130")


def test_only_the_most_recent_window_candles_are_profiled():
    # A rolling window looks BACKWARD from the newest candle. Slicing from the
    # front instead would profile ancient history and still look plausible.
    old = [_candle(i, 100, 110, 105, 5) for i in range(12)]
    recent = [_candle(i, 200, 210, 205, 5) for i in range(12, 24)]
    dist = build_profile(old + recent, 12)
    assert dist is not None
    assert (dist.range_low, dist.range_high) == (Decimal("200"), Decimal("210"))


def test_a_zero_range_candle_puts_its_whole_volume_in_one_bucket():
    # A doji has no span to spread across; it must not vanish, and must not be
    # smeared across the range either.
    candles = [_candle(i, 100, 110, 105, 1) for i in range(11)]
    candles.append(_candle(11, 110, 110, 110, 1000))  # a bar pinned at range_high
    dist = build_profile(candles, 12)
    assert dist is not None
    went_in = sum(c.volume for c in candles)
    assert abs(sum(dist.bucket_volumes) - went_in) < went_in * Decimal("1e-20")
    # Pinned at range_high -> the clamp puts it in the LAST bucket, not out of range.
    assert dist.bucket_volumes[-1] > Decimal("1000")
    assert max(dist.bucket_volumes) == dist.bucket_volumes[-1]


def test_a_zero_volume_candle_inside_a_live_window_is_skipped_not_mishandled():
    # The per-candle ``volume == 0`` skip, which the all-zero window test never
    # reaches (that one trips the earlier whole-window guard). An illiquid 4h
    # bar is real, and it must contribute nothing while still counting toward
    # the window and toward the range it printed.
    candles = [_candle(i, 104, 106, 105, 50) for i in range(14)]
    candles.insert(4, _candle(90, 100, 110, 105, 0))  # wide, but traded nothing
    candles.insert(9, _candle(91, 100, 110, 105, 0))
    dist = build_profile(candles, len(candles))
    assert dist is not None
    # It counts as a candle and its high/low still set the range...
    assert dist.candle_count == 16
    assert (dist.range_low, dist.range_high) == (Decimal("100"), Decimal("110"))
    # ...but contributes no volume, so the total is only the paying bars'.
    went_in = Decimal(14 * 50)
    assert abs(sum(dist.bucket_volumes) - went_in) < went_in * Decimal("1e-20")
    # ...and it did not smear anything into the empty tails it spanned.
    assert dist.bucket_volumes[0] == 0
    assert dist.bucket_volumes[-1] == 0


def test_a_candle_spanning_the_whole_range_spreads_evenly():
    dist = build_profile([_candle(i, 100, 110, 105, 24) for i in range(12)], 12)
    assert dist is not None
    # 12 bars x 24 volume spread evenly over 24 buckets -> 12 apiece. Not bit-exact:
    # the top bucket's edge is pinned to range_high rather than recomputed from the
    # width (that pin is what makes conservation exact), so it absorbs the sub-ulp
    # remainder of a range that does not divide evenly. Assert the spread is flat to
    # far below any volume anyone reports, and that nothing was created or lost.
    assert max(dist.bucket_volumes) - min(dist.bucket_volumes) < Decimal("1e-20")
    assert all(abs(v - Decimal(12)) < Decimal("1e-20") for v in dist.bucket_volumes)
    assert abs(sum(dist.bucket_volumes) - Decimal(12 * 24)) < Decimal("1e-20")


def test_value_area_can_reach_a_range_whose_width_does_not_divide_evenly():
    # Mutation-checked. ``(range_high - range_low) / 24 * 24`` can overshoot
    # range_high; 452.59-937.7339 is such a range. It is NOT BTC-shaped on
    # purpose: a range of the size this project actually profiles does not
    # overshoot, so a BTC-like fixture would pass with the pin removed. That is
    # about the range's MAGNITUDE, not its tick grid — whole-dollar prices
    # overshoot too once they are large enough, which the test below pins.
    # If the top bucket edge were recomputed from the
    # width instead of pinned to range_high, a value area reaching that bucket
    # would carry a value_area_high a hair ABOVE range_high — which
    # VolumeProfile's own guard rejects, killing the cycle with a ValueError
    # out of a pure function. The fat node sits in the top bucket precisely so
    # the value area has to reach it.
    candles = [_candle(i, "452.59", "937.7339", "930", 1) for i in range(4)]
    candles += [_candle(i, "920", "937.7339", "935", 500) for i in range(4, 16)]
    profile = _classify(candles)  # must not raise
    assert profile.value_area_high == Decimal("937.7339")
    assert profile.value_area_high == profile.range_high


def test_a_whole_dollar_range_overshoots_once_it_is_large_enough():
    """The counterexample to "fine ticks overshoot, coarse ticks do not".

    ``_bucket_edges``'s docstring attributes the ``width * count`` residual to
    the MAGNITUDES involved, not to the coin's tick grid. This pins the half of
    that claim a fixture can hold still: a range on BTC's own whole-dollar grid
    that overshoots anyway, because ``range_low`` and ``width`` together demand
    more than the context's 28 significant digits.

    Kept as one hand-checked pair rather than a sweep — the rates quoted in
    that docstring came from a one-off script, and a 50k-window sweep in the
    suite would trade real seconds for a number nothing depends on.
    """
    low, high = Decimal("109618"), Decimal("509273")
    with localcontext(DECIMAL_CONTEXT):
        # Production's own width, not a hand-rolled copy of the formula — a
        # copy would keep passing after _bucket_width changed underneath it.
        unpinned_top = low + _bucket_width(low, high, BUCKET_COUNT) * BUCKET_COUNT
    # Both operands are whole dollars, and it still misses.
    assert low == low.to_integral_value()
    assert high == high.to_integral_value()
    assert unpinned_top > high
    # ...and the pin is what keeps that out of value_area_high, which
    # VolumeProfile's guard would reject.
    assert _bucket_edges(low, high, BUCKET_COUNT - 1, BUCKET_COUNT)[1] == high


def test_build_profile_is_deterministic():
    candles = _d_shape()
    first = build_profile(candles, 16)
    second = build_profile(candles, 16)
    assert first == second


# --------------------------------------------------------------------------
# classify_shape — POC, value area, and the four rules.
# --------------------------------------------------------------------------


def _classify(candles: list[Candle]) -> VolumeProfile:
    dist = build_profile(candles, len(candles))
    assert dist is not None
    return classify_shape(dist, candles[-1].close)


@pytest.mark.parametrize(
    ("fixture", "expected"),
    [
        (_d_shape, ProfileShape.D),
        (_p_shape, ProfileShape.P),
        (_b_shape, ProfileShape.B),
        (_thin_shape, ProfileShape.THIN),
    ],
)
def test_each_synthetic_shape_classifies_as_intended(fixture, expected):
    assert _classify(fixture()).shape is expected


def test_poc_is_the_midpoint_of_the_heaviest_bucket():
    profile = _classify(_d_shape())
    # The fat node sits at 104-106 in a 100-110 range, so the POC must land
    # inside it — not merely "somewhere in the range".
    assert Decimal("104") <= profile.poc <= Decimal("106")
    assert profile.range_low == Decimal("100")
    assert profile.range_high == Decimal("110")


def test_value_area_holds_at_least_the_required_share_of_volume():
    candles = _d_shape()
    dist = build_profile(candles, len(candles))
    assert dist is not None
    profile = classify_shape(dist, candles[-1].close)
    # Re-derive the held share from the buckets the reported edges cover.
    total = sum(dist.bucket_volumes)
    width = (dist.range_high - dist.range_low) / BUCKET_COUNT
    held = sum(
        v
        for i, v in enumerate(dist.bucket_volumes)
        if profile.value_area_low <= dist.range_low + width * i
        and dist.range_low + width * (i + 1) <= profile.value_area_high
    )
    assert held >= total * VALUE_AREA_FRACTION
    # And it is the TIGHTEST such band, not the whole range.
    assert profile.value_area_width_ratio < 1.0


def test_value_area_brackets_the_poc_and_sits_inside_the_range():
    for fixture in (_d_shape, _p_shape, _b_shape, _thin_shape):
        profile = _classify(fixture())
        assert profile.range_low <= profile.value_area_low < profile.value_area_high
        assert profile.value_area_high <= profile.range_high
        assert profile.value_area_low <= profile.poc <= profile.value_area_high


def test_value_area_grows_toward_the_heavier_neighbour():
    # The 104-105 node (240 volume) is the heavy neighbour, so the value area
    # must extend BELOW the POC to reach it. Note what this fixture is NOT: an
    # earlier comment here claimed there was "nothing above" the POC, which the
    # fixture contradicts — the four wide bars put volume across the whole
    # 100-110 range, and the walk does step upward before turning down. The
    # invariant is "the band reaches the heavy side", not "the band never grows
    # upward".
    candles = [_candle(i, 100, 110, 105, 1) for i in range(4)]
    candles += [_candle(i, 104, 105, 105, 40) for i in range(4, 10)]  # just below
    candles += [_candle(i, 105, 106, 105, 90) for i in range(10, 16)]  # the POC node
    profile = _classify(candles)
    assert profile.value_area_low < Decimal("105")
    # Tightened from the old ``<= 106.5``: the real edge is ~105.83 (two
    # buckets of 10/24 above the POC bucket's floor), and the range reaches
    # 110, so 106.5 also admitted a band that had sprawled upward. Left as a
    # bound rather than an equality — the edge carries 28 significant digits
    # and pinning them would pin rounding luck, not the invariant.
    assert profile.value_area_high < Decimal("106")


def test_value_area_ties_resolve_upward_for_reproducibility():
    # The documented "(ties go upward)" rule had no test: mutating the walk's
    # ``above >= below`` to ``above > below`` left the whole suite green,
    # because no other fixture reaches that comparison with two EQUAL
    # neighbours and a non-None ``below``.
    #
    # 24 buckets of width 1 over [100, 124]. The POC bucket holds 68 of 100
    # volume and both neighbours hold 16, so the walk needs exactly ONE step to
    # clear the 70 target — and that step lands on a tie. Ties up puts the band
    # at buckets 12-13; ties down would put it at 11-12, which is what the
    # assertions below discriminate.
    #
    # Volume is placed with zero-range bars (dropped whole into one bucket) and
    # the range is fixed by a zero-VOLUME bar: build_profile skips a bar with no
    # volume when bucketing but still takes it into the window's min/max, so it
    # sets the range without smearing volume across it.
    candles = [
        _candle(0, 100, 124, 112.5, 0),  # sets the range, contributes no volume
        _candle(1, 112.5, 112.5, 112.5, 68),  # bucket 12 — the POC
        _candle(2, 111.5, 111.5, 111.5, 16),  # bucket 11
        _candle(3, 113.5, 113.5, 113.5, 16),  # bucket 13
        *[_candle(i, 112.5, 112.5, 112.5, 0) for i in range(4, 12)],
    ]
    profile = _classify(candles)
    assert profile.poc == Decimal("112.5")
    # The band is the POC bucket plus the one ABOVE it, not the one below.
    assert profile.value_area_low == Decimal("112")
    assert profile.value_area_high == Decimal("114")


def test_the_poc_band_thresholds_are_pinned_to_their_values():
    # ``POC_UPPER_BAND``/``POC_LOWER_BAND`` (0.60/0.40) had no pinning test at all:
    # widening either to 0.50 changed real classifications while the suite
    # stayed green, because every other fixture sits far from both edges.
    #
    # A POC one bucket above centre (bucket 12, poc_position 25/48 = 0.52) with
    # a CONFIRMING close is the discriminating case: a D at 0.60, but a P at
    # 0.50. Its mirror (bucket 11, 23/48 = 0.479, close below the midpoint) is
    # a D at 0.40 and would become a b at 0.50.
    def _one_bucket(bucket: int, close) -> list[Candle]:
        price = Decimal(100) + Decimal(bucket) + Decimal("0.5")
        return [
            _candle(0, 100, 124, close, 0),
            *[_candle(i, price, price, price, 10) for i in range(1, 11)],
            _candle(11, close, close, close, 1),
        ]

    # --- Not LOOSER than 0.60/0.40: these two must stay D. ------------------
    # Close 96% up the range confirms an upward skew — still not a P.
    above_centre = _classify(_one_bucket(12, Decimal("123")))
    assert above_centre.poc_position == pytest.approx(25 / 48)
    assert above_centre.shape is ProfileShape.D

    # Close 4% up the range confirms a downward skew — still not a b.
    below_centre = _classify(_one_bucket(11, Decimal("101")))
    assert below_centre.poc_position == pytest.approx(23 / 48)
    assert below_centre.shape is ProfileShape.D

    # --- Not TIGHTER than 0.60/0.40: the first qualifying bucket each way. ---
    # Without these the test's name overclaims: it pinned only the loosening
    # direction, and `POC_LOWER_BAND = 0.40 -> 0.30` survived the whole suite
    # because every other fixture sits far from the edges (_b_shape at 0.021,
    # _p_shape at 0.854). These two sit ON the first bucket that qualifies.
    just_upper = _classify(_one_bucket(14, Decimal("123")))
    assert just_upper.poc_position == pytest.approx(29 / 48)  # 0.6042, just over
    assert just_upper.shape is ProfileShape.P

    just_lower = _classify(_one_bucket(9, Decimal("101")))
    assert just_lower.poc_position == pytest.approx(19 / 48)  # 0.3958, just under
    assert just_lower.shape is ProfileShape.B


def test_poc_ties_resolve_to_the_lowest_bucket_for_reproducibility():
    # A perfectly uniform profile has 24 tied buckets. The choice itself is
    # arbitrary; being STABLE is not — an unstable tie-break would make the
    # rendered POC jump between identical cycles.
    candles = [_candle(i, 100, 110, 109, 10) for i in range(16)]
    profile = _classify(candles)
    assert profile.poc_position == pytest.approx(1 / (2 * BUCKET_COUNT))
    assert _classify(candles).poc == profile.poc


def test_thin_rule_wins_over_a_poc_rule_that_would_otherwise_fire():
    # Mutation-checked, and the ONLY case here that pins the rule ORDER. The
    # plain _thin_shape fixture does not: its POC sits low but its close is
    # high, so the b rule's own condition fails and thin would win from any
    # position in the chain.
    #
    # This fixture satisfies BOTH of P's conditions — POC in the upper part of
    # the range AND a close above the midpoint — while the value area still
    # spans 71% of the range. Nearly-uniform volume with one small extra node
    # in bucket 17 is what does it: the node wins the POC without narrowing the
    # value area. Move the thin check below the POC checks and this returns P.
    candles = [_candle(i, 100, 110, 109, 10) for i in range(20)]
    candles.insert(10, _candle(99, "107.1", "107.4", "107.2", 4))  # the POC node
    candles.append(_candle(50, 100, 110, 109, 10))  # a high close, last
    profile = _classify(candles)
    assert profile.poc_position >= 0.6  # P's POC condition: met
    assert profile.close_position > 0.5  # P's close condition: met
    assert profile.value_area_width_ratio >= 0.7  # ...but the profile is smeared
    assert profile.shape is ProfileShape.THIN


def test_a_uniform_profile_is_thin():
    # The threshold's anchor: volume spread perfectly evenly puts ~70% of it
    # inside ~70% of the range, so uniform must land on the thin side.
    profile = _classify(_thin_shape())
    assert profile.shape is ProfileShape.THIN
    assert profile.value_area_width_ratio >= 0.7


def test_a_high_poc_without_a_confirming_close_is_not_p():
    # The article's confirmation rule, and the discrimination test for it: same
    # volume distribution as the P fixture, only the latest close moved below
    # mid-range. If the close check stopped being consulted this stays "P".
    candles = _p_shape()
    candles[-1] = _candle(15, 100, 110, 101, 60)
    profile = _classify(candles)
    assert profile.poc_position >= 0.6
    assert profile.close_position < 0.5
    assert profile.shape is ProfileShape.D


def test_a_low_poc_without_a_confirming_close_is_not_b():
    candles = _b_shape()
    candles[-1] = _candle(15, 100, 110, 109, 60)
    profile = _classify(candles)
    assert profile.poc_position <= 0.4
    assert profile.close_position > 0.5
    assert profile.shape is ProfileShape.D


def test_a_close_exactly_on_the_midpoint_confirms_neither_p_nor_b():
    # Both close checks are STRICT (``> RANGE_MIDPOINT`` / ``< RANGE_MIDPOINT``), so the midpoint
    # itself confirms nothing and the skew falls through to D. Built with a
    # high POC so the P branch is one relational operator away from firing.
    candles = _p_shape()
    candles[-1] = _candle(15, 100, 110, 105, 60)  # closes exactly mid-range
    profile = _classify(candles)
    assert profile.close_position == 0.5
    assert profile.poc_position >= 0.6
    assert profile.shape is ProfileShape.D


def test_positions_are_fractions_of_the_window_range():
    candles = _p_shape()
    profile = _classify(candles)
    assert 0.0 <= profile.poc_position <= 1.0
    assert 0.0 <= profile.close_position <= 1.0
    # close 109 in a 100-110 range -> 90% up the range.
    assert profile.close_position == pytest.approx(0.9)
    assert profile.bucket_count == BUCKET_COUNT
    assert profile.candle_count == len(candles)


def test_classify_rejects_a_close_from_a_different_window():
    dist = build_profile(_d_shape(), 16)
    assert dist is not None
    with pytest.raises(ValueError, match="outside the profile's range"):
        classify_shape(dist, Decimal("999"))
    with pytest.raises(ValueError, match="outside the profile's range"):
        classify_shape(dist, Decimal("1"))


def test_classify_accepts_a_close_exactly_on_either_range_edge():
    dist = build_profile(_d_shape(), 16)
    assert dist is not None
    assert classify_shape(dist, dist.range_low).close_position == 0.0
    assert classify_shape(dist, dist.range_high).close_position == 1.0


# --------------------------------------------------------------------------
# compute_volume_profile — the wrapper the context builder uses.
# --------------------------------------------------------------------------


def test_the_volume_shares_separate_a_dominant_poc_from_a_marginal_one():
    # The whole reason these two fields exist: geometry cannot tell a POC that
    # owned most of the window from one that barely won its bucket. Both
    # fixtures below put the fat node in the SAME place, so every *_position
    # field matches — only the shares move.
    # The node sits inside ONE bucket in both, so the POC lands on the same
    # bucket and poc_position is identical. Only the weight behind it differs.
    # (value_area_width_ratio is NOT held equal — the outward walk stops at a
    # different bucket when the weights change, which is correct behaviour.)
    node = [_candle(i, "105.05", "105.35", "105.2", 400) for i in range(4, 16)]
    dominant = _wide_tails(4, "105.2", volume=1) + node
    marginal = _wide_tails(20, "105.2", volume=100) + [
        _candle(i, "105.05", "105.35", "105.2", 45) for i in range(20, 32)
    ]
    hot, cool = _classify(dominant), _classify(marginal)

    assert hot.poc == cool.poc
    assert hot.poc_position == cool.poc_position
    # ...and yet they are not the same profile, which is the point.
    assert hot.poc_volume_share > 0.9
    assert cool.poc_volume_share < 0.4


def test_two_profiles_can_render_identically_yet_hold_very_different_volume():
    # The justification for carrying the share fields at all, as an executable
    # fact rather than a sentence. Both windows put the node in the same single
    # bucket and both let the value area collapse onto it, so every rendered
    # line matches to the character — while one POC holds ~99% of the window's
    # volume and the other ~79%. Geometry alone cannot report that difference.
    from contrib.hyperliquid_perp.domains.perp.prompt_context import _volume_profile_lines

    def _window(tail_volume, node_volume):
        return [_candle(i, 100, 110, "105.2", tail_volume) for i in range(4)] + [
            _candle(i, "105.05", "105.35", "105.2", node_volume) for i in range(4, 16)
        ]

    dominant = compute_volume_profile(_window(1, 400), 16)
    merely_winning = compute_volume_profile(_window(300, 350), 16)
    assert dominant is not None and merely_winning is not None
    assert _volume_profile_lines(dominant, "4h") == _volume_profile_lines(merely_winning, "4h")
    assert dominant.poc_volume_share > 0.99
    assert merely_winning.poc_volume_share < 0.80


def test_the_value_area_share_is_what_the_walk_reached_not_the_target():
    # It must be the volume the walk ACTUALLY holds, not VALUE_AREA_FRACTION
    # echoed back: the walk stops on the first bucket that crosses the target,
    # so it lands at or above it. Returning the constant would look right in
    # every assertion that only checks ">= 0.70".
    profile = _classify(_d_shape())
    assert profile.value_area_volume_share >= float(VALUE_AREA_FRACTION)
    assert profile.value_area_volume_share != float(VALUE_AREA_FRACTION)
    # The POC bucket is inside the value area, so its share cannot exceed it.
    assert profile.poc_volume_share <= profile.value_area_volume_share


def test_the_shares_are_fractions_of_volume_not_of_the_range():
    # A concentrated profile pulls the two apart in OPPOSITE directions: its
    # value area covers a small slice of the RANGE while holding most of the
    # VOLUME. That is only possible if they measure different things.
    #
    # Deliberately not asserted on a thin profile: volume spread perfectly
    # evenly puts N/24 of the volume in N/24 of the range, so the two numbers
    # coincide there and the assertion would pass whichever one were returned.
    profile = _classify(_d_shape())
    assert profile.value_area_width_ratio < 0.4
    assert profile.value_area_volume_share > 0.7


def test_compute_matches_building_and_classifying_separately():
    candles = _p_shape()
    dist = build_profile(candles, 16)
    assert dist is not None
    assert compute_volume_profile(candles, 16) == classify_shape(dist, candles[-1].close)


def test_compute_returns_none_whenever_the_distribution_is_unavailable():
    assert compute_volume_profile(_d_shape(), 0) is None
    assert compute_volume_profile(_d_shape(), DEFAULT_WINDOW_CANDLES) is None
    assert compute_volume_profile([_candle(i, 100, 110, 105, 0) for i in range(16)], 16) is None


# --------------------------------------------------------------------------
# PriceDistribution — the intermediate type's own invariants.
# --------------------------------------------------------------------------


def _distribution(**overrides) -> PriceDistribution:
    base = {
        "range_low": Decimal("100"),
        "range_high": Decimal("110"),
        "bucket_volumes": (Decimal("1"),) * BUCKET_COUNT,
        "candle_count": 12,
    }
    base.update(overrides)
    return PriceDistribution(**base)


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"range_low": Decimal("0")}, "range_low must be > 0"),
        ({"range_high": Decimal("100")}, "must be > range_low"),
        ({"bucket_volumes": ()}, "must not be empty"),
        ({"bucket_volumes": (Decimal("1"), Decimal("-1"))}, "must all be >= 0"),
        ({"bucket_volumes": (Decimal("0"), Decimal("0"))}, "must sum to > 0"),
        ({"candle_count": 0}, "candle_count must be >= 1"),
    ],
)
def test_price_distribution_rejects_structurally_broken_input(overrides, match):
    with pytest.raises(ValueError, match=match):
        _distribution(**overrides)
