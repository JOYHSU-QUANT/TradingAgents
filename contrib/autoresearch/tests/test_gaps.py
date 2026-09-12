"""What the gap scan says about a series, and what it refuses to say."""

from __future__ import annotations

from decimal import Decimal

import pytest

from contrib.autoresearch.constants import FUNDING_INTERVAL_MS, FUNDING_STAMP_TOLERANCE_MS
from contrib.autoresearch.gaps import (
    Gap,
    GapReport,
    render_report,
    scan_candles,
    scan_funding,
)
from contrib.autoresearch.upstream import Candle, FundingPoint, interval_to_ms

from .conftest import ANCHOR_MS, MS_PER_HOUR, bars, funding_points

STEP_4H = interval_to_ms("4h")


def test_an_empty_series_says_so_rather_than_reporting_a_clean_grid(store):
    report = scan_candles(store, coin="BTC", interval="4h")
    assert report.rows == 0
    assert report.first_ms is None
    assert render_report(report) == ["BTC 4h candles: no rows stored"]


def test_a_complete_series_is_complete_and_still_gets_a_line(store):
    store.upsert_candles("BTC", "4h", bars(50))
    report = scan_candles(store, coin="BTC", interval="4h")
    assert report.complete
    assert report.rows == 50
    assert report.missing_rows == 0
    lines = render_report(report)
    assert len(lines) == 1
    assert "no gaps" in lines[0]


def test_one_hole_is_reported_with_the_bars_it_is_missing(store):
    store.upsert_candles("BTC", "4h", bars(20, skip={7, 8, 9}))
    report = scan_candles(store, coin="BTC", interval="4h")
    assert not report.complete
    assert len(report.gaps) == 1
    gap = report.gaps[0]
    assert gap.missing == 3
    assert gap.after_ms == ANCHOR_MS + 6 * STEP_4H
    assert gap.before_ms == ANCHOR_MS + 10 * STEP_4H
    assert report.missing_rows == 3


def test_two_holes_are_two_findings_not_one_span(store):
    store.upsert_candles("BTC", "4h", bars(30, skip={5, 20, 21}))
    report = scan_candles(store, coin="BTC", interval="4h")
    assert [gap.missing for gap in report.gaps] == [1, 2]
    assert report.missing_rows == 3


def test_the_grid_is_anchored_on_the_first_row_not_on_the_epoch(store):
    """A venue whose bars sit off the epoch grid is regular, not broken.

    Anchored on the epoch, every row of such a series would be reported as
    off-grid — a report that flags everything answers nothing.
    """
    offset = ANCHOR_MS + 7 * 60_000
    store.upsert_candles("BTC", "4h", bars(10, start_ms=offset))
    assert scan_candles(store, coin="BTC", interval="4h").complete


def _stray(at_ms):
    """A well-formed bar parked at ``at_ms``, for the off-grid cases."""
    return Candle(
        open_time=at_ms,
        close_time=at_ms + STEP_4H,
        open=Decimal("1"),
        high=Decimal("1"),
        low=Decimal("1"),
        close=Decimal("1"),
        volume=Decimal("1"),
    )


def test_a_stamp_off_the_grid_does_not_invent_a_gap_in_an_intact_series(store):
    """Two different defects with two different remedies, counted separately.

    A hole is re-fetchable; a stamp that is not on the grid is not, and
    letting one inflate the other's count would send an operator re-fetching
    a window that already holds everything the venue ever published. The
    stray sits more than one step from its predecessor, so a scan judging
    gaps by raw width alone would call that span a gap on top of calling the
    stamp off-grid; a stray closer than one step would leave both readings
    identical and prove nothing.
    """
    series = bars(8)
    stray = _stray(series[2].open_time + 137)
    store.upsert_candles("BTC", "4h", [*series, stray])
    report = scan_candles(store, coin="BTC", interval="4h")
    assert report.misaligned_ms == (stray.open_time,)
    assert report.gaps == ()
    assert report.missing_rows == 0
    assert not report.complete


def test_a_stamp_off_the_grid_does_not_fill_the_slot_it_sits_in(store):
    """The mirror case: a stray must not HIDE the hole it landed in either.

    Slots 3 and 4 have no bar. A scan comparing raw consecutive stamps would
    see two short hops either side of the stray and report a complete series,
    which is the more dangerous of the two mistakes: the evaluator would read
    across a two-bar hole as if it were one step.
    """
    series = bars(8, skip={3, 4})
    stray = _stray(series[2].open_time + 2 * STEP_4H + 137)
    store.upsert_candles("BTC", "4h", [*series, stray])
    report = scan_candles(store, coin="BTC", interval="4h")
    assert report.misaligned_ms == (stray.open_time,)
    assert [gap.missing for gap in report.gaps] == [2]
    assert report.gaps[0].after_ms == series[2].open_time
    assert report.gaps[0].before_ms == series[3].open_time  # index 3 of the SKIPPED list


def test_two_stamps_in_one_slot_are_named_rather_than_counted_as_coverage(store):
    """Two settlements inside one hour double-count that hour's carry.

    The row count looks HEALTHIER for the duplicate, so nothing else in the
    report would notice it.
    """
    points = funding_points(10)
    twin = FundingPoint(time=points[4].time + 900, rate=Decimal("0.00009"))
    store.upsert_funding("BTC", [*points, twin])
    report = scan_funding(store, coin="BTC")
    assert report.duplicate_ms == (twin.time,)
    assert report.gaps == ()
    assert not report.complete
    assert any("duplicate slot" in line for line in render_report(report))


def test_the_jitter_a_real_venue_stamps_funding_with_is_not_a_finding(store):
    """Measured against mainnet on 2026-09-11: settlements post tens of ms late.

    Judged on an exact grid, 524 of 531 real settlements read as off-grid.
    A report that flags everything answers nothing, so the hourly slot carries
    a tolerance — and this is the series that tolerance exists for.
    """
    jitter = [2, 57, 30, 20, 58, 50, 12, 99, 40, 47, 51, 3]
    points = [
        FundingPoint(time=ANCHOR_MS + i * MS_PER_HOUR + offset, rate=Decimal("0.00001"))
        for i, offset in enumerate(jitter)
    ]
    store.upsert_funding("BTC", points)
    report = scan_funding(store, coin="BTC")
    assert report.complete, render_report(report)
    assert report.rows == len(jitter)


def test_a_stamp_past_the_tolerance_is_still_named(store):
    """The tolerance absorbs posting latency, not a stamp in the wrong slot."""
    late = FUNDING_STAMP_TOLERANCE_MS + 1
    points = [
        FundingPoint(time=ANCHOR_MS, rate=Decimal("0.00001")),
        FundingPoint(time=ANCHOR_MS + MS_PER_HOUR + late, rate=Decimal("0.00001")),
        FundingPoint(time=ANCHOR_MS + 2 * MS_PER_HOUR, rate=Decimal("0.00001")),
    ]
    store.upsert_funding("BTC", points)
    report = scan_funding(store, coin="BTC")
    assert report.misaligned_ms == (points[1].time,)


def test_funding_is_scanned_on_the_hourly_settlement_grid(store):
    store.upsert_funding("BTC", funding_points(48))
    report = scan_funding(store, coin="BTC")
    assert report.step_ms == FUNDING_INTERVAL_MS == MS_PER_HOUR
    assert report.complete
    assert report.rows == 48


def test_a_missing_funding_hour_is_a_gap(store):
    points = [p for i, p in enumerate(funding_points(12)) if i != 4]
    store.upsert_funding("BTC", points)
    report = scan_funding(store, coin="BTC")
    assert [gap.missing for gap in report.gaps] == [1]
    assert report.gaps[0].after_ms == ANCHOR_MS + 3 * MS_PER_HOUR


def test_a_wall_of_holes_is_summarised_rather_than_listed_in_full(store):
    """A store whose backfill never ran must not bury its own first line."""
    store.upsert_candles("BTC", "4h", bars(120, skip=set(range(1, 120, 2))))
    lines = render_report(scan_candles(store, coin="BTC", interval="4h"))
    assert sum(1 for line in lines if line.strip().startswith("gap:")) == 10
    assert any("more gap(s)" in line for line in lines)
    assert "row(s) missing" in lines[0]


def test_the_rendered_span_names_the_instants_not_the_raw_stamps(store):
    store.upsert_candles("BTC", "4h", bars(3))
    head = render_report(scan_candles(store, coin="BTC", interval="4h"))[0]
    assert str(ANCHOR_MS) not in head
    assert "+00:00" in head


def test_scanning_one_series_ignores_the_other(store):
    store.upsert_candles("BTC", "4h", bars(10, skip={4}))
    store.upsert_candles("BTC", "1d", bars(10, interval="1d"))
    store.upsert_funding("BTC", funding_points(10))
    assert not scan_candles(store, coin="BTC", interval="4h").complete
    assert scan_candles(store, coin="BTC", interval="1d").complete
    assert scan_funding(store, coin="BTC").complete


def test_a_single_row_series_is_complete_and_names_one_instant(store):
    store.upsert_funding("BTC", [FundingPoint(time=ANCHOR_MS, rate=Decimal("0.00001"))])
    report = scan_funding(store, coin="BTC")
    assert report.complete
    assert report.first_ms == report.last_ms == ANCHOR_MS


def test_a_settlement_stamped_slightly_EARLY_lands_in_the_hour_it_belongs_to(store):
    """Nearest-slot, not floor division - and only an early stamp can show it.

    Floor division and rounding agree on every stamp that is LATE by less than
    half a step, which is every other fixture in this file and every real
    reading behind the tolerance constant. They disagree the moment a stamp
    arrives early: two seconds before the hour is two seconds from slot 1 and
    fifty-nine minutes from slot 0, so rounding files it in slot 1 and finds a
    complete series, while floor division files it in slot 0 - where it is far
    outside the tolerance, so it is reported off-grid AND leaves slot 1 empty,
    turning one punctual settlement into two findings.
    """
    early = FundingPoint(time=ANCHOR_MS + MS_PER_HOUR - 2_000, rate=Decimal("0.00001"))
    store.upsert_funding(
        "BTC",
        [
            FundingPoint(time=ANCHOR_MS, rate=Decimal("0.00001")),
            early,
            FundingPoint(time=ANCHOR_MS + 2 * MS_PER_HOUR, rate=Decimal("0.00001")),
        ],
    )
    report = scan_funding(store, coin="BTC")
    assert report.complete, render_report(report)
    assert report.rows == 3


def test_a_wall_of_off_grid_stamps_is_summarised_too(store):
    """The shared list renderer, which only the gap list exercised."""
    strays = [
        FundingPoint(time=ANCHOR_MS + i * MS_PER_HOUR + 40 * 60_000, rate=Decimal("0.00001"))
        for i in range(14)
    ]
    store.upsert_funding("BTC", [FundingPoint(time=ANCHOR_MS, rate=Decimal("0.00001")), *strays])
    lines = render_report(scan_funding(store, coin="BTC"))
    listed = [line for line in lines if line.strip().startswith("off-grid:")]
    assert len(listed) == 10
    assert any(line.strip() == "... and 4 more off-grid stamp(s)" for line in lines)


def test_the_summary_line_counts_what_it_left_out(store):
    """The arithmetic of "... and N more", not merely that the phrase appears."""
    store.upsert_candles("BTC", "4h", bars(40, skip=set(range(1, 40, 2))))
    report = scan_candles(store, coin="BTC", interval="4h")
    lines = render_report(report)
    assert any(line.strip() == f"... and {len(report.gaps) - 10} more gap(s)" for line in lines)


@pytest.mark.parametrize(
    ("rows", "first_ms", "last_ms", "gaps"),
    [
        (5, None, None, ()),          # claims rows, carries no span
        (0, ANCHOR_MS, ANCHOR_MS, ()),  # claims a span, says it has no rows
        (0, None, None, (Gap(after_ms=1, before_ms=3, missing=1),)),  # findings, no rows
    ],
)
def test_a_report_that_contradicts_itself_is_refused_where_it_is_built(
    rows, first_ms, last_ms, gaps
):
    """A report is read as authoritative, so it may not disagree with itself.

    Only ``scan_stamps`` builds these today and it always builds them consistently,
    which is exactly why the coupling needs saying out loud: the type is
    exported, and the first hand-built one would otherwise fail somewhere
    downstream - rendering a span it does not have - rather than here.
    """
    with pytest.raises(ValueError, match="autoresearch|inconsistent|findings"):
        GapReport(
            label="autoresearch test",
            step_ms=STEP_4H,
            tolerance_ms=0,
            rows=rows,
            first_ms=first_ms,
            last_ms=last_ms,
            gaps=gaps,
            duplicate_ms=(),
            misaligned_ms=(),
        )
