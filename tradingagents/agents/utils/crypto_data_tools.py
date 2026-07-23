"""Crypto-only analyst tools.

@tool wrappers for data sources that only make sense for crypto assets, bound to
the analysts only when ``asset_type == "crypto"``. Each routes through
``route_to_vendor`` so the configured vendor and the optional-category
degradation behaviour apply, exactly like the stock/macro tools.

Later data-source PRs add their crypto tools here too (Deribit options, whale
positioning).
"""

from typing import Annotated

from langchain_core.tools import tool

from tradingagents.dataflows.interface import route_to_vendor


@tool
def get_etf_flows(
    asset: Annotated[
        str,
        "Crypto asset whose US spot-ETF flows to fetch: 'BTC' or 'ETH' "
        "(pair forms like 'BTC-USD' are accepted).",
    ],
    curr_date: Annotated[str, "Current date in yyyy-mm-dd format; the end of the window"],
    look_back_days: Annotated[
        int | None, "Trailing window length in days; omit for a 30-day window"
    ] = None,
) -> str:
    """
    Retrieve US spot Bitcoin/Ethereum ETF daily net flows (US$m) from Farside.
    Returns the latest day's net flow, the window's cumulative net flow, the
    consecutive inflow/outflow streak, the latest day's issuer breakdown, and a
    recent daily-flow table. Persistent inflows/outflows are a demand-side
    signal that complements price and news. Uses the configured etf_flows vendor.

    Args:
        asset (str): 'BTC' or 'ETH'
        curr_date (str): Current date in yyyy-mm-dd format
        look_back_days (int): Trailing window length; omit for a 30-day window

    Returns:
        str: A formatted markdown report of spot-ETF flows
    """
    return route_to_vendor("get_etf_flows", asset, curr_date, look_back_days)


@tool
def get_fear_greed(
    curr_date: Annotated[str, "Current date in yyyy-mm-dd format; the end of the window"],
    look_back_days: Annotated[
        int | None, "Trailing window length in days; omit for a 30-day window"
    ] = None,
) -> str:
    """
    Retrieve the Crypto Fear & Greed Index (0-100, where 0 is Extreme Fear and
    100 is Extreme Greed) history from alternative.me. Returns the latest value
    and classification, the change vs 7 and 30 days ago, and a recent
    daily-reading table. A broad crowd-sentiment gauge for crypto. Uses the
    configured crypto_sentiment vendor.

    Args:
        curr_date (str): Current date in yyyy-mm-dd format
        look_back_days (int): Trailing window length; omit for a 30-day window

    Returns:
        str: A formatted markdown report of the Fear & Greed Index
    """
    return route_to_vendor("get_fear_greed", curr_date, look_back_days)
