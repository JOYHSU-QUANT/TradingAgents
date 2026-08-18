from typing import Annotated

import pandas as pd
from langchain_core.tools import tool

from tradingagents.dataflows.errors import VendorError
from tradingagents.dataflows.market_data_validator import build_verified_market_snapshot


@tool
def get_verified_market_snapshot(
    symbol: Annotated[str, "ticker symbol of the company"],
    curr_date: Annotated[str, "the current trading date, YYYY-mm-dd"],
    look_back_days: Annotated[
        int, "number of recent trading rows to include for sanity-checking"
    ] = 30,
) -> str:
    """Deterministic verification snapshot for exact market-data claims.

    Returns the latest OHLCV row on or before curr_date, common technical
    indicators, and recent closes. Call this before making exact claims about
    price levels, Bollinger bands, RSI, MACD, moving averages, support /
    resistance, or historical comparisons, and treat it as the source of truth.
    """
    # An LLM-supplied curr_date that doesn't parse would raise a bare
    # ValueError deep in load_ohlcv — outside the VendorError taxonomy — so
    # guard it here and answer with the same instructive-sentinel style the
    # fundamentals path uses (INVALID_CURR_DATE precedent).
    if pd.isna(pd.to_datetime(curr_date, errors="coerce")):
        return (
            f"INVALID_CURR_DATE: '{curr_date}' is not a parseable date "
            f"(expected YYYY-MM-DD). No data served; retry with a valid date."
        )
    # This tool calls the builder directly (it does not go through
    # route_to_vendor), so the vendor-error taxonomy must be turned into the
    # instructive no-data sentinel here — otherwise a typed raise surfaces as
    # a generic ToolNode error string instead (#32).
    try:
        return build_verified_market_snapshot(symbol, curr_date, look_back_days)
    except VendorError as e:
        return (
            f"NO_DATA_AVAILABLE: could not build a verified market snapshot "
            f"for '{symbol}' ({e}). Do not estimate or fabricate values — "
            f"report that verified data is unavailable for this symbol."
        )
