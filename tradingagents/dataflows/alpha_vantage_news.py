from .alpha_vantage_common import _make_api_request, format_datetime_for_api

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

    return _make_api_request("NEWS_SENTIMENT", params)


def get_insider_transactions(symbol: str) -> dict[str, str] | str:
    """Returns latest and historical insider transactions by key stakeholders.

    Covers transactions by founders, executives, board members, etc.

    Args:
        symbol: Ticker symbol. Example: "IBM".

    Returns:
        Dictionary containing insider transaction data or JSON string.
    """

    params = {
        "symbol": symbol,
    }

    return _make_api_request("INSIDER_TRANSACTIONS", params)
