"""Render a :class:`PerpMarketContext` into prompt text for the engine.

Every number is annotated (units, basis points, z-score) so the model reads
context instead of re-deriving it. ``None`` values render as ``n/a`` — we never
print ``NaN``.

NOTE: the funding wording here is a deliberately **neutral placeholder**. The
real funding framing (the strategy's edge) is dropped in privately later; keep
this file free of any directional funding interpretation.
"""

from __future__ import annotations

from decimal import Decimal

from .schema import MarketRegime, PerpMarketContext

_INDICATOR_LABEL = {
    "rsi_14": "RSI(14)",
    "ema_20": "EMA(20)",
    "ema_50": "EMA(50)",
    "atr_14": "ATR(14)",
    "macd": "MACD",
}

# Cost-awareness note keyed to the computed regime (paper-tuning, 2026-07).
# Behavioral, not directional — keep long/short framing out of these strings.
# The mapping is exhaustive over MarketRegime; a new member fails loud at
# render time instead of silently inheriting another regime's advice.
_REGIME_NOTE = {
    MarketRegime.TRENDING: (
        "holding an established position with the trend usually beats frequent adjustment."
    ),
    MarketRegime.RANGING: (
        "resizing an existing position rarely earns back its fees — prefer "
        "maintain_current unless conviction is high."
    ),
    MarketRegime.VOLATILE: (
        "wide swings inflate the cost of reactive resizing — change the "
        "position on conviction, not on noise."
    ),
}


def _num(value, places: int = 2) -> str:
    """Format a number to ``places`` decimals; ``None`` -> ``n/a``."""
    if value is None:
        return "n/a"
    if isinstance(value, Decimal):
        value = float(value)
    return f"{value:,.{places}f}"


def _funding_bps(rate: Decimal | None) -> str:
    """Funding as basis points (rate * 1e4). ``None`` -> ``n/a``."""
    if rate is None:
        return "n/a"
    return f"{float(rate) * 1e4:,.4f} bps"


def render_market_context(ctx: PerpMarketContext) -> str:
    """Return the human/LLM-readable perp context block."""
    lines: list[str] = []
    lines.append(f"Coin: {ctx.coin} (perpetual)")
    lines.append(f"As of: {ctx.as_of.isoformat()} UTC")
    lines.append(f"Candles: {ctx.candle_count} x {ctx.candle_interval}")
    lines.append("")

    lines.append("Price:")
    lines.append(f"  Mark: {_num(ctx.mark_price)}")
    lines.append(f"  Oracle: {_num(ctx.oracle_price)}")
    if ctx.mid_price is not None:
        lines.append(f"  Mid: {_num(ctx.mid_price)}")
    lines.append(f"  Prev-day: {_num(ctx.prev_day_price)}")
    lines.append(f"  24h change: {_num(ctx.day_change_pct)}%")
    lines.append("")

    lines.append("Market:")
    lines.append(f"  Open interest: {_num(ctx.open_interest)}")
    lines.append(f"  24h notional volume: {_num(ctx.day_ntl_volume)}")
    lines.append(f"  Regime (computed): {ctx.market_regime.value}")
    lines.append(f"  Regime note: {_REGIME_NOTE[ctx.market_regime]}")
    lines.append("")

    # Neutral funding wording — placeholder; do not add directional framing here.
    lines.append("Funding:")
    lines.append(f"  Current rate: {_funding_bps(ctx.funding_rate)} (per hour)")
    if ctx.funding_premium is not None:
        lines.append(f"  Premium: {_num(ctx.funding_premium, 6)}")
    z = ctx.funding_zscore_30d
    z_text = "n/a (insufficient data)" if z is None else f"{z:+.2f}"
    lines.append(f"  {ctx.funding_window_days}d z-score: {z_text} (n={ctx.funding_sample_count})")
    lines.append("")

    lines.append("Indicators:")
    for name, value in ctx.indicators.items():
        label = _INDICATOR_LABEL.get(name, name)
        lines.append(f"  {label}: {_num(value, 4)}")

    return "\n".join(lines)
