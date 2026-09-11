"""What each feature is worth at a bar, and the property that it saw no future.

The centre of this file is
:func:`test_no_feature_changes_when_the_future_is_taken_away`: every feature
in the vocabulary is computed twice over the same bundle, once with the rest
of history present and once with everything after the bar removed, and the two
have to agree. That is the check on plan §3.6's construction guarantee. It is
written as a sweep over ``feature_names()`` rather than as a list, so a
feature added to the vocabulary is covered the day it is added — and it is
guarded by a companion test asserting that every feature actually HAS a value
at the bar being checked, because a sweep over columns of ``None`` would agree
with itself perfectly.

Everything else here is the arithmetic: exact values on hand-written series,
the alignment rules for the daily and funding sources, and the places a value
is deliberately ``None`` rather than a number.
"""

from __future__ import annotations

import dataclasses
from decimal import Decimal

import pytest

from contrib.autoresearch import features as features_module
from contrib.autoresearch.features import (
    LIVE_CANDLE_LOOKBACK,
    FeatureError,
    FeatureFrame,
    SeriesBundle,
)
from contrib.autoresearch.upstream import Candle, FundingPoint, MarketRegime, context_analytics
from contrib.autoresearch.vocabulary import (
    FeatureKind,
    FeatureRef,
    feature_names,
    parse_feature_name,
)

from .conftest import ANCHOR_MS, MS_PER_HOUR, candles, funding_points

_STEP_MS = 4 * MS_PER_HOUR
_DAY_MS = 24 * MS_PER_HOUR
_ANALYTICS = context_analytics()


def _daily(count: int, *, start_ms: int, closes: list[float] | None = None) -> list[Candle]:
    """``count`` consecutive DAILY bars, for the backdrop the 4h series reads."""
    prices = closes or [1000 + index for index in range(count)]
    return candles(prices, start_ms=start_ms, step_ms=_DAY_MS)


def _frame(closes: list[float], **kwargs) -> FeatureFrame:
    return FeatureFrame(SeriesBundle(candles(closes, **kwargs)))


def _column(frame: FeatureFrame, name: str) -> tuple:
    """One feature's whole array, addressed the way a spec addresses it.

    By NAME rather than by ``(kind, period)``, because that is the only way in:
    ``series`` takes a validated ``FeatureRef``, so a test cannot reach for a
    period the vocabulary does not have — which is the point, and is why the
    arithmetic below is written at real periods rather than at a convenient 2
    or 3.
    """
    return frame.series(FeatureRef(*parse_feature_name(name)))


# -- the bundle ------------------------------------------------------------


def test_a_bundle_with_no_bars_is_refused_rather_than_measured():
    with pytest.raises(FeatureError, match="at least one bar"):
        SeriesBundle([])


def test_a_series_out_of_order_is_refused_naming_the_pair():
    """Every lookback below is a statement about an ordered series."""
    ordered = candles([100, 101, 102])
    with pytest.raises(FeatureError, match="bars must be strictly ascending"):
        SeriesBundle([ordered[0], ordered[2], ordered[1]])
    with pytest.raises(FeatureError, match="funding settlements must be strictly ascending"):
        SeriesBundle(ordered, funding=list(reversed(funding_points(3))))


def test_a_series_whose_closes_are_out_of_order_is_refused_too():
    """The stamp the ALIGNMENTS read, which is not the one the lookbacks read.

    Venue rows have ``close_time`` one interval after ``open_time``, so on real
    data the two orders agree. A hand-assembled bundle mixing intervals is
    where they do not — and there the daily pointer would stall silently
    rather than refuse.
    """
    long_bar = candles([100])[0]
    # A shorter bar that OPENS later and CLOSES earlier — each one valid on its
    # own (the DTO refuses a bar that closes before it opens), ascending by
    # open, and out of order by close. A 1h page written into a 4h series looks
    # exactly like this.
    nested = candles([110], start_ms=long_bar.open_time + MS_PER_HOUR, step_ms=MS_PER_HOUR)[0]
    assert nested.open_time > long_bar.open_time and nested.close_time < long_bar.close_time
    with pytest.raises(FeatureError, match="bar closes must be strictly ascending"):
        SeriesBundle([long_bar, nested])


def test_a_frame_cannot_be_pointed_at_a_different_history():
    """Its cached columns belong to the bundle it was built from, and to no other.

    ``_require_source`` is consulted on a cache MISS, so a swapped bundle would
    not even be re-checked for a missing series.
    """
    frame = _frame([100, 110, 120])
    with pytest.raises(dataclasses.FrozenInstanceError):
        frame.bundle = SeriesBundle(candles([200, 210, 220]))


def test_a_bundle_keeps_its_own_copy_of_what_it_was_handed():
    """A caller appending to its list must not change a frame's computed columns."""
    given = candles([100, 101, 102])
    bundle = SeriesBundle(given)
    given.append(candles([103], start_ms=ANCHOR_MS + 3 * _STEP_MS)[0])
    assert len(bundle.bars) == 3


# -- the arithmetic --------------------------------------------------------


def test_close_is_the_bar_that_closed():
    assert _column(_frame([100, 110, 120]), "close") == (100.0, 110.0, 120.0)


def test_a_return_is_none_until_the_bar_it_reaches_back_to_exists():
    series = _column(_frame([100, 110, 121]), "ret_1")
    assert series[0] is None
    assert series[1] == pytest.approx(0.1)
    assert series[2] == pytest.approx(0.1)


def test_a_moving_average_includes_the_bar_it_is_reported_at():
    closes = [100 + index for index in range(10)] + [200]
    series = _column(_frame(closes), "sma_10")
    assert series[:9] == (None,) * 9
    assert series[9] == pytest.approx(104.5)  # the mean of 100..109
    assert series[10] == pytest.approx((sum(range(101, 110)) + 200) / 10)  # it slid by one


def test_a_donchian_channel_excludes_the_bar_it_is_reported_at():
    """The exclusion is what makes ``close > donchian_high_N`` expressible.

    With the current bar inside the window the condition could only hold when
    the close IS the window's high, so the rule would read as a breakout and
    fire almost never. The highs here are deliberately not the closes, so the
    two windows give different answers and the test can tell them apart.
    """
    closes = [100 + index for index in range(11)]
    frame = _frame(closes, highs=[close + 10 for close in closes])
    series = _column(frame, "donchian_high_10")
    assert series[:10] == (None,) * 10
    assert series[10] == pytest.approx(119.0)  # the highs of bars 0..9, not this bar's 120


def test_a_donchian_low_reads_the_low_series():
    closes = [100 + index for index in range(11)]
    lows = [close - 10 for close in closes]
    lows[3] = 50.0  # one bar dips far below the rest of the channel
    assert _column(_frame(closes, lows=lows), "donchian_low_10")[10] == pytest.approx(50.0)


def test_realised_volatility_is_the_deviation_of_the_bar_returns():
    """Ten returns of one size, then ten of alternating sign — both hand-checkable."""
    steady = [100.0]
    for _ in range(10):
        steady.append(steady[-1] * 1.1)
    assert _column(_frame(steady), "realized_vol_10")[10] == pytest.approx(0.0, abs=1e-9)

    swinging = [100.0]
    for index in range(10):
        swinging.append(swinging[-1] * (1.1 if index % 2 == 0 else 0.9))
    # Five returns of +0.1 and five of -0.1: sample deviation sqrt(10 * 0.01 / 9).
    assert _column(_frame(swinging), "realized_vol_10")[10] == pytest.approx(0.10540925533894598)


def test_atr_percent_is_the_atr_against_this_bar_s_close():
    closes = [30000 + 100 * index for index in range(40)]
    frame = _frame(closes)
    atr = _column(frame, "atr_14")
    pct = _column(frame, "atr_pct_14")
    assert atr[-1] is not None
    assert pct[-1] == pytest.approx(atr[-1] / closes[-1])


# -- the daily backdrop ----------------------------------------------------


def test_a_daily_bar_is_invisible_until_the_day_it_covers_has_closed():
    """The alignment rule plan §4 states, pinned at the millisecond either side.

    The daily bars here close on the 4h grid: the first at bar 0's close, the
    second at bar 6's. So bar 5 must still read the first day's close — the
    second day is in progress, and its close is a number no rule may see yet —
    while bar 6, which closes at the very instant the day does, reads it.
    """
    day_bars = _daily(3, start_ms=ANCHOR_MS - _DAY_MS + _STEP_MS, closes=[1000, 2000, 3000])
    bundle = SeriesBundle(candles([100, 110, 120, 130, 140, 150, 160]), daily=day_bars)
    series = _column(FeatureFrame(bundle), "close_1d")
    assert series[0] == pytest.approx(1000.0)
    assert series[5] == pytest.approx(1000.0)  # the second day has not closed yet
    assert series[6] == pytest.approx(2000.0)  # it closes at exactly this bar's close


def test_a_daily_average_is_none_until_that_many_days_have_closed():
    """Warm-up, checked on a bundle that can answer the feature somewhere.

    Where it can never answer it, the refusal two tests down takes over: a
    column of ``None`` at every bar is not a warm-up story, it is a feature
    this history cannot produce.
    """
    daily = _daily(22, start_ms=ANCHOR_MS - 22 * _DAY_MS)
    bundle = SeriesBundle(candles([100 + index for index in range(12)]), daily=daily)
    series = _column(FeatureFrame(bundle), "sma_1d_20")
    assert series[0] is not None  # twenty-two days have closed behind this bar


def test_a_daily_series_that_stops_early_does_not_carry_its_last_close_forward():
    """The same rule the funding rate gets, and for the same reason.

    ``fetch --interval 4h`` and ``fetch --interval 1d`` are separate
    invocations, so "4h current, 1d months behind" is the default consequence
    of running one and not the other — and the gap scan is per-interval, so
    the short daily series scans clean. Carried forward, ``sma_1d_20`` freezes
    at one number and a daily trend filter is permanently on or permanently
    off, scored as a filter that was live.
    """
    day_bars = _daily(2, start_ms=ANCHOR_MS - _DAY_MS + _STEP_MS, closes=[1000, 2000])
    bars = candles([100 + index for index in range(18)])  # three days of 4h bars
    frame = FeatureFrame(SeriesBundle(bars, daily=day_bars))
    series = _column(frame, "close_1d")
    assert series[6] == pytest.approx(2000.0)  # the second day closed here
    assert series[11] == pytest.approx(2000.0)  # still inside that day
    # Bar 12 closes exactly one day after that daily bar did — the instant the
    # next daily bar was due, and it is not there. On a complete series this
    # age is unreachable: a bar closing when a day closes sees that day at an
    # age of zero, so the oldest a close ever gets is 20h. Reaching 24h IS the
    # gap, and it lands on the 00:00 UTC bar.
    assert series[12] is None


def test_a_daily_close_one_millisecond_into_the_future_is_invisible():
    """The boundary, pinned where a coarser test cannot see it.

    A daily bar closing at the same instant as a 4h bar is visible to it; one
    closing a millisecond later is not, and nothing about the number would
    look wrong if it were.
    """
    # Two daily bars a clean day apart, and 4h bars placed so that one closes
    # one millisecond BEFORE the second day does and the next closes after it.
    daily = candles([1000, 2000], start_ms=ANCHOR_MS, step_ms=_DAY_MS)
    just_before = daily[1].close_time - 1
    bars = candles([100, 110], start_ms=just_before - _STEP_MS, step_ms=_STEP_MS)
    assert bars[0].close_time == just_before
    series = _column(FeatureFrame(SeriesBundle(bars, daily=daily)), "close_1d")
    assert series[0] == pytest.approx(1000.0)  # the second day closes 1 ms too late
    assert series[1] == pytest.approx(2000.0)  # and by this bar it has closed


def test_a_daily_mean_spans_the_days_it_names_rather_than_counting_closes():
    """Twenty closes drawn from twenty-five days is a twenty-five-day mean.

    The name says twenty, so a series missing days inside the window is
    ``None`` rather than a number over a longer stretch than it claims — the
    same rule the funding windows are held to, counted the same way.
    """
    whole = _daily(40, start_ms=ANCHOR_MS - 40 * _DAY_MS)
    # Five days missing from INSIDE the window the bars will read — a hole
    # further back would be outside it and would rightly change nothing.
    holed = whole[:30] + whole[35:]
    bars = candles([100 + index for index in range(6)])
    assert _column(FeatureFrame(SeriesBundle(bars, daily=whole)), "sma_1d_20")[0] is not None
    with pytest.raises(FeatureError, match="sma_1d_20 has no value at any"):
        _column(FeatureFrame(SeriesBundle(bars, daily=holed)), "sma_1d_20")


def test_a_daily_series_that_is_not_daily_is_refused_at_construction():
    """One positional slip in a bundle builder, and every 1d name is a lie.

    ``SeriesBundle(bars, daily=bars)`` made ``sma_1d_200`` a 200-BAR mean of
    4h candles — 33 days under a 200-day name — and left the staleness rule
    measuring against a cadence nothing had checked.
    """
    bars = candles([100 + index for index in range(6)])
    with pytest.raises(FeatureError, match="this is not a 1d series"):
        SeriesBundle(bars, daily=bars)


def test_a_daily_feature_without_a_daily_series_is_refused_by_name():
    """``None`` at every bar would score the strategy as tried; this says it was not."""
    frame = FeatureFrame(SeriesBundle(candles([100, 110])))
    with pytest.raises(FeatureError, match="close_1d is computed from the daily series"):
        _column(frame, "close_1d")


# -- funding ---------------------------------------------------------------


def test_the_funding_rate_is_the_settlement_in_force_at_the_close():
    bundle = SeriesBundle(candles([100, 110]), funding=funding_points(12))
    series = _column(FeatureFrame(bundle), "funding_rate")
    # Bar 0 closes four hours in, so the settlement stamped at that instant is
    # the fifth one; bar 1 closes four hours later again.
    assert series[0] == pytest.approx(float(Decimal("0.00001") * 5))
    assert series[1] == pytest.approx(float(Decimal("0.00001") * 9))


def test_a_funding_series_that_stops_early_does_not_carry_its_last_rate_forward():
    """Carrying it would price weeks of funding off one observation."""
    bundle = SeriesBundle(candles([100, 110, 120]), funding=funding_points(5))
    series = _column(FeatureFrame(bundle), "funding_rate")
    assert series[0] == pytest.approx(float(Decimal("0.00001") * 5))
    assert series[1] is None
    assert series[2] is None


def test_cumulative_funding_sums_the_settlements_inside_the_bars_it_names():
    bundle = SeriesBundle(candles([100, 110, 120]), funding=funding_points(12))
    series = _column(FeatureFrame(bundle), "funding_cum_1")
    assert series[0] is None  # there is no previous bar to open the window
    # Bar 1's window is the four settlements after bar 0's close: numbers 6..9.
    assert series[1] == pytest.approx(float(Decimal("0.00001") * (6 + 7 + 8 + 9)))


@pytest.mark.parametrize(
    ("observed", "expected", "covered"),
    [(4, 4, True), (3, 4, False), (7, 7, True), (6, 7, False), (18, 20, True), (17, 20, False)],
)
def test_the_coverage_rule_rounds_up_whatever_the_window_counts(observed, expected, covered):
    """One rounding rule for every window, asserted on the rule itself.

    The daily lane used to write this comparison out by hand with a floor, and
    agreed with the shared one only by luck of the declared periods (90% of 20
    is exactly 18). A period of 7 is where they part: floored, it accepts six
    days under a seven-day name.
    """
    assert features_module._window_is_covered(observed, expected) is covered


def test_a_four_settlement_window_needs_all_four():
    """The coverage fraction rounds UP, and at this window that is the whole rule.

    ``funding_cum_1`` spans four hours. Floored, 90% of four is three — so the
    window this guard was written to stop reporting, the one missing a
    settlement, would have gone on being reported a quarter short. Every
    longer window lands well clear of its boundary; this is the one where the
    rounding decides.
    """
    whole = funding_points(20)
    holed = [point for point in whole if point.time != whole[9].time]
    bars = candles([100, 110, 120])
    assert _column(FeatureFrame(SeriesBundle(bars, funding=whole)), "funding_cum_1")[2] is not None
    assert _column(FeatureFrame(SeriesBundle(bars, funding=holed)), "funding_cum_1")[2] is None


def test_cumulative_funding_is_none_when_the_series_starts_inside_the_window():
    """A sum missing part of its window is understated and does not look wrong.

    Bar 1's window runs from bar 0's close to its own, and the settlements it
    should hold are the ones an hour, two, three and four after that opening.
    A series starting three hours in holds only the last two of them, so the
    carry it would report is a little over half the real one — which is why
    this is ``None`` rather than a sum. The neighbouring test covers the case
    that looks the same and is not: a series starting exactly one hour in
    misses nothing, because the settlement ON the boundary belongs to the
    previous window.
    """
    late = funding_points(12, start_ms=ANCHOR_MS + 7 * MS_PER_HOUR)
    bundle = SeriesBundle(candles([100, 110, 120]), funding=late)
    assert _column(FeatureFrame(bundle), "funding_cum_1")[1] is None


def test_cumulative_funding_still_sums_a_series_that_starts_on_the_window_edge():
    """The boundary the guard above must not over-refuse.

    The window is half-open — the settlement at bar 0's close is that bar's,
    not bar 1's — so a series whose first point lands one hour after bar 0
    closes covers bar 1 completely.
    """
    edge = funding_points(12, start_ms=ANCHOR_MS + 5 * MS_PER_HOUR)
    bundle = SeriesBundle(candles([100, 110, 120]), funding=edge)
    summed = _column(FeatureFrame(bundle), "funding_cum_1")[1]
    assert summed == pytest.approx(float(Decimal("0.00001") * (1 + 2 + 3 + 4)))


def test_the_funding_zscore_is_the_borrowed_function_over_the_whole_history():
    """The slice is an optimisation, so it is checked against the unsliced call.

    :mod:`~contrib.autoresearch.features` hands the borrowed z-score a window
    cut to the bounds that function selects for itself, minus the one point
    being scored. That is only sound while the two agree, and nothing about a
    z-score's magnitude would look wrong if they stopped agreeing — so the
    check is an equality against the function applied to the whole history
    before that point.
    """
    points = funding_points(400)
    bars = candles(
        [30000 + 10 * index for index in range(6)], start_ms=ANCHOR_MS + 200 * MS_PER_HOUR
    )
    computed = _column(FeatureFrame(SeriesBundle(bars, funding=points)), "funding_zscore_7")
    times = [point.time for point in points]
    for index, bar in enumerate(bars):
        found = max(i for i, time in enumerate(times) if time <= bar.close_time)
        earlier = [point for point in points if point.time < points[found].time]
        expected, _samples = _ANALYTICS.funding_zscore(
            earlier, points[found].rate, bar.close_time, 7
        )
        assert computed[index] == pytest.approx(expected)
    assert all(value is not None for value in computed)


def test_the_rate_being_scored_is_not_part_of_the_window_it_is_scored_against():
    """The live caller's arrangement: ``current`` is not a member of ``history``.

    Live passes the venue's unsettled rate, which is not in the settlement
    history at all, and upstream's own comment says why that matters — folding
    a value into its own mean and deviation makes a genuine outlier read as
    less extreme than it is. Here the rate IS a settled point, so keeping the
    arrangement means cutting the window at it. The two answers differ, which
    is what makes this a choice rather than a detail.
    """
    points = funding_points(400)
    spike = points[-1]
    points = points[:-1] + [FundingPoint(time=spike.time, rate=spike.rate * 40)]
    bars = candles([30000], start_ms=points[-1].time - 3 * MS_PER_HOUR)
    computed = _column(FeatureFrame(SeriesBundle(bars, funding=points)), "funding_zscore_7")[0]
    times = [point.time for point in points]
    found = max(i for i, time in enumerate(times) if time <= bars[0].close_time)
    including, _samples = _ANALYTICS.funding_zscore(
        points, points[found].rate, bars[0].close_time, 7
    )
    assert computed == pytest.approx(
        _ANALYTICS.funding_zscore(
            [p for p in points if p.time < points[found].time],
            points[found].rate,
            bars[0].close_time,
            7,
        )[0]
    )
    assert abs(computed) > abs(including)  # the outlier reads as MORE extreme, correctly


def test_a_funding_window_the_series_does_not_reach_back_across_is_refused():
    """Otherwise the 7-, 14- and 30-day features are one identical column.

    The borrowed function's own floor is 24 samples — one day of settlements —
    so on a store whose funding walk is younger than the window, it answers a
    number for all three window lengths and nothing says they are the same
    number. ``fetch`` walks funding from the same ``--since`` as the candles,
    so the opening month of every store is in exactly that state.
    """
    points = funding_points(48)  # two days of settlements
    bars = candles([30000 + 10 * index for index in range(4)], start_ms=ANCHOR_MS + 40 * MS_PER_HOUR)
    frame = FeatureFrame(SeriesBundle(bars, funding=points))
    for window in ("funding_zscore_30", "funding_zscore_7"):
        # Refused rather than answered: unavailable at every bar is not a
        # market fact, it is a bundle that cannot be asked this question.
        with pytest.raises(FeatureError, match=f"{window} has no value at any"):
            _column(frame, window)


def test_a_window_with_a_hole_in_its_middle_is_refused_as_well():
    """The case the front-edge guard this replaced could not see at all.

    A settlement series that STARTS before the window and ENDS after it can
    still be missing most of the middle — an interrupted walk resumed from a
    later ``--since`` — and the borrowed z-score's own floor is 24 samples, so
    it answers a one-day z-score under a thirty-day name. Occupancy is
    therefore counted rather than inferred from where the series begins.
    """
    whole = funding_points(40 * 24)
    holed = [point for point in whole if not 5 * 24 <= whole.index(point) < 33 * 24]
    bars = candles(
        [30000 + 10 * index for index in range(4)], start_ms=ANCHOR_MS + 34 * 24 * MS_PER_HOUR
    )
    intact = FeatureFrame(SeriesBundle(bars, funding=whole))
    assert all(value is not None for value in _column(intact, "funding_zscore_30"))
    with pytest.raises(FeatureError, match="funding_zscore_30 has no value at any"):
        _column(FeatureFrame(SeriesBundle(bars, funding=holed)), "funding_zscore_30")


def test_a_funding_feature_without_a_funding_series_is_refused_by_name():
    frame = FeatureFrame(SeriesBundle(candles([100, 110])))
    with pytest.raises(FeatureError, match="funding_rate is computed from the funding series"):
        _column(frame, "funding_rate")


# -- the regime ------------------------------------------------------------


def test_the_regime_is_none_while_its_indicators_are_warming_up():
    """The live path refuses the cycle here; the mirror of that is "not available".

    A ``RANGING`` during warm-up would be a fabricated-calm label, and a
    ``regime == ranging`` filter would fire on it — trading the warm-up rather
    than the market.
    """
    series = _column(_frame([30000 + 100 * index for index in range(60)]), "regime")
    assert all(value is None for value in series[:49])
    assert series[-1] is not None


def test_the_regime_is_the_borrowed_label_once_the_indicators_exist():
    closes = [30000 + 100 * index for index in range(60)]
    values = _ANALYTICS.compute_indicators(candles(closes), ["atr_14", "ema_20", "ema_50"])
    expected = _ANALYTICS.classify_regime(values, Decimal(str(closes[-1])))
    assert isinstance(expected, MarketRegime)
    assert _column(_frame(closes), "regime")[-1] is expected


def test_every_engine_backed_feature_is_computed_in_one_walk(monkeypatch):
    """Asking for two indicators must not walk the series twice.

    The walk is the only expensive thing this module does — about 2.5 ms a bar —
    so a frame computing each indicator separately would multiply the cost of
    an experiment by the number of indicators its spec happens to mention.
    """
    real = features_module._ANALYTICS
    calls: list[int] = []

    def counting(candles, names):
        calls.append(len(candles))
        return real.compute_indicators(candles, names)

    monkeypatch.setattr(
        features_module,
        "_ANALYTICS",
        type(real)(
            compute_indicators=counting,
            classify_regime=real.classify_regime,
            funding_zscore=real.funding_zscore,
        ),
    )
    # Sixty bars, because every one of the four has to have a value: a column
    # of ``None`` is refused now, and a fixture too short for ``ema_50`` would
    # be testing the refusal rather than the walk.
    frame = _frame([30000 + 100 * index for index in range(60)])
    _column(frame, "ema_20")
    _column(frame, "ema_50")
    _column(frame, "rsi_14")
    _column(frame, "regime")
    assert len(calls) == 60  # one per bar, not one per bar per feature


def test_the_indicator_engine_sees_the_window_the_live_path_fetches():
    """Not the whole prefix: the live fetch asks for a fixed lookback each cycle.

    The two answers differ — an EMA carries seed weight from wherever it
    started — so a research indicator computed over an ever-growing prefix is
    not the number the trader's context was built from. It is also the
    difference between a linear cost and a quadratic one, since the engine
    rebuilds its frame on every call.
    """
    closes = [30000 + 40 * index for index in range(LIVE_CANDLE_LOOKBACK + 60)]
    bars = candles(closes)
    computed = _column(FeatureFrame(SeriesBundle(bars)), "ema_50")[-1]
    windowed = _ANALYTICS.compute_indicators(bars[-LIVE_CANDLE_LOOKBACK:], ["ema_50"])["ema_50"]
    whole = _ANALYTICS.compute_indicators(bars, ["ema_50"])["ema_50"]
    assert computed == pytest.approx(windowed, rel=1e-12)
    assert computed != pytest.approx(whole, rel=1e-12)


def test_an_indicator_engine_that_answers_nothing_at_all_is_refused(monkeypatch):
    """A column of ``None`` past its warm-up is a broken engine, not a quiet market.

    Upstream catches a stockstats failure per indicator and returns ``None``.
    On the live path that is one warning an operator sees per cycle; here the
    engine is called once per bar, so the same failure is thousands of
    ``None``s that read exactly like warm-up — and every strategy touching
    them would be scored as tried.
    """
    real = features_module._ANALYTICS
    monkeypatch.setattr(
        features_module,
        "_ANALYTICS",
        type(real)(
            compute_indicators=lambda candles, names: dict.fromkeys(names),
            classify_regime=real.classify_regime,
            funding_zscore=real.funding_zscore,
        ),
    )
    frame = _frame([30000 + 100 * index for index in range(120)])
    with pytest.raises(FeatureError, match="the indicator engine returned nothing"):
        _column(frame, "ema_20")


def test_a_column_of_silence_is_refused_however_many_times_it_is_asked_for():
    """The indicator walk fills five columns at once, so four reach a caller cached.

    Checked on the SECOND request as well as the first: the guard used to run
    only on the computing path, so asking for a working indicator first and
    the short one afterwards handed back the column of silence. That is the
    order an evaluator actually uses — ``spec.features`` sorts the regime last.
    """
    frame = _frame([30000 + 100 * index for index in range(45)])
    assert _column(frame, "rsi_14")[-1] is not None  # fills the cache for all five
    for _attempt in range(2):
        with pytest.raises(FeatureError, match="ema_50 has no value at any"):
            _column(frame, "ema_50")


def test_a_warming_up_engine_is_not_mistaken_for_a_broken_one():
    """Both refuse, and the sentences are what tell them apart.

    A bundle too short for ``ema_50`` cannot answer it — true, and said in
    those words. Calling that a broken engine would send someone to look at
    stockstats when what they need is more history.
    """
    short = _frame([30000 + 100 * index for index in range(12)])
    with pytest.raises(FeatureError, match="ema_50 has no value at any of this bundle's 12 bars"):
        _column(short, "ema_50")
    assert _column(_frame([30000 + 100 * index for index in range(60)]), "ema_50")[-1] is not None


# -- offsets ---------------------------------------------------------------


def test_an_offset_reads_the_value_the_named_bar_had():
    frame = _frame([100, 110, 120, 130])
    assert frame.value_at(FeatureRef(FeatureKind.CLOSE, None, 1), 3) == pytest.approx(120.0)
    assert frame.value_at(FeatureRef(FeatureKind.CLOSE, None, 3), 3) == pytest.approx(100.0)


def test_an_offset_reaching_past_the_start_of_the_series_is_none():
    frame = _frame([100, 110])
    assert frame.value_at(FeatureRef(FeatureKind.CLOSE, None, 5), 1) is None


def test_a_bar_index_outside_the_bundle_is_refused_rather_than_wrapped():
    """A negative index would silently read from the END of the series."""
    frame = _frame([100, 110])
    with pytest.raises(FeatureError, match="outside the bundle's 2 bars"):
        frame.value_at(FeatureRef(FeatureKind.CLOSE), -1)
    with pytest.raises(FeatureError, match="outside the bundle's 2 bars"):
        frame.value_at(FeatureRef(FeatureKind.CLOSE), 2)


# -- the property the whole module exists for ------------------------------

# Long enough for every period in the vocabulary to have warmed up by the bar
# the sweep checks, and long enough AFTER it that removing the rest is a real
# removal: a truncation at the last bar would compare a bundle with itself.
_SWEEP_BARS = 260
_SWEEP_AT = 210


@pytest.fixture(scope="module")
def sweep() -> SeriesBundle:
    """A bundle broad enough that every feature has a value at :data:`_SWEEP_AT`.

    The daily series starts well before the bars, the way a real store's does
    — 200 daily closes have to be behind the checked bar for ``sma_1d_200`` to
    be anything but ``None`` — and the funding series starts a month early for
    the same reason on ``funding_zscore_30``.
    """
    closes = [
        30000 + 120 * index + (400 if index % 3 == 0 else -250) for index in range(_SWEEP_BARS)
    ]
    daily = _daily(300, start_ms=ANCHOR_MS - 250 * _DAY_MS)
    funding = funding_points(75 * 24, start_ms=ANCHOR_MS - 31 * _DAY_MS)
    return SeriesBundle(candles(closes), daily=daily, funding=funding)


def test_every_feature_has_a_value_at_the_bar_the_sweep_checks(sweep):
    """Guard the sweep below: columns of ``None`` would agree with anything."""
    frame = FeatureFrame(sweep)
    missing = [
        name
        for name in feature_names()
        if frame.value_at(FeatureRef(*parse_feature_name(name)), _SWEEP_AT) is None
    ]
    assert missing == []


@pytest.mark.parametrize("drop_other_series", [False, True], ids=["bars-only", "everything"])
def test_no_feature_changes_when_the_future_is_taken_away(sweep, drop_other_series):
    """Plan §3.6: recompute at bar ``t`` with nothing after ``t`` in the bundle.

    Twice over, because there are two futures to remove. Truncating the BARS
    alone catches a lookback that indexes forward; truncating the daily and
    funding series as well catches an alignment reaching for a daily close or
    a settlement the bar could not have seen — the failure a bars-only
    truncation sails past, since those rows would still be there.
    """
    full = FeatureFrame(sweep)
    cut_at = sweep.bars[_SWEEP_AT].close_time
    truncated = FeatureFrame(
        SeriesBundle(
            sweep.bars[: _SWEEP_AT + 1],
            daily=[bar for bar in sweep.daily if bar.close_time <= cut_at]
            if drop_other_series
            else sweep.daily,
            funding=[point for point in sweep.funding if point.time <= cut_at]
            if drop_other_series
            else sweep.funding,
        )
    )
    for name in feature_names():
        ref = FeatureRef(*parse_feature_name(name))
        assert full.value_at(ref, _SWEEP_AT) == truncated.value_at(ref, _SWEEP_AT), name
