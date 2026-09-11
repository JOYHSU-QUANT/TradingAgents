"""The two walks: do they land every row, exactly once, and stop for a stated reason?"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from contrib.autoresearch import fetch as fetch_module
from contrib.autoresearch.fetch import (
    StopReason,
    backfill_candles,
    backfill_funding,
    render_fetch,
)
from contrib.autoresearch.gaps import scan_candles, scan_funding
from contrib.autoresearch.store import ResearchStore
from contrib.autoresearch.upstream import (
    ExchangeError,
    ExchangeThrottledError,
    epoch_ms,
    from_epoch_ms,
    interval_to_ms,
)

from .conftest import ANCHOR_MS, MS_PER_HOUR, ScriptedMarket, bars, funding_points, market_at

STEP_4H = interval_to_ms("4h")


def _since(stamp_ms: int) -> datetime:
    return from_epoch_ms(stamp_ms)


# -- the backward candle walk ---------------------------------------------


def test_a_history_longer_than_one_page_lands_whole_and_ungapped(store):
    """The paging arithmetic, judged by the gap scan rather than by a row count.

    A count alone passes for a walk that stored the same page many times and
    skipped a bar at every boundary; only the scan can tell those apart.
    """
    series = bars(250)
    market = market_at(series[-1].close_time, candles={("BTC", "4h"): series})
    result = backfill_candles(
        market,
        store,
        coin="BTC",
        interval="4h",
        since=_since(ANCHOR_MS),
        end=market.clock,
        page_bars=40,
    )
    assert result.pages > 1, "the test's page size did not force paging"
    assert result.stopped is StopReason.REACHED_SINCE
    assert scan_candles(store, coin="BTC", interval="4h").complete
    assert [c.open_time for c in store.iter_candles("BTC", "4h")] == [c.open_time for c in series]


def test_consecutive_pages_neither_repeat_a_bar_nor_skip_the_one_between_them(store):
    """The boundary claim, made about the REQUESTS rather than about the result.

    Each window ends at the previous page's oldest open. A walk stepping one
    bar too far leaves a hole; one stepping too little serves the same bar
    twice. Both are invisible in a total, so the windows themselves are
    asserted to descend by exactly one page.
    """
    series = bars(100)
    market = market_at(series[-1].close_time, candles={("BTC", "4h"): series})
    backfill_candles(
        market,
        store,
        coin="BTC",
        interval="4h",
        since=_since(ANCHOR_MS),
        end=market.clock,
        page_bars=10,
    )
    ends = [end_ms for _coin, _interval, _lookback, end_ms in market.candle_calls]
    assert len(set(ends)) == len(ends), "a window was requested twice"
    # The first window ends at the venue's clock, which sits wherever the venue
    # happens to be between bars; every window after it ends at its
    # predecessor's oldest open, which is exactly one page lower.
    assert ends[0] == epoch_ms(market.clock, what="test")
    steps = [a - b for a, b in zip(ends[1:], ends[2:], strict=False)]
    assert steps == [10 * STEP_4H] * len(steps), ends


def test_bars_older_than_since_are_not_stored_even_when_the_venue_serves_them(store):
    series = bars(60)
    market = market_at(series[-1].close_time, candles={("BTC", "4h"): series})
    cutoff = series[20].open_time
    backfill_candles(
        market,
        store,
        coin="BTC",
        interval="4h",
        since=_since(cutoff),
        end=market.clock,
        page_bars=100,
    )
    stored = [c.open_time for c in store.iter_candles("BTC", "4h")]
    assert min(stored) == cutoff
    assert len(stored) == 40


def test_a_backfill_that_starts_before_the_coin_listed_stops_saying_so(store):
    series = bars(30)
    market = market_at(series[-1].close_time, candles={("BTC", "4h"): series})
    result = backfill_candles(
        market,
        store,
        coin="BTC",
        interval="4h",
        since=_since(ANCHOR_MS - 400 * STEP_4H),
        end=market.clock,
        page_bars=10,
    )
    assert result.stopped is StopReason.VENUE_EXHAUSTED
    assert result.rows_after == 30


def test_a_second_identical_run_writes_rows_and_adds_none(store):
    """The success case a rows-written count alone would misreport as work."""
    series = bars(40)
    market = market_at(series[-1].close_time, candles={("BTC", "4h"): series})
    window = {
        "coin": "BTC",
        "interval": "4h",
        "since": _since(ANCHOR_MS),
        "end": market.clock,
    }
    first = backfill_candles(market, store, page_bars=15, **window)
    second = backfill_candles(market, store, page_bars=15, **window)
    assert first.rows_added == 40
    assert second.rows_written > 0
    assert second.rows_added == 0
    assert second.rows_after == 40


def test_a_venue_that_ignores_the_window_is_stopped_rather_than_asked_forever(store):
    """A page that never gets older is a stall, and must be reported as one."""

    class StuckMarket(ScriptedMarket):
        def get_candles(self, coin, interval, lookback, *, end):
            super().get_candles(coin, interval, lookback, end=end)
            return self.candles[(coin, interval)]

    series = bars(20)
    market = StuckMarket(
        clock=from_epoch_ms(series[-1].close_time) + timedelta(seconds=1),
        candles={("BTC", "4h"): series},
    )
    result = backfill_candles(
        market,
        store,
        coin="BTC",
        interval="4h",
        since=_since(ANCHOR_MS - 100 * STEP_4H),
        end=market.clock,
        page_bars=5,
    )
    assert result.stopped is StopReason.NO_PROGRESS
    assert len(market.candle_calls) == 2


def test_a_walk_that_never_finishes_stops_at_the_request_limit(store, monkeypatch):
    """The defect bound, tested by lowering it rather than by scripting 2000 pages."""
    monkeypatch.setattr(fetch_module, "MAX_PAGES", 3)
    series = bars(200)
    market = market_at(series[-1].close_time, candles={("BTC", "4h"): series})
    result = backfill_candles(
        market,
        store,
        coin="BTC",
        interval="4h",
        since=_since(ANCHOR_MS),
        end=market.clock,
        page_bars=5,
    )
    assert result.stopped is StopReason.PAGE_LIMIT
    assert result.pages == 3


# -- the forward funding walk ---------------------------------------------


def test_funding_lands_whole_when_the_venue_answers_in_full(store):
    points = funding_points(24 * 45)
    market = market_at(points[-1].time, funding={"BTC": points})
    result = backfill_funding(
        market, store, coin="BTC", since=_since(ANCHOR_MS), end=market.clock, page_days=20
    )
    assert result.stopped is StopReason.REACHED_END
    assert scan_funding(store, coin="BTC").complete
    assert store.count_funding("BTC") == len(points)


def test_a_truncated_funding_page_costs_a_request_and_loses_no_tail(store):
    """Why the funding walk runs forwards.

    The endpoint is anchored on its start, so a short response is missing its
    NEWEST records. Walked backwards that tail would be stepped over; walked
    forwards the next request resumes from the newest record RECEIVED and
    picks it up.
    """
    points = funding_points(24 * 30)
    market = market_at(points[-1].time, funding={"BTC": points}, funding_cap=7)
    result = backfill_funding(
        market, store, coin="BTC", since=_since(ANCHOR_MS), end=market.clock, page_days=20
    )
    assert result.stopped is StopReason.REACHED_END
    assert store.count_funding("BTC") == len(points)
    assert scan_funding(store, coin="BTC").complete
    # One request per seven records it was allowed to serve, give or take the
    # last partial window — far more than the two an untruncated walk needs.
    assert len(market.funding_calls) > len(points) // 7


def test_a_stretch_the_venue_has_no_funding_for_is_stepped_over(store):
    later = ANCHOR_MS + 200 * 24 * MS_PER_HOUR
    points = funding_points(48, start_ms=later)
    market = market_at(points[-1].time, funding={"BTC": points})
    result = backfill_funding(
        market, store, coin="BTC", since=_since(ANCHOR_MS), end=market.clock, page_days=20
    )
    assert result.stopped is StopReason.REACHED_END
    assert store.count_funding("BTC") == 48


def test_funding_outside_the_window_is_not_stored(store):
    points = funding_points(24 * 10)
    market = market_at(points[-1].time, funding={"BTC": points})
    cutoff = points[100].time
    backfill_funding(
        market, store, coin="BTC", since=_since(cutoff), end=market.clock, page_days=20
    )
    assert min(p.time for p in store.iter_funding("BTC")) == cutoff


# -- window validation, shared by both walks ------------------------------


@pytest.mark.parametrize("walk", ["candles", "funding"])
def test_a_window_that_runs_backwards_is_refused_by_name(store, walk):
    market = market_at(ANCHOR_MS, candles={("BTC", "4h"): bars(5)}, funding={"BTC": []})
    end = from_epoch_ms(ANCHOR_MS)
    with pytest.raises(ValueError) as caught:
        if walk == "candles":
            backfill_candles(
                market, store, coin="BTC", interval="4h", since=end + timedelta(days=1), end=end
            )
        else:
            backfill_funding(market, store, coin="BTC", since=end + timedelta(days=1), end=end)
    assert "--since" in str(caught.value)


def test_a_naive_since_is_refused_rather_than_read_in_the_host_zone(store):
    market = market_at(ANCHOR_MS, candles={("BTC", "4h"): bars(5)})
    with pytest.raises(ValueError):
        backfill_candles(
            market,
            store,
            coin="BTC",
            interval="4h",
            since=datetime(2023, 1, 1),
            end=market.clock,
        )


def test_the_rendered_line_says_both_what_was_written_and_what_was_new(store):
    series = bars(10)
    market = market_at(series[-1].close_time, candles={("BTC", "4h"): series})
    result = backfill_candles(
        market, store, coin="BTC", interval="4h", since=_since(ANCHOR_MS), end=market.clock
    )
    line = render_fetch(result)
    assert "10 row(s) written" in line
    assert "10 new" in line
    assert StopReason.REACHED_SINCE.value in line


# -- waiting out a venue throttle -----------------------------------------


class _ThrottlingMarket(ScriptedMarket):
    """Serves the scripted history, but sheds whichever requests ``shed_when`` picks.

    The predicate is given the 1-based index of the request being made,
    refusals included, so a test can say "shed the first two", "shed
    everything after the first", or "shed everything" without arithmetic —
    and the three say very different things about what the walk must do.
    """

    shed_when = staticmethod(lambda seen: False)
    seen = 0
    refusals = 0

    def get_candles(self, coin, interval, lookback, *, end):
        self.seen += 1
        if self.shed_when(self.seen):
            self.refusals += 1
            raise ExchangeThrottledError("Hyperliquid request failed: ClientError: (429, ...)")
        return super().get_candles(coin, interval, lookback, end=end)


def _throttler(series, *, shed_when):
    market = _ThrottlingMarket(
        clock=from_epoch_ms(series[-1].close_time) + timedelta(seconds=1),
        candles={("BTC", "4h"): series},
    )
    market.shed_when = shed_when
    return market


def test_a_shed_request_is_waited_out_rather_than_ending_the_backfill(store):
    """Measured on mainnet: ~44 rapid reads earn a 429, well inside one backfill."""
    series = bars(30)
    market = _throttler(series, shed_when=lambda seen: seen <= 2)
    waits = []
    result = backfill_candles(
        market,
        store,
        coin="BTC",
        interval="4h",
        since=_since(ANCHOR_MS),
        end=market.clock,
        sleep=waits.append,
    )
    assert result.stopped is StopReason.REACHED_SINCE
    assert store.count_candles("BTC", "4h") == 30
    assert waits == list(fetch_module.THROTTLE_BACKOFF_SECONDS[:2])


def test_the_waits_rise_so_a_venue_that_is_down_is_reported_not_retried_all_evening(store):
    """A throttle that outlasts every wait propagates, carrying the venue's words."""
    series = bars(30)
    market = _throttler(series, shed_when=lambda seen: True)
    waits = []
    with pytest.raises(ExchangeThrottledError) as caught:
        backfill_candles(
            market,
            store,
            coin="BTC",
            interval="4h",
            since=_since(ANCHOR_MS),
            end=market.clock,
            sleep=waits.append,
        )
    assert "429" in str(caught.value)
    assert waits == list(fetch_module.THROTTLE_BACKOFF_SECONDS)
    assert market.refusals == len(fetch_module.THROTTLE_BACKOFF_SECONDS) + 1


def test_a_throttle_partway_through_keeps_the_pages_already_written(store):
    """The walk is built out of upserts precisely so a half-done backfill is resumable.

    The venue serves one page and then sheds everything, so the walk dies with
    a page banked. Those rows must survive: without them a backfill that met a
    throttle on its fifth page would have to start from the venue's clock
    again, and meet the same wall in the same place.
    """
    series = bars(60)
    market = _throttler(series, shed_when=lambda seen: seen > 1)
    with pytest.raises(ExchangeThrottledError):
        backfill_candles(
            market,
            store,
            coin="BTC",
            interval="4h",
            since=_since(ANCHOR_MS),
            end=market.clock,
            page_bars=20,
            sleep=lambda _seconds: None,
        )
    # Exactly the one page the venue served, and it is the NEWEST twenty bars.
    stored = [c.open_time for c in store.iter_candles("BTC", "4h")]
    assert stored == [c.open_time for c in series[-20:]]


def test_a_failure_that_is_not_a_throttle_is_not_waited_on(store):
    """Retrying a malformed response would turn one clear failure into five slow ones."""

    class BrokenMarket(ScriptedMarket):
        def get_candles(self, coin, interval, lookback, *, end):
            raise ExchangeError("Hyperliquid request failed: malformed candleSnapshot")

    market = BrokenMarket(clock=from_epoch_ms(ANCHOR_MS + 10_000_000))
    waits = []
    with pytest.raises(ExchangeError):
        backfill_candles(
            market,
            store,
            coin="BTC",
            interval="4h",
            since=_since(ANCHOR_MS),
            end=market.clock,
            sleep=waits.append,
        )
    assert waits == []


def test_the_funding_walk_never_asks_for_a_window_that_moved_backward(store):
    """The unclamped window end, asserted on the REQUESTS rather than the rows.

    Clamping the window end to the backfill end drags the window's START back
    below the cursor, because the endpoint derives its start from its end. The
    store still ends up correct in the easy case - the clamped request happens
    to cover the remaining ground - so a row count cannot see it. What the
    clamp really costs is a window that re-reads ground already walked and
    then reports the records it finds there as older than the cursor, which is
    how the walk used to stop a page short of the end.
    """
    points = funding_points(24 * 45)
    market = market_at(points[-1].time, funding={"BTC": points})
    backfill_funding(
        market, store, coin="BTC", since=_since(ANCHOR_MS), end=market.clock, page_days=20
    )
    starts = [start for _coin, start, _end in market.funding_calls]
    assert starts == sorted(starts), starts
    assert len(set(starts)) == len(starts), "a window was requested twice"


def test_a_funding_walk_that_cannot_finish_stops_at_the_request_limit(store, monkeypatch):
    """The candle walk's limit is tested; the forward walk computes its own.

    The limit is lowered rather than the span stretched, for the same reason
    the candle test lowers it: what is under test is the bound, not the
    constant's value, and scripting two thousand real pages would test the
    constant instead.
    """
    monkeypatch.setattr(fetch_module, "MAX_PAGES", 4)
    market = market_at(ANCHOR_MS + 4000 * MS_PER_HOUR, funding={"BTC": []})
    result = backfill_funding(
        market,
        store,
        coin="BTC",
        since=_since(ANCHOR_MS),
        end=market.clock,
        page_days=1,
    )
    assert result.stopped is StopReason.PAGE_LIMIT
    assert result.pages == 4


def test_the_rendered_line_does_not_swap_written_for_new(store):
    """Rendered on a SECOND run, where the two counts differ.

    On a fresh store every walk has written == added, so the format string can
    interchange them and read correctly. Only a re-run separates them.
    """
    series = bars(10)
    market = market_at(series[-1].close_time, candles={("BTC", "4h"): series})
    window = {
        "coin": "BTC",
        "interval": "4h",
        "since": _since(ANCHOR_MS),
        "end": market.clock,
    }
    backfill_candles(market, store, **window)
    line = render_fetch(backfill_candles(market, store, **window))
    assert "10 row(s) written" in line
    assert "0 new" in line
    assert "10 -> 10" in line


def test_a_window_whose_ends_are_equal_is_refused(store):
    """The boundary itself, not a day either side of it."""
    market = market_at(ANCHOR_MS, candles={("BTC", "4h"): bars(5)})
    end = from_epoch_ms(ANCHOR_MS)
    with pytest.raises(ValueError, match="--since"):
        backfill_candles(market, store, coin="BTC", interval="4h", since=end, end=end)


def test_each_walk_records_where_it_reached_and_why_it_stopped(store):
    """The fact the rows cannot carry, written down where A3 can read it.

    A 4h series that ends early because the venue serves nothing older and one
    that ends early because a backfill was interrupted hold identical rows and
    scan identically. Only the recorded stop reason separates them.
    """
    series = bars(30)
    points = funding_points(24 * 3)
    market = market_at(
        series[-1].close_time,
        candles={("BTC", "4h"): series},
        funding={"BTC": points},
    )
    since = _since(ANCHOR_MS - 400 * STEP_4H)
    backfill_candles(market, store, coin="BTC", interval="4h", since=since, end=market.clock)
    backfill_funding(market, store, coin="BTC", since=_since(ANCHOR_MS), end=market.clock)

    candles = store.series_state(coin="BTC", series="4h")
    assert candles["stopped"] == StopReason.VENUE_EXHAUSTED.name
    assert candles["earliest_ms"] == series[0].open_time
    assert candles["latest_ms"] == series[-1].open_time
    assert candles["rows"] == 30
    assert candles["since_ms"] == epoch_ms(since, what="test")
    assert candles["venue_clock_ms"] == epoch_ms(market.clock, what="test")

    funding = store.series_state(coin="BTC", series=fetch_module.FUNDING_SERIES)
    assert funding["stopped"] == StopReason.REACHED_END.name
    assert funding["rows"] == len(points)


def test_a_front_truncated_series_scans_clean_but_records_the_interruption(store):
    """Why the recorded reach exists at all.

    A walk stopped by the request limit lands the NEWEST pages and none of the
    older ones, so what it leaves behind is a contiguous grid missing its
    front. The gap scan anchors on the first stamp it finds, so that store
    reports no holes - exactly like a series the venue genuinely has no more
    of. The stop reason is the only thing that separates them, and it lives
    nowhere in the rows.
    """
    series = bars(60)
    market = market_at(series[-1].close_time, candles={("BTC", "4h"): series})
    result = backfill_candles(
        market,
        store,
        coin="BTC",
        interval="4h",
        since=_since(ANCHOR_MS),
        end=market.clock,
        page_bars=20,
        sleep=lambda _seconds: None,
    )
    assert result.stopped is StopReason.REACHED_SINCE

    # Now the same series, cut short: only the newest two pages landed.
    with ResearchStore() as truncated:
        truncated.upsert_candles("BTC", "4h", series[20:])
        truncated.record_series_state(
            coin="BTC",
            series="4h",
            venue_clock_ms=epoch_ms(market.clock, what="test"),
            since_ms=ANCHOR_MS,
            earliest_ms=series[20].open_time,
            latest_ms=series[-1].open_time,
            rows=40,
            stopped=StopReason.PAGE_LIMIT.name,
        )
        # Indistinguishable to the scan...
        assert scan_candles(truncated, coin="BTC", interval="4h").complete
        assert scan_candles(store, coin="BTC", interval="4h").complete
        # ...and told apart by what each walk recorded.
        cut = truncated.series_state(coin="BTC", series="4h")
        whole = store.series_state(coin="BTC", series="4h")
        assert cut["stopped"] != whole["stopped"]
        assert cut["earliest_ms"] > cut["since_ms"], "the front of the asked-for span is missing"
        assert whole["earliest_ms"] == whole["since_ms"]
