import json
import logging
from datetime import datetime

from .alpha_vantage_common import _make_api_request
from .utils import data_lag_note, live_snapshot_note, statement_lag_bound

logger = logging.getLogger(__name__)

# Where a freshness disclosure lives in an Alpha Vantage payload. This vendor
# answers in JSON (yfinance answers in CSV with "# " header lines), so the note
# is carried as a key rather than a prefixed line: the body stays parseable, and
# a leading underscore cannot collide with an Alpha Vantage field name. Written
# first so a disclosure is not buried under a long report list (#58).
_FRESHNESS_NOTE_KEY = "_freshness_note"

# Keys Alpha Vantage answers with when it is reporting a problem rather than
# serving data. A body made only of these is a failure envelope, not a payload.
# Deliberately wider than the notices ``_make_api_request`` classifies into the
# error taxonomy (it reads only Information/Note, and only to recognise a rate
# limit or a bad key): anything left here reached this module as a body, and
# whether it is a *classified* failure has no bearing on whether it contains
# fundamentals to disclose about.
_AV_ENVELOPE_KEYS = {"Error Message", "Information", "Note"}

# The statement tools Alpha Vantage serves, mapped to the phrase a freshness
# note uses for that statement's fiscal period. One table so an endpoint and
# its wording cannot drift apart across the three getters below.
_STATEMENT_PERIOD_PHRASES = {
    "BALANCE_SHEET": "balance sheet period",
    "CASH_FLOW": "cash flow period",
    "INCOME_STATEMENT": "income statement period",
}


def _parsed_payload(result) -> dict | None:
    """The response body as a JSON object, or ``None`` when it is not one.

    One decoder for both annotation paths: ``_make_api_request`` returns response
    *text*, which is a JSON object for every fundamentals endpoint but plain
    prose for a rejection page. Whatever "decodable as a payload" means has to
    mean the same thing to the look-ahead filter and to the freshness
    disclosures, so it is decided here rather than restated in each.
    """
    if not isinstance(result, str):
        return None
    try:
        parsed = json.loads(result)
    except ValueError:
        return None  # not JSON (an error/plain-text body)
    return parsed if isinstance(parsed, dict) else None


def _with_freshness_note(payload: dict, note: str) -> str:
    """Render ``payload`` with its freshness note first, as JSON text."""
    return json.dumps({_FRESHNESS_NOTE_KEY: note, **payload}, indent=2)


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


def _statement_lag_note(payload: dict, curr_date: str, freq, what: str) -> str:
    """Data-lag note for the report list this call's ``freq`` selects, or ``""``.

    Alpha Vantage returns BOTH ``annualReports`` and ``quarterlyReports`` on
    every statement call, so the note has to pick one: the quarterly list for a
    quarterly request, the annual list otherwise — which is exactly the frame
    the yfinance path fetches for the same freq, and it is bounded by the same
    shared :func:`statement_lag_bound` (#58). The newest ``fiscalDateEnding``
    left after the look-ahead filter is the newest period the agent will see,
    so the note describes that row, not the raw response's.

    The phrase names the cadence it judged ("the newest annual balance sheet
    period ..."). Unlike the yfinance path, which renders only the frame it
    fetched, this payload still carries the OTHER list — an unqualified note
    would read as a verdict on a statement it never looked at, e.g. calling a
    freshly filed quarterly stale because the annual list is old.

    Returns ``""`` when the selected list is missing, empty, or has no parseable
    period — an annotation degrades to silence rather than guessing.
    """
    cadence = "quarterly" if isinstance(freq, str) and freq.lower() == "quarterly" else "annual"
    reports = payload.get(f"{cadence}Reports")
    if not isinstance(reports, list):
        return ""
    endings = [
        ending
        for r in reports
        if (ending := _normalize_iso_date(r.get("fiscalDateEnding"))) is not None
    ]
    if not endings:
        return ""
    # Bound looked up from the resolved cadence, not the raw freq: one
    # normalization decides both which list is read and which bound applies, so
    # they cannot answer a future freq spelling differently.
    return data_lag_note(max(endings), curr_date, statement_lag_bound(cadence), f"{cadence} {what}")


def _filter_response_json(result, curr_date, freq, what):
    """Look-ahead-filter a raw ``_make_api_request`` fundamentals response.

    ``_make_api_request`` returns the response *text* (a JSON string), never a
    parsed dict, so the reports must be parsed before the filter can see them — an
    earlier version type-checked ``isinstance(result, dict)`` on this always-str
    value, so the guard silently never fired in production. A ``None`` curr_date
    (no point-in-time bound) or a non-JSON body (an error/notice page) is returned
    untouched.

    ``freq`` and ``what`` are the caller's statement identity; the surviving
    reports carry a freshness note built from them (see
    :func:`_statement_lag_note`).

    A present-but-unparseable curr_date makes ``_filter_reports_by_date`` raise;
    that is caught here and turned into a tool-friendly ``INVALID_CURR_DATE``
    string rather than propagated. fundamental_data is a NON-optional category, so
    a raised ValueError would escape ``route_to_vendor`` (``raise first_error``)
    and crash the ToolNode-wrapped graph run — unlike the optional farside/F&G
    vendors, whose raise degrades to a sentinel. The string is loud to the LLM (it
    can retry with a valid date), leaks no future data, and never aborts the run.
    """
    parsed = _parsed_payload(result) if curr_date else None
    if parsed is None:
        return result
    try:
        filtered = _filter_reports_by_date(parsed, curr_date)
    except ValueError:
        return (
            f"INVALID_CURR_DATE: curr_date {curr_date!r} is not a valid yyyy-mm-dd "
            f"date, so fundamentals cannot be bounded to a point in time. No data "
            f"returned; retry with a valid yyyy-mm-dd date. Do not fabricate values."
        )
    note = _statement_lag_note(filtered, curr_date, freq, what)
    return _with_freshness_note(filtered, note) if note else json.dumps(filtered, indent=2)


def _annotate_live_snapshot(result, curr_date):
    """Disclose that an OVERVIEW payload is today's state, not ``curr_date``'s.

    OVERVIEW carries current-state ratios with no historical form — the same
    live-only shape as yfinance's ``info`` — so when the analysis date sits
    behind the wall clock (a backtest), today's market cap and P/E would
    otherwise read as that date's (#30). The same tool routes to either vendor,
    so the disclosure cannot depend on which one ``data_vendors`` picked (#58).

    Returns the body untouched when there is nothing to disclose, when it is not
    a JSON object, or when the object carries no fundamentals to disclose about:
    an unknown symbol answers ``{}`` and a rejected call answers an envelope of
    nothing but ``Error Message`` / ``Information`` / ``Note`` (the notices
    ``_make_api_request`` does not already classify into the taxonomy).
    Annotating either would assert that fundamentals were fetched when none
    were.
    """
    if not curr_date:
        return result
    note = live_snapshot_note(curr_date, "these fundamentals are")
    if not note:
        return result
    parsed = _parsed_payload(result)
    if parsed is None or not parsed.keys() - _AV_ENVELOPE_KEYS:
        return result
    return _with_freshness_note(parsed, note)


def get_fundamentals(ticker: str, curr_date: str = None) -> str:
    """
    Retrieve comprehensive fundamental data for a given ticker symbol using Alpha Vantage.

    Args:
        ticker (str): Ticker symbol of the company
        curr_date (str): Current date you are trading at, yyyy-mm-dd. Alpha
            Vantage has no historical OVERVIEW, so this does not bound the
            data — it decides whether the report discloses that the values are
            live as of the fetch.

    Returns:
        str: Company overview data including financial ratios and key metrics
    """
    params = {
        "symbol": ticker,
    }

    return _annotate_live_snapshot(_make_api_request("OVERVIEW", params), curr_date)


def _get_statement(function_name: str, ticker: str, freq: str, curr_date: str | None) -> str:
    """Fetch one financial statement, look-ahead-filtered and freshness-noted."""
    result = _make_api_request(function_name, {"symbol": ticker})
    return _filter_response_json(result, curr_date, freq, _STATEMENT_PERIOD_PHRASES[function_name])


def get_balance_sheet(ticker: str, freq: str = "quarterly", curr_date: str = None):
    """Retrieve balance sheet data for a given ticker symbol using Alpha Vantage."""
    return _get_statement("BALANCE_SHEET", ticker, freq, curr_date)


def get_cashflow(ticker: str, freq: str = "quarterly", curr_date: str = None):
    """Retrieve cash flow statement data for a given ticker symbol using Alpha Vantage."""
    return _get_statement("CASH_FLOW", ticker, freq, curr_date)


def get_income_statement(ticker: str, freq: str = "quarterly", curr_date: str = None):
    """Retrieve income statement data for a given ticker symbol using Alpha Vantage."""
    return _get_statement("INCOME_STATEMENT", ticker, freq, curr_date)
