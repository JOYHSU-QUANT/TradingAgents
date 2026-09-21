"""Tests for the funding z-score and full context build (pure functions)."""

from __future__ import annotations

import math
from datetime import timezone
from decimal import Decimal

import pytest

from contrib.hyperliquid_perp.common.instants import epoch_ms, from_epoch_ms
from contrib.hyperliquid_perp.domains.perp.context_builder import (
    MIN_FUNDING_SAMPLES,
    build_market_context,
    classify_regime,
    context_as_of,
    funding_zscore,
)
from contrib.hyperliquid_perp.domains.perp.indicator_vocab import REGIME_INDICATORS
from contrib.hyperliquid_perp.domains.perp.market_data_config import MarketDataConfig
from contrib.hyperliquid_perp.domains.perp.schema import (
    FundingPoint,
    ResearchSignal,
    derive_day_change_pct,
)
from contrib.hyperliquid_perp.exchanges.hyperliquid import mapper

_H = 3600_000

# The parsed ``market_data:`` block the builder takes; the fixtures are 4h
# candles, and every test that does not say otherwise wants the 30-day funding
# window and the profile OFF — i.e. the field defaults.
_MD = MarketDataConfig()


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


def test_zscore_negative_when_current_below_mean():
    end = 1_700_000_000_000
    # Same window as the positive pin, but ``current`` sits *below* the mean -> the sign
    # must flip. funding_zscore feeds funding_view_for via ``zscore * bias_sign``, so a
    # sign inversion in the formula would flip every side's FundingView while the
    # positive test stayed green. current=0 is mean(0.00002) - 0.00002, the mirror of
    # the +0.00004 case, so the magnitude must match at the opposite sign.
    rates = [0.00001, 0.00002, 0.00003] * 10
    pts = _points(rates, end)
    z, count = funding_zscore(pts, Decimal("0.0"), end, 30)
    assert z is not None
    assert count == 30
    assert z < 0
    assert z == pytest.approx(-2.408, abs=0.005)


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


@pytest.mark.parametrize("dead", REGIME_INDICATORS)
def test_classify_regime_forces_ranging_when_any_regime_indicator_dead(dead):
    # Drift lock for REGIME_INDICATORS (the loader/pre-LLM-guard tuple): None-ing
    # any single member forces RANGING no matter how volatile the others look.
    # If classify_regime ever reads a different indicator set, this and the
    # non-member test below fail — update the tuple with it.
    ind = {"atr_14": 3000.0, "ema_20": 60000.0, "ema_50": 60000.0, dead: None}
    assert classify_regime(ind, Decimal("60000")) == "ranging"


def test_classify_regime_ignores_non_regime_indicators():
    # The complement of the drift lock: a dead non-member must not affect the
    # classification — the same volatile-shaped trio still reads VOLATILE.
    ind = {"atr_14": 3000.0, "ema_20": 60000.0, "ema_50": 60000.0, "rsi_14": None}
    assert classify_regime(ind, Decimal("60000")) == "volatile"


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
        market_data=_MD,
        indicator_names=["rsi_14", "ema_20", "ema_50", "atr_14", "macd"],
        exchange_time=None,
        position=None,
        research_signal=None,
        daily_candles=None,
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


def test_build_market_context_carries_the_exchange_clock_through(
    meta_and_asset_ctxs, candle_snapshot, funding_history
):
    # Issue #51: the builder neither measures against nor alters the exchange
    # clock — it rides the context to the freshness guard untouched, and is
    # absent (not fabricated from the wall clock) when the caller has none.
    from datetime import datetime, timezone

    snapshot = mapper.map_market_snapshot(meta_and_asset_ctxs, "BTC")
    candles = mapper.map_candles(candle_snapshot)
    funding = mapper.map_funding_history(funding_history)
    kwargs = {
        "market_data": _MD,
        "indicator_names": ["rsi_14"],
        "position": None,
        "research_signal": None,
        "daily_candles": None,
    }
    stamp = datetime(2026, 8, 22, 4, 0, tzinfo=timezone.utc)
    with_clock = build_market_context(
        "BTC", snapshot, candles, funding, exchange_time=stamp, **kwargs
    )
    assert with_clock.exchange_time == stamp
    without = build_market_context("BTC", snapshot, candles, funding, exchange_time=None, **kwargs)
    assert without.exchange_time is None
    # ...and it has no default: a caller with no exchange clock must say so.
    with pytest.raises(TypeError, match="exchange_time"):
        build_market_context("BTC", snapshot, candles, funding, **kwargs)


def _priced_at_50k(snapshot):
    """The fixture snapshot repriced to round numbers the position math shows.

    Mark 50,000 with funding 1 bp/h, so a 0.005 BTC long entered at the mark
    is notional 250 on a 1,000 wallet — 25% committed, no unrealized PnL, and
    an 8h holding cost of 0.0001 * 8 * 250 = 0.20 USDC. Every figure below is
    hand-computed from those, never re-derived by calling the pricer.
    """
    from dataclasses import replace

    return replace(
        snapshot,
        mark_price=Decimal(50000),
        oracle_price=Decimal(50000),
        prev_day_price=Decimal(50000),
        mid_price=Decimal(50000),
        funding=Decimal("0.0001"),
    )


def _long_at_50k():
    from contrib.hyperliquid_perp.domains.perp.marginal_cost import (
        BookPosition,
        PositionInputs,
        PositionPricing,
    )

    return PositionInputs(
        book=BookPosition(
            size=Decimal("0.005"),
            entry_price=Decimal(50000),
            wallet_balance=Decimal(1000),
            last_fill_at=None,
        ),
        pricing=PositionPricing(
            leverage=Decimal(1),
            grid_min=0,
            grid_max=60,
            grid_step=1,
            taker_fee_rate=Decimal("0.00045"),
            slippage_bps=Decimal(5),
        ),
    )


def test_the_position_section_is_priced_onto_the_context_at_its_own_snapshot(
    meta_and_asset_ctxs, candle_snapshot, funding_history
):
    # Issue #134: the section is assembled HERE, from the same snapshot the
    # Mark: and Funding: lines are printed from — so the context has one
    # construction path and the two readings cannot be of different markets.
    snapshot = _priced_at_50k(mapper.map_market_snapshot(meta_and_asset_ctxs, "BTC"))
    ctx = build_market_context(
        "BTC",
        snapshot,
        mapper.map_candles(candle_snapshot),
        mapper.map_funding_history(funding_history),
        market_data=_MD,
        indicator_names=["rsi_14"],
        exchange_time=None,
        position=_long_at_50k(),
        research_signal=None,
        daily_candles=None,
    )
    pos = ctx.position
    assert pos is not None
    assert pos.notional == Decimal(250)
    assert pos.margin_pct == Decimal(25)
    assert pos.unrealized_pnl == Decimal(0)
    assert pos.holding_cost_8h == Decimal("0.2")
    # The ceiling the caller declared legal is a printed row, so the model is
    # never shown a table that stops short of the margin it may ask for.
    assert max(row.target_margin_pct for row in pos.cost_rows) == 60


def test_a_position_the_books_cannot_price_leaves_the_context_position_blind(
    meta_and_asset_ctxs, candle_snapshot, funding_history
):
    # The pricer's fail-closed rule reaches the context unchanged: a wallet
    # wiped out below the position's loss has no basis to price a move
    # against, so the section is absent rather than half-filled.
    from dataclasses import replace

    snapshot = _priced_at_50k(mapper.map_market_snapshot(meta_and_asset_ctxs, "BTC"))
    inputs = _long_at_50k()
    broke = replace(inputs, book=replace(inputs.book, wallet_balance=Decimal(0)))
    ctx = build_market_context(
        "BTC",
        snapshot,
        mapper.map_candles(candle_snapshot),
        mapper.map_funding_history(funding_history),
        market_data=_MD,
        indicator_names=["rsi_14"],
        exchange_time=None,
        position=broke,
        research_signal=None,
        daily_candles=None,
    )
    assert ctx.position is None


def test_the_position_argument_has_no_default(
    meta_and_asset_ctxs, candle_snapshot, funding_history
):
    # Same rule as exchange_time: a caller with no books must write
    # ``position=None`` and mean it, because the default it would otherwise
    # inherit silently produces a position-blind prompt on a lane that has
    # books — the one failure nothing downstream can see.
    snapshot = mapper.map_market_snapshot(meta_and_asset_ctxs, "BTC")
    with pytest.raises(TypeError, match="position"):
        build_market_context(
            "BTC",
            snapshot,
            mapper.map_candles(candle_snapshot),
            mapper.map_funding_history(funding_history),
            market_data=_MD,
            indicator_names=["rsi_14"],
            exchange_time=None,
        )


def test_volume_profile_is_absent_unless_a_window_is_configured(
    meta_and_asset_ctxs, candle_snapshot, funding_history
):
    # The feature is OFF by default — the MarketDataConfig field default, which
    # is the same default the config loader validates against — and an
    # explicit 0 means the same thing.
    snapshot = mapper.map_market_snapshot(meta_and_asset_ctxs, "BTC")
    candles = mapper.map_candles(candle_snapshot)
    funding = mapper.map_funding_history(funding_history)
    kwargs = {
        "indicator_names": ["rsi_14"],
        "exchange_time": None,
        "position": None,
        "research_signal": None,
        "daily_candles": None,
    }
    assert (
        build_market_context(
            "BTC", snapshot, candles, funding, market_data=MarketDataConfig(), **kwargs
        ).volume_profile
        is None
    )
    assert (
        build_market_context(
            "BTC",
            snapshot,
            candles,
            funding,
            market_data=MarketDataConfig(volume_profile_window_candles=0),
            **kwargs,
        ).volume_profile
        is None
    )


def _daily_ending_at(close_time: int, n: int = 260):
    """``n`` synthetic daily bars whose newest closes exactly at ``close_time``.

    Aligned to the 4h fixture's own as-of rather than to a calendar grid,
    because the producer measures the daily feed's freshness against THAT —
    a daily series built on its own grid would be refused as stale and the
    test would pass for the wrong reason.
    """
    from contrib.hyperliquid_perp.domains.perp.schema import Candle

    day_ms = 24 * 60 * 60_000
    bars = []
    for i in range(n):
        end = close_time - (n - 1 - i) * day_ms
        # A step up 50 bars from the end, so the pair is strictly ordered and
        # the run has a visible start (see tests/domains/test_macro_trend).
        price = Decimal(110) if i >= n - 50 else Decimal(100)
        bars.append(
            Candle(
                open_time=end - day_ms + 1,
                close_time=end,
                open=price,
                high=price,
                low=price,
                close=price,
                volume=Decimal(1),
            )
        )
    return bars


def test_the_daily_candles_argument_has_no_default(
    meta_and_asset_ctxs, candle_snapshot, funding_history
):
    # The fourth kwarg on this rule, after exchange_time, position and
    # research_signal, and for the same reason: forgetting it costs a prompt
    # quietly missing a section and a context_shape quietly missing its token,
    # with nothing raising — indistinguishable from the switch being off.
    snapshot = mapper.map_market_snapshot(meta_and_asset_ctxs, "BTC")
    with pytest.raises(TypeError, match="daily_candles"):
        build_market_context(
            "BTC",
            snapshot,
            mapper.map_candles(candle_snapshot),
            mapper.map_funding_history(funding_history),
            market_data=_MD,
            indicator_names=["rsi_14"],
            exchange_time=None,
            position=None,
            research_signal=None,
        )


def test_no_daily_candles_means_no_macro_trend_and_no_log(
    meta_and_asset_ctxs, candle_snapshot, funding_history, caplog
):
    # ``None`` is the configured OFF state, not a degradation: the section is
    # absent and nothing is logged. Every refusal the producer makes DOES log,
    # so a silent absence here is what tells the two apart in a run's journal.
    import logging

    snapshot = mapper.map_market_snapshot(meta_and_asset_ctxs, "BTC")
    with caplog.at_level(logging.WARNING):
        ctx = build_market_context(
            "BTC",
            snapshot,
            mapper.map_candles(candle_snapshot),
            mapper.map_funding_history(funding_history),
            market_data=_MD,
            indicator_names=["rsi_14"],
            exchange_time=None,
            position=None,
            research_signal=None,
            daily_candles=None,
        )
    assert ctx.macro_trend is None
    assert "macro" not in caplog.text.lower()


def test_the_macro_trend_is_built_from_the_daily_series_against_this_contexts_as_of(
    meta_and_asset_ctxs, candle_snapshot, funding_history
):
    # Two things at once, and the second is the one worth pinning: the section
    # comes from the DAILY series (not the 4h one the indicators use), and its
    # freshness is judged against the 4h series' own as-of — the instant the
    # whole context is dated to. Judged against the daily series' own newest
    # bar instead, the staleness check would compare a value with itself and
    # pass on any feed at all.
    from contrib.hyperliquid_perp.domains.perp.macro_trend import compute_macro_trend

    snapshot = mapper.map_market_snapshot(meta_and_asset_ctxs, "BTC")
    candles = mapper.map_candles(candle_snapshot)
    _, as_of_ms = context_as_of(candles)
    daily = _daily_ending_at(as_of_ms)
    ctx = build_market_context(
        "BTC",
        snapshot,
        candles,
        mapper.map_funding_history(funding_history),
        market_data=_MD,
        indicator_names=["rsi_14"],
        exchange_time=None,
        position=None,
        research_signal=None,
        daily_candles=daily,
    )
    assert ctx.macro_trend is not None
    assert ctx.macro_trend == compute_macro_trend(daily, as_of_ms=as_of_ms)
    # A daily feed a day and a millisecond behind that as-of is refused all
    # the way to the context, rather than averaged into a stale section.
    stale = _daily_ending_at(as_of_ms - 24 * 60 * 60_000 - 1)
    assert (
        build_market_context(
            "BTC",
            snapshot,
            candles,
            mapper.map_funding_history(funding_history),
            market_data=_MD,
            indicator_names=["rsi_14"],
            exchange_time=None,
            position=None,
            research_signal=None,
            daily_candles=stale,
        ).macro_trend
        is None
    )


def test_volume_profile_is_cut_from_the_same_candles_as_the_indicators(
    meta_and_asset_ctxs, candle_snapshot, funding_history
):
    # Same series, same window semantics: a profile built from some *other*
    # slice would describe a different stretch of history than the regime does
    # while sitting in the same prompt.
    from contrib.hyperliquid_perp.domains.perp.volume_profile import compute_volume_profile

    snapshot = mapper.map_market_snapshot(meta_and_asset_ctxs, "BTC")
    candles = mapper.map_candles(candle_snapshot)
    funding = mapper.map_funding_history(funding_history)
    ctx = build_market_context(
        "BTC",
        snapshot,
        candles,
        funding,
        market_data=MarketDataConfig(volume_profile_window_candles=30),
        indicator_names=["rsi_14", "ema_20", "ema_50", "atr_14"],
        exchange_time=None,
        position=None,
        research_signal=None,
        daily_candles=None,
    )
    assert ctx.volume_profile is not None
    assert ctx.volume_profile == compute_volume_profile(candles, 30)
    assert ctx.volume_profile.candle_count == 30


def test_volume_profile_stays_none_when_the_window_cannot_be_filled(
    meta_and_asset_ctxs, candle_snapshot, funding_history
):
    # Fail-closed all the way to the context: too little history leaves the
    # field absent rather than producing a narrower profile that claims 30.
    snapshot = mapper.map_market_snapshot(meta_and_asset_ctxs, "BTC")
    candles = mapper.map_candles(candle_snapshot)
    funding = mapper.map_funding_history(funding_history)
    ctx = build_market_context(
        "BTC",
        snapshot,
        candles[:5],
        funding,
        market_data=MarketDataConfig(volume_profile_window_candles=30),
        indicator_names=["rsi_14"],
        exchange_time=None,
        position=None,
        research_signal=None,
        daily_candles=None,
    )
    assert ctx.volume_profile is None


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
        market_data=_MD,
        indicator_names=["rsi_14", "atr_14"],
        exchange_time=None,
        position=None,
        research_signal=None,
        daily_candles=None,
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
    # wide 30d window sees every fixture point; a narrow 1d window sees only the most
    # recent day's worth, so the in-window sample count is strictly smaller. A regression
    # that ignored the param would leave the sample count (and the recorded window)
    # unchanged between the two — invisible in the rendered prompt. (A sub-1-day window
    # is rejected at construction; see test_schema's funding_window_days guard test.)
    snapshot = mapper.map_market_snapshot(meta_and_asset_ctxs, "BTC")
    candles = mapper.map_candles(candle_snapshot)
    funding = mapper.map_funding_history(funding_history)

    def _ctx_with_window(days):
        return build_market_context(
            "BTC",
            snapshot,
            candles,
            funding,
            market_data=MarketDataConfig(funding_zscore_window_days=days),
            indicator_names=["rsi_14"],
            exchange_time=None,
            position=None,
            research_signal=None,
            daily_candles=None,
        )

    wide = _ctx_with_window(30)
    assert wide.funding_zscore_30d is not None
    assert wide.funding_sample_count >= MIN_FUNDING_SAMPLES
    assert wide.funding_window_days == 30  # the param is recorded on the context

    narrow = _ctx_with_window(1)
    assert narrow.funding_window_days == 1
    # The narrow window reaches the computation: it keeps strictly fewer points than the
    # wide one, proving funding_window_days is plumbed through rather than ignored.
    assert narrow.funding_sample_count < wide.funding_sample_count


def test_context_indicators_are_read_only(meta_and_asset_ctxs, candle_snapshot, funding_history):
    # PerpMarketContext is frozen; indicators must reject in-place mutation too,
    # so the immutability the frozen flag advertises actually holds.
    snapshot = mapper.map_market_snapshot(meta_and_asset_ctxs, "BTC")
    ctx = build_market_context(
        "BTC",
        snapshot,
        mapper.map_candles(candle_snapshot),
        mapper.map_funding_history(funding_history),
        market_data=_MD,
        indicator_names=["rsi_14"],
        exchange_time=None,
        position=None,
        research_signal=None,
        daily_candles=None,
    )
    with pytest.raises(TypeError):
        ctx.indicators["rsi_14"] = 99.9


@pytest.mark.parametrize(
    "mark, prev_day, expected",
    [
        (Decimal("110"), Decimal("100"), 10.0),  # +10% up
        (Decimal("90"), Decimal("100"), -10.0),  # -10% down (sign must not flip)
        (Decimal("100"), Decimal("100"), 0.0),  # unchanged
        (Decimal("150"), Decimal("120"), 25.0),  # non-round denominator
    ],
)
def test_day_change_pct_formula(mark, prev_day, expected):
    # The prompt renders this as "24h change: X.XX%", read by the LLM as a directional
    # signal. The end-to-end build/prompt tests inject a hardcoded value and never call
    # this formula, so pin it directly: (mark - prev_day) / prev_day * 100. A swapped
    # denominator or inverted numerator would silently misstate the 24h move.
    assert derive_day_change_pct(mark, prev_day) == pytest.approx(expected)


def test_day_change_pct_zero_prev_day_is_none():
    # A zero prior price can't yield a percentage change; return None rather than
    # divide-by-zero, so the renderer omits the field instead of crashing.
    assert derive_day_change_pct(Decimal("100"), Decimal("0")) is None


# -- the research signal the builder carries (plan §7 / PR C1) -----------------


def _signal():
    return ResearchSignal(
        coin="BTC",
        interval="4h",
        as_of_ms=1_704_182_399_999,
        strategy_id="btc-4h#7",
        bias="long",
        confidence="medium",
        drawdown="moderate",
        eval_window_days=90,
        holdout_window_days=30,
        notes="held-back window, measured once: net return positive",
    )


def test_the_research_signal_argument_has_no_default(
    meta_and_asset_ctxs, candle_snapshot, funding_history
):
    # The third kwarg on this rule, after exchange_time and position, and for
    # the same reason: forgetting it costs a prompt quietly missing a section
    # and a context_shape quietly missing its token, with nothing raising.
    snapshot = mapper.map_market_snapshot(meta_and_asset_ctxs, "BTC")
    with pytest.raises(TypeError, match="research_signal"):
        build_market_context(
            "BTC",
            snapshot,
            mapper.map_candles(candle_snapshot),
            mapper.map_funding_history(funding_history),
            market_data=_MD,
            indicator_names=["rsi_14"],
            exchange_time=None,
            position=None,
            daily_candles=None,
        )


def test_the_research_signal_is_carried_through_untouched(
    meta_and_asset_ctxs, candle_snapshot, funding_history
):
    # Unlike the profile and the position section, the builder does not build
    # this one — every rule about whether the document may be believed belongs
    # to ``research_signal``, so the builder must neither re-judge it nor drop
    # it. Pinned by identity.
    snapshot = mapper.map_market_snapshot(meta_and_asset_ctxs, "BTC")
    kwargs = {
        "market_data": _MD,
        "indicator_names": ["rsi_14"],
        "exchange_time": None,
        "position": None,
        "daily_candles": None,
    }
    candles = mapper.map_candles(candle_snapshot)
    funding = mapper.map_funding_history(funding_history)
    signal = _signal()
    built = build_market_context(
        "BTC", snapshot, candles, funding, research_signal=signal, **kwargs
    )
    assert built.research_signal is signal
    blind = build_market_context(
        "BTC", snapshot, candles, funding, research_signal=None, **kwargs
    )
    assert blind.research_signal is None


def test_the_context_as_of_is_one_function_the_builder_and_its_caller_share(candle_snapshot):
    # The bridge judges the signal's freshness against the very instant the
    # context will be dated to, so the derivation has one home. Written twice
    # the two would drift most easily on the no-candles branch, where one side
    # takes the wall clock and the other has nothing.
    candles = mapper.map_candles(candle_snapshot)
    as_of, as_of_ms = context_as_of(candles)
    assert as_of_ms == candles[-1].close_time
    assert as_of == from_epoch_ms(as_of_ms)
    empty_as_of, empty_ms = context_as_of([])
    assert empty_as_of.tzinfo is timezone.utc
    assert empty_ms == epoch_ms(empty_as_of, what="test")
