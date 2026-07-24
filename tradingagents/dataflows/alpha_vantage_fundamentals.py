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


def _filter_reports_by_date(result, curr_date: str):
    """Filter annualReports/quarterlyReports to exclude entries after curr_date.

    Prevents look-ahead bias by removing fiscal periods that end after the
    simulation's current date. A report whose ``fiscalDateEnding`` is missing or
    unparseable cannot be proven to be in the past, so it is dropped rather than
    silently kept — an undated row must not leak into a point-in-time view.
    """
    if not curr_date or not isinstance(result, dict):
        return result
    cutoff = _normalize_iso_date(curr_date)
    if cutoff is None:
        # An unparseable curr_date cannot bound anything; leave the data untouched
        # (matching the no-curr_date path above) but say so, rather than filtering
        # on a raw string that compares arbitrarily against ISO dates.
        logger.warning(
            "Alpha Vantage fundamentals: curr_date %r is not YYYY-MM-DD; "
            "skipping the look-ahead filter",
            curr_date,
        )
        return result
    for key in ("annualReports", "quarterlyReports"):
        if key in result:
            result[key] = [
                r
                for r in result[key]
                if (ending := _normalize_iso_date(r.get("fiscalDateEnding"))) is not None
                and ending <= cutoff
            ]
    return result


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
    return _filter_reports_by_date(result, curr_date)


def get_cashflow(ticker: str, freq: str = "quarterly", curr_date: str = None):
    """Retrieve cash flow statement data for a given ticker symbol using Alpha Vantage."""
    result = _make_api_request("CASH_FLOW", {"symbol": ticker})
    return _filter_reports_by_date(result, curr_date)


def get_income_statement(ticker: str, freq: str = "quarterly", curr_date: str = None):
    """Retrieve income statement data for a given ticker symbol using Alpha Vantage."""
    result = _make_api_request("INCOME_STATEMENT", {"symbol": ticker})
    return _filter_reports_by_date(result, curr_date)

