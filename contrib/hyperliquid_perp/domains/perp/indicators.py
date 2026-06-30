"""Technical indicators over Hyperliquid candles.

Reuses the same engine ``tradingagents`` uses — ``stockstats.wrap`` — so the
numbers match the rest of the project and we add no new dependency
(``tradingagents/dataflows/stockstats_utils.py`` wraps a DataFrame the same way;
its public helper is hard-wired to yfinance loading and ``tradingagents/`` is
read-only, so we mirror just the ``wrap()`` call here over HL candles).

Indicator names are the project's config names (e.g. ``"ema_20"``); this module
maps them to the stockstats column that computes them. A value that can't be
computed (too few candles) comes back as ``None`` — never ``NaN`` — so nothing
downstream leaks ``NaN`` into a prompt.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Sequence

import pandas as pd
from stockstats import wrap

from .schema import Candle

logger = logging.getLogger(__name__)

# Config indicator name -> stockstats column that produces it.
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


def _candles_to_frame(candles: Sequence[Candle]) -> pd.DataFrame:
    """Build the OHLCV frame stockstats expects (floats; oldest row first)."""
    return pd.DataFrame(
        {
            "open": [float(c.open) for c in candles],
            "high": [float(c.high) for c in candles],
            "low": [float(c.low) for c in candles],
            "close": [float(c.close) for c in candles],
            "volume": [float(c.volume) for c in candles],
        }
    )


def _clean(value) -> float | None:
    """Coerce a stockstats cell to a finite float, or ``None``."""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) or math.isinf(f) else f


def compute_indicators(candles: Sequence[Candle], names: Sequence[str]) -> dict[str, float | None]:
    """Compute the latest value of each requested indicator.

    Returns a dict keyed by the requested names. Unknown names and indicators
    without enough candles map to ``None``.
    """
    result: dict[str, float | None] = dict.fromkeys(names)
    if not candles:
        return result

    frame = _candles_to_frame(candles)
    stats = wrap(frame)
    n = len(candles)

    for name in names:
        column = _STOCKSTATS_COLUMN.get(name)
        if column is None:
            # A name with no stockstats column (e.g. a YAML typo like "rsi14" for
            # "rsi_14") otherwise maps to None silently — the prompt then shows the
            # whole signal as "unavailable" with no hint that the *name* was wrong
            # rather than the candle history being too short. Leave a trace.
            logger.warning(
                "indicator %r is not a recognized indicator (no stockstats column); "
                "reporting it as unavailable — check for a typo in the configured "
                "indicator names.",
                name,
            )
            continue  # unknown indicator -> stays None
        if n < _MIN_CANDLES.get(name, 1):
            continue  # not enough warm-up -> None, never a NaN artifact
        try:
            series = stats[column]
        except Exception as exc:  # noqa: BLE001 — a bad column shouldn't crash the build
            # Leave a trace: a silently-missing indicator is otherwise invisible. Call
            # out atr_14 specifically — its absence is not just "one missing number":
            # classify_regime falls back to RANGING (hiding a volatile market) and
            # _invalidation_price returns None (no ATR stop-loss), so a bare "failed"
            # would understate the blast radius.
            consequence = (
                " — regime will default to RANGING and the ATR stop-loss is disabled"
                if name == "atr_14"
                else ""
            )
            logger.warning(
                "indicator %r (stockstats column %r) failed: %s%s",
                name,
                column,
                exc,
                consequence,
                exc_info=True,
            )
            continue
        result[name] = _clean(series.iloc[-1]) if len(series) else None

    return result
