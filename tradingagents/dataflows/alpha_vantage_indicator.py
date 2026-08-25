import requests

from .alpha_vantage_common import _make_api_request
from .errors import NoMarketDataError, VendorError
from .utils import data_lag_note

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
# mapping invariant (every entry has a CSV column mapping below) is testable.
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
        ValueError: When the indicator is unsupported, or is registered as
            supported without a request definition or a CSV column mapping —
            our own wiring gaps, raised before any request is made.
        VendorError, requests.RequestException: Propagated (see the handlers at
            the end of this function).

    The price series is not a parameter: each indicator's entry in
    ``_SUPPORTED_INDICATORS`` names the ``series_type`` its request carries (or
    names none, as ATR does). A caller-supplied one used to be accepted and then
    overwritten by that entry on every indicator, so it never reached a request.
    """
    from datetime import datetime

    from dateutil.relativedelta import relativedelta

    supported_indicators = _SUPPORTED_INDICATORS

    indicator_descriptions = {
        "close_50_sma": "50 SMA: A medium-term trend indicator. Usage: Identify trend direction and serve as dynamic support/resistance. Tips: It lags price; combine with faster indicators for timely signals.",
        "close_200_sma": "200 SMA: A long-term trend benchmark. Usage: Confirm overall market trend and identify golden/death cross setups. Tips: It reacts slowly; best for strategic trend confirmation rather than frequent trading entries.",
        "close_10_ema": "10 EMA: A responsive short-term average. Usage: Capture quick shifts in momentum and potential entry points. Tips: Prone to noise in choppy markets; use alongside longer averages for filtering false signals.",
        "macd": "MACD: Computes momentum via differences of EMAs. Usage: Look for crossovers and divergence as signals of trend changes. Tips: Confirm with other indicators in low-volatility or sideways markets.",
        "macds": "MACD Signal: An EMA smoothing of the MACD line. Usage: Use crossovers with the MACD line to trigger trades. Tips: Should be part of a broader strategy to avoid false positives.",
        "macdh": "MACD Histogram: Shows the gap between the MACD line and its signal. Usage: Visualize momentum strength and spot divergence early. Tips: Can be volatile; complement with additional filters in fast-moving markets.",
        "rsi": "RSI: Measures momentum to flag overbought/oversold conditions. Usage: Apply 70/30 thresholds and watch for divergence to signal reversals. Tips: In strong trends, RSI may remain extreme; always cross-check with trend analysis.",
        "boll": "Bollinger Middle: A 20 SMA serving as the basis for Bollinger Bands. Usage: Acts as a dynamic benchmark for price movement. Tips: Combine with the upper and lower bands to effectively spot breakouts or reversals.",
        "boll_ub": "Bollinger Upper Band: Typically 2 standard deviations above the middle line. Usage: Signals potential overbought conditions and breakout zones. Tips: Confirm signals with other tools; prices may ride the band in strong trends.",
        "boll_lb": "Bollinger Lower Band: Typically 2 standard deviations below the middle line. Usage: Indicates potential oversold conditions. Tips: Use additional analysis to avoid false reversal signals.",
        "atr": "ATR: Averages true range to measure volatility. Usage: Set stop-loss levels and adjust position sizes based on current market volatility. Tips: It's a reactive measure, so use it as part of a broader risk management strategy.",
        # No entry for the _NO_ENDPOINT_INDICATORS members: they raise above,
        # so nothing here would ever be rendered for them.
    }

    if indicator not in supported_indicators:
        raise ValueError(
            f"Indicator {indicator} is not supported. Please choose from: {list(supported_indicators.keys())}"
        )

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
        display, _ = supported_indicators[indicator]
        # The detail is spliced into the router's agent-facing sentinel, so it
        # says what is true of THIS vendor only: whether a vendor that computes
        # from OHLCV is configured is not something this getter can see.
        raise NoMarketDataError(
            symbol,
            detail=(
                f"Alpha Vantage has no {display} endpoint; it can only be computed from OHLCV data"
            ),
        )

    # Both wiring checks run before the request and outside the broad handler at
    # the end: a supported indicator with no request definition or no CSV column
    # is our bug, not a vendor condition. Raising rather than returning prose
    # stops it costing a request and leaves a traceback in the logs. It does NOT
    # abort the run: the tool wrapper catches ValueError and appends the message
    # to the report (technical_indicators_tools.py), so the agent still reads a
    # sentence — what changes is that the router no longer records a successful
    # answer, so a multi-vendor chain reaches the next vendor (#106). Guessing a
    # column would silently render numbers from the wrong field (#31).
    if indicator not in _INDICATOR_REQUESTS:
        raise ValueError(
            f"Indicator '{indicator}' is registered as supported but has no "
            f"Alpha Vantage request defined"
        )
    if indicator not in _CSV_COLUMN_MAP:
        raise ValueError(
            f"Indicator '{indicator}' is registered as supported but has no CSV column mapping"
        )

    _, required_series_type = supported_indicators[indicator]

    av_function, time_period_spec = _INDICATOR_REQUESTS[indicator]
    params = {"symbol": symbol, "interval": interval, "datatype": "csv"}
    if required_series_type:
        params["series_type"] = required_series_type
    if time_period_spec is _CALLER_TIME_PERIOD:
        params["time_period"] = str(time_period)
    elif time_period_spec is not None:
        params["time_period"] = time_period_spec

    try:
        # Get indicator data for the period
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
        for line in lines[1:]:
            if not line.strip():
                continue
            values = line.split(",")
            if len(values) > value_col_idx:
                try:
                    date_str = values[date_col_idx].strip()
                    # Parse the date
                    date_dt = datetime.strptime(date_str, "%Y-%m-%d")

                    # Check if date is in our range
                    if before <= date_dt <= curr_date_dt:
                        value = values[value_col_idx].strip()
                        result_data.append((date_dt, value))
                except (ValueError, IndexError):
                    continue

        if not result_data:
            # Every fetched row fell outside the window. This used to embed
            # "No data available for the specified date range." inside a
            # well-formed "## RSI values from ... to ..." report — the most
            # concealed of this getter's prose exits, since it carried no error
            # wording at all. Raising instead matches what the same vendor's
            # daily-bars getter does with a header-only CSV (#30/#106): the
            # chain can fall back, and a chain with no other vendor emits the
            # router's no-data sentinel.
            raise NoMarketDataError(
                symbol,
                detail=(
                    f"no {indicator} rows between {before.strftime('%Y-%m-%d')} and {curr_date}"
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

        result_str = (
            f"## {indicator.upper()} values from {before.strftime('%Y-%m-%d')} to {curr_date}:\n\n"
            + ind_string
            + lag_note
            + "\n\n"
            + indicator_descriptions.get(indicator, "No description available.")
        )

        return result_str

    except VendorError:
        # Every typed vendor failure propagates so the router can react by
        # behavior: a missing key takes the "vendor unavailable" lane and a 429
        # takes the rate-limit lane, both of which hand the next vendor in the
        # chain its turn, and the NoMarketDataError raises above take the
        # no-data lane. On a chain with no other vendor the router raises the
        # first two instead — technical_indicators is a core category, and a
        # loud failure is the decided outcome there — while no-data ends at the
        # router's sentinel, which is that lane's own decided outcome. Caught as
        # the taxonomy's base type, not
        # one leaf at a time: the rate-limit case used to reach the broad
        # handler below and come back as a successful-looking "Error retrieving
        # ..." string, so the router saw a successful answer and never fell
        # back once Alpha Vantage's daily quota was spent (#60).
        raise
    except requests.RequestException:
        # A transport-layer failure propagates for the same reason, one lane
        # over: #72 classified only HTTP 429, so every other status code — and
        # every connection reset or timeout — arrives here as a plain requests
        # exception. Swallowed, an Alpha Vantage 503 came back as "Error
        # retrieving {indicator} data: 503 Server Error", which route_to_vendor
        # reads as a successful answer: the chain stopped at the vendor that
        # had just failed and the agent analysed the error prose as an
        # indicator report. Every other Alpha Vantage getter (fundamentals,
        # news, stock) carries no broad except at all, so a requests exception
        # already reaches the router's generic error lane from those — this
        # getter was the only one converting it into a success (#87). What the
        # router does with it from there is described one block up.
        raise
    except Exception as e:
        print(f"Error getting Alpha Vantage indicator data for {indicator}: {e}")
        return f"Error retrieving {indicator} data: {str(e)}"
