import json
import logging
from datetime import date, datetime

from .alpha_vantage_common import (
    _AV_ENVELOPE_KEYS,
    _FRESHNESS_NOTE_KEY,
    _carries_payload,
    _make_api_request,
    _newest_row_date,
    _parsed_payload,
    _served_body,
    _with_freshness_note,
)
from .errors import NoMarketDataError
from .utils import data_lag_note, live_snapshot_note, statement_lag_bound

logger = logging.getLogger(__name__)

# Error contract shared by all four public getters in this module. None of them
# answers only with data:
#
#   raises  NoMarketDataError            the vendor served nothing usable for the
#                                        symbol (empty payload, no report of the
#                                        requested cadence within the
#                                        point-in-time bound, or a report list of
#                                        the wrong shape). route_to_vendor turns
#                                        this into its NO_DATA_AVAILABLE sentinel
#                                        after trying the rest of the chain.
#   raises  AlphaVantageRateLimitError / AlphaVantageNotConfiguredError /
#           NoMarketDataError            propagated from _make_api_request (an
#                                        HTTP 429 or throttle notice, a bad key,
#                                        or an "Error Message" rejection
#                                        envelope, #68/#72), which also lets
#                                        requests' own HTTP errors out.
#   returns INVALID_CURR_DATE            a curr_date was supplied but is not a
#                                        usable date, so nothing can be bounded
#                                        to a point in time (a string, not a
#                                        raise: fundamental_data is a core
#                                        category, and the LLM can retry).
#   returns the vendor body              a prose rejection, or an
#                                        Information/Note body whose text
#                                        matched none of _make_api_request's
#                                        classification patterns.

# The freshness-note carrier and its shared helpers (_FRESHNESS_NOTE_KEY,
# _parsed_payload, _served_body, _with_freshness_note, _carries_payload) live
# in alpha_vantage_common so every Alpha Vantage module annotates the same way
# (#69); this module re-imports them above.


def _carries_anything(parsed: dict) -> bool:
    """True when a parsed body has content of its own.

    The freshness key does not count: a body whose only key is one this module
    writes carries nothing the vendor served, and serving it would render as an
    empty object once the key is stripped.
    """
    return bool(parsed.keys() - {_FRESHNESS_NOTE_KEY})


# The statement tools Alpha Vantage serves, mapped to the name a freshness note
# or a no-data reason calls that statement. One table so an endpoint and its
# wording cannot drift apart across the three getters below.
_STATEMENT_LABELS = {
    "BALANCE_SHEET": "balance sheet",
    "CASH_FLOW": "cash flow",
    "INCOME_STATEMENT": "income statement",
}

# Every report list a statement response can carry. The served body keeps only
# the requested cadence's list, so these are stripped before it is rebuilt.
_REPORT_LIST_KEYS = ("annualReports", "quarterlyReports")


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
    future data. In production that raise is unreachable — ``_filter_response_json``
    validates curr_date and answers ``INVALID_CURR_DATE`` before delegating here,
    and it also handles ``None``-curr_date and non-JSON bodies — so the raise
    stands as the invariant for anything calling this helper directly.
    """
    cutoff = _normalize_iso_date(curr_date)
    if cutoff is None:
        raise ValueError(
            f"Alpha Vantage fundamentals: curr_date {curr_date!r} is not a valid "
            f"YYYY-MM-DD date; refusing to serve reports unfiltered (look-ahead guard)"
        )
    for key in _REPORT_LIST_KEYS:
        rows = result.get(key)
        if isinstance(rows, list):
            # Row-shape guard, not defensive noise: a scalar or string row makes
            # ``r.get`` raise AttributeError, which no caller catches — the
            # router logs it and falls back, leaving this vendor quietly broken.
            # A row that is not a mapping cannot be dated, so it is dropped by
            # the same rule as an undated one.
            result[key] = [
                r
                for r in rows
                if isinstance(r, dict)
                and (ending := _normalize_iso_date(r.get("fiscalDateEnding"))) is not None
                and ending <= cutoff
            ]
    return result


def _statement_cadence(freq) -> str:
    """``"quarterly"`` or ``"annual"`` — the cadence a ``freq`` argument asks for.

    One normalization decides which report list is served AND which bound judges
    it (via :func:`statement_lag_bound`), so the two cannot answer a future freq
    spelling differently. Anything but an explicit "quarterly" resolves to
    annual, matching which frame the yfinance path fetches for the same freq.
    """
    return "quarterly" if isinstance(freq, str) and freq.lower() == "quarterly" else "annual"


def _statement_lag_note(reports: list, curr_date: str, cadence: str, label: str) -> str:
    """Data-lag note for the served report list, or ``""``.

    The newest ``fiscalDateEnding`` left after the look-ahead filter is the
    newest period the agent will see, so the note describes that row rather than
    the raw response's, and it is bounded by the same shared
    :func:`statement_lag_bound` the yfinance path uses (#58). Returns ``""``
    when no row carries a parseable period — an annotation degrades to silence
    rather than guessing. The shared reduction shape-guards each row, which
    matters here because the date-less fallback (#73) hands this raw vendor
    rows the filtered path's :func:`_filter_reports_by_date` has not vetted.
    """
    newest = _newest_row_date(reports, "fiscalDateEnding")
    if newest is None:
        return ""
    return data_lag_note(newest, curr_date, statement_lag_bound(cadence), f"{label} period")


def _invalid_curr_date(curr_date) -> str:
    """The sentinel served when a supplied curr_date is not a usable date.

    Loud to the LLM (it can retry with a valid date), leaks no data, and never
    raises: fundamental_data is a NON-optional category, so a ValueError escaping
    ``route_to_vendor`` (``raise first_error``) would crash the ToolNode-wrapped
    graph run, unlike the optional farside/F&G vendors whose raise degrades to a
    sentinel.
    """
    return (
        f"INVALID_CURR_DATE: curr_date {curr_date!r} is not a valid yyyy-mm-dd "
        f"date, so fundamentals cannot be bounded to a point in time. No data "
        f"returned; retry with a valid yyyy-mm-dd date. Do not fabricate values."
    )


def _filter_response_json(result, curr_date, freq, label, symbol):
    """Look-ahead-filter a raw ``_make_api_request`` fundamentals response.

    ``_make_api_request`` returns the response *text* (a JSON string), never a
    parsed dict, so the reports must be parsed before the filter can see them — an
    earlier version type-checked ``isinstance(result, dict)`` on this always-str
    value, so the guard silently never fired in production. A non-JSON body (an
    error/notice page) is served as it arrived, bar a vendor-supplied freshness
    key — and bar the empty-payload raise below, which fires before any of
    those checks because "this symbol has nothing" is true regardless of the
    analysis date.

    A ``None`` curr_date (the model omitted it) leaves the body unfiltered and
    both report lists intact — there is no point-in-time bound, and pretending
    to filter without one would fake look-ahead protection (#73). The freshness
    disclosure needs only a reference date, so it falls back to the wall clock
    (the insider path's design) instead of switching off with the filter; the
    degraded mode is also logged, because both protections used to vanish
    silently.

    ``freq`` and ``label`` are the caller's statement identity; the served reports
    carry a freshness note built from them (see :func:`_statement_lag_note`).

    Alpha Vantage answers every statement call with BOTH ``annualReports`` and
    ``quarterlyReports``, so the requested cadence's list is the one served and
    the other is dropped: the yfinance path fetches only the requested frame, and
    shipping a second, unjudged list would hand the agent (say) a seven-year-old
    annual balance sheet with no disclosure attached to it.

    Raises:
        NoMarketDataError: when the vendor has no fundamentals for the symbol at
            all (an empty payload), none of the requested cadence within the
            point-in-time bound, or a report list of the wrong shape. The yfinance path raises on the same inputs, so
            the router opens its no-data lane for either vendor rather than
            serving an empty body as a successful report.
    """
    parsed = _parsed_payload(result)
    if parsed is not None and not _carries_anything(parsed):
        # Alpha Vantage answers an unknown symbol with "{}"; a body of nothing
        # but a vendor-written freshness key is the same emptiness wearing our
        # disclosure's name, and the strip would serve it as "{}".
        raise NoMarketDataError(symbol, detail="Alpha Vantage returned an empty payload")
    if parsed is None or not _carries_payload(parsed):
        # A prose body, or an envelope the request boundary classifies but does
        # not raise on (an unmatched Information/Note, see #68) — served as it
        # arrived, bar a vendor-supplied freshness key. Checked before the
        # curr_date handling because the vendor's reason for having no data
        # outranks anything we could say about the analysis date.
        return _served_body(result, parsed)
    if curr_date is None:
        # Date-less fallback (#73): no filtering (see the docstring), but the
        # requested cadence's newest period is still judged — against today,
        # the only reference date left. Only the requested list is judged; the
        # other cadence's list rides along unfiltered and undisclosed. The
        # filtered path refuses exactly that, but here "unfiltered means
        # unfiltered" wins (user-decided), and the analyst prompt steers the
        # model out of this date-less mode entirely.
        logger.warning(
            "Alpha Vantage %s for %s called without curr_date: look-ahead "
            "filtering is off; freshness is judged against today instead",
            label,
            symbol,
        )
        cadence = _statement_cadence(freq)
        reports = parsed.get(f"{cadence}Reports")
        note = (
            _statement_lag_note(reports, date.today().strftime("%Y-%m-%d"), cadence, label)
            if isinstance(reports, list)
            else ""
        )
        return _with_freshness_note(parsed, note) if note else _served_body(result, parsed)
    if _normalize_iso_date(curr_date) is None:
        return _invalid_curr_date(curr_date)
    cadence = _statement_cadence(freq)
    supplied = parsed.get(f"{cadence}Reports")
    supplied = supplied if isinstance(supplied, list) else []
    undatable = sum(
        1
        for r in supplied
        if not isinstance(r, dict) or _normalize_iso_date(r.get("fiscalDateEnding")) is None
    )
    filtered = _filter_reports_by_date(parsed, curr_date)
    reports = filtered.get(f"{cadence}Reports")
    if reports is not None and not isinstance(reports, list):
        # Present but wrong-shaped is a vendor schema break, not an uncovered
        # symbol; say so rather than letting the two look identical in the log
        # and in the router's no-data sentinel. An absent key — or an explicit
        # null, which carries no more shape information than an absent one — is
        # not this case: it means the vendor served no such list, which the
        # no-reports branch below reports (with any notice that rode along).
        raise NoMarketDataError(
            symbol,
            detail=f"{cadence}Reports is {type(reports).__name__}, not a list of reports",
        )
    reports = reports or []
    if not reports:
        # Three different situations reach the agent as the same
        # NO_DATA_AVAILABLE sentinel, so the reason has to separate them.
        # A wholly undatable list is the only one that indicates a fault: a
        # schema rename or a nulled date field would otherwise report every
        # ticker as an uncovered symbol, with the analyst writing its report
        # around that verdict — so that case, and only that case, is also
        # logged. If even one row was datable, coverage explains the outcome:
        # dropping rows that postdate curr_date is correct point-in-time
        # behaviour on any backtest older than the vendor's window, and must
        # not page anyone.
        notices = sorted(_AV_ENVELOPE_KEYS & parsed.keys())
        because = f"; vendor body also carried {', '.join(notices)}" if notices else ""
        if supplied and undatable == len(supplied):
            detail = (
                f"all {undatable} {cadence} {label} reports carried no usable "
                f"fiscalDateEnding{because}"
            )
            logger.warning(
                "Alpha Vantage %s: all %d %s reports for %s were undatable at %s",
                label,
                undatable,
                cadence,
                symbol,
                curr_date,
            )
        elif supplied:
            # Some rows WERE datable and still did not survive, so coverage — not
            # a fault — explains the outcome; any undatable minority is reported
            # without being blamed, and without paging anyone.
            partly = (
                f" ({undatable} of them carried no usable fiscalDateEnding)" if undatable else ""
            )
            detail = (
                f"{len(supplied)} {cadence} {label} reports returned, none on or before "
                f"{curr_date}{partly}{because}"
            )
        else:
            detail = f"no {cadence} {label} reports on or before {curr_date}{because}"
        raise NoMarketDataError(symbol, detail=detail)
    body = {
        k: v for k, v in filtered.items() if k not in _REPORT_LIST_KEYS and k != _FRESHNESS_NOTE_KEY
    }
    body[f"{cadence}Reports"] = reports
    note = _statement_lag_note(reports, curr_date, cadence, label)
    return _with_freshness_note(body, note) if note else json.dumps(body, indent=2)


def _annotate_live_snapshot(result, curr_date, symbol):
    """Disclose that an OVERVIEW payload is today's state, not ``curr_date``'s.

    OVERVIEW carries current-state ratios with no historical form — the same
    live-only shape as yfinance's ``info`` — so when the analysis date sits
    behind the wall clock (a backtest), today's market cap and P/E would
    otherwise read as that date's (#30). The same tool routes to either vendor,
    so the disclosure cannot depend on which one ``data_vendors`` picked (#58).

    Serves the body as it arrived (bar a vendor-supplied freshness key) when
    there is nothing to disclose, when it is not a JSON object, or when the
    object is an envelope of nothing but notice keys — since #68 the request
    boundary raises on an ``Error Message`` body, so what still reaches here is
    an Information/Note whose text ``_make_api_request`` classified but did not
    raise on. Annotating one of those would assert that fundamentals were
    fetched when none were.

    An unparseable curr_date takes the same ``INVALID_CURR_DATE`` route as the
    statement tools, and at the same depth: with no usable analysis date this
    path cannot tell a backtest from live trading, and staying silent would serve
    today's ratios undisclosed — the exact failure the disclosure exists to
    prevent. A body the vendor refused to fill still outranks it, so a prose or
    envelope answer is served rather than replaced by the date complaint.

    Raises:
        NoMarketDataError: on an empty payload, which is how Alpha Vantage
            answers an unknown symbol (the yfinance path raises on its own
            equivalent, so the router's no-data lane opens for either vendor).
    """
    parsed = _parsed_payload(result)
    if parsed is not None and not _carries_anything(parsed):
        raise NoMarketDataError(symbol, detail="Alpha Vantage returned an empty OVERVIEW payload")
    if curr_date is None or parsed is None or not _carries_payload(parsed):
        # Same order as the statement path: a prose body or a failure envelope is
        # served as-is even when curr_date is unusable, because the vendor's
        # reason for having no data outranks ours for not being able to bound it.
        # A None curr_date needs no wall-clock fallback here (#73): this
        # disclosure only ever says "today's values are not curr_date's", which
        # is vacuous when the reference date IS today.
        return _served_body(result, parsed)
    if _normalize_iso_date(curr_date) is None:
        return _invalid_curr_date(curr_date)
    note = live_snapshot_note(curr_date, "these fundamentals are")
    if not note:
        return _served_body(result, parsed)
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
        str: Company overview data including financial ratios and key metrics,
            carrying a live-snapshot disclosure when curr_date sits behind the
            wall clock. In place of that it may return the vendor's own prose or
            failure-envelope body, or the ``INVALID_CURR_DATE`` sentinel when a
            supplied curr_date is not a usable date. See the module's error
            contract note for what every fundamentals getter can raise.
    """
    params = {
        "symbol": ticker,
    }

    return _annotate_live_snapshot(_make_api_request("OVERVIEW", params), curr_date, ticker)


def _get_statement(function_name: str, ticker: str, freq: str, curr_date: str | None) -> str:
    """Fetch one financial statement.

    With a curr_date it is look-ahead-filtered, narrowed to the requested
    cadence, and freshness-noted; without one the body is served unfiltered
    with both report lists, but the freshness note survives on a wall-clock
    reference (#73). An empty payload raises either way, since an unknown
    symbol is unknown with or without an analysis date.
    """
    result = _make_api_request(function_name, {"symbol": ticker})
    return _filter_response_json(result, curr_date, freq, _STATEMENT_LABELS[function_name], ticker)


def get_balance_sheet(ticker: str, freq: str = "quarterly", curr_date: str = None):
    """Retrieve balance sheet data for a given ticker symbol using Alpha Vantage.

    Serves the requested cadence's reports; see the module's error contract note
    for what it raises and what it returns in place of data.
    """
    return _get_statement("BALANCE_SHEET", ticker, freq, curr_date)


def get_cashflow(ticker: str, freq: str = "quarterly", curr_date: str = None):
    """Retrieve cash flow data for a given ticker symbol using Alpha Vantage.

    Serves the requested cadence's reports; see the module's error contract note
    for what it raises and what it returns in place of data.
    """
    return _get_statement("CASH_FLOW", ticker, freq, curr_date)


def get_income_statement(ticker: str, freq: str = "quarterly", curr_date: str = None):
    """Retrieve income statement data for a given ticker symbol using Alpha Vantage.

    Serves the requested cadence's reports; see the module's error contract note
    for what it raises and what it returns in place of data.
    """
    return _get_statement("INCOME_STATEMENT", ticker, freq, curr_date)
