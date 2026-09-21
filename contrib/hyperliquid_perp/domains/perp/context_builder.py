"""Assemble market data + indicators into a :class:`PerpMarketContext`.

Pure functions only — given the snapshot, candles, funding history and the
run's books, the output is deterministic (``as_of`` is taken from the latest
candle close, not the wall clock), so this is straightforward to unit-test with
recorded HL JSON.

Two computations carry the design's "no NaN in the prompt" rule:

- :func:`funding_zscore` returns ``None`` when the window has too few samples or
  zero variance, never ``NaN``.
- indicators come from :mod:`indicators`, which already maps NaN -> ``None``.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from datetime import datetime, timezone
from decimal import Decimal

from ...common.instants import epoch_ms, from_epoch_ms
from .indicators import compute_indicators
from .macro_trend import compute_macro_trend
from .marginal_cost import PositionInputs, build_position_context
from .market_data_config import MarketDataConfig
from .schema import (
    Candle,
    FundingPoint,
    MarketRegime,
    MarketSnapshot,
    PerpMarketContext,
    ResearchSignal,
    derive_day_change_pct,
)
from .volume_profile import compute_volume_profile

# A 30-day window of hourly funding holds ~720 points. Require at least a day of
# samples before a z-score is meaningful; below this -> None (decision: no NaN).
MIN_FUNDING_SAMPLES = 24

_MS_PER_DAY = 24 * 60 * 60_000

# Regime thresholds (deterministic; tune in Phase 2 with paper data).
_VOLATILE_ATR_PCT = 4.0  # ATR >= 4% of price -> volatile
_TREND_EMA_SEP_PCT = 0.75  # |EMA20-EMA50| >= 0.75% of price + aligned -> trending


def funding_zscore(
    history: Sequence[FundingPoint],
    current: Decimal,
    as_of_ms: int,
    window_days: int,
) -> tuple[float | None, int]:
    """Z-score of ``current`` funding vs the trailing ``window_days`` of history.

    Returns ``(zscore_or_None, sample_count)``. ``None`` when there are fewer
    than :data:`MIN_FUNDING_SAMPLES` points in the window or the window has zero
    variance — never ``NaN``.
    """
    cutoff = as_of_ms - window_days * _MS_PER_DAY
    # Strict upper bound: a funding point landing exactly at ``as_of_ms`` is the
    # current epoch, which is also the value being z-scored (``current``). Including
    # it would fold ``current`` into its own mean/stdev and deflate the z-score, so a
    # genuine outlier reads as less extreme than it is. The window is the strictly
    # *prior* history: ``cutoff <= p.time < as_of_ms``.
    rates = [float(p.rate) for p in history if cutoff <= p.time < as_of_ms]
    count = len(rates)
    if count < MIN_FUNDING_SAMPLES:
        return None, count

    mean = statistics.fmean(rates)
    # Sample standard deviation (Bessel's n-1), not population: the window is a
    # sample drawn from the ongoing funding process, so stdev is the unbiased
    # estimator of its dispersion. Safe here — the count >= MIN_FUNDING_SAMPLES
    # gate above guarantees n >= 2, which stdev requires.
    stdev = statistics.stdev(rates)
    if stdev == 0:
        return None, count

    z = (float(current) - mean) / stdev
    if math.isnan(z) or math.isinf(z):
        return None, count
    return z, count


def classify_regime(indicators: dict[str, float | None], reference_price: Decimal) -> MarketRegime:
    """Deterministic trending / ranging / volatile label from indicators.

    ``reference_price`` should come from the same series the EMAs are built on
    (the latest candle close), not the live mark — otherwise the mark/close basis
    can flip the label near an EMA boundary. Defaults to ``RANGING`` when
    indicators are missing (insufficient candles). The names read here are
    mirrored in ``indicator_vocab.REGIME_INDICATORS`` — the config loader and
    the pre-LLM guard enforce their presence, so keep the two in sync (the
    drift-lock test in test_context_builder pins the membership).
    """
    price = float(reference_price)
    atr = indicators.get("atr_14")
    ema20 = indicators.get("ema_20")
    ema50 = indicators.get("ema_50")

    if price <= 0 or atr is None or ema20 is None or ema50 is None:
        return MarketRegime.RANGING

    if atr / price * 100 >= _VOLATILE_ATR_PCT:
        return MarketRegime.VOLATILE

    ema_sep_pct = abs(ema20 - ema50) / price * 100
    aligned = (price > ema20 > ema50) or (price < ema20 < ema50)
    if ema_sep_pct >= _TREND_EMA_SEP_PCT and aligned:
        return MarketRegime.TRENDING

    return MarketRegime.RANGING


def context_as_of(candles: Sequence[Candle]) -> tuple[datetime, int]:
    """The instant a context built on ``candles`` describes: ``(as_of, as_of_ms)``.

    The newest bar's CLOSE, taken from the raw epoch-ms integer the exchange
    sent, because two comparisons downstream are comparisons of exchange
    stamps and must not depend on a conversion in between: the funding
    window's strict ``p.time < as_of_ms`` bound below, and the research
    signal's freshness bound (:mod:`.research_signal`).
    ``from_epoch_ms`` is integer arithmetic, so ``as_of`` is exactly that
    millisecond by construction rather than by a float route happening to
    round-trip at this magnitude (issue #157). The funding window's strict
    ``p.time < as_of_ms`` bound inside :func:`build_market_context` is the
    first of those comparisons.

    With no candles at all there is no bar to date the context to and the
    wall clock is the only answer left. That context is refused downstream —
    by the warm-up guard, which owns the empty-window case (the freshness
    guard is vacuous there) — and it stays buildable so the refusal happens
    where refusals are read rather than in the middle of a fetch.

    Its own function because the caller that fetches the candles needs the
    same instant before the context exists: :mod:`..engine_bridge` judges the
    research signal's freshness against it. What the two share is the RULE,
    not the reading — the bridge calls this and so does the builder, so on the
    no-candles branch their two wall-clock readings are milliseconds apart.
    That is also why the bridge does not load a signal at all without candles:
    the freshness bound is defined against a CLOSED BAR, and with no bar there
    is nothing to judge against.
    """
    if candles:
        as_of_ms = candles[-1].close_time
        return from_epoch_ms(as_of_ms), as_of_ms
    as_of = datetime.now(tz=timezone.utc)
    return as_of, epoch_ms(as_of, what="context as_of")


def build_market_context(
    coin: str,
    snapshot: MarketSnapshot,
    candles: Sequence[Candle],
    funding_history: Sequence[FundingPoint],
    *,
    market_data: MarketDataConfig,
    indicator_names: Sequence[str],
    exchange_time: datetime | None,
    position: PositionInputs | None,
    research_signal: ResearchSignal | None,
    daily_candles: Sequence[Candle] | None,
    host_time_at_exchange_read: datetime | None = None,
) -> PerpMarketContext:
    """Build the full :class:`PerpMarketContext` from raw domain inputs.

    ``market_data`` is the parsed ``market_data:`` block. Three of its fields
    are read here: the interval the candles were fetched at (recorded on the
    context), the funding z-score window, and the volume-profile window —
    ``0`` leaves the profile off and the context's ``volume_profile`` ``None``.
    ``candle_lookback`` is the fetch's concern, not this function's, and so is
    ``macro_trend_daily_lookback`` — that switch decides whether the caller
    fetches a daily series at all, which reaches this function as
    ``daily_candles`` being a sequence rather than ``None``. Passing
    the parsed block rather than its fields one by one keeps the defaults
    declared once (on :class:`MarketDataConfig`), so a caller cannot build a
    context from a different default than the config loader validated.

    ``exchange_time`` is the exchange's clock as read during the same fetch —
    since issue #124 the very reading the candle window was cut at (see
    ``PerpMarketContext.exchange_time``); it is carried through untouched —
    nothing here measures against it, the freshness guard does.

    REQUIRED, with no default, even though the field it fills is optional: a
    caller with no exchange clock has to write ``exchange_time=None`` and mean
    it. The default it would otherwise inherit is precisely the issue-#51 blind
    spot (the guard falls back to the caller's host clock, against which a
    window a slow host cut looks current), so it must never be reachable by
    forgetting a kwarg.

    ``position`` — the run's books plus the rules a move is priced under — is
    required for the same reason and fills the ``Position:`` section here,
    beside ``volume_profile``, rather than being grafted on afterwards
    (issue #134). ``None`` is the position-blind context: no books wired (the
    one-shot CLI), or none seeded yet. The pricer's own fail-closed rule still
    applies — books it cannot price (non-positive equity) yield ``None`` plus
    its WARNING, and the section is omitted. Assembling it here is what makes
    ``PerpMarketContext`` single-construction-path: the section is priced at
    the very ``snapshot.mark_price`` / ``snapshot.funding`` the rest of the
    context is built from, so the ``Mark:`` line and the notional under it are
    the same reading by construction, not by a later cross-check.

    ``daily_candles`` is the SECOND candle series — a ``1d`` window fetched
    by the caller — from which the macro-trend section is computed
    (:mod:`.macro_trend`). ``None`` means the feature is off and the section
    is absent with no log line; a sequence means it is on, and the module's
    own refusals then apply (and log) — with ONE exception, below: a context
    built without ``candles`` drops the section silently however good the
    daily series is, because there is no closed bar to judge that series
    against. REQUIRED with no default, the last of
    the four kwargs on that rule in this signature and for the same reason as
    the others: forgetting it would silently produce a context with no macro
    section and a ``context_shape`` quietly missing its token, with nothing
    raising — indistinguishable from an operator having left the switch off.

    It is a separate argument rather than something built from ``candles``
    because it is a separate FETCH: taking the daily bars from the ``4h``
    series would mean widening that series, which would move every existing
    indicator (see :mod:`.macro_trend`). ``market_data`` is what decides
    whether the caller fetches it at all; this function is handed the result.

    ``research_signal`` is REQUIRED with no default too, for the same reason: forgetting it would cost a prompt quietly missing a
    section and a ``context_shape`` quietly missing its token, with nothing
    raising — exactly the failure the position kwarg's rule exists for. It is
    carried through untouched. Unlike the profile and the position section,
    nothing here builds it: it is read from a document another process wrote,
    and every rule about whether that document may be believed — version,
    coin, freshness — belongs to :mod:`.research_signal`, which answers
    ``None`` and logs when it may not. This function stays pure, so the one
    caller that does the reading does it before calling.
    """
    as_of, as_of_ms = context_as_of(candles)

    indicators = compute_indicators(candles, indicator_names)
    zscore, sample_count = funding_zscore(
        funding_history, snapshot.funding, as_of_ms, market_data.funding_zscore_window_days
    )
    # Use the latest candle close (the EMAs' own series) so mark/close basis can't
    # flip the regime; fall back to mark only when there are no candles at all.
    regime_price = candles[-1].close if candles else snapshot.mark_price
    regime = classify_regime(indicators, regime_price)
    # Cut from the same candle series as the indicators, so the profile and the
    # regime describe the same window of history. ``None`` whenever the feature
    # is off or the window is unusable — the renderer then omits the section.
    volume_profile = compute_volume_profile(candles, market_data.volume_profile_window_candles)
    # The one section cut from a DIFFERENT series. Judged against this
    # context's own ``as_of_ms`` — the newest 4h bar's close — so a daily feed
    # that has stopped publishing is refused rather than averaged (see
    # :func:`.macro_trend.compute_macro_trend`). ``None`` in means the switch
    # is off, and nothing is logged; anything else is the module's own
    # fail-closed decision, with its WARNING.
    #
    # Not computed at all without ``candles``. ``as_of_ms`` is then the WALL
    # CLOCK (``context_as_of``'s no-bar fallback), and the freshness rule this
    # section is judged by is defined against a closed bar — measured against
    # the wall clock a daily series cut at the exchange clock always passes,
    # so the one degraded context where every other number is missing would
    # carry a full, confident, internally consistent macro block. Same rule
    # the research signal follows, for the same reason.
    #
    # Silent, unlike the producer's refusals, and that is the deliberate part:
    # a context with no candles is refused wholesale upstream (the warm-up
    # guard owns the empty window), so a WARNING here would fire on every
    # cycle of a run that is already failing loudly for a better-named
    # reason. The docstring above says so, because the silence is otherwise
    # indistinguishable from the switch being off.
    macro_trend = (
        None
        if daily_candles is None or not candles
        else compute_macro_trend(daily_candles, as_of_ms=as_of_ms)
    )
    # Priced at the snapshot's own mark and funding — the same two values the
    # ``Mark:`` and ``Funding:`` lines print — so the section cannot quote a
    # notional or a holding cost against a different reading of the market.
    position_context = (
        None
        if position is None
        else build_position_context(
            position, mark=snapshot.mark_price, funding_rate=snapshot.funding
        )
    )

    return PerpMarketContext(
        coin=coin,
        as_of=as_of,
        candle_interval=market_data.candle_interval,
        candle_count=len(candles),
        mark_price=snapshot.mark_price,
        oracle_price=snapshot.oracle_price,
        prev_day_price=snapshot.prev_day_price,
        mid_price=snapshot.mid_price,
        day_change_pct=derive_day_change_pct(snapshot.mark_price, snapshot.prev_day_price),
        open_interest=snapshot.open_interest,
        day_ntl_volume=snapshot.day_ntl_volume,
        funding_rate=snapshot.funding,
        funding_premium=snapshot.premium,
        funding_zscore_30d=zscore,
        funding_window_days=market_data.funding_zscore_window_days,
        funding_sample_count=sample_count,
        indicators=indicators,
        market_regime=regime,
        exchange_time=exchange_time,
        host_time_at_exchange_read=host_time_at_exchange_read,
        macro_trend=macro_trend,
        volume_profile=volume_profile,
        research_signal=research_signal,
        position=position_context,
    )
