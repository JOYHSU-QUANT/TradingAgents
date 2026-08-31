"""Assemble market data + indicators into a :class:`PerpMarketContext`.

Pure functions only — given the snapshot, candles and funding history, the
output is deterministic (``as_of`` is taken from the latest candle close, not the
wall clock), so this is straightforward to unit-test with recorded HL JSON.

Two computations carry the design's "no NaN in the prompt" rule:

- :func:`funding_zscore` returns ``None`` when the window has too few samples or
  zero variance, never ``NaN``.
- indicators come from :mod:`indicators`, which already maps NaN -> ``None``.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from datetime import datetime, timezone
from decimal import Decimal

from .indicators import compute_indicators
from .market_data_config import MarketDataConfig
from .schema import (
    Candle,
    FundingPoint,
    MarketRegime,
    MarketSnapshot,
    PerpMarketContext,
    derive_day_change_pct,
)
from .volume_profile import compute_volume_profile

# A 30-day window of hourly funding holds ~720 points. Require at least a day of
# samples before a z-score is meaningful; below this -> None (decision: no NaN).
MIN_FUNDING_SAMPLES = 24

_MS_PER_DAY = 24 * 60 * 60_000

# Regime thresholds (deterministic; tune in Phase 2 with paper data).
_VOLATILE_ATR_PCT = 4.0  # ATR >= 4% of price -> volatile
_TREND_EMA_SEP_PCT = 0.75  # |EMA20-EMA50| >= 0.75% of price + aligned -> trending


def funding_zscore(
    history: Sequence[FundingPoint],
    current: Decimal,
    as_of_ms: int,
    window_days: int,
) -> tuple[float | None, int]:
    """Z-score of ``current`` funding vs the trailing ``window_days`` of history.

    Returns ``(zscore_or_None, sample_count)``. ``None`` when there are fewer
    than :data:`MIN_FUNDING_SAMPLES` points in the window or the window has zero
    variance — never ``NaN``.
    """
    cutoff = as_of_ms - window_days * _MS_PER_DAY
    # Strict upper bound: a funding point landing exactly at ``as_of_ms`` is the
    # current epoch, which is also the value being z-scored (``current``). Including
    # it would fold ``current`` into its own mean/stdev and deflate the z-score, so a
    # genuine outlier reads as less extreme than it is. The window is the strictly
    # *prior* history: ``cutoff <= p.time < as_of_ms``.
    rates = [float(p.rate) for p in history if cutoff <= p.time < as_of_ms]
    count = len(rates)
    if count < MIN_FUNDING_SAMPLES:
        return None, count

    mean = statistics.fmean(rates)
    # Sample standard deviation (Bessel's n-1), not population: the window is a
    # sample drawn from the ongoing funding process, so stdev is the unbiased
    # estimator of its dispersion. Safe here — the count >= MIN_FUNDING_SAMPLES
    # gate above guarantees n >= 2, which stdev requires.
    stdev = statistics.stdev(rates)
    if stdev == 0:
        return None, count

    z = (float(current) - mean) / stdev
    if math.isnan(z) or math.isinf(z):
        return None, count
    return z, count


def classify_regime(indicators: dict[str, float | None], reference_price: Decimal) -> MarketRegime:
    """Deterministic trending / ranging / volatile label from indicators.

    ``reference_price`` should come from the same series the EMAs are built on
    (the latest candle close), not the live mark — otherwise the mark/close basis
    can flip the label near an EMA boundary. Defaults to ``RANGING`` when
    indicators are missing (insufficient candles). The names read here are
    mirrored in ``indicator_vocab.REGIME_INDICATORS`` — the config loader and
    the pre-LLM guard enforce their presence, so keep the two in sync (the
    drift-lock test in test_context_builder pins the membership).
    """
    price = float(reference_price)
    atr = indicators.get("atr_14")
    ema20 = indicators.get("ema_20")
    ema50 = indicators.get("ema_50")

    if price <= 0 or atr is None or ema20 is None or ema50 is None:
        return MarketRegime.RANGING

    if atr / price * 100 >= _VOLATILE_ATR_PCT:
        return MarketRegime.VOLATILE

    ema_sep_pct = abs(ema20 - ema50) / price * 100
    aligned = (price > ema20 > ema50) or (price < ema20 < ema50)
    if ema_sep_pct >= _TREND_EMA_SEP_PCT and aligned:
        return MarketRegime.TRENDING

    return MarketRegime.RANGING


def build_market_context(
    coin: str,
    snapshot: MarketSnapshot,
    candles: Sequence[Candle],
    funding_history: Sequence[FundingPoint],
    *,
    market_data: MarketDataConfig,
    indicator_names: Sequence[str],
    exchange_time: datetime | None,
    host_time_at_exchange_read: datetime | None = None,
) -> PerpMarketContext:
    """Build the full :class:`PerpMarketContext` from raw domain inputs.

    ``market_data`` is the parsed ``market_data:`` block. Three of its fields
    are read here: the interval the candles were fetched at (recorded on the
    context), the funding z-score window, and the volume-profile window —
    ``0`` leaves the profile off and the context's ``volume_profile`` ``None``.
    ``candle_lookback`` is the fetch's concern, not this function's. Passing
    the parsed block rather than its fields one by one keeps the defaults
    declared once (on :class:`MarketDataConfig`), so a caller cannot build a
    context from a different default than the config loader validated.

    ``exchange_time`` is the exchange's clock as read during the same fetch —
    since issue #124 the very reading the candle window was cut at (see
    ``PerpMarketContext.exchange_time``); it is carried through untouched —
    nothing here measures against it, the freshness guard does.

    REQUIRED, with no default, even though the field it fills is optional: a
    caller with no exchange clock has to write ``exchange_time=None`` and mean
    it. The default it would otherwise inherit is precisely the issue-#51 blind
    spot (the guard falls back to the caller's host clock, against which a
    window a slow host cut looks current), so it must never be reachable by
    forgetting a kwarg.
    """
    if candles:
        # Anchor the funding window on the raw epoch-ms integer, not a float
        # round-trip: ``int(datetime.fromtimestamp(close_time/1000).timestamp()*1000)``
        # can drift by 1ms and shift the strict ``p.time < as_of_ms`` boundary,
        # spuriously including/excluding a funding point landing on the candle close.
        as_of_ms = candles[-1].close_time
        as_of = datetime.fromtimestamp(as_of_ms / 1000, tz=timezone.utc)
    else:
        as_of = datetime.now(tz=timezone.utc)
        as_of_ms = int(as_of.timestamp() * 1000)

    indicators = compute_indicators(candles, indicator_names)
    zscore, sample_count = funding_zscore(
        funding_history, snapshot.funding, as_of_ms, market_data.funding_zscore_window_days
    )
    # Use the latest candle close (the EMAs' own series) so mark/close basis can't
    # flip the regime; fall back to mark only when there are no candles at all.
    regime_price = candles[-1].close if candles else snapshot.mark_price
    regime = classify_regime(indicators, regime_price)
    # Cut from the same candle series as the indicators, so the profile and the
    # regime describe the same window of history. ``None`` whenever the feature
    # is off or the window is unusable — the renderer then omits the section.
    volume_profile = compute_volume_profile(candles, market_data.volume_profile_window_candles)

    return PerpMarketContext(
        coin=coin,
        as_of=as_of,
        candle_interval=market_data.candle_interval,
        candle_count=len(candles),
        mark_price=snapshot.mark_price,
        oracle_price=snapshot.oracle_price,
        prev_day_price=snapshot.prev_day_price,
        mid_price=snapshot.mid_price,
        day_change_pct=derive_day_change_pct(snapshot.mark_price, snapshot.prev_day_price),
        open_interest=snapshot.open_interest,
        day_ntl_volume=snapshot.day_ntl_volume,
        funding_rate=snapshot.funding,
        funding_premium=snapshot.premium,
        funding_zscore_30d=zscore,
        funding_window_days=market_data.funding_zscore_window_days,
        funding_sample_count=sample_count,
        indicators=indicators,
        market_regime=regime,
        exchange_time=exchange_time,
        host_time_at_exchange_read=host_time_at_exchange_read,
        volume_profile=volume_profile,
    )
