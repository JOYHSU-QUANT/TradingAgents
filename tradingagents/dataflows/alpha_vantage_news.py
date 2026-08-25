from datetime import datetime

from .alpha_vantage_common import (
    _AV_ENVELOPE_KEYS,
    _carries_payload,
    _make_api_request,
    _newest_row_date,
    _parsed_payload,
    _served_body,
    _with_freshness_note,
    format_datetime_for_api,
)
from .config import get_config
from .utils import MAX_INSIDER_LAG_DAYS, data_lag_note, date_range_refusal, date_refusal

# Clamp untrusted request sizes before they parameterize an external call
# (#33): an LLM-supplied or misconfigured value must not turn into an
# unbounded lookback window or article count.
MAX_NEWS_LIMIT = 1000  # Alpha Vantage NEWS_SENTIMENT hard maximum
MAX_NEWS_LOOKBACK_DAYS = 365


def _news_body(result, empty_answer: str) -> str:
    """The news body as served, with an empty feed answered in prose (#90).

    Two jobs, both mirroring what ``_annotate_insider_freshness`` does for the
    third getter in this module:

    * An empty ``feed`` answers in the yfinance sibling's voice rather than as
      empty JSON. Alpha Vantage filters ``NEWS_SENTIMENT`` server-side by
      ``time_from``/``time_to``, so an empty feed means "nothing in the window
      you asked for" — which is what ``empty_answer`` says, and what the other
      vendor serving the same routed tool already said. Cross-vendor tests pin
      the two sentences equal.
    * A vendor-written ``_freshness_note`` is dropped on every served path.
      These getters attach no disclosure of their own, so such a key would
      reach the agent looking like a system-issued freshness statement with
      nothing beside it to contradict it — the same hole PR #88 closed for the
      insider path.

    The empty-feed verdict reads ``feed`` alone: the documented companion
    ``items`` count plays no part, so a body carrying one, the other, or a
    differently spelled count behaves the same. Anything this cannot read as an
    affirmed empty window — a non-JSON body, a failure envelope, a ``feed``
    that is not a list, or a body with no ``feed`` at all — is served as it
    arrived, bar the vendor-written note key above, which is dropped from any
    of those that parses as an object (a non-JSON body has nothing to drop from
    and comes back byte-identical). An empty feed riding next to an
    unclassified Information/Note also passes through — there the emptiness may
    be the notice's side effect, and the prose would discard the vendor's own
    explanation.
    """
    parsed = _parsed_payload(result)
    if parsed is None or not _carries_payload(parsed):
        return _served_body(result, parsed)
    feed = parsed.get("feed")
    if isinstance(feed, list) and not feed and not (_AV_ENVELOPE_KEYS & parsed.keys()):
        return empty_answer
    return _served_body(result, parsed)


def get_news(ticker, start_date, end_date) -> str:
    """Returns live and historical market news & sentiment data from premier news outlets worldwide.

    Covers stocks, cryptocurrencies, forex, and topics like fiscal policy, mergers & acquisitions, IPOs.

    Args:
        ticker: Stock symbol for news articles.
        start_date: Start date for news search.
        end_date: End date for news search.

    Returns:
        The vendor's JSON body as text, minus any ``_freshness_note`` key it
        supplied — or the shared no-news prose when the feed comes back empty
        (see ``_news_body``).

    The article count comes from ``news_article_limit``, the same key the
    yfinance sibling sizes its fetch with: how much news a routed tool ASKS FOR
    must not depend on which vendor ``data_vendors`` selected (#107). Sending no
    ``limit`` left this getter on the endpoint's own default of 50 against that
    key's 20. How much comes BACK can still differ — this vendor filters the
    window server-side while the sibling fetches that many and filters after —
    and each clamps the request to its own endpoint ceiling.

    An unusable start or end date answers the shared sentinel before any
    request, as the yfinance sibling does (#111). That gate is the strict
    ``yyyy-mm-dd`` rule, so the intraday and ``datetime`` forms
    ``format_datetime_for_api`` also reads no longer reach it from here.
    """
    if (refusal := date_range_refusal(start_date, end_date, what="news")) is not None:
        return refusal

    limit = max(1, min(int(get_config()["news_article_limit"]), MAX_NEWS_LIMIT))
    params = {
        "tickers": ticker,
        "time_from": format_datetime_for_api(start_date),
        "time_to": format_datetime_for_api(end_date),
        "limit": str(limit),
    }

    return _news_body(
        _make_api_request("NEWS_SENTIMENT", params),
        # Keep this sentence in lockstep with the yfinance getter's
        # nothing-in-window answer — a cross-vendor test pins the two equal for
        # the canonical spelling. This vendor names the symbol it actually
        # queried (raw, not normalized): echoing a spelling it never sent would
        # misattribute the emptiness.
        f"No news found for {ticker} between {start_date} and {end_date}",
    )


def get_global_news(curr_date, look_back_days: int | None = None, limit: int | None = None) -> str:
    """Returns global market news & sentiment data without ticker-specific filtering.

    Covers broad market topics like financial markets, economy, and more.

    Args:
        curr_date: Current date in yyyy-mm-dd format.
        look_back_days: Number of days to look back; ``None`` falls back to
            ``global_news_lookback_days`` from the active config (the tool
            wrapper forwards omitted optionals as explicit ``None``).
        limit: Maximum number of articles; ``None`` falls back to
            ``global_news_article_limit`` from the active config.

    Returns:
        The vendor's JSON body as text, minus any ``_freshness_note`` key it
        supplied — or the shared no-news prose when the feed comes back empty
        (see ``_news_body``).

    Both defaults read the same config keys as the yfinance sibling. They used
    to be literals here (7 and 50), so ``global_news_article_limit`` had no
    effect on this vendor at all and a routed call returned five times the
    articles its sibling would — the tool wrapper documents those keys as where
    the defaults come from, which was true of only one of the two vendors
    serving it (#107).

    An unusable curr_date answers the shared ``INVALID_CURR_DATE`` sentinel
    before any request, as the yfinance sibling does (#111).
    """
    from datetime import datetime, timedelta

    refusal = date_refusal(curr_date, what="global news", kind="point")
    if refusal is not None:
        return refusal

    # The tool wrapper forwards omitted optionals as explicit None through the
    # router (see news_data_tools), so resolve them to the configured defaults
    # BEFORE the int() clamp — mirroring the yfinance sibling. Without this,
    # int(None) raises a bare TypeError outside the vendor-error taxonomy.
    config = get_config()
    if look_back_days is None:
        look_back_days = config["global_news_lookback_days"]
    if limit is None:
        limit = config["global_news_article_limit"]
    look_back_days = max(1, min(int(look_back_days), MAX_NEWS_LOOKBACK_DAYS))
    limit = max(1, min(int(limit), MAX_NEWS_LIMIT))

    # Calculate start date
    curr_dt = datetime.strptime(curr_date, "%Y-%m-%d")
    start_dt = curr_dt - timedelta(days=look_back_days)
    start_date = start_dt.strftime("%Y-%m-%d")

    params = {
        "topics": "financial_markets,economy_macro,economy_monetary",
        "time_from": format_datetime_for_api(start_date),
        "time_to": format_datetime_for_api(curr_date),
        "limit": str(limit),
    }

    # This request carries no symbol/tickers, so name the subject a rejection
    # is attributed to — the fallback would present the function name as a
    # tradable symbol in the router's no-data sentinel.
    return _news_body(
        _make_api_request("NEWS_SENTIMENT", params, subject="global market news"),
        # The yfinance sibling's nothing-in-window sentence, pinned equal by a
        # cross-vendor test. Both vendors resolve an omitted lookback from the
        # same config key, so the two windows named here agree unless a caller
        # overrides one.
        f"No global news found between {start_date} and {curr_date}",
    )


def _annotate_insider_freshness(result, symbol: str) -> str:
    """Attach a data-lag note when the newest insider filing is stale (#69).

    The yfinance vendor serving the same routed tool flags a long-dead filing
    stream; without this, the tool's honesty depended on which vendor
    ``data_vendors`` selected. Same design as that path: the reference date is
    the wall clock (no curr_date reaches an insider call) and the bound is the
    shared ``MAX_INSIDER_LAG_DAYS``. The response body is
    ``{"data": [{"transaction_date": "yyyy-mm-dd", ...}, ...]}`` (confirmed
    against the live endpoint); the note rides in the family's
    ``_freshness_note`` key so the body stays parseable JSON.

    An empty ``data`` list answers in the yfinance vendor's voice — the same
    "no insider transactions reported" prose — because an empty stream is
    normal for insiders and the two vendors must say so the same way. Only an
    empty list with no notice key beside it takes that exit: an empty ``{}``
    or an envelope body keeps its own passthrough, and so does an empty list
    riding next to an unclassified Information/Note — there the emptiness may
    be the notice's side effect, and the prose would discard the vendor's own
    explanation.

    A non-JSON body, a failure envelope, or a body without a parseable filing
    date is served as it arrived, bar a vendor-supplied freshness key — an
    annotation degrades to silence rather than guessing (and rather than
    dressing an error body in a freshness disclosure, #68).
    """
    parsed = _parsed_payload(result)
    if parsed is None or not _carries_payload(parsed):
        return _served_body(result, parsed)
    rows = parsed.get("data")
    if not isinstance(rows, list):
        return _served_body(result, parsed)
    if not rows and not (_AV_ENVELOPE_KEYS & parsed.keys()):
        # Keep this sentence in lockstep with the yfinance getter's empty-frame
        # answer — a cross-vendor test pins the two equal for the canonical
        # spelling. This vendor names the symbol it actually queried (raw, not
        # normalized): echoing a spelling it never sent would misattribute the
        # emptiness.
        return f"No insider transactions reported for symbol '{symbol}'"
    latest = _newest_row_date(rows, "transaction_date")
    if latest is None:
        return _served_body(result, parsed)
    note = data_lag_note(
        latest,
        datetime.now().strftime("%Y-%m-%d"),
        MAX_INSIDER_LAG_DAYS,
        "insider filing",
    )
    return _with_freshness_note(parsed, note) if note else _served_body(result, parsed)


def get_insider_transactions(symbol: str) -> str:
    """Returns latest and historical insider transactions by key stakeholders.

    Covers transactions by founders, executives, board members, etc.

    Args:
        symbol: Ticker symbol. Example: "IBM".

    Returns:
        JSON string of insider transaction data, carrying the family's
        ``_freshness_note`` key when the newest filing trails the wall clock
        by more than ``MAX_INSIDER_LAG_DAYS`` (#69) — or the shared
        no-transactions prose when the vendor answers an empty list with no
        notice key beside it.
    """

    params = {
        "symbol": symbol,
    }

    return _annotate_insider_freshness(_make_api_request("INSIDER_TRANSACTIONS", params), symbol)
