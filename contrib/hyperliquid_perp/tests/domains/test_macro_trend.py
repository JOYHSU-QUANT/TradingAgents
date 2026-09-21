"""Tests for the daily SMA(50)/SMA(200) macro-trend backdrop.

The series in this file are built from STEPS in a flat price, because a step
makes both averages solvable by hand. With a single step of size ``s`` at bar
``K``, and ``m = i - K + 1`` bars elapsed since it:

    SMA(50)(i) - SMA(200)(i) = s * g(m),  g(m) = min(50, m)/50 - min(200, m)/200

so ``g`` is 0 before the step, rises linearly to 0.75 at ``m == 50``, falls
back to 0 at ``m == 200``, and is 0 after. Two steps superpose, because an
average is linear in its inputs. Every expected number below is derived from
that identity rather than read off a run — the derivation is written out at
each case.

A flat stretch is therefore also the EQUALITY case (both averages sit on the
same price), which is what the streak rule's own test uses.
"""

from __future__ import annotations

import logging
from decimal import Decimal

import pytest

from contrib.hyperliquid_perp.common.constants import MIN_MACRO_TREND_LOOKBACK
from contrib.hyperliquid_perp.domains.perp.macro_trend import (
    MACRO_CANDLE_INTERVAL,
    MACRO_FAST_PERIOD,
    MACRO_SLOW_PERIOD,
    MAX_DAILY_CANDLE_AGE_MS,
    compute_macro_trend,
)
from contrib.hyperliquid_perp.domains.perp.schema import (
    Candle,
    CandleInterval,
    MacroAlignment,
)

_DAY_MS = 24 * 60 * 60_000
# 2024-01-01T00:00:00Z — a round UTC midnight, so bar ``i`` covers the day
# ``2024-01-01 + i`` and its date is checkable by hand.
_DAY0_MS = 1_704_067_200_000


def _daily(index: int, close, *, day0: int = _DAY0_MS) -> Candle:
    """One daily bar at ``close``, flat OHLC, on the ``day0`` grid.

    ``close_time`` is the next bar's open minus a millisecond, which is how
    Hyperliquid stamps a closed candle (measured in PR #258). The producer
    dates a bar by its OPEN, so this convention is the one that would hide an
    off-by-one-day if it ever read the close instead.

    ``day0`` moves the whole series; every test in THIS file leaves it at the
    default, so its dates stay hand-checkable. It exists for the renderer's
    tests, whose context carries its own ``as_of`` that the series has to sit
    behind.
    """
    price = close if isinstance(close, Decimal) else Decimal(str(close))
    open_time = day0 + index * _DAY_MS
    return Candle(
        open_time=open_time,
        close_time=open_time + _DAY_MS - 1,
        open=price,
        high=price,
        low=price,
        close=price,
        volume=Decimal(1),
    )


def _series(closes, *, day0: int = _DAY0_MS) -> list[Candle]:
    return [_daily(i, c, day0=day0) for i, c in enumerate(closes)]


def _as_of(candles) -> int:
    """The context anchor that makes the feed exactly current."""
    return candles[-1].close_time


def _stepped(n: int, base, steps: dict[int, str], *, day0: int = _DAY0_MS) -> list[Candle]:
    """``n`` bars at ``base``, with each ``{bar: delta}`` applied from that bar on."""
    closes = []
    for i in range(n):
        price = Decimal(str(base))
        for at, delta in steps.items():
            if i >= at:
                price += Decimal(delta)
        closes.append(price)
    return _series(closes, day0=day0)


# --------------------------------------------------------------------------
# The vocabulary this module is bound to
# --------------------------------------------------------------------------


def test_the_slow_period_and_the_config_floor_are_one_number():
    # They are two NAMES for one value, bound by assignment: the floor exists
    # BECAUSE a window shorter than the slow period has no slow average at any
    # bar. Written out twice they could drift, and the drift would be
    # invisible — the config would accept a lookback the compute module then
    # always refuses.
    #
    # There is deliberately no companion assertion for the fast period. It is
    # imported straight from ``common.constants``, so ``MACRO_FAST_PERIOD ==
    # CONFIG_LAYER_FAST_PERIOD`` compares an object with itself and cannot
    # fail. What actually needs pinning there — that the DTO's label-bearing
    # ``fast_period`` field must equal it — is a ``test_schema`` case.
    assert MACRO_SLOW_PERIOD == MIN_MACRO_TREND_LOOKBACK
    assert MACRO_FAST_PERIOD < MACRO_SLOW_PERIOD


def test_the_fetch_interval_comes_from_the_candle_vocabulary():
    # The adapter echoes the interval back and compares it, so the spelling
    # has to be the vocabulary's own rather than a literal at the call site.
    assert CandleInterval.D1.value == MACRO_CANDLE_INTERVAL
    assert CandleInterval(MACRO_CANDLE_INTERVAL) is CandleInterval.D1


# --------------------------------------------------------------------------
# The two orderings, and the age of the run
# --------------------------------------------------------------------------


def test_a_step_up_puts_the_fast_average_above_and_dates_the_change():
    # 260 bars flat at 100 with a step to 101 at bar 205.
    #
    # Before the step both averages sit on 100 and are EQUAL; from bar 205 on,
    # g(m) > 0 for m in [1, 50], so the fast average is above. The run of
    # "above" is therefore bars 205..259 = 55 bars, and it started on bar 205,
    # which is 2024-01-01 + 205 days = 2024-07-24.
    candles = _stepped(260, 100, {205: "1"})
    macro = compute_macro_trend(candles, as_of_ms=_as_of(candles))

    assert macro is not None
    assert macro.alignment is MacroAlignment.ABOVE
    assert macro.bars_in_state == 55
    assert macro.state_age_capped is False
    assert macro.run_started_date is not None
    assert macro.run_started_date.isoformat() == "2024-07-24"
    assert macro.as_of_date.isoformat() == "2024-09-16"  # bar 259
    assert macro.separation_pct > 0
    assert macro.candle_count == 260
    assert macro.fast_period == MACRO_FAST_PERIOD
    assert macro.slow_period == MACRO_SLOW_PERIOD


def test_a_bar_where_the_averages_are_equal_ends_the_run_rather_than_extending_it():
    # The mutation this exists for: treat an equal bar as CONTINUING the run
    # and this series reports the full 61 bars, capped, with the date dropped
    # — a ten-bar-old alignment read as one six times older, and the one fact
    # the block exists to give up entirely.
    #
    # A LATE step (bar 250), so the equal stretch inside the comparable
    # window is long: bars 199..249 are flat-at-100 on both averages, and the
    # run of "above" is bars 250..259 = 10 against the 61 available. The step
    # is late purely to make the gap between the two readings wide — the
    # earlier-step series above would catch the same mutant (its bars 199..204
    # are equal too), just by 55 against 61 instead of 10 against 61.
    candles = _stepped(260, 100, {250: "1"})
    macro = compute_macro_trend(candles, as_of_ms=_as_of(candles))
    assert macro is not None

    comparable = 260 - MACRO_SLOW_PERIOD + 1  # 61 bars have both averages
    assert macro.bars_in_state == 10
    assert macro.bars_in_state < comparable
    assert macro.state_age_capped is False
    assert macro.run_started_date is not None
    assert macro.run_started_date.isoformat() == "2024-09-07"  # bar 250


def test_a_step_down_puts_the_fast_average_below():
    candles = _stepped(260, 100, {205: "-1"})
    macro = compute_macro_trend(candles, as_of_ms=_as_of(candles))

    assert macro is not None
    assert macro.alignment is MacroAlignment.BELOW
    assert macro.separation_pct < 0
    assert macro.bars_in_state == 55
    assert macro.run_started_date is not None
    assert macro.run_started_date.isoformat() == "2024-07-24"


def test_a_run_reaching_the_oldest_comparable_bar_is_reported_as_capped():
    # A pure ramp: SMA(50)(i) - SMA(200)(i) = slope * 75 at EVERY bar where
    # both exist, so the ordering never changes inside the window and the
    # window cannot say when it started.
    candles = _series([100 + i for i in range(260)])
    macro = compute_macro_trend(candles, as_of_ms=_as_of(candles))

    assert macro is not None
    assert macro.alignment is MacroAlignment.ABOVE
    assert macro.state_age_capped is True
    assert macro.run_started_date is None
    assert macro.bars_in_state == 260 - MACRO_SLOW_PERIOD + 1


def test_a_genuine_flip_with_no_equal_bar_is_dated_to_the_bar_it_flipped_on():
    # Two steps: -1 at bar 210, +1.7 at bar 213. With m1 = i-209 and
    # m2 = i-212 both inside [1, 50], the difference is proportional to
    # -3*m1 + 1.7*3*m2, i.e. 1.7*m2 - m1. At bar 216: 1.7*4 - 7 = -0.2 (below);
    # at bar 217: 1.7*5 - 8 = +0.5 (above). No integer m lands on zero here —
    # 0.7*m2 == 3 has no integer solution — so this flip has no equal bar in
    # it, unlike the case above. The run of "above" is 217..259 = 43 bars.
    candles = _stepped(260, 100, {210: "-1", 213: "1.7"})
    macro = compute_macro_trend(candles, as_of_ms=_as_of(candles))

    assert macro is not None
    assert macro.alignment is MacroAlignment.ABOVE
    assert macro.bars_in_state == 43
    assert macro.run_started_date is not None
    assert macro.run_started_date.isoformat() == "2024-08-05"  # bar 217


def test_exactly_the_floor_of_history_renders_but_can_only_say_one_bar():
    # 200 bars is legal and correct: there is exactly ONE bar with both
    # averages, so the run is one bar long and its start is outside the
    # window. This is the case the config comment warns about.
    candles = _series([100 + i for i in range(MACRO_SLOW_PERIOD)])
    macro = compute_macro_trend(candles, as_of_ms=_as_of(candles))

    assert macro is not None
    assert macro.candle_count == MACRO_SLOW_PERIOD
    assert macro.bars_in_state == 1
    assert macro.state_age_capped is True
    assert macro.run_started_date is None


def test_a_bar_is_dated_by_its_own_day_under_either_close_stamp_convention():
    # Hyperliquid stamps a close as the next open minus a millisecond, so
    # dating a bar by its CLOSE happens to land on the right day today. A
    # venue that stamped the close as the next open exactly — the other
    # convention, and the one a resampled series naturally produces — would
    # push every date in this section forward by a day, silently. Reading the
    # OPEN is right under both, and this is what makes that choice checkable
    # rather than a claim in a docstring.
    exclusive = [
        Candle(
            open_time=c.open_time,
            close_time=c.open_time + _DAY_MS,  # the exclusive-end convention
            open=c.open,
            high=c.high,
            low=c.low,
            close=c.close,
            volume=c.volume,
        )
        for c in _stepped(260, 100, {205: "1"})
    ]
    macro = compute_macro_trend(exclusive, as_of_ms=exclusive[-1].close_time)
    assert macro is not None
    # Bar 259 covers 2024-09-16 whichever way its close is stamped.
    assert macro.as_of_date.isoformat() == "2024-09-16"
    assert macro.run_started_date is not None
    assert macro.run_started_date.isoformat() == "2024-07-24"


# --------------------------------------------------------------------------
# The three refusals — each drops the WHOLE section and says which it was
# --------------------------------------------------------------------------


def test_one_bar_short_of_the_floor_is_refused_naming_both_counts(caplog):
    candles = _series([100 + i for i in range(MACRO_SLOW_PERIOD - 1)])
    with caplog.at_level(logging.WARNING):
        assert compute_macro_trend(candles, as_of_ms=_as_of(candles)) is None
    # Both numbers, because "not enough history" without them cannot tell a
    # newly listed coin from a lookback set too low.
    assert "199" in caplog.text
    assert str(MACRO_SLOW_PERIOD) in caplog.text


def test_an_empty_series_is_refused_the_same_way(caplog):
    # A sequence at all means the switch is ON, so this is a refusal that has
    # to log — unlike ``None``, which the builder turns into silence.
    with caplog.at_level(logging.WARNING):
        assert compute_macro_trend([], as_of_ms=_DAY0_MS) is None
    assert "only 0 are available" in caplog.text


@pytest.mark.parametrize(
    ("lag_ms", "expected"),
    [
        (0, True),  # the daily bar closed on the same instant as the 4h one
        (20 * 60 * 60_000, True),  # mid-day 4h bar, yesterday's daily bar
        (MAX_DAILY_CANDLE_AGE_MS, True),  # a 00:00 4h bar, daily not published yet
        (MAX_DAILY_CANDLE_AGE_MS + 1, False),  # one whole daily bar is missing
        (-1, False),  # the daily bar closes AFTER the context's own bar
    ],
)
def test_the_daily_feed_is_admitted_up_to_one_day_behind_and_never_ahead(lag_ms, expected):
    # The ``<=`` on the upper bound is load-bearing and is why the exactly-24h
    # case is in this table: a 4h bar closing at 00:00 UTC while the daily bar
    # covering the day just ended has not been published is exactly that far
    # behind, and refusing it would drop the section on a healthy feed once
    # every day.
    candles = _stepped(260, 100, {205: "1"})
    macro = compute_macro_trend(candles, as_of_ms=_as_of(candles) + lag_ms)
    assert (macro is not None) is expected


def test_a_stale_daily_feed_is_refused_in_hours_and_blames_the_daily_feed(caplog):
    # Two days behind. An operator reads this line every cycle while the feed
    # is down, so it says the date and the lag in hours rather than handing
    # over two 13-digit epoch stamps to subtract.
    candles = _stepped(260, 100, {205: "1"})
    as_of = _as_of(candles) + 2 * MAX_DAILY_CANDLE_AGE_MS
    with caplog.at_level(logging.WARNING):
        assert compute_macro_trend(candles, as_of_ms=as_of) is None
    assert "2024-09-16" in caplog.text  # the newest bar's own date
    assert "48.0h" in caplog.text
    assert "stopped publishing" in caplog.text
    assert str(as_of) not in caplog.text  # no raw epoch stamps


def test_the_gap_is_reported_at_a_scale_that_cannot_contradict_the_sentence(caplog):
    # The FIRST illegal lag — one millisecond past the bound, the value the
    # boundary table above pins as refused. At one decimal place of hours it
    # read "closed 24.0h before this context's as-of, past the 24h a healthy
    # daily feed stays within": a figure equal to the limit in a sentence
    # saying the limit was exceeded. The mirror case, a bar 1 ms AHEAD, read
    # "0.0h AFTER" — no gap at all, in a sentence about a gap.
    candles = _stepped(260, 100, {205: "1"})
    first_illegal = _as_of(candles) + MAX_DAILY_CANDLE_AGE_MS + 1
    with caplog.at_level(logging.WARNING):
        assert compute_macro_trend(candles, as_of_ms=first_illegal) is None
    assert "1 ms past the 24h" in caplog.text

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        assert compute_macro_trend(candles, as_of_ms=_as_of(candles) - 1) is None
    assert "closes 1 ms AFTER" in caplog.text


def test_a_daily_bar_ahead_of_the_context_blames_the_short_series_not_the_daily_feed(caplog):
    # The mirror fault, and the one an earlier message got backwards:
    # ``as_of_ms`` is the newest CLOSED bar of the 4h series, not a clock, so
    # a daily bar sitting ahead of it means THAT series is lagging. Pointing
    # the operator at the daily feed would send them to inspect the healthy
    # one.
    candles = _stepped(260, 100, {205: "1"})
    with caplog.at_level(logging.WARNING):
        assert compute_macro_trend(candles, as_of_ms=_as_of(candles) - 3_600_000) is None
    assert "1.0h AFTER" in caplog.text
    assert "shorter series" in caplog.text
    assert "stopped publishing" not in caplog.text


def test_a_series_handed_over_newest_first_is_refused_rather_than_averaged():
    # Nothing here sorts, and a reversed series would average the same 200
    # prices while dating itself to the OLDEST bar. It lands in the freshness
    # refusal instead, which is the point of checking the last element.
    candles = _stepped(260, 100, {205: "1"})
    assert compute_macro_trend(list(reversed(candles)), as_of_ms=_as_of(candles)) is None


def test_two_exactly_equal_averages_are_refused_rather_than_given_an_ordering(caplog):
    # A flat price: both averages sit on it, so there is no ordering. The
    # alternative — picking one — would print a confident alignment for a
    # market that produced none.
    candles = _series([100] * 260)
    with caplog.at_level(logging.WARNING):
        assert compute_macro_trend(candles, as_of_ms=_as_of(candles)) is None
    assert "exactly equal" in caplog.text


# --------------------------------------------------------------------------
# The numbers themselves
# --------------------------------------------------------------------------


def test_the_two_percentages_are_of_the_slow_average_and_signed_from_it():
    # 260 bars flat at 100 with a step to 110 at bar 210, read at bar 259:
    # m = 50 exactly, so the fast average is wholly at 110 and the slow one
    # holds 50 bars of 110 and 150 of 100 -> 102.5. Separation is
    # (110 - 102.5) / 102.5 * 100, and the latest close is 110.
    candles = _stepped(260, 100, {210: "10"})
    macro = compute_macro_trend(candles, as_of_ms=_as_of(candles))

    assert macro is not None
    assert macro.sma_fast == Decimal(110)
    assert macro.sma_slow == Decimal("102.5")
    assert macro.latest_close == Decimal(110)
    expected = float(Decimal("7.5") / Decimal("102.5") * 100)
    assert macro.separation_pct == pytest.approx(expected)
    # The close sits at the fast average here, so the two percentages coincide
    # — which is exactly why the case is useful: they are computed from
    # different numerators and must still agree when those numerators are
    # equal.
    assert macro.close_vs_slow_pct == pytest.approx(expected)


def test_the_averages_are_exact_decimals_never_routed_through_float():
    # Prices carrying more significant digits than a float holds: summed as
    # floats, the averages would come back shortened. Asserted two ways — the
    # value equals the hand-computed Decimal quotient exactly, AND it carries
    # more digits than float could have produced.
    base = Decimal("60000.123456789012345678")
    candles = _series([base + Decimal("0.000000000000000001") * i for i in range(260)])
    macro = compute_macro_trend(candles, as_of_ms=_as_of(candles))
    assert macro is not None

    closes = [c.close for c in candles]
    assert macro.sma_slow == sum(closes[-MACRO_SLOW_PERIOD:], Decimal(0)) / MACRO_SLOW_PERIOD
    assert macro.sma_fast == sum(closes[-MACRO_FAST_PERIOD:], Decimal(0)) / MACRO_FAST_PERIOD
    assert len(macro.sma_slow.as_tuple().digits) > 17
