"""Crypto-only analyst tools.

@tool wrappers for data sources that only make sense for crypto assets, bound to
the analysts only when ``asset_type == "crypto"``. Each routes through
``route_to_vendor`` so the configured vendor and the optional-category
degradation behaviour apply, exactly like the stock/macro tools.

The flows/sentiment/calendar/treasury/positioning tools go to the news
analyst; the options-volatility and futures-basis tools go to the market
analyst, where vol regime and the futures curve belong alongside the
technical indicators.

The economic calendar is not crypto-specific data — FOMC-week risk moves
equities too — but it is bound crypto-only for now so the stock path's tools
and prompts stay byte-identical (the standing rule for these data PRs);
offering it to stock runs is a separate, deliberate change.
"""

from typing import Annotated

from langchain_core.tools import tool

from tradingagents.dataflows.interface import route_to_vendor

from .tool_notes import notes_date_sentinel


@notes_date_sentinel("curr_date")
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


@notes_date_sentinel("curr_date")
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


@notes_date_sentinel("curr_date")
@tool
def get_options_market(
    asset: Annotated[
        str,
        "Crypto asset whose options market to read: 'BTC' or 'ETH' (pair forms "
        "like 'BTC-USD' are accepted). Another recognized crypto risk asset "
        "(SOL, XRP, ...) has no chain read for it, so BTC's DVOL level alone is "
        "returned as a market-wide proxy and the skew is withheld; a stablecoin "
        "or unrecognized symbol returns a no-signal note.",
    ],
    curr_date: Annotated[str, "Current date in yyyy-mm-dd format; the end of the DVOL window"],
) -> str:
    """
    Retrieve crypto options-implied volatility from Deribit: the DVOL index
    (a 30-day forward implied-vol gauge) with its 30-day min/max range when that
    window holds at least two readings, and its 365-day percentile when that
    window holds enough readings for one, plus ATM (50-delta) implied vol, the
    25-delta call/put vols and the 25-delta risk reversal (RR25). Those chain
    figures are read for one expiry inside a bounded band around 30 days —
    normally the eligible expiry nearest 30 days, or the next eligible one when
    that cannot be used, which the report labels and whose tenor it always prints
    (RR25 is not comparable across tenors, so an expiry outside the band yields no
    skew at all rather than a figure from an unrelated tenor). RR25 is the
    25-delta call IV minus the 25-delta put IV, so a negative value means the put
    wing carries the higher implied vol. The DVOL history is filtered to
    curr_date; the options chain has no historical endpoint, so its figures are
    withheld when curr_date is EARLIER than today, and also when curr_date runs
    more than a day AHEAD of the UTC clock (within a day is served with a note,
    since callers east of UTC routinely run a few hours ahead). The chain is
    likewise withheld for an asset this vendor reads no chain for, which receives
    BTC's DVOL level as a market-wide proxy but not BTC's skew. The report's
    Forward is Deribit's forward for the selected expiry, not spot, and is
    expected to differ from a spot price level. Whenever no risk reversal is in
    the report — the chain withheld, yielding no usable surface, or not supplying
    both wings — the report's closing one-line summary says so and why rather than
    falling silent, so the absence survives a downstream summary; that sentence
    also carries the DVOL level itself (or names that half's absence) and any
    fallback expiry or missing ATM point. Where a risk reversal IS printed it
    additionally names each 25-delta wing whose bracket was unusually wide, that
    being a qualification of a figure the sentence itself states. Uses the
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


@notes_date_sentinel("curr_date")
@tool
def get_economic_calendar(
    curr_date: Annotated[str, "Current date in yyyy-mm-dd format; the anchor of the window"],
    look_back_days: Annotated[
        int | None, "Trailing window for the released section; omit for a 30-day window"
    ] = None,
) -> str:
    """
    Retrieve the US macro economic calendar: scheduled releases over the next
    two weeks (CPI, NFP, jobless claims, PCE, GDP, retail sales — with the
    consensus forecast and prior print) and the releases of the trailing
    window with actual-vs-forecast surprises. Event risk contextualizes
    position sizing and timing (a regime / risk modifier); it is not a
    directional signal, and the report says so. The feed carries no Fed
    rate-decision events at all — the report flags that coverage gap so an
    empty FOMC row is never read as a quiet Fed schedule. Scheduled rows show
    forecast and previous but never an actual; released figures appear only
    on or before curr_date, so a backtest date never sees a future print.
    Uses the configured economic_calendar vendor.

    Args:
        curr_date (str): Current date in yyyy-mm-dd format
        look_back_days (int): Trailing window for the released section; omit
            for a 30-day window

    Returns:
        str: A formatted markdown report of scheduled events and releases
    """
    return route_to_vendor("get_economic_calendar", curr_date, look_back_days)


@notes_date_sentinel("curr_date")
@tool
def get_btc_treasuries(
    asset: Annotated[
        str,
        "Crypto asset the demand signal is for: 'BTC' natively (pair forms like "
        "'BTC-USD' are accepted). Corporate treasuries hold BTC only, so another "
        "recognized crypto risk asset (ETH, SOL, ...) is served the BTC data as a "
        "market-wide demand proxy; a stablecoin or unrecognized symbol returns a "
        "no-signal note.",
    ],
    curr_date: Annotated[str, "Current date in yyyy-mm-dd format; the end of the window"],
    look_back_days: Annotated[
        int | None,
        "Trailing window for the activity section; omit for a 90-day window "
        "(treasury disclosures are sparse)",
    ] = None,
) -> str:
    """
    Retrieve corporate BTC treasury holdings and disclosed changes from the
    largest tracked holders: combined and top-5 holdings (each company as of
    its own latest disclosure), and the window's disclosed buys/disposals with
    an implied US$/BTC where a cost was filed. A demand-side flow signal of
    the same family as spot-ETF flows, but announcement-driven and lumpy —
    a medium-term narrative input, not a timing signal. Disclosure dates can
    lag the underlying transactions, and some companies file only monthly or
    quarterly snapshots. Corporate treasuries hold BTC only, so a recognized
    crypto risk asset other than BTC (ETH included) receives the BTC data as
    a market-wide demand proxy; a stablecoin or unrecognized symbol returns a
    no-signal note. Uses the configured btc_treasuries vendor.

    Args:
        asset (str): 'BTC' (other recognized risk coins get the BTC data as a proxy)
        curr_date (str): Current date in yyyy-mm-dd format
        look_back_days (int): Trailing window for the activity section; omit
            for a 90-day window

    Returns:
        str: A formatted markdown report of treasury holdings and activity
    """
    return route_to_vendor("get_btc_treasuries", asset, curr_date, look_back_days)


@notes_date_sentinel("curr_date")
@tool
def get_whale_positions(
    asset: Annotated[
        str,
        "Crypto asset whose large-account positioning to read: 'BTC', 'ETH', "
        "'SOL', ... (pair forms like 'BTC-USD' are accepted). A stablecoin or "
        "an unrecognized symbol returns a no-signal note — no other coin's "
        "positioning stands in for it.",
    ],
    curr_date: Annotated[str, "Current date in yyyy-mm-dd format"],
) -> str:
    """
    Retrieve how the largest Hyperliquid accounts are positioned in one
    perpetual market: how many of the sampled accounts are long vs short, the
    notional on each side with the long/short ratio, the notional-weighted
    leverage, the largest individual positions, and the change since a snapshot
    about a day earlier when one exists. The sample is the top accounts of
    Hyperliquid's public leaderboard, ranked by ITS account value, which is not
    the same as perp equity — many of those accounts hold no perp position at
    all, and the ones that do are often market makers or vaults hedging
    exposure held elsewhere. So read the split as venue-level positioning of
    large accounts, not as crowd sentiment, and never as a standalone
    directional signal; the report states the same and prints how many sampled
    accounts actually held the coin. Both endpoints are live-only, so the
    report is a snapshot labelled with the UTC instant it was fetched rather
    than a state as of curr_date, and a report for a past date says so. A coin
    no sampled account holds yields an explicit "no position in this sample"
    statement, which is not the same as no open interest. Uses the configured
    whale_positioning vendor.

    Args:
        asset (str): 'BTC', 'ETH', or another recognized crypto risk asset
        curr_date (str): Current date in yyyy-mm-dd format

    Returns:
        str: A formatted markdown report of large-account positioning
    """
    return route_to_vendor("get_whale_positions", asset, curr_date)


@notes_date_sentinel("curr_date")
@tool
def get_futures_basis(
    asset: Annotated[
        str,
        "Crypto asset whose CME futures basis to read: 'BTC' only (pair forms "
        "like 'BTC-USD' are accepted). Any other symbol returns a no-signal "
        "note — BTC's basis does not stand in for another asset's.",
    ],
    curr_date: Annotated[str, "Current date in yyyy-mm-dd format; no later hour is read"],
) -> str:
    """
    Retrieve the CME Bitcoin front-month futures basis: what the regulated
    future (Yahoo BTC=F) trades at over spot (Yahoo BTC-USD). Reported as a
    nominal percentage — the median over the latest 24 hours in which both
    traded, matched on the same UTC hour, because the two DAILY closes are
    hours apart and their difference is mostly clock mismatch — and annualized
    by the front contract's days to expiry, with a 7-day annualized median and
    the change against the reading 7 days earlier. The nominal basis decays
    toward zero as the contract nears expiry and steps back up at the monthly
    roll, so annualized figures are the comparable ones. The annualized figure
    is withheld within 5 days of expiry, where it divides by almost nothing,
    and for the first hours after a roll; the expiry date follows the
    last-Friday rule and can be a day late around an exchange holiday, so the
    annualized figure is approximate. Hours after curr_date are never read.
    CME closes for the weekend: a reading whose newest hour ended more than 3
    hours earlier is labelled as not live. The whole report is withheld, with
    no figures, for a curr_date more than a day ahead of the UTC clock or more
    than 709 days behind it (Yahoo's hourly history reaches no further), and
    when Yahoo served too few synchronous hours or none newer than 7 days. A
    positive basis is the usual state and mostly reflects the
    cost of carry: a positioning-and-carry input, not a standalone directional
    signal. Uses the configured futures_basis vendor.

    Args:
        asset (str): 'BTC'
        curr_date (str): Current date in yyyy-mm-dd format

    Returns:
        str: A markdown report of the nominal and annualized basis — or a
            withheld notice carrying no figures, or for a symbol other than
            BTC a plain no-signal sentence.
    """
    return route_to_vendor("get_futures_basis", asset, curr_date)
