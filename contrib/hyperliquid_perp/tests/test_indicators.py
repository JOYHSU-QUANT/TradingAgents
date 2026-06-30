"""Tests for indicator computation (pure functions, NaN-free output)."""

from __future__ import annotations

import math

from contrib.hyperliquid_perp.domains.perp.indicators import _clean, compute_indicators
from contrib.hyperliquid_perp.exchanges.hyperliquid import mapper

NAMES = ["rsi_14", "ema_20", "ema_50", "atr_14", "macd"]


def test_clean_non_numeric_cell_returns_none():
    # _clean guards the stockstats read: a cell that isn't float-coercible (a string
    # like "N/A", or a non-numeric object) makes float() raise ValueError/TypeError, and
    # a NaN/inf cell must not leak into the prompt. Each must degrade to None rather than
    # crash the whole compute pass or emit a non-finite indicator.
    assert _clean("N/A") is None  # ValueError branch
    assert _clean(object()) is None  # TypeError branch
    assert _clean(float("nan")) is None  # NaN guard
    assert _clean(float("inf")) is None  # inf guard
    assert _clean(None) is None
    assert _clean("3.5") == 3.5  # a numeric string still parses through


def test_all_indicators_computed_with_enough_candles(candle_snapshot):
    candles = mapper.map_candles(candle_snapshot)  # 60 candles
    out = compute_indicators(candles, NAMES)
    assert set(out) == set(NAMES)
    for name in NAMES:
        value = out[name]
        assert value is not None, f"{name} should compute with 60 candles"
        assert isinstance(value, float)
        assert not math.isnan(value) and not math.isinf(value)
    # RSI is bounded 0..100.
    assert 0.0 <= out["rsi_14"] <= 100.0


def test_insufficient_candles_returns_none_not_nan(candle_snapshot):
    candles = mapper.map_candles(candle_snapshot)[:10]  # too few for everything
    out = compute_indicators(candles, NAMES)
    # ema_50 needs 50 candles -> None (never NaN).
    assert out["ema_50"] is None
    assert all(v is None or not math.isnan(v) for v in out.values())


def test_empty_candles_all_none():
    out = compute_indicators([], NAMES)
    assert out == dict.fromkeys(NAMES)


def test_unknown_indicator_is_none(candle_snapshot):
    candles = mapper.map_candles(candle_snapshot)
    out = compute_indicators(candles, ["bollinger_99"])
    assert out["bollinger_99"] is None


def test_min_candle_boundary_per_indicator(candle_snapshot):
    # The warm-up gate is strict less-than (n < MIN -> None), so exactly MIN
    # candles must compute. An off-by-one would return a warm-up artifact.
    candles = mapper.map_candles(candle_snapshot)

    # 15 candles: rsi_14 / atr_14 (need 15) compute; the longer ones do not.
    out15 = compute_indicators(candles[:15], NAMES)
    assert out15["rsi_14"] is not None
    assert out15["atr_14"] is not None
    assert out15["ema_20"] is None  # needs 20
    assert out15["macd"] is None  # needs 26
    assert out15["ema_50"] is None  # needs 50

    # 20 candles: ema_20 now computes; ema_50 still does not.
    out20 = compute_indicators(candles[:20], ["ema_20", "ema_50"])
    assert out20["ema_20"] is not None
    assert out20["ema_50"] is None
