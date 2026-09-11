"""Walking BTC history off the venue and into this package's store.

Two walks, in opposite directions, because the two endpoints truncate at
opposite ends and a walk that guesses wrong loses a window silently.

**Candles walk backwards** (plan §3.4). ``get_candles`` is anchored on its
``end`` and answers with the bars closed as of it, so stepping ``end`` back to
the oldest bar of the page just received is exact: that bar's own
``close_time`` is past the new ``end``, so it is not served twice, and the bar
immediately before it closes exactly AT the new ``end``, so nothing falls
between two pages.

**Funding walks forwards.** ``fundingHistory`` is anchored on its START and
caps how many records one response carries, so a truncated page is missing its
NEWEST records, not its oldest. Walked backwards, that missing tail would be
stepped straight over and never noticed. Walked forwards from the newest point
actually received, a truncated page just means the next request starts earlier
than planned and picks the tail up — the walk self-heals, and its cost is one
extra request rather than a hole.

Neither walk is the last word on completeness: whatever lands is scanned by
:mod:`~contrib.autoresearch.gaps`, which is the thing that actually says
whether the store covers its span.

A venue failure is NOT contained here. ``ExchangeError`` propagates to the CLI,
which names it and exits 1, because the pages already written are durable and
correct — the walk is built out of upserts precisely so that re-running it
after an outage walks back over the same ground harmlessly. Swallowing the
error would instead leave a short history that reads exactly like a coin which
listed later.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from functools import partial
from typing import TypeVar

from .constants import MS_PER_DAY
from .ports import HistoryMarketData
from .store import ResearchStore
from .upstream import ExchangeThrottledError, epoch_ms, from_epoch_ms, parse_interval

__all__ = [
    "CANDLE_PAGE_BARS",
    "FUNDING_PAGE_DAYS",
    "FUNDING_SERIES",
    "MAX_PAGES",
    "THROTTLE_BACKOFF_SECONDS",
    "SeriesFetch",
    "StopReason",
    "backfill_candles",
    "backfill_funding",
    "render_fetch",
]

_T = TypeVar("_T")

logger = logging.getLogger(__name__)

# Bars per candle request, kept well under the venue's 5000-bar answer so the
# cap is never what ends a page.
#
# That 5000 is a HISTORY DEPTH bound, not only a per-response one, and the
# difference decides what this package can ever study. Measured against
# mainnet on 2026-09-11: a 4h backfill asked to start at 2024-01-01 stopped at
# 2024-05-30 with 4999 bars and the venue then served nothing older, six
# requests in. So ``4h`` reaches back about 833 days and no further, whatever
# ``--since`` says, while ``1d`` at the same depth covers ~13 years — the same
# read pulled BTC's whole daily history, 2214 bars from 2020-08-19, and also
# stopped on an exhausted venue rather than on the cap. A walk that treated
# 5000 as per-response would read the stop as a bug and page forever.
CANDLE_PAGE_BARS = 1000

# Days per funding request. Funding settles hourly, so 20 days is 480 records
# — under the 500-record cap the endpoint is documented to have. The walk does
# not DEPEND on that number being right (it resumes from the newest record it
# actually received); staying under it just keeps the common case to one
# request per window.
FUNDING_PAGE_DAYS = 20

# A walk that neither finishes nor stalls stops here rather than requesting
# forever. A defect bound, not a tuning knob: at the page sizes above, a
# decade of either series is well under a hundred requests, so reaching this
# means the venue is answering in a shape the walk does not understand —
# worth reporting as itself instead of hiding inside a long silence.
MAX_PAGES = 2000

# What the funding series is called in ``series_state``. Candles are filed
# under their interval, which is a member of the venue's own vocabulary;
# funding has no interval, so it needs a name of its own, and that name is
# written once here rather than as a literal at the writer and every reader.
FUNDING_SERIES = "funding"

# How long to wait out a venue throttle, and therefore how many times. A
# backfill is dozens of requests in a row with no user waiting on any single
# one, which is exactly the shape a public endpoint sheds: measured against
# mainnet on 2026-09-11, 44 consecutive funding reads earned a 429. Left
# unhandled that ends a multi-year backfill partway through with an error,
# and the re-run walks into the same wall at the same place.
#
# Waiting is the whole remedy — there is no alternative endpoint and no
# cheaper request to make — so the only real questions are how long and when
# to give up. Four rising waits, five attempts in all: enough that a shed
# request costs seconds, few enough that a venue which is genuinely down is
# reported within a couple of minutes instead of being retried into the
# evening. A throttle that survives all five propagates, and the pages
# already written stay written.
THROTTLE_BACKOFF_SECONDS = (2.0, 5.0, 15.0, 30.0)


def _served(call: Callable[[], _T], *, sleep: Callable[[float], None], what: str) -> _T:
    """``call()``, waiting out a venue THROTTLE a bounded number of times.

    Only ``ExchangeThrottledError`` is waited on, never the wider
    ``ExchangeError``: the venue typing a refusal as "not served, try later"
    is what makes waiting the right answer. Retrying a malformed response or
    an unknown coin would turn one clear failure into five slow ones.
    """
    for attempt, pause in enumerate(THROTTLE_BACKOFF_SECONDS, start=1):
        try:
            return call()
        except ExchangeThrottledError:
            logger.warning(
                "%s: the venue is throttling (attempt %d of %d); waiting %.0fs",
                what,
                attempt,
                len(THROTTLE_BACKOFF_SECONDS) + 1,
                pause,
            )
            sleep(pause)
    # The last attempt is deliberately outside the loop and unguarded: its
    # failure is the one that should reach the operator, with the venue's own
    # sentence, rather than a summary invented here.
    return call()


class StopReason(str, Enum):
    """Why a walk ended. Reported, because the endings mean very different things.

    ``REACHED_SINCE`` / ``REACHED_END`` is the walk finishing its job.
    ``INTERRUPTED`` is the one nothing inside the walk ever sets: it is what
    the recorded reach still says when the walk left by raising — a venue
    failure, a Ctrl-C — so an ending that was never reached cannot name
    itself.
    ``VENUE_EXHAUSTED`` means the venue stopped serving older data — for a
    backfill starting before the coin listed, the correct and expected
    ending. ``NO_PROGRESS`` and ``PAGE_LIMIT`` are the walk protecting itself:
    the store still holds everything that landed, but the span it covers is
    not the span that was asked for, and a report saying only "done" would be
    lying about that.
    """

    REACHED_SINCE = "reached the requested start"
    REACHED_END = "reached the requested end"
    VENUE_EXHAUSTED = "the venue served no older data"
    NO_PROGRESS = "the venue stopped moving the window"
    PAGE_LIMIT = "hit the request limit"
    INTERRUPTED = "the walk did not finish"


@dataclass(frozen=True)
class SeriesFetch:
    """What one walk did, in the terms an operator has to judge it by.

    ``rows_written`` and ``rows_added`` are both here because they answer
    different questions, and a report carrying only the first is misleading:
    re-running a complete backfill writes thousands of rows and adds none,
    and that is the SUCCESS case, not a stalled one.
    """

    label: str
    pages: int
    rows_written: int
    rows_before: int
    rows_after: int
    stopped: StopReason

    @property
    def rows_added(self) -> int:
        return self.rows_after - self.rows_before


def _record_reach(
    store: ResearchStore,
    *,
    coin: str,
    series: str,
    venue_clock_ms: int,
    since_ms: int,
    stopped: StopReason,
    span: Callable[[], tuple[int | None, int | None]],
    count: Callable[[], int],
) -> None:
    """Write where a walk reached, and never become the failure it reports.

    Called from a ``finally``, so it runs while an exception may already be
    propagating — which is exactly when the row matters most and exactly when
    writing it is most likely to fail too. A store error here must therefore
    be contained: replacing "the venue refused" with "the store could not be
    written" would send an operator to the wrong system entirely, and losing
    a breadcrumb is the smaller harm. The same discipline the perp package
    applies to its own backfill breadcrumbs.

    ``span`` and ``count`` are called HERE rather than by the caller for the
    same reason: they are store reads, so they can fail the same way, and
    inside the containment they cost a warning instead of a traceback.
    """
    try:
        earliest, latest = span()
        store.record_series_state(
            coin=coin,
            series=series,
            venue_clock_ms=venue_clock_ms,
            since_ms=since_ms,
            earliest_ms=earliest,
            latest_ms=latest,
            rows=count(),
            stopped=stopped.name,
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "%s %s: could not record where the backfill reached; the stored reach "
            "now describes an earlier run",
            coin,
            series,
            exc_info=True,
        )


def _window(since: datetime, end: datetime, *, what: str) -> tuple[int, int]:
    """``since``/``end`` as epoch ms, refusing a pair that is not a window.

    Both bounds go through ``epoch_ms``, which refuses a naive datetime by
    name — the discipline the venue reader itself is held to, so a caller
    cannot reach the wire with a bound read in the host's local zone. The
    ordering check is here rather than inside each walk because getting it
    wrong is silent in opposite ways: the backward walk would stop on its
    first page and report success over an empty span, and the forward walk
    would never enter its loop at all.
    """
    since_ms = epoch_ms(since, what=f"{what} start")
    end_ms = epoch_ms(end, what=f"{what} end")
    if since_ms >= end_ms:
        raise ValueError(
            f"{what}: --since ({since.isoformat()}) must be before the window end "
            f"({end.isoformat()})"
        )
    return since_ms, end_ms


def backfill_candles(
    market: HistoryMarketData,
    store: ResearchStore,
    *,
    coin: str,
    interval: str,
    since: datetime,
    end: datetime,
    page_bars: int = CANDLE_PAGE_BARS,
    sleep: Callable[[float], None] = time.sleep,
) -> SeriesFetch:
    """Walk ``coin``/``interval`` bars backwards from ``end`` to ``since``, storing each page.

    ``end`` is the VENUE's clock, read by the caller before the first request
    (issue #124's discipline, restated by plan §3.4): a host clock running
    ahead would otherwise ask for a bar the venue has not closed, and one
    running behind would truncate the newest end of the backfill by its lag.
    """
    key = parse_interval(interval).value
    since_ms, _ = _window(since, end, what=f"{coin} {key} candle backfill")
    before = store.count_candles(coin, key)
    cursor, pages, written = end, 0, 0
    # INTERRUPTED until the walk says otherwise, because the recording below
    # happens in a ``finally``: an ending this function never reached must not
    # be able to name itself. Initialising to PAGE_LIMIT here (which the
    # ``else`` arm now sets) would have a throttled walk record "hit the
    # request limit" on its way out.
    stopped = StopReason.INTERRUPTED
    try:
        while pages < MAX_PAGES:
            page = _served(
                partial(market.get_candles, coin, key, page_bars, end=cursor),
                sleep=sleep,
                what=f"{coin} {key} candles",
            )
            pages += 1
            if not page:
                stopped = StopReason.VENUE_EXHAUSTED
                break
            # ``min`` rather than ``page[0]``: the port says oldest first, and
            # the walk's whole correctness rests on which bar the next window
            # ends at. Deriving it from the values costs one pass and cannot
            # be wrong.
            oldest = min(bar.open_time for bar in page)
            written += store.upsert_candles(
                coin, key, [b for b in page if b.open_time >= since_ms]
            )
            if oldest <= since_ms:
                stopped = StopReason.REACHED_SINCE
                break
            # The next window ENDS at this page's oldest open. That bar closes
            # after it, so it is not served again; the bar before it closes
            # exactly at it, so nothing is skipped. Anything else here is
            # either a duplicated page or a one-bar hole per page.
            nxt = from_epoch_ms(oldest)
            if nxt >= cursor:
                stopped = StopReason.NO_PROGRESS
                break
            cursor = nxt
        else:
            # The loop CONDITION went false, which is the request bound.
            stopped = StopReason.PAGE_LIMIT
    finally:
        # In a ``finally``, because the ending this row exists to record is the
        # one that does not return: a venue failure propagates out of
        # ``_served`` by design, and a Ctrl-C arrives anywhere. On the return
        # path alone the row kept the PREVIOUS run's answer while the store
        # had grown underneath it, so a store left half-filled by an
        # interrupted 2020 backfill still read "reached the requested start"
        # — the exact claim this table was added to be able to contradict.
        _record_reach(
            store,
            coin=coin,
            series=key,
            venue_clock_ms=epoch_ms(end, what=f"{coin} {key} candle backfill end"),
            since_ms=since_ms,
            stopped=stopped,
            span=partial(store.candle_span, coin, key),
            count=partial(store.count_candles, coin, key),
        )
    logger.info(
        "%s %s candles: %d page(s), %d row(s) written, stopped because %s",
        coin,
        key,
        pages,
        written,
        stopped.value,
    )
    after = store.count_candles(coin, key)
    return SeriesFetch(
        label=f"{coin} {key} candles",
        pages=pages,
        rows_written=written,
        rows_before=before,
        rows_after=after,
        stopped=stopped,
    )


def backfill_funding(
    market: HistoryMarketData,
    store: ResearchStore,
    *,
    coin: str,
    since: datetime,
    end: datetime,
    page_days: int = FUNDING_PAGE_DAYS,
    sleep: Callable[[float], None] = time.sleep,
) -> SeriesFetch:
    """Walk ``coin``'s funding settlements forwards from ``since`` to ``end``, storing each page.

    Forwards, for the truncation reason the module docstring gives. Each step
    resumes from the newest record the previous page actually carried, so a
    page the venue cut short costs one extra request and loses nothing; a
    window the venue has no records for at all is stepped over whole.
    """
    since_ms, end_ms = _window(since, end, what=f"{coin} funding backfill")
    span_ms = page_days * MS_PER_DAY
    before = store.count_funding(coin)
    cursor_ms, pages, written = since_ms, 0, 0
    # INTERRUPTED until the walk says otherwise; the ``else`` arm below is what
    # now sets REACHED_END. See the candle walk for why the initial value may
    # not be an ending this function might never reach.
    stopped = StopReason.INTERRUPTED
    try:
        while cursor_ms <= end_ms:
            if pages >= MAX_PAGES:
                stopped = StopReason.PAGE_LIMIT
                break
            # The window end is NOT clamped to ``end_ms``, and that is
            # load-bearing: the endpoint derives its START as
            # ``end - page_days``, so clamping the end drags the start back
            # below the cursor. The walk then re-requests records it already
            # holds, reads their stamps as "older than the cursor", concludes
            # the venue served nothing new and stops — silently, a page short
            # of the end, every single time the last window would overhang.
            # Asking past the venue's clock costs nothing (there is no data
            # there) and the filter below keeps anything past it out.
            window_end_ms = cursor_ms + span_ms
            window_end = from_epoch_ms(window_end_ms)
            points = _served(
                partial(market.get_funding_history, coin, page_days, end=window_end),
                sleep=sleep,
                what=f"{coin} funding",
            )
            pages += 1
            written += store.upsert_funding(
                coin, [p for p in points if since_ms <= p.time <= end_ms]
            )
            newest = max((p.time for p in points if p.time <= end_ms), default=None)
            # Resume just past the newest record RECEIVED, not past the window
            # that was asked for: those differ exactly when the venue
            # truncated, and that difference is the tail this walk exists not
            # to lose. An empty window has no tail, so it is stepped over as
            # asked.
            #
            # Both arms are strictly greater than ``cursor_ms`` — the first by
            # its own guard, the second because ``window_end_ms >= cursor_ms``
            # — so the forward walk cannot stall the way the backward one can,
            # and there is no NO_PROGRESS branch here to match the candle
            # walk's. A venue answering every request with the same single
            # record would advance one millisecond per page and be stopped by
            # MAX_PAGES instead. Writing the guard anyway would have looked
            # like protection while being unreachable code no test could reach.
            cursor_ms = (
                newest + 1
                if newest is not None and newest >= cursor_ms
                else window_end_ms + 1
            )
        else:
            # The loop CONDITION went false: the cursor passed the window end.
            stopped = StopReason.REACHED_END
    finally:
        _record_reach(
            store,
            coin=coin,
            series=FUNDING_SERIES,
            venue_clock_ms=end_ms,
            since_ms=since_ms,
            stopped=stopped,
            span=partial(store.funding_span, coin),
            count=partial(store.count_funding, coin),
        )
    logger.info(
        "%s funding: %d page(s), %d row(s) written, stopped because %s",
        coin,
        pages,
        written,
        stopped.value,
    )
    after = store.count_funding(coin)
    return SeriesFetch(
        label=f"{coin} funding",
        pages=pages,
        rows_written=written,
        rows_before=before,
        rows_after=after,
        stopped=stopped,
    )


def render_fetch(result: SeriesFetch) -> str:
    """One line saying what the walk landed, including whether any of it was new."""
    return (
        f"{result.label}: {result.pages} request(s), {result.rows_written} row(s) written, "
        f"{result.rows_added} new ({result.rows_before} -> {result.rows_after}); "
        f"stopped because {result.stopped.value}"
    )
