"""Crypto-only analyst tools.

@tool wrappers for data sources that only make sense for crypto assets, bound to
the analysts only when ``asset_type == "crypto"``. Each routes through
``route_to_vendor`` so the configured vendor and the optional-category
degradation behaviour apply, exactly like the stock/macro tools.

The flows/sentiment tools go to the news analyst; the options-volatility tool
goes to the market analyst, where vol regime belongs alongside the technical
indicators. A later data-source PR adds whale positioning here too.
"""

from typing import Annotated

from langchain_core.tools import tool

from tradingagents.dataflows.interface import route_to_vendor


@tool
def get_etf_flows(
    asset: Annotated[
        str,
        "Crypto asset whose US spot-ETF flows to fetch: 'BTC' or 'ETH' "
        "(pair forms like 'BTC-USD' are accepted). Another recognized crypto risk "
        "asset (SOL, XRP, ...) has no spot ETF of its own, so BTC flows are returned "
        "as a market-wide proxy; a stablecoin or unrecognized symbol returns a "
        "no-signal note.",
    ],
    curr_date: Annotated[str, "Current date in yyyy-mm-dd format; the end of the window"],
    look_back_days: Annotated[
        int | None, "Trailing window length in days; omit for a 30-day window"
    ] = None,
) -> str:
    """
    Retrieve US spot Bitcoin/Ethereum ETF daily net flows (US$m).
    Returns the latest day's net flow, the window's cumulative net flow, the
    consecutive inflow/outflow streak, the latest day's issuer breakdown, and a
    recent daily-flow table. Persistent inflows/outflows are a demand-side
    signal that complements price and news. A recognized crypto risk asset without
    its own spot ETF (SOL, XRP, ...) returns BTC flows as a market-wide proxy; a
    stablecoin or unrecognized symbol returns a no-signal note. Uses the configured
    crypto_etf_flows vendor.

    Args:
        asset (str): 'BTC' or 'ETH' (recognized risk coins get BTC as a proxy)
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


@tool
def get_options_market(
    asset: Annotated[
        str,
        "Crypto asset whose options market to read: 'BTC' or 'ETH' (pair forms "
        "like 'BTC-USD' are accepted). Another recognized crypto risk asset "
        "(SOL, XRP, ...) has no listed chain, so BTC's DVOL level alone is "
        "returned as a market-wide proxy and the skew is withheld; a stablecoin "
        "or unrecognized symbol returns a no-signal note.",
    ],
    curr_date: Annotated[str, "Current date in yyyy-mm-dd format; the end of the DVOL window"],
) -> str:
    """
    Retrieve crypto options-implied volatility from Deribit: the DVOL index
    (a 30-day forward implied-vol gauge) with its 30-day min/max range, and its
    365-day percentile when that window holds enough readings for one, plus ATM
    implied vol, the 25-delta call/put vols and the 25-delta risk reversal (RR25)
    for the listed expiry nearest 30 days — or, when that expiry cannot be used,
    the next-nearest one, which the report labels and whose tenor it always prints
    (RR25 is not comparable across tenors). RR25 is the 25-delta call IV minus the
    25-delta put IV, so a negative value means the put wing carries the higher
    implied vol. The DVOL history is filtered to curr_date; the options chain has
    no historical endpoint, so its figures are withheld when curr_date is EARLIER
    than today (a curr_date later than the UTC clock is served, with a note). The
    chain is also withheld for an asset Deribit does not list, which receives
    BTC's DVOL level as a market-wide proxy but not BTC's skew. Uses the
    configured options_data vendor.

    Args:
        asset (str): 'BTC' or 'ETH' (recognized risk coins get BTC as a proxy)
        curr_date (str): Current date in yyyy-mm-dd format

    Returns:
        str: A markdown report of implied volatility and skew — or, for a symbol
            with no crypto-vol signal to serve (a stablecoin, an unrecognized
            ticker), a plain no-signal sentence carrying no figures at all.
    """
    return route_to_vendor("get_options_market", asset, curr_date)
