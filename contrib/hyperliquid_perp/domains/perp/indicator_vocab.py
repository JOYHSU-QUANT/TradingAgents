"""Indicator-name vocabulary and warm-up minimums (dependency-free on purpose).

Split out of :mod:`.indicators` so the config loader can validate names
against the closed vocabulary without importing that module's pandas/
stockstats stack — ``load_config`` also runs on paths that never compute an
indicator (e.g. the ``live`` config-check mode).
"""

from __future__ import annotations

from collections.abc import Sequence

# Config indicator name -> stockstats column that computes it.
_STOCKSTATS_COLUMN = {
    "rsi_14": "rsi_14",
    "ema_20": "close_20_ema",
    "ema_50": "close_50_ema",
    "atr_14": "atr_14",
    "macd": "macd",
}

# The *minimum usable* candle count per indicator — below this we report ``None``
# rather than an obvious warm-up artifact. NOTE: these are minimums, not full
# convergence. An EMA/MACD computed at exactly its period still carries meaningful
# seed weight (e.g. EMA(50) at 50 candles anchors ~14% on the first close), so the
# value is usable-but-not-settled at the boundary; callers wanting a fully converged
# reading should warm up well beyond the period (a ~2x-period budget is typical).
_MIN_CANDLES = {
    "rsi_14": 15,
    "ema_20": 20,
    "ema_50": 50,
    "atr_14": 15,
    "macd": 26,
}


def supported_indicators() -> list[str]:
    return list(_STOCKSTATS_COLUMN)


def required_candles(names: Sequence[str]) -> int:
    """Largest warm-up candle count any of ``names`` needs (0 if none are known).

    Lets the caller refuse an engine run when fewer than this many candles are
    available, rather than reasoning over a context where every indicator is
    ``None`` (under-warmed) but the prompt looks superficially complete.
    """
    return max((_MIN_CANDLES.get(name, 0) for name in names), default=0)
