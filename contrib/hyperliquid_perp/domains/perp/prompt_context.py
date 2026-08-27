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

from .schema import (
    MarketRegime,
    PerpMarketContext,
    PositionContext,
    ProfileShape,
    VolumeProfile,
    derive_round_trip_rate,
)
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
# Each note states what its own rule TESTED, and nothing more. Note what that
# is NOT: none of them says where the bulk of the volume sat. P and b test
# where the single heaviest BUCKET sits; thin tests how WIDE the value area
# came out; D is the catch-all and tests nothing positive at all. The close
# clause is the one direct observation, and it is only about the close.
#
# They also stop short of naming who did it or why.
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
    # Same discipline as D, for the same reason: each note may state only what
    # its rule TESTED. P and b are decided by where the single heaviest BUCKET
    # sits, which is not a claim about where the bulk of the volume sat — a
    # window can put its heaviest bucket at 60% of the range with most of the
    # volume below the midpoint, and the value-area line rendered directly above
    # would then contradict a note saying "volume built up in the upper part".
    # thin is decided by the value area being WIDE, which a two-cluster window
    # with a quiet middle also achieves while its volume is in fact highly
    # concentrated. Both cases are pinned by tests in test_prompt_context.
    ProfileShape.P: (
        "the heaviest single price bucket sits in the upper part of the range "
        "and the latest close is above the window's midpoint. Says nothing "
        "about where the bulk of the volume sat."
    ),
    ProfileShape.B: (
        "the heaviest single price bucket sits in the lower part of the range "
        "and the latest close is below the window's midpoint. Says nothing "
        "about where the bulk of the volume sat."
    ),
    ProfileShape.THIN: (
        "the value area spans most of the range — the walk out from the POC "
        "ended up that wide. Width is a property of the walk, not proof the "
        "volume needed the range: between equal neighbours the walk expands "
        "upward, so it can cross near-empty buckets, and a window holding two "
        "separate clusters with a quiet middle also lands here."
    ),
}


def _num(value, places: int = 2, *, sign: bool = False) -> str:
    """Format a number to ``places`` decimals; ``None`` -> ``n/a``.

    ``sign`` forces an explicit ``+``/``-`` (a PnL, never a price).
    """
    if value is None:
        return "n/a"
    if isinstance(value, Decimal):
        value = float(value)
    return f"{value:{'+' if sign else ''},.{places}f}"


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
        # NOT "most-traded price". The POC is the MIDPOINT of the heaviest
        # bucket, and that midpoint can be a price the window never traded: a
        # window whose heavy bars are all zero-range prints at 112.05 still
        # reports the bucket [112, 113)'s midpoint, 112.50. Naming it the
        # most-traded price states a measurement that was never made — the
        # bucket is what was measured.
        f"  POC (midpoint of the heaviest price bucket): {_num(profile.poc)} "
        f"({_whole_pct(profile.poc_position)} up the range)",
        # The share is taken from VALUE_AREA_FRACTION, never written out here:
        # a literal would keep saying "70%" after the convention moved, and it
        # is the prompt — the model would be told a threshold the code no longer
        # uses. Only one test would notice, and it exists for exactly that:
        # test_the_value_area_share_is_taken_from_the_constant_not_written_out.
        # "at least" is load-bearing, not hedging: the walk stops on the FIRST
        # bucket that crosses the target, so the band holds >= the share, never
        # == it except by coincidence. A one-bucket value area holding 99% of
        # the window would otherwise be labelled "70% of volume", telling the
        # model the other 30% sits outside a band that in truth excludes 1% —
        # inverting the concentration reading this block exists to convey.
        f"  Value area (band holding at least "
        f"{_whole_pct(float(VALUE_AREA_FRACTION))} of volume): "
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


def _position_lines(pos: PositionContext, ctx: PerpMarketContext) -> list[str]:
    """The ``Position:`` section. Only called when a position context exists.

    Facts and prices only. What is deliberately NOT here: any sentence about
    which gate bar a target faces (the open / flip / flat exemptions), any
    reading of the position ("underwater", "winning"), and any accumulated
    cost — the marginal cost of the NEXT move is the only cost printed.
    ``test_the_position_section_never_names_a_gate_threshold`` holds the
    first of those; the module's standing rule holds the second.
    """
    lines = ["Position:"]
    if pos.side is None:
        lines.append(f"  flat, no open position (account equity {_num(pos.equity)} USDC)")
        return lines
    # Open: the DTO's own guards make these three non-None; narrowed once for
    # the type checker rather than re-checked line by line.
    unrealized, holding = pos.unrealized_pnl, pos.holding_cost_8h
    assert unrealized is not None and holding is not None
    lines.append(
        f"  Side: {pos.side.value}, size {abs(pos.size)} {ctx.coin}, "
        f"notional {_num(pos.notional)} USDC at mark"
    )
    lines.append(
        f"  Entry: {_num(pos.entry_price)} (unrealized PnL {_num(unrealized, sign=True)} USDC)"
    )
    lines.append(
        f"  Committed margin: {_num(pos.margin_pct)}% of account equity "
        f"{_num(pos.equity)} USDC (at the configured {_num(pos.leverage, 0)}x leverage)"
    )
    if pos.last_fill_at is None:
        lines.append("  Last fill: none recorded for this run")
    else:
        # Against the context's own as-of (the last closed candle), the same
        # vintage every other line here is dated to. A fill booked AFTER
        # that close is possible (an order filled minutes ago against a
        # candle that closed hours ago) and is said so rather than shown as
        # a negative age.
        age_hours = (ctx.as_of - pos.last_fill_at).total_seconds() / 3600
        when = (
            f"{age_hours:.1f} hours before the as-of time above"
            if age_hours >= 0
            else "after the as-of time above"
        )
        lines.append(f"  Last fill: {pos.last_fill_at.isoformat()} UTC ({when})")
    if holding > 0:
        verb = f"pays {_num(holding, 4)} USDC"
    elif holding < 0:
        verb = f"receives {_num(-holding, 4)} USDC"
    else:
        verb = "0.0000 USDC (funding rate is zero)"
    lines.append(f"  Holding cost at the current funding rate: {verb} per 8h")
    rate = derive_round_trip_rate(pos.taker_fee_rate, pos.slippage_bps)
    rate_bps = rate * 10_000
    fee_pct = pos.taker_fee_rate * 100
    # "assumptions", not "fees": these are the configured fill-cost parameters
    # (paper_trading.execution — the paper fill model's), and on the live lane
    # the exchange's actual per-fill fee is what the books post. The word
    # keeps the line true on both lanes; the numbers are still the only cost
    # model the config carries.
    lines.append(
        f"  Cost of moving to another legal margin, as a round trip (the fee and "
        f"slippage on this move plus the same again when it is later reversed), "
        f"priced with the configured fill-cost assumptions: taker fee "
        f"{_num(fee_pct, 4)}% and {_num(pos.slippage_bps)} bps slippage per fill, "
        f"{_num(rate_bps)} bps of the traded notional in total. Breakeven is the "
        f"favourable price move, in bps of the traded notional, that exactly pays "
        f"for that round trip:"
    )
    for row in pos.cost_rows:
        lines.append(
            f"    -> {row.target_margin_pct}%: trades {_num(row.trade_notional)} USDC "
            f"notional, round-trip cost {_num(row.round_trip_cost)} USDC, "
            f"breakeven {_num(rate_bps)} bps"
        )
    # The table is sampled when the grid is fine (marginal_cost.MAX_COST_ROWS);
    # the per-point rate is what makes every legal target in between priced
    # rather than merely implied. Cost is exactly linear in the distance
    # moved, so this is arithmetic the rows above already obey, not a claim.
    per_point_notional = pos.equity * pos.leverage / 100
    per_point_cost = per_point_notional * rate
    lines.append(
        f"  Every 1 percentage point of margin moved trades {_num(per_point_notional)} USDC "
        f"and costs {_num(per_point_cost, 4)} USDC round trip; a legal target between "
        f"two rows costs in proportion to its distance from the current margin."
    )
    return lines


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

    # The account's own position, last: it is the one section about the
    # decision rather than the market, and it sits directly above the output
    # contract that asks for a target. Optional like the profile — absent
    # whenever no position source was wired or the books were unusable — and
    # for the same reason it drops out whole (see PositionContext).
    if ctx.position is not None:
        lines.append("")
        lines.extend(_position_lines(ctx.position, ctx))

    return "\n".join(lines)


def context_shape(ctx: PerpMarketContext) -> str:
    """The STRUCTURE of what :func:`render_market_context` prints for ``ctx``.

    One canonical string, e.g.
    ``price|market|funding|indicators(rsi_14,ema_20,macd)|volume_profile``:
    the fixed sections in render order, the indicator rows by configured name
    (in render order — a reorder is a different prompt), and the optional
    volume-profile section when it is present. It is stored beside
    ``prompt_version`` on every ``ai_inputs`` row (issue #97) so the paper
    review can segment on ``(prompt_version, context_shape)`` and a
    config-only change that adds or removes a section — flipping
    ``market_data.volume_profile_window_candles``, editing ``indicators`` —
    lands in the data by itself, with no code deploy and nobody remembering
    to bump anything.

    What it deliberately does NOT cover, so that it changes only when the
    prompt's shape does:

    - the numbers inside labels (``Candles: 200 x 4h``, ``30d z-score``) —
      those are content; a change there is what the config-drift warning on
      resume already names;
    - the ``Mid:`` and ``Premium:`` lines, which drop out per cycle on data
      availability, not on configuration — folding them in would split one
      regime into two on a flaky mid read;
    - the indicator VALUES, so a dead indicator rendering ``n/a`` is the same
      shape as a live one.

    One thing it does NOT smooth over: the volume-profile section is read off
    ``ctx.volume_profile``, which the builder leaves ``None`` not only when
    the window is configured off but also on a cycle whose window was
    unusable (too few candles, zero price width, zero volume — each logged as
    a WARNING by ``volume_profile``). That cycle's prompt really had no such
    section, so it files under the no-profile shape: the shape describes the
    prompt the model was shown, not the configuration. A run with the window
    on and an occasional skip will show those cycles as a small second bucket
    next to the WARNING that explains them.

    The position section (prompt ``phase2-target-v4``) files as one shape,
    ``position``, whether the account is open (cost table) or flat (one
    line). Open-vs-flat changes what the section prints, but it is the
    account's STATE, which alternates within a run cycle by cycle — folding
    it in would split one run into two buckets on nothing the operator
    configured, exactly what the ``Mid:`` / ``Premium:`` rule above forbids,
    and would break the paper review's "one shape per run" reading. The
    review splits open from flat on ``ai_inputs.current_position_side``,
    which every row already carries.

    The section names here are the render's own headers, lower-cased; the
    prompt-context tests hold the two in lockstep in both directions.
    """
    parts = [
        "price",
        "market",
        "funding",
        f"indicators({','.join(ctx.indicators)})",
    ]
    if ctx.volume_profile is not None:
        parts.append("volume_profile")
    if ctx.position is not None:
        parts.append("position")
    return "|".join(parts)
