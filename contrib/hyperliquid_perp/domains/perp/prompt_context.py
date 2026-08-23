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

from .schema import MarketRegime, PerpMarketContext, ProfileShape, VolumeProfile

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
        "resizing an existing position rarely earns back its fees — size "
        "changes need high conviction."
    ),
    MarketRegime.VOLATILE: (
        "wide swings inflate the cost of reactive resizing — change the "
        "position on conviction, not on noise."
    ),
}


# What each volume-profile shape says about the window, keyed to the computed
# shape. Exhaustive over ProfileShape — a new member fails loud at render time
# rather than silently inheriting another shape's description.
#
# These are OBSERVATIONS about where volume sat, not trade advice: unlike the
# regime notes above they may name a direction (a P shape is about buyers), but
# none of them tells the model what to do about it.
_SHAPE_NOTE = {
    # D is classify_shape's CATCH-ALL, so this note must not assert anything
    # positive about the distribution. It used to open with "volume is
    # concentrated near the middle of the range"; over simulated windows most
    # D results had their POC outside the middle band, which put that sentence
    # directly next to a "POC ... (95% up the range)" line one row above.
    ProfileShape.D: (
        "catch-all — neither the P nor the b condition was met. That covers a "
        "POC near the middle of the range AND a POC skewed to one end whose "
        "latest close did not confirm the skew, so read the POC position above "
        "rather than this letter. Does not test symmetry."
    ),
    ProfileShape.P: (
        "volume built up in the upper part of the range and the latest close "
        "is above the window's midpoint — buyers absorbed the move up."
    ),
    ProfileShape.B: (
        "volume built up in the lower part of the range and the latest close "
        "is below the window's midpoint — sellers absorbed the move down."
    ),
    ProfileShape.THIN: (
        "volume is spread thinly across the range — the value area covers most "
        "of it, so no price level held the activity."
    ),
}


def _num(value, places: int = 2) -> str:
    """Format a number to ``places`` decimals; ``None`` -> ``n/a``."""
    if value is None:
        return "n/a"
    if isinstance(value, Decimal):
        value = float(value)
    return f"{value:,.{places}f}"


def _pct_of_range(fraction: float) -> str:
    """A 0-1 position within the window range, as a whole-number percentage."""
    return f"{fraction * 100:.0f}%"


def _volume_profile_lines(profile: VolumeProfile, candle_interval: str) -> list[str]:
    """The volume-profile block. Only called when a profile exists."""
    return [
        f"Volume profile (rolling window of {profile.candle_count} x {candle_interval} candles):",
        f"  Range: {_num(profile.range_low)} - {_num(profile.range_high)}",
        f"  POC (most-traded price): {_num(profile.poc)} "
        f"({_pct_of_range(profile.poc_position)} up the range)",
        f"  Value area (70% of volume): {_num(profile.value_area_low)} - "
        f"{_num(profile.value_area_high)} "
        f"({_pct_of_range(profile.value_area_width_ratio)} of the range width)",
        f"  Latest close sits {_pct_of_range(profile.close_position)} up the range",
        f"  Shape: {profile.shape.value} — {_SHAPE_NOTE[profile.shape]}",
        # The approximation is stated in the prompt on purpose: these levels are
        # derived from OHLCV bars, not from tick or footprint data, and a model
        # told only "POC: 63,450" would reasonably read it as a traded-volume
        # peak measured at that price. It was not measured; it was inferred.
        f"  Basis: each candle's volume is spread evenly across that candle's own "
        f"high-low range and bucketed into {profile.bucket_count} price levels. "
        f"This is a coarse approximation of intra-candle volume, not tick data — "
        f"treat these levels as approximate reference, not precise support or "
        f"resistance.",
    ]


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

    # Optional and last: absent whenever the feature is off or the window was
    # unusable. The WHOLE block drops out — there is no "Volume profile: n/a"
    # form, because a header with nothing under it reads as a measurement that
    # came back empty rather than one that was never taken.
    if ctx.volume_profile is not None:
        lines.append("")
        lines.extend(_volume_profile_lines(ctx.volume_profile, ctx.candle_interval))

    return "\n".join(lines)
