"""Tests for the funding z-score and full context build (pure functions)."""

from __future__ import annotations

import math
from decimal import Decimal

import pytest

from contrib.hyperliquid_perp.domains.perp.context_builder import (
    MIN_FUNDING_SAMPLES,
    build_market_context,
    classify_regime,
    funding_zscore,
)
from contrib.hyperliquid_perp.domains.perp.schema import FundingPoint
from contrib.hyperliquid_perp.exchanges.hyperliquid import mapper

_H = 3600_000


def _points(rates, end_ms):
    """Build hourly FundingPoints strictly before end_ms (oldest first; the newest
    sits one hour *before* end_ms).

    Funding settles in the past relative to the current epoch ``as_of_ms`` (the last
    candle close), and :func:`funding_zscore` now excludes a point landing exactly on
    ``as_of_ms`` (strict upper bound). Placing the newest sample one hour before keeps
    every point inside the ``cutoff <= t < end_ms`` window, so sample values/counts are
    unchanged by the bound switch and the z-score assertions still hold.
    """
    n = len(rates)
    return [
        FundingPoint(time=end_ms - (n - i) * _H, rate=Decimal(str(r))) for i, r in enumerate(rates)
    ]


def test_zscore_insufficient_samples_returns_none():
    end = 1_700_000_000_000
    pts = _points([0.00001] * (MIN_FUNDING_SAMPLES - 1), end)
    z, count = funding_zscore(pts, Decimal("0.00002"), end, 30)
    assert z is None
    assert count == MIN_FUNDING_SAMPLES - 1


def test_zscore_zero_variance_returns_none():
    end = 1_700_000_000_000
    pts = _points([0.00001] * (MIN_FUNDING_SAMPLES + 10), end)  # all identical -> std 0
    z, count = funding_zscore(pts, Decimal("0.00005"), end, 30)
    assert z is None
    assert count == MIN_FUNDING_SAMPLES + 10


def test_zscore_known_value():
    end = 1_700_000_000_000
    # 30 points spread around a mean -> finite, non-NaN z-score.
    rates = [0.00001, 0.00002, 0.00003] * 10
    pts = _points(rates, end)
    z, count = funding_zscore(pts, Decimal("0.00004"), end, 30)
    assert z is not None
    assert not math.isnan(z) and not math.isinf(z)
    assert count == 30
    assert z > 0  # current 0.00004 is above the window mean (0.00002)
    # Pin the exact value to lock the *sample* stdev (Bessel n-1) estimator: with
    # population stdev this would be ~2.449, so this guards against a silent revert.
    assert z == pytest.approx(2.408, abs=0.005)


def test_zscore_excludes_points_outside_window():
    end = 1_700_000_000_000
    inside = _points([0.00001, 0.00002, 0.00003] * 10, end)
    # An old point well before the 30-day window should be ignored.
    old = FundingPoint(time=end - 60 * 24 * _H, rate=Decimal("0.05"))
    _z, count = funding_zscore([old, *inside], Decimal("0.00002"), end, 30)
    assert count == 30  # the stale point dropped


def test_zscore_includes_point_exactly_on_window_boundary():
    end = 1_700_000_000_000
    inside = _points([0.00001, 0.00002, 0.00003] * 10, end)  # 30 points near end
    cutoff = end - 30 * 24 * 60 * 60_000  # window start (inclusive)
    boundary = FundingPoint(time=cutoff, rate=Decimal("0.00002"))
    just_outside = FundingPoint(time=cutoff - 1, rate=Decimal("0.05"))
    # The boundary point counts; the one 1ms earlier does not (cutoff <= t < end).
    _z, count = funding_zscore([just_outside, boundary, *inside], Decimal("0.00002"), end, 30)
    assert count == 31


def test_zscore_excludes_point_exactly_at_as_of():
    end = 1_700_000_000_000
    inside = _points([0.00001, 0.00002, 0.00003] * 10, end)  # 30 points strictly before end
    # A funding point landing exactly on as_of_ms is the current epoch — the same value
    # being z-scored — so it must be excluded from the historical sample pool (strict
    # upper bound), otherwise current is folded into its own mean/stdev.
    at_now = FundingPoint(time=end, rate=Decimal("0.05"))
    _z, count = funding_zscore([at_now, *inside], Decimal("0.00002"), end, 30)
    assert count == 30  # at_now dropped; only the 30 strictly-prior points remain


def test_classify_regime_volatile():
    # ATR 5% of price -> volatile.
    out = classify_regime(
        {"atr_14": 3000.0, "ema_20": 60000.0, "ema_50": 60000.0}, Decimal("60000")
    )
    assert out == "volatile"


def test_classify_regime_trending_up():
    ind = {"atr_14": 300.0, "ema_20": 61000.0, "ema_50": 60000.0}
    assert classify_regime(ind, Decimal("62000")) == "trending"


def test_classify_regime_defaults_ranging_when_missing():
    out = classify_regime({"atr_14": None, "ema_20": None, "ema_50": None}, Decimal("60000"))
    assert out == "ranging"


def test_classify_regime_trending_down():
    # price < ema20 < ema50 with enough EMA separation -> trending (bearish). This
    # is the primary short-bias signal; misclassifying it suppresses the short.
    ind = {"atr_14": 300.0, "ema_20": 60000.0, "ema_50": 61000.0}
    assert classify_regime(ind, Decimal("59000")) == "trending"


def test_classify_regime_ranging_with_present_but_unaligned_indicators():
    # Low ATR (not volatile) and EMAs too close together -> ranging, not trending.
    ind = {"atr_14": 100.0, "ema_20": 60000.0, "ema_50": 60100.0}
    assert classify_regime(ind, Decimal("60050")) == "ranging"


def test_classify_regime_wide_ema_sep_but_price_between_emas_is_ranging():
    # EMA separation is well past the trend threshold (~2.5%) but price sits
    # between the EMAs (not aligned) -> ranging. Without the alignment gate this
    # would mislabel a non-trending market as trending and suppress urgency.
    ind = {"atr_14": 100.0, "ema_20": 61500.0, "ema_50": 60000.0}
    assert classify_regime(ind, Decimal("61000")) == "ranging"  # 60000 < 61000 < 61500


def test_classify_regime_trending_at_exact_ema_sep_threshold():
    # ema_sep_pct == 0.75% exactly with bullish alignment -> trending (>= is
    # inclusive); a strict `>` slip would misclassify the boundary as ranging.
    # |59900 - 59450| / 60000 * 100 == 0.75 and 60000 > 59900 > 59450.
    ind = {"atr_14": 100.0, "ema_20": 59900.0, "ema_50": 59450.0}
    assert classify_regime(ind, Decimal("60000")) == "trending"


def test_classify_regime_volatile_at_exact_atr_threshold():
    # ATR == exactly 4% of price must be VOLATILE (>= is inclusive). A `>` slip
    # would misclassify the boundary as ranging, dropping urgency from HIGH to LOW
    # and suppressing the volatility risk string.
    ind = {"atr_14": 60000 * 4.0 / 100, "ema_20": 60000.0, "ema_50": 60000.0}
    assert classify_regime(ind, Decimal("60000")) == "volatile"


def test_classify_regime_just_below_volatile_threshold_is_not_volatile():
    # ATR == 3.99% of price must NOT be volatile (guards the inclusive boundary
    # from drifting the other way).
    ind = {"atr_14": 60000 * 3.99 / 100, "ema_20": 60000.0, "ema_50": 60000.0}
    assert classify_regime(ind, Decimal("60000")) != "volatile"


def test_build_market_context_end_to_end(meta_and_asset_ctxs, candle_snapshot, funding_history):
    snapshot = mapper.map_market_snapshot(meta_and_asset_ctxs, "BTC")
    candles = mapper.map_candles(candle_snapshot)
    funding = mapper.map_funding_history(funding_history)

    ctx = build_market_context(
        "BTC",
        snapshot,
        candles,
        funding,
        candle_interval="4h",
        funding_window_days=30,
        indicator_names=["rsi_14", "ema_20", "ema_50", "atr_14", "macd"],
    )

    assert ctx.coin == "BTC"
    assert ctx.candle_count == len(candles)
    # as_of is derived from the last candle close (deterministic, no wall clock).
    assert int(ctx.as_of.timestamp() * 1000) == candles[-1].close_time
    # No NaN anywhere that would reach the prompt.
    for value in ctx.indicators.values():
        assert value is None or not math.isnan(value)
    assert ctx.funding_zscore_30d is None or not math.isnan(ctx.funding_zscore_30d)
    # With 200 hourly funding points in-window, the z-score should compute.
    assert ctx.funding_zscore_30d is not None
    assert ctx.funding_sample_count >= MIN_FUNDING_SAMPLES
    assert ctx.market_regime in {"trending", "ranging", "volatile"}


def test_build_market_context_with_zero_candles(meta_and_asset_ctxs, funding_history):
    # No candles (newly listed coin / too-recent startTime): the context must still
    # build a valid, usable shape rather than raise. Indicators are all None, regime
    # falls back to RANGING, and as_of comes from the wall clock (not a candle close).
    snapshot = mapper.map_market_snapshot(meta_and_asset_ctxs, "BTC")
    funding = mapper.map_funding_history(funding_history)

    ctx = build_market_context(
        "BTC",
        snapshot,
        [],
        funding,
        candle_interval="4h",
        funding_window_days=30,
        indicator_names=["rsi_14", "atr_14"],
    )

    assert ctx.candle_count == 0
    assert all(value is None for value in ctx.indicators.values())
    assert ctx.market_regime == "ranging"
    # regime_price falls back to the snapshot mark when there are no candles.
    assert ctx.mark_price == snapshot.mark_price
    # No NaN ever reaches the prompt, even on the degenerate empty-window path.
    assert ctx.funding_zscore_30d is None or not math.isnan(ctx.funding_zscore_30d)


def test_build_market_context_funding_window_days_plumbs_to_zscore(
    meta_and_asset_ctxs, candle_snapshot, funding_history
):
    # The funding_window_days config value must actually reach the z-score window. A
    # full 30d window computes a z-score over the in-window points; a 0d window collapses
    # the window to empty (cutoff == as_of_ms) and yields None with n=0. A regression
    # that ignored the param would leave the z-score unchanged between the two and
    # silently suppress (or fix) the window — invisible in the rendered prompt.
    snapshot = mapper.map_market_snapshot(meta_and_asset_ctxs, "BTC")
    candles = mapper.map_candles(candle_snapshot)
    funding = mapper.map_funding_history(funding_history)

    def _ctx_with_window(days):
        return build_market_context(
            "BTC",
            snapshot,
            candles,
            funding,
            candle_interval="4h",
            funding_window_days=days,
            indicator_names=["rsi_14"],
        )

    wide = _ctx_with_window(30)
    assert wide.funding_zscore_30d is not None
    assert wide.funding_sample_count >= MIN_FUNDING_SAMPLES

    collapsed = _ctx_with_window(0)
    assert collapsed.funding_zscore_30d is None
    assert collapsed.funding_sample_count == 0
    assert collapsed.funding_window_days == 0  # the param is recorded on the context


def test_context_indicators_are_read_only(meta_and_asset_ctxs, candle_snapshot, funding_history):
    # PerpMarketContext is frozen; indicators must reject in-place mutation too,
    # so the immutability the frozen flag advertises actually holds.
    snapshot = mapper.map_market_snapshot(meta_and_asset_ctxs, "BTC")
    ctx = build_market_context(
        "BTC",
        snapshot,
        mapper.map_candles(candle_snapshot),
        mapper.map_funding_history(funding_history),
        candle_interval="4h",
        funding_window_days=30,
        indicator_names=["rsi_14"],
    )
    with pytest.raises(TypeError):
        ctx.indicators["rsi_14"] = 99.9
