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
from .volume_profile import VALUE_AREA_FRACTION

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
# These are OBSERVATIONS about where volume sat, and nothing more. They state
# the geometry — which half of the range the volume built up in, which side of
# the midpoint the close landed on — and stop there, WITHOUT naming who did it
# or why.
#
# That restraint is the point, for two reasons. First, this file's standing
# rule (see the module docstring and _REGIME_NOTE) keeps directional framing
# out of these strings. Second, the causal readings are not even agreed: a P
# reads as buyers absorbing a move up, and equally as short covering at the end
# of a decline — opposite trades from identical geometry. Naming one would hand
# the model a confident story about volume that was never measured at these
# prices, only smeared across each candle's own high-low.
_SHAPE_NOTE = {
    # D is classify_shape's CATCH-ALL, so this note must not assert anything
    # positive about the distribution. It used to open with "volume is
    # concentrated near the middle of the range", which is false by inspection
    # of the rule rather than by any measurement: classify_shape reaches D for
    # a skewed POC whose close failed to confirm it, so a POC at 95% of the
    # range is a legal D — and that sentence would then sit one row under a
    # "POC ... (95% up the range)" line contradicting it. Pinned by
    # test_the_d_note_asserts_nothing_positive_about_the_distribution, which
    # renders exactly that case.
    ProfileShape.D: (
        "catch-all — neither the P nor the b condition was met. That covers a "
        "POC near the middle of the range AND a POC skewed to one end whose "
        "latest close did not confirm the skew, so read the POC position above "
        "rather than this letter. Does not test symmetry."
    ),
    ProfileShape.P: (
        "volume built up in the upper part of the range and the latest close "
        "is above the window's midpoint."
    ),
    ProfileShape.B: (
        "volume built up in the lower part of the range and the latest close "
        "is below the window's midpoint."
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


def _whole_pct(fraction: float) -> str:
    """A 0-1 fraction as a whole-number percentage.

    Deliberately NOT named for the range: the block renders positions within
    the price range AND shares of the window's volume, and both must come out
    of the same formatter or the percentages in one block would round two
    different ways. Each call site says which kind it is printing.
    """
    return f"{fraction * 100:.0f}%"


def _volume_profile_lines(profile: VolumeProfile, candle_interval: str) -> list[str]:
    """The volume-profile block. Only called when a profile exists."""
    return [
        # "as of the last closed candle" is not decoration. Every level below is
        # cut from CLOSED candles, so on a 4h interval the whole block can be up
        # to one interval behind the live mark printed further up. Those two
        # numbers come from different places and nothing reconciles them: the
        # Range is the min/max of CLOSED candles, while the mark is read from
        # the snapshot, so the mark can sit anywhere — including outside the
        # Range this block prints — and no code here would notice. How often
        # that happens is not something this file can honestly say; that it CAN
        # happen is enough reason to date the block. Same house rule as the
        # freshness disclosures elsewhere in the context: state the vintage
        # rather than let it be inferred.
        f"Volume profile (rolling window of {profile.candle_count} x {candle_interval} "
        f"candles, as of the last closed candle):",
        f"  Range: {_num(profile.range_low)} - {_num(profile.range_high)}",
        f"  POC (most-traded price): {_num(profile.poc)} "
        f"({_whole_pct(profile.poc_position)} up the range)",
        # The share is taken from VALUE_AREA_FRACTION, never written out here:
        # a literal would keep saying "70%" after the convention moved, and it
        # is the prompt — the model would be told a threshold the code no longer
        # uses. Only one test would notice, and it exists for exactly that:
        # test_the_value_area_share_is_taken_from_the_constant_not_written_out.
        f"  Value area ({_whole_pct(float(VALUE_AREA_FRACTION))} of volume): "
        f"{_num(profile.value_area_low)} - "
        f"{_num(profile.value_area_high)} "
        f"({_whole_pct(profile.value_area_width_ratio)} of the range width)",
        f"  Latest close sits {_whole_pct(profile.close_position)} up the range",
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
