from datetime import datetime
from io import StringIO

import pandas as pd

from .alpha_vantage_common import _filter_csv_by_date_range, _make_api_request
from .errors import NoMarketDataError

# Maximum age (calendar days) of the newest daily row relative to end_date
# before the frame is rejected as stale. Mirrors the stockstats OHLCV bound
# (MAX_OHLCV_STALE_DAYS = 10, pinned equal by a test) so both market-data
# paths reject the same gap — the yfinance path raises on it, and an
# annotated success here would let a stalled Alpha Vantage feed short-circuit
# the vendor chain that could have served fresh bars (#30).
MAX_STOCK_LAG_DAYS = 10


def get_stock(symbol: str, start_date: str, end_date: str) -> str:
    """
    Returns raw daily OHLCV values, adjusted close values, and historical split/dividend events
    filtered to the specified date range.

    Args:
        symbol: The name of the equity. For example: symbol=IBM
        start_date: Start date in yyyy-mm-dd format
        end_date: End date in yyyy-mm-dd format

    Returns:
        CSV string containing the daily adjusted time series data filtered to the date range.

    Raises:
        NoMarketDataError: When the vendor returns a blank body, when every
            fetched row falls outside the range (the old behavior returned a
            header-only CSV the agent read as a successful fetch), when the
            newest surviving row trails end_date by more than
            MAX_STOCK_LAG_DAYS, or (propagated from the range filter) when
            the CSV or date range cannot be parsed. Every raise lets the
            router fall back to the next vendor and otherwise emit one honest
            no-data sentinel, mirroring the yfinance path's empty-frame and
            stale-frame raises (#30).
    """
    # Parse dates to determine the range
    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    today = datetime.now()

    # Choose outputsize based on whether the requested range is within the latest 100 days
    # Compact returns latest 100 data points, so check if start_date is recent enough
    days_from_today_to_start = (today - start_dt).days
    outputsize = "compact" if days_from_today_to_start < 100 else "full"

    params = {
        "symbol": symbol,
        "outputsize": outputsize,
        "datatype": "csv",
    }

    response = _make_api_request("TIME_SERIES_DAILY_ADJUSTED", params)

    filtered = _filter_csv_by_date_range(response, start_date, end_date, symbol=symbol)

    # A blank body passed through the filter untouched; there is nothing to
    # render, so surface it as no data rather than an empty success. The
    # (filtered or "") guard covers a None passthrough — dropping it would
    # turn a vendor outage into an AttributeError instead of a typed raise.
    body = (filtered or "").strip()
    if not body:
        raise NoMarketDataError(symbol, detail="Alpha Vantage returned an empty body for the range")

    # Header-only CSV: every fetched row fell outside the range. Decided at
    # the string level so the no-data case never pays a DataFrame parse.
    if "\n" not in body:
        raise NoMarketDataError(
            symbol,
            detail=(
                f"no daily rows between {start_date} and {end_date} "
                f"(vendor data exists only outside the range)"
            ),
        )

    # Staleness gate. The filter just wrote this CSV from dates it parsed
    # (raising on anything unparseable), so the first column — the date column
    # on the TIME_SERIES_DAILY_ADJUSTED shape — parses cleanly here.
    latest = pd.to_datetime(pd.read_csv(StringIO(filtered)).iloc[:, 0]).max()
    stale_days = (pd.to_datetime(end_date) - latest).days
    if stale_days > MAX_STOCK_LAG_DAYS:
        raise NoMarketDataError(
            symbol,
            detail=(
                f"latest row is {latest.strftime('%Y-%m-%d')} but {end_date} was "
                f"requested ({stale_days} days stale)"
            ),
        )

    return filtered
