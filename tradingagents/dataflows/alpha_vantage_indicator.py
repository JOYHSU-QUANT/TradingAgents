from .alpha_vantage_common import _make_api_request
from .errors import NoMarketDataError, UnsupportedIndicatorError, WiringGapError
from .utils import (
    INDICATOR_DESCRIPTIONS,
    MAX_UNTRUSTED_CHARS,
    data_lag_note,
    date_refusal,
    echo_argument,
    is_finite_number,
    sanitize_untrusted,
    unsupported_indicator,
)

# Maximum age (calendar days) of the newest indicator row relative to
# curr_date before the report carries a data-lag note, keyed by the requested
# interval — a monthly bar is legitimately ~30 days old, so a flat bound would
# permanently false-alarm on non-daily cadences. Unknown intervals get no note
# (same must-not-false-alarm rule as fred's frequency map, #30).
_MAX_LAG_DAYS_BY_INTERVAL = {
    "daily": 7,
    "weekly": 14,
    "monthly": 45,
}

# Indicator registry: display name + required series_type. Module-level so the
# wiring invariants below are testable: every entry outside
# _NO_ENDPOINT_INDICATORS has both a request definition and a CSV column, and
# neither of those tables carries an entry this one does not.
_SUPPORTED_INDICATORS = {
    "close_50_sma": ("50 SMA", "close"),
    "close_200_sma": ("200 SMA", "close"),
    "close_10_ema": ("10 EMA", "close"),
    "macd": ("MACD", "close"),
    "macds": ("MACD Signal", "close"),
    "macdh": ("MACD Histogram", "close"),
    "rsi": ("RSI", "close"),
    "boll": ("Bollinger Middle", "close"),
    "boll_ub": ("Bollinger Upper Band", "close"),
    "boll_lb": ("Bollinger Lower Band", "close"),
    "atr": ("ATR", None),
    # The _NO_ENDPOINT_INDICATORS members never build a request, so their
    # series_type is read by nothing; None says that rather than implying a
    # request shape they do not have.
    "vwma": ("VWMA", None),
}

# Supported indicators Alpha Vantage has no endpoint for. They never reach the
# request table or the CSV parser, so they are the one exemption from both
# wiring checks below; get_indicator answers them with the taxonomy's no-data
# error so the router can hand the call to a vendor that computes them.
_NO_ENDPOINT_INDICATORS = frozenset({"vwma"})

# Sentinel: forward the caller's time_period rather than a fixed one.
_CALLER_TIME_PERIOD = object()

# The Alpha Vantage request each indicator maps to: (function, time_period).
# ``None`` omits time_period entirely (MACD derives its own periods).
# ``series_type`` rides along exactly when _SUPPORTED_INDICATORS declares one,
# so ATR — which takes none — needs no column here. Every indicator in
# _SUPPORTED_INDICATORS outside _NO_ENDPOINT_INDICATORS MUST have an entry: a
# gap is a wiring bug, and get_indicator raises on it before any request rather
# than returning prose route_to_vendor reads as a successful report (#106).
_INDICATOR_REQUESTS = {
    "close_50_sma": ("SMA", "50"),
    "close_200_sma": ("SMA", "200"),
    "close_10_ema": ("EMA", "10"),
    "macd": ("MACD", None),
    "macds": ("MACD", None),
    "macdh": ("MACD", None),
    "rsi": ("RSI", _CALLER_TIME_PERIOD),
    "boll": ("BBANDS", "20"),
    "boll_ub": ("BBANDS", "20"),
    "boll_lb": ("BBANDS", "20"),
    "atr": ("ATR", _CALLER_TIME_PERIOD),
}

# Maps internal indicator names to the CSV column Alpha Vantage returns.
# Every indicator in _SUPPORTED_INDICATORS that reaches the CSV-parsing path
# MUST have an entry here — there is no fallback column guessing (the
# _NO_ENDPOINT_INDICATORS members are exempt: they raise before any parsing).
_CSV_COLUMN_MAP = {
    "macd": "MACD",
    "macds": "MACD_Signal",
    "macdh": "MACD_Hist",
    "boll": "Real Middle Band",
    "boll_ub": "Real Upper Band",
    "boll_lb": "Real Lower Band",
    "rsi": "RSI",
    "atr": "ATR",
    "close_10_ema": "EMA",
    "close_50_sma": "SMA",
    "close_200_sma": "SMA",
}

# The description each rendered report ends with. Module-level, beside the
# other three tables, so the same set check covers it: this used to be a local
# dict read with a "No description available." fallback — unreachable, since an
# unsupported name raises first, and untested, so an indicator added without a
# description would have rendered that placeholder into the agent's report
# silently (#117). Derived from the cross-vendor table rather than written out
# again: the sentences are agent-facing text, and this used to be a verbatim
# copy of the yfinance vendor's — one prompt sentence editable in two places,
# with nothing comparing them (#137). A supported indicator the shared table
# does not describe fails here, at import, before it could cost a request.
# No entry for the _NO_ENDPOINT_INDICATORS members: they raise before any
# request, so nothing here would ever be rendered for them.
_INDICATOR_DESCRIPTIONS = {
    indicator: INDICATOR_DESCRIPTIONS[indicator]
    for indicator in _SUPPORTED_INDICATORS
    if indicator not in _NO_ENDPOINT_INDICATORS
}


def _rows_not_served(unusable: int, undatable: int) -> str:
    """What the vendor sent that this window could not use, or "".

    One definition for the report note and for the no-rows refusal: they name
    the same two counts, and a reader comparing a failed call against a thin
    one should not have to tell two wordings apart (#233).
    """
    parts = []
    if unusable:
        parts.append(f"{unusable} row(s) in this window whose value could not be read")
    if undatable:
        parts.append(f"{undatable} row(s) whose date could not be read at all")
    return " and ".join(parts)


def get_indicator(
    symbol: str,
    indicator: str,
    curr_date: str,
    look_back_days: int,
    interval: str = "daily",
    time_period: int = 14,
) -> str:
    """
    Returns Alpha Vantage technical indicator values over a time window.

    Args:
        symbol: ticker symbol of the company
        indicator: technical indicator to get the analysis and report of
        curr_date: The current trading date you are trading on, YYYY-mm-dd
        look_back_days: how many days to look back
        interval: Time interval (daily, weekly, monthly)
        time_period: Number of data points for calculation. Only the indicators
            whose request entry forwards it (RSI, ATR) use it; the rest carry
            the period their Alpha Vantage function is named for.

    Returns:
        String containing indicator values and description

    Raises:
        NoMarketDataError: When this vendor cannot serve the indicator at all
            (it has no endpoint for it), or when its answer carries no usable
            rows — a blank or header-only CSV, a CSV whose ``time`` or value
            column is absent, or one whose rows all fall outside the requested
            window. Each used to ``return`` prose instead, which
            ``route_to_vendor`` reads as a successful answer: the chain stopped
            at the vendor that had just failed and the agent analysed the
            sentence as an indicator report (#106).
        ValueError: When the indicator is unsupported (a caller mistake, raised
            as ``UnsupportedIndicatorError``, which the tool wrapper renders as
            report text, #117), or when it is registered as supported without
            a request definition, a CSV column mapping or a description (our
            own wiring gaps). All are raised before any request is made. A
            ``curr_date`` that will not parse
            is not a raise: it answers the shared ``INVALID_CURR_DATE``
            sentinel, as the yfinance sibling does (#111).
        Anything untyped: when parsing the answer fails outside the cases
            above, it leaves raw. The router reads it as this vendor's
            library failing — it logs the traceback, routes past to the
            vendor that computes the same indicator from OHLCV, and renders
            one line of report text only when no vendor serves (#187, #219).
        VendorError, requests.RequestException: Propagated to their own
            router lanes (a throttle, a missing key, an outage, a transport
            failure are never reports: #60, #87, #142).

    The price series is not a parameter: each indicator's entry in
    ``_SUPPORTED_INDICATORS`` names the ``series_type`` its request carries (or
    names none, as ATR does). A caller-supplied one used to be accepted and then
    overwritten by that entry on every indicator, so it never reached a request.
    """
    from datetime import datetime

    from dateutil.relativedelta import relativedelta

    if indicator not in _SUPPORTED_INDICATORS:
        # The shared definition, so this refusal and the yfinance sibling's
        # cannot differ in which guard the rejected name takes (#219, #233).
        # This side used to interpolate the name RAW while the other echoed it,
        # which is the divergence the shared sentences exist to prevent: one
        # routed tool ending alike on a clean spelling and differently on a
        # hostile one, decided by a config key the agent cannot see.
        raise UnsupportedIndicatorError(unsupported_indicator(indicator, _SUPPORTED_INDICATORS))

    # Unusable dates are refused before any request, in the shared voice (#111).
    refusal = date_refusal(curr_date, what="indicator values", kind="point")
    if refusal is not None:
        return refusal
    curr_date_dt = datetime.strptime(curr_date, "%Y-%m-%d")
    before = curr_date_dt - relativedelta(days=look_back_days)

    if indicator in _NO_ENDPOINT_INDICATORS:
        # This vendor cannot serve the indicator at all, which is a no-data
        # condition about the vendor rather than the symbol. It used to answer
        # prose ("VWMA calculation requires OHLCV data and is not directly
        # available from Alpha Vantage API"), and route_to_vendor reads a
        # returned string as a successful report — so the chain stopped here
        # even though the yfinance vendor serving the same routed tool computes
        # vwma from OHLCV via stockstats. Raising hands that vendor its turn
        # (#106). Placed before the try below because no request is made.
        display, _ = _SUPPORTED_INDICATORS[indicator]
        # The detail is spliced into the router's agent-facing sentinel, so it
        # says what is true of THIS vendor only: whether a vendor that computes
        # from OHLCV is configured is not something this getter can see.
        raise NoMarketDataError(
            symbol,
            detail=(
                f"Alpha Vantage has no {display} endpoint; it can only be computed from OHLCV data"
            ),
        )

    # All three wiring checks run before the request: a supported indicator
    # with no request definition, no CSV column or no description is our bug,
    # not a vendor condition. Raising rather than returning prose stops it
    # costing a request and leaves a traceback in the logs; the router no
    # longer records a successful answer, so a multi-vendor chain reaches the
    # next vendor (#106). A single-vendor chain surfaces it to the ToolNode as
    # the failure it is: the tool wrapper renders only UnsupportedIndicatorError
    # as report text (#117), and a wiring gap is ours to fix, not the model's
    # to route around. Guessing a column would silently render numbers from
    # the wrong field (#31); the description check is here rather than at the
    # render because a KeyError there is a failure the router cannot tell from
    # the vendor's library breaking.
    #
    # WiringGapError, not the bare ValueError these used to raise: what kept
    # them loud was sitting above the getter's ``with library_failure_lane``
    # block, and with the conversion moved to the router (#219) placement says
    # nothing — a bare ValueError from here would come back as
    # "Error retrieving rsi values for AAPL: ..." for the analyst to read as
    # its indicator report, since technical_indicators is not a loud category.
    if indicator not in _INDICATOR_REQUESTS:
        raise WiringGapError(
            f"Indicator '{indicator}' is registered as supported but has no "
            f"Alpha Vantage request defined"
        )
    if indicator not in _CSV_COLUMN_MAP:
        raise WiringGapError(
            f"Indicator '{indicator}' is registered as supported but has no CSV column mapping"
        )
    # A real table gap now fails at import (the derivation above raises
    # KeyError), so at runtime this is the drift-lock for the other failure:
    # the derivation being replaced by a literal copy that then loses a key.
    if indicator not in _INDICATOR_DESCRIPTIONS:
        raise WiringGapError(
            f"Indicator '{indicator}' is registered as supported but has no description"
        )

    _, required_series_type = _SUPPORTED_INDICATORS[indicator]

    av_function, time_period_spec = _INDICATOR_REQUESTS[indicator]
    params = {"symbol": symbol, "interval": interval, "datatype": "csv"}
    if required_series_type:
        params["series_type"] = required_series_type
    if time_period_spec is _CALLER_TIME_PERIOD:
        params["time_period"] = str(time_period)
    elif time_period_spec is not None:
        params["time_period"] = time_period_spec

    # Every failure from here down leaves this getter unhandled so the
    # router can react by behavior: a missing key takes the "vendor
    # unavailable" lane and a 429 the rate-limit lane, both of which hand
    # the next vendor in the chain its turn, and the NoMarketDataError
    # raises below take the no-data lane. So does a transport failure — a
    # 4xx the boundary leaves as HTTPError, a reset, a timeout
    # (``requests.RequestException`` is an OSError). This getter used to
    # catch all of those in a broad handler of its own and come back with a
    # successful-looking "Error retrieving ..." string, so the router saw an
    # answer and never fell back once Alpha Vantage's daily quota was spent
    # (#60), or after a 404 or a 503 (#87, #142). What is left — the parse
    # tripping over a shape this vendor did serve — the router names with
    # the subject the yfinance sibling's failure gets, since the row it
    # reads is the routed tool's and not the vendor's (#187, #219).
    data = _make_api_request(av_function, params)

    # Parse CSV data and extract values for the date range
    lines = data.strip().split("\n")
    if len(lines) < 2:
        raise NoMarketDataError(
            symbol,
            detail=(
                f"Alpha Vantage returned no {indicator} rows "
                f"(the CSV carried no data beyond its header)"
            ),
        )

    # Parse header and data
    header = [col.strip() for col in lines[0].split(",")]
    if "time" not in header:
        # A shape the parser cannot read at all. Worded as the schema break
        # it is, not as an uncovered symbol: the router splices this detail
        # into what the agent reads (#106).
        raise NoMarketDataError(
            symbol,
            detail=(
                f"Alpha Vantage's {indicator} CSV has no 'time' column (columns: {header})"
            ),
        )
    date_col_idx = header.index("time")

    target_col_name = _CSV_COLUMN_MAP[indicator]
    if target_col_name not in header:
        raise NoMarketDataError(
            symbol,
            detail=(
                f"Alpha Vantage's {indicator} CSV has no '{target_col_name}' "
                f"column (columns: {header})"
            ),
        )
    value_col_idx = header.index(target_col_name)

    result_data = []
    # Two counts, because they are two different facts and this request sends
    # no ``outputsize``: the CSV is the vendor's whole history, not the window.
    # A row whose value cannot be read is omitted FROM THE WINDOW and the report
    # says so; a row whose DATE cannot be read cannot be placed in the window at
    # all, so counting the two together would let one corrupt row from years
    # back claim a window that lost nothing. Before #233 both simply vanished,
    # and a series of unreadable cells read as a quiet week.
    unusable = 0
    undatable = 0
    for line in lines[1:]:
        if not line.strip():
            continue
        values = line.split(",")
        try:
            date_dt = datetime.strptime(values[date_col_idx].strip(), "%Y-%m-%d")
        except (ValueError, IndexError):
            undatable += 1
            continue
        if not (before <= date_dt <= curr_date_dt):
            continue
        if len(values) <= value_col_idx:
            # Dated, and inside the window, but carrying no value column: this
            # one IS a row the window lost.
            unusable += 1
            continue
        raw_value = values[value_col_idx].strip()
        shown = sanitize_untrusted(raw_value, limit=MAX_UNTRUSTED_CHARS)
        # Both spellings are asked, as fred's observation guard asks them: the
        # RAW one so flattening cannot repair a value into a number — "4.1|"
        # becomes "4.1", turning a cell the reader would have questioned into
        # an indicator reading the vendor never sent — and the RENDERED one so
        # a value only the raw form can parse cannot reach the report either.
        if not (is_finite_number(raw_value) and is_finite_number(shown)):
            unusable += 1
            continue
        result_data.append((date_dt, shown))

    if not result_data:
        # Nothing usable, for up to three different reasons, so the detail names
        # the ones that actually happened rather than blaming the window for all
        # of them. This exit used to embed "No data available for the specified
        # date range." inside a well-formed "## RSI values from ... to ..." report — the most
        # concealed of this getter's prose exits, since it carried no error
        # wording at all. Raising instead matches what the same vendor's
        # daily-bars getter does with a header-only CSV (#30/#106): the
        # chain can fall back, and a chain with no other vendor emits the
        # router's no-data sentinel.
        window = f"between {before.strftime('%Y-%m-%d')} and {curr_date}"
        served = _rows_not_served(unusable, undatable)
        raise NoMarketDataError(
            symbol,
            detail=(
                f"no usable {indicator} rows {window}: Alpha Vantage served {served}"
                if served
                else f"no {indicator} rows {window}"
            ),
        )

    # Sort by date and format output
    result_data.sort(key=lambda x: x[0])

    ind_string = ""
    for date_dt, value in result_data:
        ind_string += f"{date_dt.strftime('%Y-%m-%d')}: {value}\n"

    # Freshness: the header above claims coverage "to {curr_date}" but the
    # rows are whatever survived the range filter — a stalled upstream can
    # leave the newest value behind the date being analysed. The bound is
    # keyed by the requested interval so a normal bar gap (weekend,
    # month-boundary) is not flagged, only a genuinely behind series (#30).
    lag_note = ""
    max_lag = _MAX_LAG_DAYS_BY_INTERVAL.get(interval)
    if max_lag is not None:
        note = data_lag_note(result_data[-1][0], curr_date, max_lag, f"{indicator} value")
        if note:
            lag_note = "\n" + note + "\n"

    # Dropped rows are disclosed rather than left as a gap in the dates, in
    # fred's wording for the same fact about the same kind of payload.
    served = _rows_not_served(unusable, undatable)
    unusable_note = f"\n_(Alpha Vantage served {served}.)_\n" if served else ""

    result_str = (
        # The indicator is the caller's own argument coming back into text the
        # model reads. The membership check two hundred lines above bounds it
        # to this module's menu, which is a fact about a caller far from here
        # rather than about this line — the same reason fred echoes a series
        # id its resolver already bounded (#233). The yfinance sibling's
        # heading takes the guard in the same commit: one routed tool must not
        # render a hostile spelling one way through one vendor and another way
        # through the other (#219).
        f"## {echo_argument(indicator.upper())} values from "
        f"{before.strftime('%Y-%m-%d')} to {curr_date}:\n\n"
        + ind_string
        + lag_note
        + unusable_note
        + "\n\n"
        + _INDICATOR_DESCRIPTIONS[indicator]
    )

    return result_str
