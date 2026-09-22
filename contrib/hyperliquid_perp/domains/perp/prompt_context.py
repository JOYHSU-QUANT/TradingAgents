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

from ...common.instants import delta_ms, from_epoch_ms, gap_label
from .schema import (
    MacroAlignment,
    MacroTrend,
    MarketRegime,
    PerpMarketContext,
    PositionContext,
    ProfileShape,
    ResearchSignal,
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


# The decimal places ``_num`` prints to unless a caller says otherwise. Named
# because ``_signed_pct`` has to know where ``_num`` rounds a percentage to a
# bare zero, and a second literal 2 there could drift from this one. It is
# ``_num``'s DEFAULT, not a house rule for percentages: ``_whole_pct`` prints
# at zero places, and several call sites pass their own.
_DEFAULT_PLACES = 2


def _num(value, places: int = _DEFAULT_PLACES, *, sign: bool = False) -> str:
    """Format a number to ``places`` decimals; ``None`` -> ``n/a``.

    ``sign`` forces an explicit ``+``/``-`` — for a value whose direction is
    part of the reading (a PnL, a percentage against a reference), never for a
    price.
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


# Which way round the two daily averages are, as one word. Exhaustive over
# MacroAlignment — a new member fails loud at render time rather than silently
# inheriting the other's word.
#
# One word each, and no second clause: this is the whole vocabulary the
# section is allowed for the ordering. "golden"/"death", "bullish"/"bearish"
# and "confirmed" are the words a reader supplies for themselves and that this
# rule never measured — it measured which of two averages is the larger. Same
# restraint as ``_SHAPE_NOTE`` above, and pinned by a test that renders the
# block and looks for those words.
_MACRO_ALIGNMENT_WORD = {
    MacroAlignment.ABOVE: "above",
    MacroAlignment.BELOW: "below",
}


# The magnitude below which ``_num`` rounds a percentage to a bare zero.
# DERIVED from the same constant ``_num`` takes as its default, rather than
# written out as 0.005, so the two cannot desync if the places ever change.
_PCT_ROUNDS_TO_ZERO_BELOW = 10 ** -_DEFAULT_PLACES / 2


def _signed_pct(value: float) -> str:
    """A signed percentage, never rounded into a bare signed zero.

    ``_num``'s default precision is right for the usual case and wrong for the
    one this section exists to surface: at a crossing the separation passes
    through zero, so anything inside half of its last place renders as a bare
    signed zero — a figure that reads as "no gap" on the same line as a word
    asserting a strict ordering. The DTO refuses only a BIT-EXACT tie, so that
    window is reachable on any cycle near a crossing. The sign always comes
    from the value, so it cannot disagree with the direction word beside it.
    """
    if 0 < abs(value) < _PCT_ROUNDS_TO_ZERO_BELOW:
        # Two significant FIGURES rather than two decimal places, so the value
        # keeps its own magnitude however small it is (``+1.2e-05``) instead
        # of collapsing to a zero it is not.
        return f"{value:+.2g}"
    return _num(value, sign=True)


def _macro_trend_lines(macro: MacroTrend, candle_interval: str) -> list[str]:
    """The macro-trend block. Only called when a macro trend exists.

    ``candle_interval`` is the interval the CANDLE-derived lines above this
    block are cut from — the indicators and the regime label, not the
    snapshot-derived price and funding lines — named in the basis note so "its
    own daily series" is a contrast a reader can check rather than a claim.
    Taken from the context, never written out as ``4h``: the interval is
    configurable, and a literal here would go on asserting today's value after
    it moved.
    """
    fast, slow = macro.fast_period, macro.slow_period
    bars = macro.bars_in_state
    unit = "bar" if bars == 1 else "bars"
    if macro.state_age_capped:
        # No NUMBER on this branch. The run length here is bounded by the
        # window, not by the market: at the configured floor it is always 1,
        # and when the venue short-reads 203 of a requested 400 it comes out
        # as "4" for an alignment that may be two years old — a feed artefact
        # rendered as a freshly turned trend. The window itself is stated in
        # the header, which is where a reader can see what bounded it.
        held = (
            "  Held for: longer than this window can date — the alignment holds on every "
            "bar of it that has both averages, so no change is visible from here"
        )
    else:
        # ``state_age_capped`` is False exactly when this date exists
        # (``MacroTrend`` enforces the equivalence), so the narrowing is the
        # DTO's guarantee rather than an assumption of this branch.
        assert macro.run_started_date is not None
        # "Began on", not "changed on". The bar before this run carried
        # something else — usually the opposite alignment, occasionally an
        # exact tie between the two averages — and "changed" would read as a
        # turn in both cases while only the first is one. What the rule
        # measured is where this run starts.
        held = (
            f"  Held for: {bars} daily {unit} (this run began on the bar dated "
            f"{macro.run_started_date.isoformat()})"
        )
    return [
        # Two facts in the header. The WINDOW, because every "held for" reading
        # is relative to it and because both sibling blocks state theirs
        # (``Volume profile (rolling window of N x 4h candles…)``, ``Candles:
        # N x 4h``) — without it the capped line above is an unmeasurable
        # claim. And the DATE of the newest closed daily bar, like the volume
        # profile's "as of the last closed candle" but for a stronger version
        # of the same reason: a daily bar closes once a day, so this block can
        # be a whole day behind the As-of line above it — and further still
        # behind the live Mark, which nothing bounds it against (see the
        # vintage clause in the Basis note). It is also cut from a different
        # fetch than everything above it. Printed rather than described, so
        # the reader can measure that lag instead of assuming it.
        f"Macro trend (its own series of {macro.candle_count} daily candles, SMA({fast}) "
        f"vs SMA({slow}), newest daily bar dated {macro.as_of_date.isoformat()}):",
        f"  SMA({fast}): {_num(macro.sma_fast)}   SMA({slow}): {_num(macro.sma_slow)}",
        # The separation is printed as the SIGNED difference over the slow
        # average, spelled as the subtraction it is. Writing it unsigned under
        # the word "below" would read as a magnitude whose sign the reader has
        # to reconstruct from the word, and the two could then disagree
        # without either line being wrong on its own.
        f"  Alignment: SMA({fast}) {_MACRO_ALIGNMENT_WORD[macro.alignment]} SMA({slow}); "
        f"SMA({fast}) - SMA({slow}) is {_signed_pct(macro.separation_pct)}% of SMA({slow})",
        held,
        # The percentage only. The absolute close is deliberately NOT printed:
        # it would be a third price level entering the prompt from this block,
        # up to 24h stale, a dozen lines under the live ``Mark:`` with nothing
        # reconciling the two — and the standing reason is in
        # ``_research_signal_lines``, which withholds its own figures because
        # this prompt's history is that the model anchors on the numbers it is
        # shown (paper-BTC-2: 27 of 48 decisions at exactly the advertised bar).
        f"  Latest daily close vs SMA({slow}): {_signed_pct(macro.close_vs_slow_pct)}%",
        # Six disclosures, each of which a reader would otherwise have to
        # assume: which candles these came from, that this prompt's OTHER
        # trend reading is independent of this one, that the measure lags by
        # construction, what its vintage is bounded against, that gaps in the
        # daily series are not checked (the producer says the same in its
        # docstring), and that nothing here is wired to a decision — closing
        # with how to weigh it, which is the one thing the model has to decide
        # and the one thing the six before it do not answer.
        #
        # The vintage clause is bounded against the As-of line, NOT the Mark,
        # because As-of is the bound the code actually enforces
        # (``macro_trend`` measures its 24h against ``as_of_ms``, which IS
        # this context's as-of). The Mark is a live snapshot that nothing
        # here is measured against, and the candle series may itself lag it
        # by whatever the freshness guard tolerates — so a block well past a
        # day behind the printed Mark passes every guard, and "up to a day
        # behind the Mark", which this sentence used to say, would be a
        # promise nothing keeps. No figure is given for that slack here: it
        # depends on the configured interval, and a literal would be one
        # more 4h-specific claim in a file whose rule is not to write one.
        #
        # The regime clause says only that the two are independent and can
        # disagree. It deliberately does NOT describe how the regime is built:
        # with ``indicators: []`` (a legal, deliberate configuration)
        # ``classify_regime`` returns its RANGING default from no indicators
        # at all, so any sentence here about "built from the 4h bars" would be
        # false on that config — and one of its three outcomes, VOLATILE, is
        # an ATR reading with no counterpart in this block at all. It does say
        # the regime covers less history, which holds for every configuration
        # that renders a regime from indicators at all; under the empty-list
        # config the line is a constant default and the clause is merely
        # uninformative rather than wrong.
        f"  Basis: two simple moving averages over closed daily candles, fetched as their "
        f"own series — the candles and indicators above are {candle_interval} bars and are "
        f"not affected by this section. This prompt's other trend reading is the "
        f"'Regime (computed)' line near the top; it is not derived from this block, it "
        f"covers far less history, and the two can disagree. A lagging measure by "
        f"construction: it describes an alignment that has already formed, not one that is "
        f"starting. The figures date to the newest closed daily bar, which is at most a "
        f"day behind the As-of time at the top of this context — and the Mark above is a "
        f"live reading, so the gap to THAT can be larger. Gaps in the daily series are not "
        f"checked, so a window missing bars still averages the {slow} most recent bars it "
        f"has and still calls that SMA({slow}). Nothing in this section feeds the risk "
        f"checks, the sizing or any order. Treat it as trend context, not as an entry or "
        f"exit signal.",
    ]


def _research_signal_lines(signal: ResearchSignal) -> list[str]:
    """The research-signal block. Only called when a signal exists.

    Same label discipline as its two siblings in this file, ``_REGIME_NOTE``
    and ``_SHAPE_NOTE`` (PR #95): every line says what was MEASURED and stops
    there. So the side is
    named as a rule's own state rather than as a view of the market, the two
    bands are named with the window they were cut from, and nothing here says
    "bullish", "confirmed" or "expect".

    No figure behind a band is printed (plan §7). That is not tidiness: this
    prompt's own history is that the model anchors on the numbers it is shown
    (paper-BTC-2 put 27 of 48 decisions at exactly the advertised bar), and a
    Sharpe is a number no reader of this prompt can put in context — it was
    measured on a different history, over a window this section only names.
    """
    return [
        # Dated to the RADAR's bar, not to this context's: the two are
        # separate fetches of the same venue and the document is written out
        # of band, so it can be up to ``research_signal.MAX_SIGNAL_AGE_INTERVALS``
        # of its own bars behind the prices above. Same house rule as the
        # volume profile's "as of the last closed candle": state the vintage
        # rather than let a reader assume the section is as current as the mark.
        f"Research signal (rule {signal.strategy_id}, decided on the research radar's own "
        f"{signal.interval} bars, as of {from_epoch_ms(signal.as_of_ms).isoformat()} UTC):",
        # "holds", not "recommends": the rule is a fixed set of conditions
        # replayed over history, and this is the side its latest decision
        # leaves it on.
        #
        # It does NOT say "to be filled at that rule's next bar open", which
        # an earlier draft did. Most of the time nothing is filled at all: the
        # rule is usually continuing a side it already held, and when the
        # answer is ``flat`` there is nothing to fill in the first place. The
        # fill rule matters to how the side was DERIVED — decisions are taken
        # at a bar's close and priced at the next open, which is what stops a
        # backtest reading its own future — so it is stated as the derivation
        # it is, under Basis, rather than as an event this block measured.
        f"  Side the rule holds after its latest bar: {signal.bias.value}",
        f"  Confidence band, cut from its return-to-volatility ratio over the window it was "
        f"selected on: {signal.confidence.value}",
        f"  Drawdown band, cut from its deepest peak-to-trough fall over that same window: "
        f"{signal.drawdown.value}",
        # The bands come from the SELECTION window alone — the two lines above
        # say so — so this line must not lump the two windows together as
        # "behind those two bands", which an earlier draft did and which a
        # reader could add up into one span of 120 days. The held-back window
        # belongs to the note under it.
        f"  Window the two bands were cut from: {signal.eval_window_days} days. A further "
        f"{signal.holdout_window_days} days were held back from the search and measured once, "
        f"which is what the note below reports",
        f"  Notes: {signal.notes}",
        # The disclosure the section cannot be read honestly without. Three
        # facts, each of which a reader would otherwise have to assume:
        # where the rule came from, that its bands are ordinal rather than
        # scaled, and that nothing here is wired to a decision.
        "  Basis: a fixed rule the research radar fitted and scored offline on its own copy of "
        "this coin's history — not on the candles above, and not on this account's fills. That "
        "rule decides at a bar's close and is priced as filling at the next bar's open, so the "
        "side above is the one it carries into its next bar rather than a trade taken at the "
        "close named above. The two bands are ordinal labels over that rule's own "
        "measurements; the figures behind them are deliberately not printed. Nothing in this "
        "section feeds the risk checks, the sizing or any order — it is one more input to "
        "weigh.",
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
        # a negative age. The age itself goes through ``gap_label`` — the
        # largest unit whose figure reaches 1.0 — because a fixed ``%.1f``
        # hours rendered a fill under three minutes old as "0.0 hours
        # before", and the model reading this line has nowhere else to
        # recover that number from (issue #288). A fill stamped exactly at
        # the as-of stays on the "before" side and prints "0 ms before".
        age_ms = delta_ms(ctx.as_of, pos.last_fill_at)
        when = (
            f"{gap_label(age_ms)} before the as-of time above"
            if age_ms >= 0
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
    # Totals only — no taker-fee / slippage decomposition (decided 2026-08-27
    # in the PR #133 review). The section exists because the model anchors on
    # numbers the prompt prints (paper-BTC-2: 27/48 decisions at exactly the
    # advertised bar); the two parameters would be two more anchors that
    # nothing here needs the model to reason with. "assumptions" stays: the
    # rate is the configured fill-cost model's (paper_trading.execution),
    # and on the live lane the books post the exchange's actual fee.
    lines.append(
        f"  Cost of moving to another legal margin, as a round trip (the fee and "
        f"slippage on this move plus the same again when it is later reversed), "
        f"priced with the configured fill-cost assumptions: {_num(rate_bps)} bps of "
        f"the traded notional per round trip. Breakeven is the favourable price "
        f"move, in bps of the traded notional, that exactly pays for that round trip:"
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

    # The widest backdrop, directly under the indicators it widens: those are
    # cut from ~33 days of 4h bars, this from 200 daily ones, so the context
    # reads outward-in from here (macro trend, then the profile's window of
    # ~5 days, then the radar, then the account). Optional and absent
    # whenever the switch is off or the daily series was unusable — the WHOLE
    # block drops out, for the reason spelled out on the profile below.
    if ctx.macro_trend is not None:
        lines.append("")
        lines.extend(_macro_trend_lines(ctx.macro_trend, ctx.candle_interval))

    # Optional, and the second of the market sections that can drop out:
    # absent whenever the feature is off or the window was unusable. The
    # WHOLE block drops out — there is no "Volume profile: n/a" form, because
    # a header with nothing under it reads as a measurement that came back
    # empty rather than one that was never taken.
    if ctx.volume_profile is not None:
        lines.append("")
        lines.extend(_volume_profile_lines(ctx.volume_profile, ctx.candle_interval))

    # The research radar's reading, after the market sections and before the
    # account's: it is a statement about this coin, like everything above it,
    # but it is the only one that did not come from this cycle's own fetch,
    # so it sits at the far end of the market half where its own dateline is
    # read against the others rather than mistaken for them. Optional and
    # absent whenever the switch is off or the document could not be believed
    # — the WHOLE block drops out, like the profile's, and for the same
    # reason (see :mod:`.research_signal`).
    if ctx.research_signal is not None:
        lines.append("")
        lines.extend(_research_signal_lines(ctx.research_signal))

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
    (in render order — a reorder is a different prompt), and each optional
    section when it is present: the daily macro trend, the volume profile,
    the research radar's signal (``autoresearch``) and the position. It is
    stored beside
    ``prompt_version`` on every ``ai_inputs`` row (issue #97) — and beside
    ``format_fingerprint``, the format block's content digest, since v11
    (issue #129; ``target_decision.format_fingerprint``) — so the paper
    review can segment on all three keys and a
    config-only change that adds or removes a section — flipping
    ``market_data.volume_profile_window_candles`` or
    ``market_data.macro_trend_daily_lookback``, editing ``indicators`` —
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

    The ``macro_trend`` token reads the same way, with one caveat worth
    knowing before it is used to segment a run. The builder leaves the field
    ``None`` when the lookback is configured off AND on every cycle whose
    section could not be built: too few daily bars, a stale daily feed, a
    daily bar ahead of this context, two exactly equal averages (each logged
    by ``macro_trend``), a failed ``1d`` read (logged by
    ``engine_bridge``), or no ``4h`` candles at all — that last one silently,
    since such a cycle is refused wholesale upstream anyway.

    The caveat: unlike the profile, this section depends on a NETWORK read of
    its own, so a read that fails every cycle yields no second bucket to
    notice — it reads exactly like the switch being off. The switch's real
    state is in the run's recorded config, and the WARNINGs say which cause
    it was; the shape alone cannot, and is not meant to.

    The ``autoresearch`` token reads the same way, and it is the reason the
    research signal fails closed by section rather than by row: a cycle whose
    handoff document was missing, stale or refused really had no such section
    in its prompt, so it files under the no-signal shape and can be counted.
    A run with the switch on and an occasional refusal shows exactly that
    split, beside the WARNING (:mod:`.research_signal`) naming which refusal
    it was. Had the section instead printed "n/a" rows, every cycle would
    file under one shape and the review could not tell the two apart at all.

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
    if ctx.macro_trend is not None:
        parts.append("macro_trend")
    if ctx.volume_profile is not None:
        parts.append("volume_profile")
    if ctx.research_signal is not None:
        parts.append("autoresearch")
    if ctx.position is not None:
        parts.append("position")
    return "|".join(parts)
