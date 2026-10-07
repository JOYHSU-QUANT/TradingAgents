"""The rule over a synthetic series: when it is in, what it collects, and the summary lines."""

from __future__ import annotations

from decimal import Decimal

from contrib.carry.history import Summary, floor_day, format_summary, replay
from contrib.carry.signal import Action, Params, Side

from .conftest import COIN, DAY0, MS_PER_HOUR, alternating, day, hourly, hump_series

PARAMS = Params()


def test_a_series_without_a_hump_never_enters():
    rows, summary = replay(COIN, hourly(DAY0, alternating(90)), PARAMS)
    # The first boundary with a window behind it is day 30; the last settlement is day 89
    # 23:00, so day 88 is the last boundary whose following day is fully settled.
    assert summary.first_boundary_ms == day(30)
    assert summary.last_boundary_ms == day(88)
    assert summary.days == 59
    assert summary.days_without_z == 0
    assert summary.entries == summary.exits == summary.days_in == 0
    assert summary.collected == 0
    assert summary.annualized_while_in is None
    assert all(row.action is Action.STAY_OUT for row in rows)


def test_a_hump_enters_once_and_exits_after_the_minimum_hold():
    points = hump_series()
    rows, summary = replay(COIN, points, PARAMS)
    entered = [r for r in rows if r.action is Action.ENTER]
    exited = [r for r in rows if r.action is Action.EXIT]
    # Day 40 is the first hump day; the boundary that first SEES it as current is day 41.
    assert [r.boundary_ms for r in entered] == [day(41)]
    # The hump ends before day 55; day 55's base settlements are current at day 56's boundary,
    # against a window whose mean the hump has lifted — z goes negative, and the hold is past.
    assert [r.boundary_ms for r in exited] == [day(56)]
    assert summary.entries == summary.exits == 1
    assert summary.days_in == 15
    assert summary.longest_hold_days == 15
    assert summary.hours_in == 15 * 24
    expected = sum((p.rate for p in points if day(41) < p.time <= day(56)), Decimal(0))
    assert summary.collected == expected
    assert summary.annualized_while_in is not None and summary.annualized_over_span is not None
    assert summary.annualized_while_in > summary.annualized_over_span > 0
    assert all(r.collected == 0 and r.settlements == 0 for r in rows if r.position.side is Side.OUT)
    assert all(r.settlements == 24 for r in rows if r.position.side is Side.IN)


def test_the_minimum_hold_delays_an_exit_the_rule_would_take():
    short_hump = hump_series(hump=(40, 41))
    _, prompt = replay(COIN, short_hump, Params(min_hold_days=0))
    _, held = replay(COIN, short_hump, Params(min_hold_days=5))
    assert prompt.entries == held.entries == 1
    assert prompt.longest_hold_days < 5
    assert held.longest_hold_days == 5


def test_since_and_until_narrow_the_span_to_boundaries():
    points = hump_series()
    _, summary = replay(COIN, points, PARAMS, since_ms=day(45) + 1, until_ms=day(70) + 5)
    assert summary.first_boundary_ms == day(46)
    assert summary.last_boundary_ms == day(70)
    assert summary.days == 25
    # Starting inside the hump: out at first, and the hump is already in the window.
    assert summary.entries == 1
    assert summary.exits == 1


def test_an_empty_series_or_one_shorter_than_the_window_has_no_boundary():
    _, empty = replay(COIN, [], PARAMS)
    assert empty.days == 0 and empty.first_boundary_ms is None
    _, short = replay(COIN, hourly(DAY0, alternating(10)), PARAMS)
    assert short.days == 0
    assert format_summary(short, PARAMS) == [
        "carry history: ETH: no boundary with a full 30d window"
    ]


def test_a_gap_leaves_its_boundaries_undecided_as_live_would():
    points = [p for p in hump_series() if not (day(44) - 20 * MS_PER_HOUR < p.time < day(46))]
    rows, summary = replay(COIN, points, PARAMS)
    stale = [r for r in rows if r.stale]
    # The gap starts after day 43 04:00: the boundaries of days 44, 45 and 46 see that
    # settlement 20, 44 and 68 hours old, all over the 3-hour limit, and carry the position
    # instead of deciding. At 48 hours only day 46's is still too old.
    assert [r.boundary_ms for r in stale] == [day(44), day(45), day(46)]
    assert all(r.action is Action.HOLD and r.position.side is Side.IN for r in stale)
    assert summary.days_stale == 3
    _, lenient = replay(COIN, points, PARAMS, max_reading_age_hours=48)
    assert lenient.days_stale == 1


def test_floor_day_is_the_utc_midnight_at_or_before():
    assert floor_day(day(3)) == day(3)
    assert floor_day(day(3) + 1) == day(3)
    assert floor_day(day(4) - 1) == day(3)


def test_the_summary_lines():
    summary = Summary(
        coin="ETH",
        first_boundary_ms=day(30),
        last_boundary_ms=day(89),
        days=60,
        days_without_z=2,
        days_stale=1,
        max_reading_age_hours=3,
        days_in=15,
        entries=1,
        exits=1,
        longest_hold_days=15,
        hours_in=360,
        collected=Decimal("0.0144"),
    )
    assert format_summary(summary, PARAMS) == [
        "carry history: ETH, 60 boundaries from 2026-01-31T00:00:00+00:00 to "
        "2026-03-31T00:00:00+00:00 (window 30d, z in 1.5 / out 0.5, min hold 3d)",
        "  boundaries without a z-score: 2; with a reading older than 3h, left undecided as "
        "live would: 1",
        "  in market: 15 days (25.00%); entries 1, exits 1; longest hold 15 days",
        "  collected while in: 1.44% of notional over 360 settlements; 35.04% annualized "
        "while in, 8.76% annualized over the span",
    ]
