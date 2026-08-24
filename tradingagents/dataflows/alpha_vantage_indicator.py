import requests

from .alpha_vantage_common import _make_api_request
from .errors import VendorError
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
    "vwma": ("VWMA", "close"),
}

# Maps internal indicator names to the CSV column Alpha Vantage returns.
# Every indicator in _SUPPORTED_INDICATORS that reaches the CSV-parsing path
# MUST have an entry here — there is no fallback column guessing ("vwma" is
# exempt: it returns early because Alpha Vantage has no such endpoint).
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
    series_type: str = "close",
) -> str:
    """
    Returns Alpha Vantage technical indicator values over a time window.

    Args:
        symbol: ticker symbol of the company
        indicator: technical indicator to get the analysis and report of
        curr_date: The current trading date you are trading on, YYYY-mm-dd
        look_back_days: how many days to look back
        interval: Time interval (daily, weekly, monthly)
        time_period: Number of data points for calculation
        series_type: The desired price type (close, open, high, low)

    Returns:
        String containing indicator values and description
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
        "vwma": "VWMA: A moving average weighted by volume. Usage: Confirm trends by integrating price action with volume data. Tips: Watch for skewed results from volume spikes; use in combination with other volume analyses.",
    }

    if indicator not in supported_indicators:
        raise ValueError(
            f"Indicator {indicator} is not supported. Please choose from: {list(supported_indicators.keys())}"
        )

    curr_date_dt = datetime.strptime(curr_date, "%Y-%m-%d")
    before = curr_date_dt - relativedelta(days=look_back_days)

    # Get the full data for the period instead of making individual calls
    _, required_series_type = supported_indicators[indicator]

    # Use the provided series_type or fall back to the required one
    if required_series_type:
        series_type = required_series_type

    try:
        # Get indicator data for the period
        if indicator == "close_50_sma":
            data = _make_api_request(
                "SMA",
                {
                    "symbol": symbol,
                    "interval": interval,
                    "time_period": "50",
                    "series_type": series_type,
                    "datatype": "csv",
                },
            )
        elif indicator == "close_200_sma":
            data = _make_api_request(
                "SMA",
                {
                    "symbol": symbol,
                    "interval": interval,
                    "time_period": "200",
                    "series_type": series_type,
                    "datatype": "csv",
                },
            )
        elif indicator == "close_10_ema":
            data = _make_api_request(
                "EMA",
                {
                    "symbol": symbol,
                    "interval": interval,
                    "time_period": "10",
                    "series_type": series_type,
                    "datatype": "csv",
                },
            )
        elif indicator == "macd" or indicator == "macds" or indicator == "macdh":
            data = _make_api_request(
                "MACD",
                {
                    "symbol": symbol,
                    "interval": interval,
                    "series_type": series_type,
                    "datatype": "csv",
                },
            )
        elif indicator == "rsi":
            data = _make_api_request(
                "RSI",
                {
                    "symbol": symbol,
                    "interval": interval,
                    "time_period": str(time_period),
                    "series_type": series_type,
                    "datatype": "csv",
                },
            )
        elif indicator in ["boll", "boll_ub", "boll_lb"]:
            data = _make_api_request(
                "BBANDS",
                {
                    "symbol": symbol,
                    "interval": interval,
                    "time_period": "20",
                    "series_type": series_type,
                    "datatype": "csv",
                },
            )
        elif indicator == "atr":
            data = _make_api_request(
                "ATR",
                {
                    "symbol": symbol,
                    "interval": interval,
                    "time_period": str(time_period),
                    "datatype": "csv",
                },
            )
        elif indicator == "vwma":
            # Alpha Vantage doesn't have direct VWMA, so we'll return an informative message
            # In a real implementation, this would need to be calculated from OHLCV data
            return f"## VWMA (Volume Weighted Moving Average) for {symbol}:\n\nVWMA calculation requires OHLCV data and is not directly available from Alpha Vantage API.\nThis indicator would need to be calculated from the raw stock data using volume-weighted price averaging.\n\n{indicator_descriptions.get('vwma', 'No description available.')}"
        else:
            return f"Error: Indicator {indicator} not implemented yet."

        # Parse CSV data and extract values for the date range
        lines = data.strip().split("\n")
        if len(lines) < 2:
            return f"Error: No data returned for {indicator}"

        # Parse header and data
        header = [col.strip() for col in lines[0].split(",")]
        try:
            date_col_idx = header.index("time")
        except ValueError:
            return f"Error: 'time' column not found in data for {indicator}. Available columns: {header}"

        target_col_name = _CSV_COLUMN_MAP.get(indicator)

        if not target_col_name:
            # A supported indicator with no CSV column mapping is a wiring gap.
            # Guessing a column would silently render numbers from the wrong
            # field, so fail loud instead (#31).
            return (
                f"Error: no CSV column mapping defined for indicator "
                f"'{indicator}'. Available columns: {header}"
            )
        try:
            value_col_idx = header.index(target_col_name)
        except ValueError:
            return f"Error: Column '{target_col_name}' not found for indicator '{indicator}'. Available columns: {header}"

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

        # Sort by date and format output
        result_data.sort(key=lambda x: x[0])

        ind_string = ""
        for date_dt, value in result_data:
            ind_string += f"{date_dt.strftime('%Y-%m-%d')}: {value}\n"

        if not ind_string:
            ind_string = "No data available for the specified date range.\n"

        # Freshness: the header above claims coverage "to {curr_date}" but the
        # rows are whatever survived the range filter — a stalled upstream can
        # leave the newest value behind the date being analysed. The bound is
        # keyed by the requested interval so a normal bar gap (weekend,
        # month-boundary) is not flagged, only a genuinely behind series (#30).
        lag_note = ""
        max_lag = _MAX_LAG_DAYS_BY_INTERVAL.get(interval)
        if result_data and max_lag is not None:
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
        # chain its turn. On a chain with no other vendor the router raises
        # instead — technical_indicators is a core category, and a loud failure
        # is the decided outcome there. Caught as the taxonomy's base type, not
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
