"""Public market data reads — implements the ``ExchangeMarketData`` port.

Calls the SDK ``Info`` endpoints, then hands the raw JSON to :mod:`mapper`.
No Hyperliquid field names appear here; SDK exceptions are translated to
:class:`ExchangeRequestError` so callers stay SDK-agnostic.

No host clock is read anywhere in this module. Both windowed reads take
their upper bound as an explicit ``end`` — the exchange's own clock, from
:meth:`HyperliquidMarketData.get_exchange_time` — so the host's clock can
neither truncate a window (issue #51) nor admit a bar the exchange has not
closed (issue #124).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from ...domains.perp.margin import MarginSchedule
from ...domains.perp.schema import Candle, FundingPoint, MarketSnapshot, interval_to_ms
from . import mapper
from .sdk_client import HyperliquidClient, call_sdk

logger = logging.getLogger(__name__)

_MS_PER_DAY = 24 * 60 * 60_000
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _epoch_ms(moment: datetime) -> int:
    """``moment`` as epoch milliseconds, by integer arithmetic.

    The window ends below are compared against the exchange's own
    ``close_time`` stamps, so the millisecond the exchange sent
    (``mapper.map_exchange_time``) must come back out exactly — 1ms short of
    a bar's close would drop a bar the exchange has closed. Integer
    arithmetic makes that exact by construction rather than by float luck:
    ``int(moment.timestamp() * 1000)`` happens NOT to drift at this magnitude
    (measured 2026-08-31: 5M values across the 2026 range, built both by
    ``fromtimestamp`` and by microsecond field, zero mismatches — so the 1ms
    drift ``context_builder`` guards ``as_of_ms`` against was not reproduced),
    but the exactness here should not rest on that measurement holding for
    every future magnitude. A naive ``moment`` is refused by name — the
    subtraction would raise anyway, but about mixing offsets, not about the
    clock the caller handed in.
    """
    if moment.tzinfo is None:
        raise ValueError("market data window end must be timezone-aware (UTC)")
    return (moment - _EPOCH) // timedelta(milliseconds=1)


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

    def get_candles(
        self, coin: str, interval: str, lookback: int, *, end: datetime
    ) -> list[Candle]:
        """Up to ``lookback`` candles CLOSED as of ``end``, oldest first.

        ``end`` is the EXCHANGE's clock (:meth:`get_exchange_time`), read by
        the caller BEFORE this fetch — never the host's, and required rather
        than defaulted to ``time.time()`` on purpose (issue #124). A window
        cut at the host's clock admitted, for a host running ahead, the bar
        the exchange had not closed yet — its partial OHLCV understates ATR
        and skews RSI/EMA — and for a host running behind it truncated the
        window by the lag (issue #51). Cut at the exchange's own clock neither
        can happen, whatever the host's clock does, and the freshness guard's
        future-side check is left with nothing a live fetch can trip. A
        default would be a silent road back to the host clock.

        Read-before-fetch is part of the contract, not a detail: a clock read
        AFTER the candles would let a bar that closed during the fetch pass
        the ``close_time <= end`` filter below while the response carried its
        OHLCV as captured before that close — the very defect this closes.
        """
        end_ms = _epoch_ms(end)
        # Pad the window so we comfortably clear `lookback` closed candles (the +1
        # absorbs the still-forming bar dropped just below).
        start = end_ms - (lookback + 1) * interval_to_ms(interval)
        raw = call_sdk(self._info.candles_snapshot, coin, interval, start, end_ms)
        # Identity echo (2026-08-17): a misrouted response must never feed indicators.
        candles = mapper.map_candles(raw, expected_coin=coin, expected_interval=interval)
        # ``candleSnapshot`` is end-inclusive, so the most recent bar is usually the
        # *currently-forming* one (``open_time <= end < close_time``). Measured
        # 2026-08-26 against the public API (BTC, 1m bars, 114 paired reads over
        # 240s): it was present in every response and read as closed the moment
        # the exchange's clock passed its close — no publish step — so "closed as
        # of the exchange's clock" is exactly ``close_time <= end``. Its OHLCV is
        # partial, which understates ATR and skews RSI/EMA, and its future
        # close_time would set the context's ``as_of`` ahead of the exchange's
        # clock. Indicators must run on closed candles only — the live price is
        # carried separately by ``mark_price``. Drop any bar the exchange's clock
        # has not passed.
        settled = [c for c in candles if c.close_time <= end_ms]
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

    def get_funding_history(
        self, coin: str, window_days: int, *, end: datetime
    ) -> list[FundingPoint]:
        """Funding observations over the ``window_days`` trailing ``end``, oldest first.

        Same ``end`` discipline as :meth:`get_candles`: the context build
        hands in the exchange's clock, so a host running behind no longer
        loses the newest funding points to a window that ended early (they
        fell between the host's clock and the newest candle's close, and the
        z-score sample silently lacked them). A caller that only needs a
        PAST hour and can tolerate a miss (``cli._provider``'s rate lookup,
        where a miss is "pending", never a wrong rate) may pass its own
        clock, and says so at the call site.
        """
        end_ms = _epoch_ms(end)
        start = end_ms - window_days * _MS_PER_DAY
        raw = call_sdk(self._info.funding_history, coin, start, end_ms)
        # Identity echo: same discipline as get_candles above.
        return mapper.map_funding_history(raw, expected_coin=coin)

    def get_exchange_time(self, coin: str) -> datetime:
        """The exchange's clock, from the public ``l2Book`` snapshot for ``coin``.

        The clock the two windows above are cut at: ``engine_bridge._build_context``
        reads this FIRST and hands it to both as ``end``, so a host clock that
        runs behind truncates nothing and one that runs ahead admits nothing
        the exchange has not closed (issues #51, #124) — and the freshness
        guard measures candle age against this same reading. Keyless, so the
        daemon and ``--context-only`` read the same clock. ``coin`` only gives
        the answer an identity to echo-check; the book itself is not used. Like
        ``get_asset_meta``, not on the ``ExchangeMarketData`` port: the paper
        engine's snapshot provider has no use for it — ``_build_context`` does.
        """
        raw = call_sdk(self._info.l2_snapshot, coin)
        return mapper.map_exchange_time(raw, expected_coin=coin)
