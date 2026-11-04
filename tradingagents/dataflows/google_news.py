"""Google News RSS vendor for the global (macro) news tool.

Keyword search is this vendor's whole business, which is what the global news
tool needs and what the yfinance path behind it never had: ``yf.Search`` is a
symbol finder whose ``news`` are the stories of the tickers it fuzzy-matched,
so ``trade war`` answered The Trade Desk and ``exchange hack`` the ICE
exchange, and the only query that worked was the one that named a ticker
(#394). Here each configured query is one GET of the public search feed
``https://news.google.com/rss/search?q=<query> after:<start> before:<end>``
— keyless, no quota published, the feed every reader uses — and the
headlines of every query reach the report in turn (round-robin, in the
feed's own order, which is Google's relevance ranking), so the later queries
in the list are represented rather than starved by the first one that fills
the limit, which is the other half of #394. Represented, that is, while the
limit is at least the number of queries: a smaller limit takes the first
queries only, in order, and the call says so in a WARNING.

The window is the query's: Google's ``after:``/``before:`` date operators
bound the search to the window (measured 2026-11-04: a week in October
answered that week's headlines), so a backtest's past as-of date is served
from the archive rather than from "the last N days", which is what
``when:Nd`` would have meant. Google reads the operator dates as US Pacific
calendar days (measured: ``before:D`` answered items up to D 07:00 UTC), so
the request is padded a day on each side and ``date_window.in_window`` then
holds every item to ``[curr_date - N, curr_date]`` in UTC on the client
side, as the yfinance path does (#1007): the filter, not the operator, is
the as-of bound.

What the feed carries and what is rendered: the ``<title>`` (Google appends
`` - <publisher>``, which is removed when the ``<source>`` element names the
same publisher), the ``<source>`` as the publisher, and the ``<pubDate>``
(RFC 2822). The ``<link>`` is a Google redirect ~250 characters long, which
the citation rule drops anyway (a URL longer than one line is not cited, see
``yfinance_news._citation``), and the ``<description>`` is the same title
inside an anchor, so neither is rendered: one headline is one heading and
one ``Published:`` line.

Failures take the router's taxonomy: an unreached vendor, a 5xx, a 401/403
refusing this client and a body that is not an RSS feed are the outage type
(the optional ``news_data`` category then ends in its sentinel), a 429 is the
rate-limit type the router latches, and any other answer about the request
stays the module type. A feed that answers no items is not a failure — it is
what Google serves for no matches — so the quiet outcomes are logged
instead: one INFO line per call with what each query returned, and a WARNING
when a feed carried items that none of which had a title (what a changed
feed layout looks like) or when no query returned anything.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from itertools import chain, zip_longest

import requests

from .config import get_config
from .date_window import in_window, to_utc
from .errors import VendorError, VendorRateLimitError, VendorUnavailableError
from .utils import (
    MAX_UNTRUSTED_CHARS,
    date_refusal,
    failure_account,
    is_vendor_outage,
    raise_for_http_status,
    sanitize_untrusted,
    wiring_gap,
)
from .vendor_common import window_start_dt

logger = logging.getLogger(__name__)

VENDOR = "Google News"
FEED_URL = "https://news.google.com/rss/search"
# The English (US) edition: the feed's language and region are request
# parameters, and the queries in ``global_news_queries`` are English.
_EDITION = {"hl": "en-US", "gl": "US", "ceid": "US:en"}
_UA = "tradingagents/0.2 (+https://github.com/TauricResearch/TradingAgents)"
# Network timeout (seconds), consistent with the other vendors.
REQUEST_TIMEOUT = 30
# A search feed is at most 100 items (measured 2026-11-04: ~140 KB). Read in
# chunks under the cap, as the Hyperliquid leaderboard is, so a misbehaving
# endpoint cannot stream an unbounded body into memory before the parser.
_MAX_FEED_BYTES = 5 * 1024 * 1024
_CHUNK_BYTES = 64 * 1024
# Google's ``after:``/``before:`` are Pacific calendar days, up to eight
# hours behind the UTC day ``in_window`` judges; a day of padding on each
# side keeps the whole UTC window inside the search, and the filter trims
# the padding back off.
_OPERATOR_PAD = timedelta(days=1)

# What a heading shows where the feed sent no publisher; the same marker as
# the yfinance news path, so the two global news vendors read alike.
SOURCE_UNAVAILABLE = "(source unavailable)"


class GoogleNewsError(VendorError):
    """Google News answered something this module cannot use as a feed."""


class GoogleNewsUnavailableError(GoogleNewsError, VendorUnavailableError):
    """Google News was down or refusing this client, or answered without a feed."""


class GoogleNewsRateLimitError(GoogleNewsError, VendorRateLimitError):
    """Google News throttled this client (HTTP 429)."""


def _rate_limited(_response) -> None:
    raise GoogleNewsRateLimitError(f"{VENDOR} is rate limiting this client (HTTP 429)")


def _request(query: str, after: str, before: str) -> bytes:
    """GET the search feed for ``query`` with Google's ``after:``/``before:`` set to these days.

    The boundary reads its status through ``raise_for_http_status`` (a 5xx
    is the helper's outage type, a 429 this module's rate-limit type). Of
    the failures the library raises, the router's own outage predicate
    decides: an unreached vendor
    and a 401/403 — Google refusing this client's address, the realistic
    failure from a datacenter IP (StockTwits answered the paper host a
    Cloudflare 403 for six weeks, #395) — are the outage type, so the
    sentinel says the vendor was down rather than filing the refusal as this
    module's bug; any other answer about the request stays the module type.
    The cause is quoted through ``failure_account``, so a requests exception
    contributes its status or class and never the request URL (#203).
    """
    params = {"q": f"{query} after:{after} before:{before}", **_EDITION}
    response = None
    try:
        response = requests.get(
            FEED_URL,
            params=params,
            headers={"User-Agent": _UA},
            timeout=REQUEST_TIMEOUT,
            stream=True,
        )
        raise_for_http_status(response, VENDOR, rate_limit=_rate_limited)
        body = bytearray()
        for chunk in response.iter_content(chunk_size=_CHUNK_BYTES):
            body.extend(chunk)
            if len(body) > _MAX_FEED_BYTES:
                raise GoogleNewsError(
                    f"{VENDOR} feed exceeded {_MAX_FEED_BYTES} bytes; refusing to read it further"
                )
    except requests.RequestException as e:
        failure_cls = GoogleNewsUnavailableError if is_vendor_outage(e) else GoogleNewsError
        raise failure_cls(f"{VENDOR} did not answer with a feed ({failure_account(e)})") from e
    finally:
        # A streamed response holds its connection until it is released.
        if response is not None:
            response.close()
    return bytes(body)


def _published(text: str | None) -> datetime | None:
    """The ``<pubDate>`` as an aware UTC datetime, or ``None`` when absent or unreadable.

    Normalised here, once, so the window judges and the ``Published:`` line
    prints the same day: ``parsedate_to_datetime`` keeps a non-UTC offset as
    sent and answers naive for ``-0000``, and ``to_utc`` reads a naive value
    as UTC. ``OverflowError`` is a zone offset or a year the calendar cannot
    hold — one hostile item must not end the whole call.
    """
    if not text:
        return None
    try:
        return to_utc(parsedate_to_datetime(text))
    except (TypeError, ValueError, OverflowError):
        return None


def _parse_feed(body: bytes) -> list[dict]:
    """The feed's items, in feed order, as ``{title, publisher, pub_date}`` rows.

    A body that is not XML, or XML with no ``<channel>``, is Google answering
    without a feed (a consent or error page served with a 200) and is the
    outage type. An item without a title is skipped: an untitled heading would
    show the reader nothing — and a feed whose every item is untitled is a
    layout this parser no longer reads, which is said in a WARNING rather
    than served as "no news". The publisher suffix is removed from the RAW
    title, before the cap: cut first, a long title would keep a truncated
    tail of the suffix and show the publisher twice. Text fields are then
    flattened here, once, so every later comparison and every rendered line
    reads the same spelling.
    """
    try:
        root = ET.fromstring(body)
    except ET.ParseError as e:
        raise GoogleNewsUnavailableError(
            f"{VENDOR} answered with a body that is not an RSS feed"
        ) from e
    channel = root.find("channel")
    if channel is None:
        raise GoogleNewsUnavailableError(f"{VENDOR} answered with XML that is not an RSS feed")
    rows = []
    items = channel.findall("item")
    for item in items:
        raw_title = (item.findtext("title") or "").strip()
        raw_publisher = (item.findtext("source") or "").strip()
        # Google's title ends in `` - <publisher>``; shown once, as the source.
        suffix = f" - {raw_publisher}"
        if raw_publisher and raw_title.endswith(suffix):
            raw_title = raw_title[: -len(suffix)]
        title = sanitize_untrusted(raw_title, limit=MAX_UNTRUSTED_CHARS)
        if not title:
            continue
        publisher = sanitize_untrusted(raw_publisher, limit=MAX_UNTRUSTED_CHARS)
        rows.append(
            {
                "title": title,
                "publisher": publisher or SOURCE_UNAVAILABLE,
                "pub_date": _published(item.findtext("pubDate")),
            }
        )
    if items and not rows:
        logger.warning(
            "%s feed carried %d items and none had a readable title; the feed layout "
            "may have changed",
            VENDOR,
            len(items),
        )
    return rows


def _interleave(per_query: list[list[dict]], limit: int) -> list[dict]:
    """Up to ``limit`` rows taken one per query in turn, the same headline once.

    Round-robin is what keeps every configured query represented: taken
    query by query, the first that fills the limit would starve the rest,
    which is how the yfinance path never ran its geopolitical search (#394).
    Identity is the rendered heading, case-folded: the same story reaches
    two searches under one title, and the reader should see it once.
    """
    chosen: list[dict] = []
    seen: set[str] = set()
    for row in chain.from_iterable(zip_longest(*per_query)):
        if row is None:
            continue
        key = row["title"].casefold()
        if key in seen:
            continue
        seen.add(key)
        chosen.append(row)
        if len(chosen) >= limit:
            break
    return chosen


def _render(rows: list[dict]) -> str:
    """One heading and, when dated, one ``Published:`` line per row, a blank line between."""
    blocks = []
    for row in rows:
        block = f"### {row['title']} (source: {row['publisher']})\n"
        if row["pub_date"] is not None:
            block += f"Published: {row['pub_date'].strftime('%Y-%m-%d')}\n"
        blocks.append(block)
    return "\n".join(blocks) + "\n"


def _configured(look_back_days, limit) -> tuple[int, int, list[str]]:
    """The call's window, limit and queries, each coerced under its own guard.

    Same guards and same names as the yfinance path (#219): the config reads
    under ``global news configuration``, the window under ``global news
    lookback window``, the limit under ``global news article limit``, so an
    operator is sent to the same place whichever vendor served. The window
    is at least a day, and the queries are a non-empty list of text, because
    a bare string is iterable too and would be one search per character.
    (The perp config loader holds ``engine.global_news_queries`` to the same
    shape at load, once, rather than here on every cycle; it cannot import
    this module — a bare config load must not import the engine — so the
    predicate is written twice on purpose.)
    """
    with wiring_gap("global news configuration"):
        config = get_config()
        if look_back_days is None:
            look_back_days = config["global_news_lookback_days"]
        if limit is None:
            limit = config["global_news_article_limit"]
        queries = config["global_news_queries"]
        if not isinstance(queries, list):
            raise TypeError("global_news_queries must be a list of queries, not a string")
        if not queries or not all(isinstance(q, str) and q.strip() for q in queries):
            raise ValueError("global_news_queries must name at least one non-blank query")
    with wiring_gap("global news lookback window"):
        look_back_days = int(look_back_days)
        if look_back_days < 1:
            raise ValueError("the global news lookback window is at least one day")
    with wiring_gap("global news article limit"):
        limit = max(1, int(limit))
    return look_back_days, limit, [q.strip() for q in queries]


def _operator_days(start_dt: datetime, curr_dt: datetime) -> tuple[str, str]:
    """The ``after:``/``before:`` days for a UTC window, padded a day each side.

    ``before:`` is read against the day it names at Pacific midnight, so the
    day after the as-of day would cut the as-of day's last Pacific evening
    off and the day before the start would miss its first UTC morning; one
    day of padding each side covers both, and ``in_window`` trims it. An
    as-of day the calendar cannot pad past is refused as this vendor's own
    type, as ``window_start_dt`` refuses the other side.
    """
    try:
        before = curr_dt + 2 * _OPERATOR_PAD
    except OverflowError as e:
        raise GoogleNewsError(
            f"curr_date {curr_dt:%Y-%m-%d} is too late for the feed window: the day "
            "after it cannot be written"
        ) from e
    return (start_dt - _OPERATOR_PAD).strftime("%Y-%m-%d"), before.strftime("%Y-%m-%d")


def get_global_news(
    curr_date: str,
    look_back_days: int | None = None,
    limit: int | None = None,
) -> str:
    """Global/macro headlines from Google News search, one feed per configured query.

    Args:
        curr_date: Current date in yyyy-mm-dd format
        look_back_days: Days to look back. ``None`` falls back to
            ``global_news_lookback_days`` from the active config.
        limit: Maximum number of headlines across every query. ``None``
            falls back to ``global_news_article_limit`` from the active config.

    Returns:
        Formatted string containing the headlines, or the no-news sentence.
    """
    # Unusable dates are refused before any request, in the shared voice
    # (#111), and above the "No global news found" sentences below: those
    # are coverage claims about the day named, so only a day that was.
    refusal = date_refusal(curr_date, what="global news", kind="point")
    if refusal is not None:
        return refusal
    look_back_days, limit, queries = _configured(look_back_days, limit)
    if limit < len(queries):
        logger.warning(
            "global news limit %d is below the %d configured queries; only the first %d "
            "queries can be represented",
            limit,
            len(queries),
            limit,
        )

    curr_dt = datetime.strptime(curr_date, "%Y-%m-%d")
    # The family's window arithmetic (#380): a count the calendar cannot
    # hold is this vendor's own typed refusal, not a bare ``OverflowError``.
    start_dt = window_start_dt(curr_dt, look_back_days, error_cls=GoogleNewsError)
    start_date = start_dt.strftime("%Y-%m-%d")
    after, before = _operator_days(start_dt, curr_dt)

    # One outage anywhere is the verdict on the whole call — headlines
    # gathered by an earlier query are not served as a report with an
    # unmarked gap — and every query runs before anything is chosen, which
    # is what lets the later queries be represented at all. The GETs are
    # sequential: a fetch is about a second, and the five share the
    # router's one-at-a-time lane with every other tool call anyway.
    fetched = [_parse_feed(_request(query, after, before)) for query in queries]
    # Feed order is kept: Google ranks a search feed by relevance, and
    # re-sorting by date would put a stale story's reprint above the
    # headline the query was written for.
    per_query = [
        [row for row in rows if in_window(row["pub_date"], start_dt, curr_dt)] for rows in fetched
    ]
    # The quiet outcomes, said once per call: a query that answers nothing
    # is not a failure (Google serves an empty channel for no matches), but
    # an operator reading "no news" for a week that had headlines needs the
    # per-query counts to tell a broken query from a quiet one.
    logger.info(
        "global news %s to %s: %s",
        start_date,
        curr_date,
        "; ".join(
            f"{query!r}: {len(rows)} fetched, {len(kept)} in window"
            for query, rows, kept in zip(queries, fetched, per_query, strict=True)
        ),
    )
    if not any(fetched):
        logger.warning(
            "%s answered no items for any of the %d queries between %s and %s",
            VENDOR,
            len(queries),
            start_date,
            curr_date,
        )
        return f"No global news found for {curr_date}"
    chosen = _interleave(per_query, limit)
    # Every candidate fell outside the window: say so rather than return an
    # empty-bodied report (#993 at the yfinance path).
    if not chosen:
        return f"No global news found between {start_date} and {curr_date}"
    return f"## Global Market News, from {start_date} to {curr_date}:\n\n{_render(chosen)}"
