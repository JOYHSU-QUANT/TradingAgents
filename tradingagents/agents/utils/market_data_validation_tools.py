import logging
from typing import Annotated

import pandas as pd
from langchain_core.tools import tool

from tradingagents.dataflows.errors import VendorError, VendorRateLimitError
from tradingagents.dataflows.market_data_validator import build_verified_market_snapshot
from tradingagents.dataflows.utils import (
    MAX_UNTRUSTED_CHARS,
    echo_argument,
    refuse_date,
    sanitize_untrusted,
)

from .tool_notes import notes_date_sentinel

logger = logging.getLogger(__name__)


@notes_date_sentinel("curr_date")
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
    # guard it here and answer with the SAME sentence the routed tools serve
    # (#112), not a third hand-written copy of it. The parse rule stays the
    # looser pandas one on purpose, and is NOT the routed tools' strict
    # strptime: those compare the normalised string lexically against
    # zero-padded vendor date fields (see ``utils.normalize_iso_date``), while
    # this tool turns the value into a real Timestamp and compares numerically,
    # so a date pandas can read is one it can use. Only the wording is shared
    # — and the log line with it: the refusal is returned, not raised, so
    # this is the only operator-visible trace of it (#230).
    if pd.isna(pd.to_datetime(curr_date, errors="coerce")):
        return refuse_date(curr_date, what="verification snapshot data", kind="point")
    # This tool calls the builder directly (it does not go through
    # route_to_vendor), so the vendor-error taxonomy must be turned into the
    # instructive no-data sentinel here — otherwise a typed raise surfaces as
    # a generic ToolNode error string instead (#32). For the same reason the
    # router's cap on vendor text never sees these two slots: the reason is
    # flattened and capped here, and the whole of it goes to the log (#201).
    # ``symbol`` beside it is the model's own argument quoted back, so it gets
    # the argument echo rather than the vendor one (#231).
    try:
        return build_verified_market_snapshot(symbol, curr_date, look_back_days)
    except VendorRateLimitError as e:
        # Before the exhausted-throttle raise was typed (#67) this escaped as a
        # generic ToolNode error; the broad clause below would now flatten it
        # into the permanent-sounding no-data verdict — the agent would assert
        # that verified data does not exist when the vendor was merely
        # throttling. Say "transient" instead.
        logger.warning("Verification snapshot for %s rate-limited: %s", symbol, e)
        return (
            f"DATA_UNAVAILABLE: the market data vendor rate-limited the "
            f"verification snapshot for '{echo_argument(symbol)}' "
            f"({sanitize_untrusted(e, limit=MAX_UNTRUSTED_CHARS)}). This is transient — "
            f"do not report it as proof that data is unavailable, and do not "
            f"estimate or fabricate values; avoid exact numeric claims you "
            f"cannot verify."
        )
    except VendorError as e:
        logger.warning("Verification snapshot for %s failed: %s", symbol, e)
        return (
            f"NO_DATA_AVAILABLE: could not build a verified market snapshot "
            f"for '{echo_argument(symbol)}' "
            f"({sanitize_untrusted(e, limit=MAX_UNTRUSTED_CHARS)}). "
            f"Do not estimate or fabricate values — "
            f"report that verified data is unavailable for this symbol."
        )
