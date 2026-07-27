import json
import logging
from datetime import datetime

from .alpha_vantage_common import _make_api_request

logger = logging.getLogger(__name__)


def _normalize_iso_date(value) -> str | None:
    """Canonical ``YYYY-MM-DD`` for a date string, or None if it is not a date.

    ``strptime`` accepts non-zero-padded input ("2026-6-5"), which then compares
    WRONG lexically against Alpha Vantage's zero-padded ``fiscalDateEnding``
    values — ``"2024-12-31" <= "2026-6-5"`` is True — so the raw string must be
    normalised before the look-ahead filter rather than compared as text.
    """
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return None


def _filter_reports_by_date(result: dict, curr_date: str) -> dict:
    """Drop annual/quarterly reports whose fiscalDateEnding is after curr_date.

    Prevents look-ahead bias by removing fiscal periods that end after the
    simulation's current date. A report whose ``fiscalDateEnding`` is missing or
    unparseable cannot be proven to be in the past, so it is dropped rather than
    silently kept — an undated row must not leak into a point-in-time view.

    A present-but-unparseable ``curr_date`` RAISES rather than returning the data
    unfiltered: this backs a core fundamentals tool, so a broken point-in-time
    bound must fail loud (like farside/fear_greed) instead of silently leaking
    future data. ``None``-curr_date and non-JSON bodies are handled by
    ``_filter_response_json`` before it delegates here with a parsed dict.
    """
    cutoff = _normalize_iso_date(curr_date)
    if cutoff is None:
        raise ValueError(
            f"Alpha Vantage fundamentals: curr_date {curr_date!r} is not a valid "
            f"YYYY-MM-DD date; refusing to serve reports unfiltered (look-ahead guard)"
        )
    for key in ("annualReports", "quarterlyReports"):
        if key in result:
            result[key] = [
                r
                for r in result[key]
                if (ending := _normalize_iso_date(r.get("fiscalDateEnding"))) is not None
                and ending <= cutoff
            ]
    return result


def _filter_response_json(result, curr_date):
    """Look-ahead-filter a raw ``_make_api_request`` fundamentals response.

    ``_make_api_request`` returns the response *text* (a JSON string), never a
    parsed dict, so the reports must be parsed before the filter can see them — an
    earlier version type-checked ``isinstance(result, dict)`` on this always-str
    value, so the guard silently never fired in production. A ``None`` curr_date
    (no point-in-time bound) or a non-JSON body (an error/notice page) is returned
    untouched; a present-but-unparseable curr_date raises via
    ``_filter_reports_by_date``.
    """
    if not curr_date or not isinstance(result, str):
        return result
    try:
        parsed = json.loads(result)
    except ValueError:
        return result  # not JSON (an error/plain-text body) — nothing to filter
    if not isinstance(parsed, dict):
        return result
    return json.dumps(_filter_reports_by_date(parsed, curr_date), indent=2)


def get_fundamentals(ticker: str, curr_date: str = None) -> str:
    """
    Retrieve comprehensive fundamental data for a given ticker symbol using Alpha Vantage.

    Args:
        ticker (str): Ticker symbol of the company
        curr_date (str): Current date you are trading at, yyyy-mm-dd (not used for Alpha Vantage)

    Returns:
        str: Company overview data including financial ratios and key metrics
    """
    params = {
        "symbol": ticker,
    }

    return _make_api_request("OVERVIEW", params)


def get_balance_sheet(ticker: str, freq: str = "quarterly", curr_date: str = None):
    """Retrieve balance sheet data for a given ticker symbol using Alpha Vantage."""
    result = _make_api_request("BALANCE_SHEET", {"symbol": ticker})
    return _filter_response_json(result, curr_date)


def get_cashflow(ticker: str, freq: str = "quarterly", curr_date: str = None):
    """Retrieve cash flow statement data for a given ticker symbol using Alpha Vantage."""
    result = _make_api_request("CASH_FLOW", {"symbol": ticker})
    return _filter_response_json(result, curr_date)


def get_income_statement(ticker: str, freq: str = "quarterly", curr_date: str = None):
    """Retrieve income statement data for a given ticker symbol using Alpha Vantage."""
    result = _make_api_request("INCOME_STATEMENT", {"symbol": ticker})
    return _filter_response_json(result, curr_date)

