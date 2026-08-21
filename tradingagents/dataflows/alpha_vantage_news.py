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
from .utils import MAX_INSIDER_LAG_DAYS, data_lag_note

# Clamp untrusted request sizes before they parameterize an external call
# (#33): an LLM-supplied or misconfigured value must not turn into an
# unbounded lookback window or article count.
MAX_NEWS_LIMIT = 1000  # Alpha Vantage NEWS_SENTIMENT hard maximum
MAX_NEWS_LOOKBACK_DAYS = 365


def get_news(ticker, start_date, end_date) -> dict[str, str] | str:
    """Returns live and historical market news & sentiment data from premier news outlets worldwide.

    Covers stocks, cryptocurrencies, forex, and topics like fiscal policy, mergers & acquisitions, IPOs.

    Args:
        ticker: Stock symbol for news articles.
        start_date: Start date for news search.
        end_date: End date for news search.

    Returns:
        Dictionary containing news sentiment data or JSON string.
    """

    params = {
        "tickers": ticker,
        "time_from": format_datetime_for_api(start_date),
        "time_to": format_datetime_for_api(end_date),
    }

    return _make_api_request("NEWS_SENTIMENT", params)


def get_global_news(curr_date, look_back_days: int = 7, limit: int = 50) -> dict[str, str] | str:
    """Returns global market news & sentiment data without ticker-specific filtering.

    Covers broad market topics like financial markets, economy, and more.

    Args:
        curr_date: Current date in yyyy-mm-dd format.
        look_back_days: Number of days to look back (default 7).
        limit: Maximum number of articles (default 50).

    Returns:
        Dictionary containing global news sentiment data or JSON string.
    """
    from datetime import datetime, timedelta

    # The tool wrapper forwards omitted optionals as explicit None through the
    # router (see news_data_tools), so resolve them to the documented defaults
    # BEFORE the int() clamp — mirroring the yfinance sibling. Without this,
    # int(None) raises a bare TypeError outside the vendor-error taxonomy.
    if look_back_days is None:
        look_back_days = 7
    if limit is None:
        limit = 50
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
    return _make_api_request("NEWS_SENTIMENT", params, subject="global market news")


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
