"""yfinance-based news data fetching functions."""

import contextlib
from datetime import datetime

import yfinance as yf
from dateutil.relativedelta import relativedelta
from yfinance.data import YfData

from .config import get_config

# The date refusals live in utils so the Alpha Vantage vendor serving the same
# routed tools shares the single judgement and the single sentence (#111).
# Neither getter handles what it meets outside the taxonomy and outside
# transport: it leaves raw for the router, which reads it as this vendor's
# library failing and renders one line of report text when no vendor serves
# (#187, #219). What each getter does before it asks Yahoo anything — the
# config reads, the cache forget — is not that, and wears ``wiring_gap`` to
# say so (#111, #200).
from .symbol_utils import normalize_symbol
from .utils import (
    MAX_UNTRUSTED_CHARS,
    date_range_refusal,
    date_refusal,
    echo_argument,
    no_news_in_window,
    sanitize_untrusted,
    wiring_gap,
)
from .yfinance_common import yf_fetch_unhidden

# Clamp the untrusted article count before it sizes an external yf.Search
# call (#33): an LLM-supplied or misconfigured value must stay bounded.
MAX_SEARCH_NEWS_COUNT = 100

# How much of one article's summary may reach the prompt. Its own bound rather
# than ``MAX_UNTRUSTED_CHARS``: that cap sizes LABELS — a title, a publisher, an
# echoed argument — where 200 characters is generous, whereas the summary is the
# news report's PAYLOAD and the same cap would cut most real articles mid
# sentence, degrading what the news analyst reads to buy nothing (the structural
# forgery is closed by the flattening, not by the cap). Still bounded, because a
# vendor field with no ceiling can bury the report's own sentences under its bulk
# and the article count alone does not bound the bytes (#233).
MAX_NEWS_SUMMARY_CHARS = 2000


def _flatten_article_fields(data: dict) -> dict:
    """Flatten the four vendor-written text fields of one extracted article.

    Every one of them is written by whoever filed the story, and the report
    they are rendered into is served to the news analyst verbatim — the router
    caps only its own sentinel slots. Unflattened, a title carrying its own
    ``"\\n### "`` opens a heading inside a report the model is told to trust,
    and the title sits at the START of its line, which is the most exploitable
    position in the repo (#233). Flattening is what closes that: the report is
    block-level, so a fragment with no line breaks cannot open a block however
    it is punctuated.

    Applied HERE, at the one place both article shapes (nested ``content`` and
    flat) are read, and before the caller's window filter and title
    de-duplication, which then agree with what is actually rendered rather than
    with a spelling the report never shows. ``pub_date`` is a datetime and is
    not text; it is untouched. What keeps the two news reports from drifting is
    ``_render_article`` below, not this function: a fifth rendered field added
    to one loop and not the other would escape a guard placed only here.

    Labels take the shared cap; the summary takes its own, larger one for the
    reason given at ``MAX_NEWS_SUMMARY_CHARS``. The link takes the label cap
    too: a URL that long is already unusable as a citation, and the flattening
    turns a ``#`` fragment marker into a space, so a fragment URL renders
    changed — the report's honesty about structure is worth more than a
    fragment anchor.

    ``or ""`` before the two optional fields, because ``sanitize_untrusted``
    goes through ``str``: they reach here as ``None`` when the vendor sends the
    key carrying a null (the extraction's ``.get(key, "")`` default covers only
    an ABSENT key), and a bare flatten would hand back the truthy string
    ``"None"`` — which the renderers' ``if data["summary"]`` / ``if
    data["link"]`` would then print as a body line reading ``None`` and a
    ``Link: None``. Title and publisher cannot arrive that way: their
    extraction already substitutes an explicit unavailability marker for a
    false-y value (#31).
    """
    return {
        **data,
        "title": sanitize_untrusted(data["title"], limit=MAX_UNTRUSTED_CHARS),
        "summary": sanitize_untrusted(data["summary"] or "", limit=MAX_NEWS_SUMMARY_CHARS),
        "publisher": sanitize_untrusted(data["publisher"], limit=MAX_UNTRUSTED_CHARS),
        "link": sanitize_untrusted(data["link"] or "", limit=MAX_UNTRUSTED_CHARS),
    }


def _extract_article_data(article: dict) -> dict:
    """Extract article data from yfinance news format (handles nested 'content' structure).

    The four text fields come back FLATTENED (see ``_flatten_article_fields``);
    ``pub_date`` comes back as a datetime or None.
    """
    # Handle nested content structure
    if "content" in article:
        content = article["content"]
        # Missing fields get explicit unavailability markers, not values that
        # could be misread as a real title or a publisher named "Unknown".
        title = content.get("title") or "(title unavailable)"
        summary = content.get("summary", "")
        provider = content.get("provider") or {}
        publisher = provider.get("displayName") or "(source unavailable)"

        # Get URL from canonicalUrl or clickThroughUrl
        url_obj = content.get("canonicalUrl") or content.get("clickThroughUrl") or {}
        link = url_obj.get("url", "")

        # Get publish date
        pub_date_str = content.get("pubDate", "")
        pub_date = None
        if pub_date_str:
            with contextlib.suppress(ValueError, AttributeError):
                pub_date = datetime.fromisoformat(pub_date_str.replace("Z", "+00:00"))

        data = {
            "title": title,
            "summary": summary,
            "publisher": publisher,
            "link": link,
            "pub_date": pub_date,
        }
    else:
        # Fallback for flat structure. Parse the epoch publish time so flat
        # articles are date-filterable too (otherwise they bypass the
        # historical window and leak future news, #992/#1007).
        pub_date = None
        ts = article.get("providerPublishTime")
        if ts:
            with contextlib.suppress(ValueError, OSError, TypeError):
                pub_date = datetime.fromtimestamp(ts)
        data = {
            "title": article.get("title") or "(title unavailable)",
            "summary": article.get("summary", ""),
            "publisher": article.get("publisher") or "(source unavailable)",
            "link": article.get("link", ""),
            "pub_date": pub_date,
        }

    # ONE exit, so a third article shape added later cannot reach the report
    # unflattened by forgetting the wrapper.
    return _flatten_article_fields(data)


def _render_article(data: dict) -> str:
    """One extracted article as the block both news reports render it.

    The one place an article becomes prompt text. Both getters used to carry a
    verbatim copy of these six lines, so "the ticker report and the global
    report show an article the same way" was a fact about two literals rather
    than about the code — and the flattening that keeps a title from opening a
    heading of its own protects only the fields a copy actually renders (#233).

    The two ``if``s are what make an absent summary or link no line at all,
    rather than a line with nothing after the label; ``_flatten_article_fields``
    keeps them working by coercing a vendor's null to ``""`` rather than to the
    string ``"None"``.
    """
    block = f"### {data['title']} (source: {data['publisher']})\n"
    if data["summary"]:
        block += f"{data['summary']}\n"
    if data["link"]:
        block += f"Link: {data['link']}\n"
    return block + "\n"


def _in_news_window(pub_date, start_dt, end_dt) -> bool:
    """Whether an article belongs in the [start_dt, end_dt] window.

    Dated articles are kept only if they fall in the window. An undated article
    is kept only when the window reaches the present (live run) — in a
    historical/backtest window it's excluded, since we can't prove it isn't
    future news (look-ahead safety, #992/#1007).
    """
    if pub_date is not None:
        naive = pub_date.replace(tzinfo=None) if hasattr(pub_date, "replace") else pub_date
        return start_dt <= naive <= end_dt + relativedelta(days=1)
    return end_dt >= datetime.now() - relativedelta(days=1)


def get_news_yfinance(
    ticker: str,
    start_date: str,
    end_date: str,
) -> str:
    """
    Retrieve news for a specific stock ticker using yfinance.

    Args:
        ticker: Stock ticker symbol (e.g., "AAPL")
        start_date: Start date in yyyy-mm-dd format
        end_date: End date in yyyy-mm-dd format

    Returns:
        Formatted string containing news articles
    """
    # Unusable dates are refused before any request, in the shared voice
    # (#111) — a return, so nothing below classifies it.
    if (refusal := date_range_refusal(start_date, end_date, what="news")) is not None:
        return refusal

    # Coerced and floored inside the guard, as the Alpha Vantage sibling does
    # with the same key: sent on as it was read, a value that is not a number
    # reaches Yahoo as the article count and comes back as "No news found",
    # and so does a zero or a negative — a coverage claim over a call that
    # never asked properly (#136), where the sibling raises or serves. One
    # routed tool's two vendors must end alike (#219). Only the floor is
    # shared: Alpha Vantage also caps at a vendor maximum this path has never
    # had, and inventing one here would change what a large limit returns.
    with wiring_gap("news configuration"):
        article_limit = max(1, int(get_config()["news_article_limit"]))
    # Query Yahoo with the canonical symbol, like every other yfinance path —
    # a raw broker/forex/crypto alias (XAUUSD, BTCUSD) otherwise silently
    # returns no news. Keep the user's ticker in the report header.
    canonical = normalize_symbol(ticker)
    # The report names both spellings, and both are the caller's own argument
    # coming back into text the model reads, so both are echoed flattened and
    # capped (#233). Bare, not quoted, so ``echo_argument`` is the right helper
    # here — nothing wraps them in delimiters a value could close. The
    # comparison stays on the RAW pair: it asks whether the alias table changed
    # the symbol, which is not a question the flattening may answer.
    echoed = echo_argument(ticker)
    resolved = "" if canonical == ticker else f" (resolved to {echo_argument(canonical)})"
    stock = yf.Ticker(canonical)
    # Through the shared un-hidden boundary like every other yfinance leaf
    # (#116); an outage body takes its vendor-unavailable lane rather than
    # the empty list "No news found" below would claim as coverage (#136).
    news = yf_fetch_unhidden(lambda: stock.get_news(count=article_limit), hidden_answer=list)

    if not news:
        return f"No news found for {echoed}{resolved}"

    # Parse date range for filtering
    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")

    news_str = ""
    filtered_count = 0

    for article in news:
        data = _extract_article_data(article)

        # Keep only articles within the requested window (look-ahead safe).
        if not _in_news_window(data["pub_date"], start_dt, end_dt):
            continue

        news_str += _render_article(data)
        filtered_count += 1

    if filtered_count == 0:
        # The shared definition, so this sentence and the Alpha Vantage
        # sibling's cannot drift in wording or in which guard the symbol takes
        # (#219, #233). It echoes the ticker itself; the resolution clause is
        # this vendor's own, since only this one resolves aliases.
        return no_news_in_window(ticker, start_date, end_date, resolved=resolved)

    return f"## {echoed}{resolved} News, from {start_date} to {end_date}:\n\n{news_str}"


def get_global_news_yfinance(
    curr_date: str,
    look_back_days: int | None = None,
    limit: int | None = None,
) -> str:
    """
    Retrieve global/macro economic news using yfinance Search.

    Args:
        curr_date: Current date in yyyy-mm-dd format
        look_back_days: Number of days to look back. ``None`` falls back to
            ``global_news_lookback_days`` from the active config.
        limit: Maximum number of articles to return. ``None`` falls back to
            ``global_news_article_limit`` from the active config.

    Returns:
        Formatted string containing global news articles
    """
    # Unusable dates are refused before any request, in the shared voice
    # (#111). This must stay ABOVE the "No global news found" early exit: that
    # sentence is a coverage claim about the day named, so it may only be
    # served for a day that was.
    refusal = date_refusal(curr_date, what="global news", kind="point")
    if refusal is not None:
        return refusal

    # A key this deployment does not carry is this project's wiring, not
    # Yahoo's library, and must not come back as a line of report text
    # (#219). The reads themselves are inside the guard; what each value
    # feeds is coerced under a guard of its own below, so a malformed one
    # fails the call rather than reaching the window arithmetic two hundred
    # lines later and coming back as the news report.
    with wiring_gap("global news configuration"):
        config = get_config()
        if look_back_days is None:
            look_back_days = config["global_news_lookback_days"]
        if limit is None:
            limit = config["global_news_article_limit"]
        search_queries = config["global_news_queries"]
        if isinstance(search_queries, str):
            # A bare string is iterable too — the loop below would run one
            # Yahoo search per character (``fetch_each`` refuses the same
            # shape for the same reason). Raised untyped inside the guard so
            # it takes the block's name like every other failure here; the
            # guard is what makes it a WiringGapError.
            raise TypeError("global_news_queries must be a list of queries, not a string")

    # Each coercion below takes whichever value won above — the configured
    # default or the one this call passed — so each is guarded under a name
    # that claims neither source. Naming the config keys would send an
    # operator to a value config never supplied when it was the caller's
    # that was unusable. The window is coerced here rather than left to
    # ``relativedelta`` two hundred lines down, where an unusable one is
    # outside every guard and comes back as the news report: the Alpha
    # Vantage sibling ends such a call by raising, and one routed tool's two
    # vendors must end alike (#219). Only the ending is shared — Alpha
    # Vantage also clamps its window to a vendor maximum this one has never
    # had, and inventing one here would change what a long window returns.
    with wiring_gap("global news lookback window"):
        look_back_days = int(look_back_days)
    with wiring_gap("global news article limit"):
        limit = max(1, min(int(limit), MAX_SEARCH_NEWS_COUNT))

    # yfinance memoizes every Search fetch for the life of the process:
    # ``Search.search`` reads through ``YfData.cache_get``, an ``lru_cache``
    # on the library's singleton with no TTL and no invalidation, keyed on
    # the request parameters — and the parameters built below carry no date.
    # A long-lived daemon therefore contacted Yahoo for global news once per
    # process and served every later cycle its first cycle's headlines, until
    # a restart (#198; verified on yfinance 1.4.1, the pinned floor). Forgotten
    # here, once per call, so each call is a fresh set of requests. The forget
    # is process-wide by construction: it also drops the day's memoized
    # fundamentals-timeseries pages (behind ``info`` and the statements —
    # their keys carry today's date, so they were held a day at most) and
    # the timezone entries (served first from yfinance's persistent tz cache,
    # so rarely a request); ``get_news`` is an uncached POST. Outside the
    # boundary's lock: ``cache_clear`` is atomic, and the lock serializes the
    # hide-exceptions flag and the wire, not an in-memory forget (#137
    # measured its cost per cycle). Under ``wiring_gap``: a library that
    # drops the attribute fails loudly rather than freezing again behind a
    # report, which is what placing it above the old ``with`` block bought
    # and what the type buys now the conversion is the router's (#219).
    with wiring_gap("global news cache forget"):
        YfData.cache_get.cache_clear()

    all_news = []
    seen_titles = set()

    for query in search_queries:
        # Through the shared un-hidden boundary like every other yfinance
        # leaf (#136): an outage body takes its vendor-unavailable lane
        # rather than the news=[] that "No global news found" below would
        # claim as coverage. Search fetches in its constructor, so the
        # attribute is read inside the boundary and the hidden answer is
        # the library's own empty list. One outage anywhere in the loop is
        # the verdict on the whole call — articles gathered by an earlier
        # query are not served as a report with an unmarked gap. This
        # also puts the call under the boundary's lock, which serializes
        # it with every other yfinance fetch.
        news = yf_fetch_unhidden(
            lambda q=query: (
                yf.Search(
                    query=q,
                    news_count=limit,
                    enable_fuzzy_query=True,
                ).news
            ),
            hidden_answer=list,
        )

        if news:
            for article in news:
                # Both shapes through the one extraction, which handles them
                # both: the flat branch used to key on the RAW title while the
                # render below uses the flattened one, so the same story
                # arriving once nested and once flat with a marker in its title
                # survived de-duplication twice and rendered two identical
                # headings. Reading the same value the report shows is also
                # what lets the docstring on ``_flatten_article_fields`` claim
                # the two agree (#233).
                title = _extract_article_data(article)["title"]

                # Deduplicate by title
                if title and title not in seen_titles:
                    seen_titles.add(title)
                    all_news.append(article)

        if len(all_news) >= limit:
            break

    if not all_news:
        return f"No global news found for {curr_date}"

    # Calculate date range
    curr_dt = datetime.strptime(curr_date, "%Y-%m-%d")
    start_dt = curr_dt - relativedelta(days=look_back_days)
    start_date = start_dt.strftime("%Y-%m-%d")

    news_str = ""
    kept = 0
    for article in all_news[:limit]:
        # Extract uniformly (flat + nested) and apply the same look-ahead-safe
        # window filter, so flat articles can't leak future news (#1007).
        data = _extract_article_data(article)
        if not _in_news_window(data["pub_date"], start_dt, curr_dt):
            continue
        news_str += _render_article(data)
        kept += 1

    # All candidates fell outside the window -> say so rather than return an
    # empty-bodied report (#993).
    if kept == 0:
        return f"No global news found between {start_date} and {curr_date}"

    return f"## Global Market News, from {start_date} to {curr_date}:\n\n{news_str}"
