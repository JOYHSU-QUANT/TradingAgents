import json
import logging
from datetime import datetime

from .alpha_vantage_common import _make_api_request
from .errors import NoMarketDataError
from .utils import data_lag_note, live_snapshot_note, statement_lag_bound

logger = logging.getLogger(__name__)

# Where a freshness disclosure lives in an Alpha Vantage payload. This vendor
# answers in JSON (yfinance answers in CSV with "# " header lines), so the note
# is carried as a key rather than a prefixed line: the body stays parseable, and
# an underscore-prefixed name is not part of any Alpha Vantage schema this repo
# has seen. That last part is a convention, not a guarantee, so every path that
# serves a body drops a same-named key from it instead of trusting it to be
# absent — including the paths that attach no disclosure of their own, where a
# vendor-written note would stand unopposed. Written first so a disclosure is
# not buried under a long report list (#58).
_FRESHNESS_NOTE_KEY = "_freshness_note"

# Keys Alpha Vantage answers with when it is reporting a problem rather than
# serving data. A body made only of these is a failure envelope, not a payload.
# Deliberately wider than the notices ``_make_api_request`` classifies into the
# error taxonomy (it reads only Information/Note, and only to recognise a rate
# limit or a bad key): anything left here reached this module as a body, and
# whether it is a *classified* failure has no bearing on whether it contains
# fundamentals to disclose about.
_AV_ENVELOPE_KEYS = {"Error Message", "Information", "Note"}

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


def _parsed_payload(result) -> dict | None:
    """The response body as a JSON object, or ``None`` when it is not one.

    One decoder for both annotation paths: ``_make_api_request`` returns response
    *text*, and a fundamentals endpoint answers with a JSON object only when it
    served data — a rejection can arrive as prose instead. Whatever "decodable
    as a payload" means has to mean the same thing to the look-ahead filter and
    to the freshness disclosures, so it is decided here rather than restated in
    each.
    """
    if not isinstance(result, str):
        return None
    try:
        parsed = json.loads(result)
    except ValueError:
        return None  # not JSON (an error/plain-text body)
    return parsed if isinstance(parsed, dict) else None


def _served_body(result, parsed: dict | None) -> str:
    """The vendor body as served, minus any freshness key the vendor supplied.

    Every exit that hands the body over without a disclosure comes through here,
    because a vendor-written ``_freshness_note`` reaches the agent looking like a
    system-issued freshness statement — and on these paths there is no real note
    beside it to contradict it. Returns ``result`` untouched when there is
    nothing to strip, so the ordinary no-disclosure answer stays byte-identical
    to the vendor's own text.
    """
    if parsed is None or _FRESHNESS_NOTE_KEY not in parsed:
        return result
    return json.dumps({k: v for k, v in parsed.items() if k != _FRESHNESS_NOTE_KEY}, indent=2)


def _with_freshness_note(payload: dict, note: str) -> str:
    """Render ``payload`` with its freshness note first, as JSON text.

    Any same-named key already in the vendor body is dropped rather than merged
    over: a plain ``{key: note, **payload}`` would let the body win, silently
    replacing our disclosure with text the vendor wrote — which the agent would
    then read as a system-issued freshness statement.
    """
    body = {k: v for k, v in payload.items() if k != _FRESHNESS_NOTE_KEY}
    return json.dumps({_FRESHNESS_NOTE_KEY: note, **body}, indent=2)


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
    rather than guessing.
    """
    endings = [
        ending
        for r in reports
        if (ending := _normalize_iso_date(r.get("fiscalDateEnding"))) is not None
    ]
    if not endings:
        return ""
    return data_lag_note(max(endings), curr_date, statement_lag_bound(cadence), f"{label} period")


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
    value, so the guard silently never fired in production. A ``None`` curr_date
    (no point-in-time bound) or a non-JSON body (an error/notice page) is served
    as it arrived — bar the empty-payload raise below, which fires before any of
    those checks because "this symbol has nothing" is true regardless of the
    analysis date.

    ``freq`` and ``label`` are the caller's statement identity; the served reports
    carry a freshness note built from them (see :func:`_statement_lag_note`).

    Alpha Vantage answers every statement call with BOTH ``annualReports`` and
    ``quarterlyReports``, so the requested cadence's list is the one served and
    the other is dropped: the yfinance path fetches only the requested frame, and
    shipping a second, unjudged list would hand the agent (say) a seven-year-old
    annual balance sheet with no disclosure attached to it.

    Raises:
        NoMarketDataError: when the vendor has no fundamentals for the symbol at
            all (an empty payload) or none of the requested cadence within the
            point-in-time bound. The yfinance path raises on the same inputs, so
            the router opens its no-data lane for either vendor rather than
            serving an empty body as a successful report.
    """
    parsed = _parsed_payload(result)
    if parsed is not None and not parsed:
        # Alpha Vantage answers an unknown symbol with "{}".
        raise NoMarketDataError(symbol, detail="Alpha Vantage returned an empty payload")
    if not curr_date or parsed is None or not parsed.keys() - _AV_ENVELOPE_KEYS:
        # No point-in-time bound (legacy passthrough), a prose body, or a failure
        # envelope this module does not classify (see #68) — served unchanged.
        return _served_body(result, parsed)
    if _normalize_iso_date(curr_date) is None:
        return _invalid_curr_date(curr_date)
    cadence = _statement_cadence(freq)
    supplied = parsed.get(f"{cadence}Reports")
    supplied_count = len(supplied) if isinstance(supplied, list) else 0
    filtered = _filter_reports_by_date(parsed, curr_date)
    reports = filtered.get(f"{cadence}Reports")
    if reports is not None and not isinstance(reports, list):
        # Present but wrong-shaped is a vendor schema break, not an uncovered
        # symbol; say so rather than letting the two look identical in the log
        # and in the router's no-data sentinel. An ABSENT key is not this case —
        # it means the vendor served no such list, which the no-reports branch
        # below reports (together with any notice that rode along).
        raise NoMarketDataError(
            symbol,
            detail=f"{cadence}Reports is {type(reports).__name__}, not a list of reports",
        )
    reports = reports or []
    if not reports:
        # "The vendor sent rows we could not use" and "the vendor covers nothing
        # here" reach the agent as the same NO_DATA_AVAILABLE sentinel, so the
        # count has to separate them: a schema rename or a nulled date field
        # would otherwise report every ticker as an uncovered symbol, with the
        # analyst writing its report around that verdict. Also logged, because
        # the sentinel is the only other trace and it names the symbol, not the
        # cause.
        notices = sorted(_AV_ENVELOPE_KEYS & parsed.keys())
        because = f"; vendor body also carried {', '.join(notices)}" if notices else ""
        if supplied_count:
            detail = (
                f"{supplied_count} {cadence} {label} reports returned, none usable on or "
                f"before {curr_date} (undated, malformed, or later than that date){because}"
            )
            logger.warning(
                "Alpha Vantage %s: dropped all %d %s reports for %s at %s",
                label,
                supplied_count,
                cadence,
                symbol,
                curr_date,
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

    Serves the body as it arrived when there is nothing to disclose, when it is
    not a JSON object, or when the object is a rejection envelope of nothing but
    ``Error Message`` / ``Information`` / ``Note`` (the notices
    ``_make_api_request`` does not already classify into the taxonomy; see #68).
    Annotating one of those would assert that fundamentals were fetched when
    none were.

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
    if parsed is not None and not parsed:
        raise NoMarketDataError(symbol, detail="Alpha Vantage returned an empty OVERVIEW payload")
    if not curr_date or parsed is None or not parsed.keys() - _AV_ENVELOPE_KEYS:
        # Same order as the statement path: a prose body or a failure envelope is
        # served as-is even when curr_date is unusable, because the vendor's
        # reason for having no data outranks ours for not being able to bound it.
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
        str: Company overview data including financial ratios and key metrics
    """
    params = {
        "symbol": ticker,
    }

    return _annotate_live_snapshot(_make_api_request("OVERVIEW", params), curr_date, ticker)


def _get_statement(function_name: str, ticker: str, freq: str, curr_date: str | None) -> str:
    """Fetch one financial statement.

    With a curr_date it is look-ahead-filtered, narrowed to the requested
    cadence, and freshness-noted; without one the vendor body is served as it
    arrived — the pre-existing no-point-in-time-bound passthrough. An empty
    payload raises either way, since an unknown symbol is unknown with or
    without an analysis date.
    """
    result = _make_api_request(function_name, {"symbol": ticker})
    return _filter_response_json(result, curr_date, freq, _STATEMENT_LABELS[function_name], ticker)


def get_balance_sheet(ticker: str, freq: str = "quarterly", curr_date: str = None):
    """Retrieve balance sheet data for a given ticker symbol using Alpha Vantage."""
    return _get_statement("BALANCE_SHEET", ticker, freq, curr_date)


def get_cashflow(ticker: str, freq: str = "quarterly", curr_date: str = None):
    """Retrieve cash flow statement data for a given ticker symbol using Alpha Vantage."""
    return _get_statement("CASH_FLOW", ticker, freq, curr_date)


def get_income_statement(ticker: str, freq: str = "quarterly", curr_date: str = None):
    """Retrieve income statement data for a given ticker symbol using Alpha Vantage."""
    return _get_statement("INCOME_STATEMENT", ticker, freq, curr_date)
