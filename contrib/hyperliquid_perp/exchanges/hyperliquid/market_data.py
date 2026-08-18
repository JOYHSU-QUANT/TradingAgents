"""Public market data reads — implements the ``ExchangeMarketData`` port.

Calls the SDK ``Info`` endpoints, then hands the raw JSON to :mod:`mapper`.
No Hyperliquid field names appear here; SDK exceptions are translated to
:class:`ExchangeRequestError` so callers stay SDK-agnostic.
"""

from __future__ import annotations

import logging
import time

from ...domains.perp.margin import MarginSchedule
from ...domains.perp.schema import Candle, CandleInterval, FundingPoint, MarketSnapshot
from . import mapper
from .sdk_client import HyperliquidClient, call_sdk

logger = logging.getLogger(__name__)

# How many milliseconds each supported candle interval spans, used to turn a
# "last N candles" lookback into a startTime. Keyed by the CandleInterval enum so
# the supported set has a single source of truth (schema.CandleInterval).
_INTERVAL_MS = {
    CandleInterval.M1: 60_000,
    CandleInterval.M5: 5 * 60_000,
    CandleInterval.M15: 15 * 60_000,
    CandleInterval.H1: 60 * 60_000,
    CandleInterval.H4: 4 * 60 * 60_000,
    CandleInterval.D1: 24 * 60 * 60_000,
}
_MS_PER_DAY = 24 * 60 * 60_000


def _now_ms() -> int:
    return int(time.time() * 1000)


def interval_to_ms(interval: str) -> int:
    # ``CandleInterval(interval)`` raises ValueError on an unknown value (e.g. a
    # mis-cased "4H"); translate it into the same clear message the caller expects.
    try:
        return _INTERVAL_MS[CandleInterval(interval)]
    except ValueError:
        raise ValueError(
            f"unsupported candle interval {interval!r}; "
            f"choose from {[i.value for i in CandleInterval]}"
        ) from None


class HyperliquidMarketData:
    """Read-only public market data. No wallet / API key required."""

    def __init__(self, client: HyperliquidClient) -> None:
        self._info = client.info

    def get_market_snapshot(self, coin: str) -> MarketSnapshot:
        raw = call_sdk(self._info.meta_and_asset_ctxs)
        return mapper.map_market_snapshot(raw, coin)

    def get_asset_meta(self, coin: str) -> tuple[int, MarginSchedule]:
        """``(szDecimals, MarginSchedule)`` for ``coin``, from one meta request.

        The paper engine's :class:`AssetSpec` inputs (execution §1.2 / §6.6.1):
        both come from the exchange meta, never hardcoded, and from the *same*
        response so the pair cannot be internally inconsistent.
        """
        raw = call_sdk(self._info.meta_and_asset_ctxs)
        return mapper.map_sz_decimals(raw, coin), mapper.map_margin_schedule(raw, coin)

    def get_candles(self, coin: str, interval: str, lookback: int) -> list[Candle]:
        end = _now_ms()
        # Pad the window so we comfortably clear `lookback` closed candles (the +1
        # absorbs the still-forming bar dropped just below).
        start = end - (lookback + 1) * interval_to_ms(interval)
        raw = call_sdk(self._info.candles_snapshot, coin, interval, start, end)
        # Identity echo (2026-08-17): a misrouted response must never feed indicators.
        candles = mapper.map_candles(raw, expected_coin=coin, expected_interval=interval)
        # ``candleSnapshot`` is end-inclusive, so the most recent bar is usually the
        # *currently-forming* one (``open_time <= end < close_time``): its OHLCV is
        # partial, which understates ATR and skews RSI/EMA, and its future close_time
        # would set the context's ``as_of`` ahead of now. Indicators must run on closed
        # candles only — the live price is carried separately by ``mark_price``. Drop any
        # bar whose close is still in the future.
        settled = [c for c in candles if c.close_time <= end]
        dropped_live = len(candles) - len(settled)
        if dropped_live:
            logger.debug(
                "dropped %d unsettled (still-forming) candle(s) for %s/%s",
                dropped_live,
                coin,
                interval,
            )
        candles = settled
        trimmed = candles[-lookback:] if lookback and len(candles) > lookback else candles
        if lookback and len(trimmed) < lookback:
            # A short return (newly listed coin, too-recent startTime, or SDK-side
            # truncation) looks identical to a full window in the logs. engine_bridge's
            # warm-up gate stops a bad run, but without this an operator can't tell
            # "asked 200, got 45 because new coin" from a normal read — surface it.
            logger.warning(
                "requested %d candles for %s/%s but exchange returned only %d",
                lookback,
                coin,
                interval,
                len(trimmed),
            )
        return trimmed

    def get_funding_history(self, coin: str, window_days: int) -> list[FundingPoint]:
        end = _now_ms()
        start = end - window_days * _MS_PER_DAY
        raw = call_sdk(self._info.funding_history, coin, start, end)
        # Identity echo: same discipline as get_candles above.
        return mapper.map_funding_history(raw, expected_coin=coin)
