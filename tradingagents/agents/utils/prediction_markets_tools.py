from typing import Annotated

from langchain_core.tools import tool

from tradingagents.dataflows.interface import route_to_vendor

from .tool_notes import notes_date_sentinel


@notes_date_sentinel("curr_date", omitted_ok=True, disclosure=True)
@tool
def get_prediction_markets(
    topic: Annotated[
        str,
        "Event topic/keyword, e.g. 'Fed rate cut', 'recession 2026', "
        "'US election', or a sector/company event.",
    ],
    limit: Annotated[int | None, "Max markets to return; omit for a default of 6"] = None,
    curr_date: Annotated[
        str | None,
        "The date you are analysing, yyyy-mm-dd (today's date from your "
        "context). Prices are always live; this only lets the report disclose "
        "when they are newer than the analysis date. Omit it rather than send "
        "any other format: a supplied value that is not yyyy-mm-dd is refused.",
    ] = None,
) -> str:
    """
    Retrieve live, market-implied probabilities for forward-looking events from
    prediction markets (Polymarket): Fed decisions, recession, elections,
    geopolitics, crypto. Returns the most-traded open markets matching the
    topic, each with its implied probability, traded volume, resolution date,
    and recent move. Uses the configured prediction_markets vendor.

    Args:
        topic (str): Event keyword(s) to search
        limit (int): Max markets to return; omit for a default of 6
        curr_date (str): Analysis date yyyy-mm-dd; enables the live-price
            disclosure when it trails the fetch date. Omit it rather than
            send any other format.

    Returns:
        str: A formatted markdown report of matching prediction markets
    """
    return route_to_vendor("get_prediction_markets", topic, limit, curr_date)
