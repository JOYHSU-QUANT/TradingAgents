"""Macro trend — how the DAILY SMA(50)/SMA(200) pair is ordered, and for how long.

Adds the one thing the ``4h`` context cannot express: a multi-month backdrop.
``context_builder.classify_regime`` already labels the trend, but it does so
from EMA(20)/EMA(50) over the ``4h`` series — at this project's 200-candle
lookback that is about 33 days, so the widest thing it can see is a swing.
Two daily averages over 200 bars see the segment that swing sits in.

What is reported is a STATE, never an event. The ordering of the two averages,
how far apart they are, how many daily bars have carried that ordering, and
where the latest daily close sits against the slow average. Deliberately NOT
"a change of ordering happened": that is one bar's worth of change, it happens
once or twice a year on BTC, and a ``4h`` decision cycle asking "did it happen
this cycle?" would read ``False`` essentially forever. The run length says the
same thing better — a run of one IS the bar the ordering changed on.

Same discipline as :mod:`.volume_profile`, which this module is modelled on:

- **Pure and stdlib-only.** :class:`~decimal.Decimal` throughout, no
  stockstats, no numpy; the averages are ``sum / period`` over closed candles.
- **Fail-closed as a WHOLE.** Too little history, a stale daily feed, a daily
  bar dated after the context, or two exactly equal averages each return
  ``None`` for the entire section rather than a partly-filled one. The prompt
  then has no macro block at all — there is no "n/a" form, because a header
  with nothing under it reads as a measurement that came back empty rather
  than one that was never taken.
- **Not a gate.** Nothing here feeds sizing, the risk gate or any refusal. It
  is an analyst *input* only; its value to decision quality is unverified and
  is meant to be measured on a paper run.

Three more things this module is deliberately NOT:

- **Not the 4h series resampled.** The daily candles are their own fetch. Both
  alternatives — resampling ``4h`` bars, or asking for 1200 of them and taking
  SMA(300)/SMA(1200) — would change the ``4h`` window every existing indicator
  is computed over, so turning this feature on would silently move RSI, the
  EMAs and the regime label too. Two series, one of which nothing else reads,
  keeps the switch honest: with it off, not one existing prompt byte moves.
- **Not gap-checked.** Nothing here verifies that the daily series has no
  missing bars. A window with a hole still averages the 200 most recent bars
  it HAS and still calls the result SMA(200), which is then a 200-bar average
  spanning more than 200 days. The renderer says so in the prompt; if a paper
  measurement ever depends on this section, a continuity check is the first
  thing to add.
- **Not configurable in its periods.** 50 and 200 are what the pair is
  defined by, and a configurable period would be a configurable prompt
  vocabulary: every value would be its own prompt regime that ``context_shape``
  cannot distinguish (it names sections, not the numbers inside their labels).
  The one operator knob is how much daily history to fetch,
  ``market_data.macro_trend_daily_lookback``, whose only effect on the text is
  how far back a change of ordering can be SEEN.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import date
from decimal import Decimal, localcontext
from typing import Final

from ...common.constants import MIN_MACRO_TREND_LOOKBACK
from ...common.decimal_context import DECIMAL_CONTEXT
from ...common.instants import from_epoch_ms
from .schema import Candle, CandleInterval, MacroTrend, derive_macro_alignment

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_DAILY_LOOKBACK",
    "MACRO_CANDLE_INTERVAL",
    "MACRO_FAST_PERIOD",
    "MACRO_SLOW_PERIOD",
    "MAX_DAILY_CANDLE_AGE_MS",
    "compute_macro_trend",
]

# The two periods. Here rather than in ``common.constants`` because nothing
# outside this module and the DTO it builds needs them — the config layer's
# only rule is the FLOOR, which is the slow period under its config-facing
# name.
MACRO_FAST_PERIOD: Final = 50
# Bound to the config floor rather than written out again: the floor exists
# BECAUSE a window shorter than the slow period has no slow average at any
# bar, so the two are one number. ``MacroTrend`` pins its ``slow_period``
# against the same constant, which is what makes this binding checkable
# instead of a claim in a comment.
MACRO_SLOW_PERIOD: Final = MIN_MACRO_TREND_LOOKBACK

# The interval the daily series is fetched at, derived from the vocabulary
# rather than typed as a bare ``"1d"`` at the call site in ``engine_bridge``:
# the exchange adapter echoes the interval back and compares it, so a typo
# would be a fetch failure rather than a wrong average, but the vocabulary is
# still the one place the spelling is decided.
MACRO_CANDLE_INTERVAL: Final[str] = CandleInterval.D1.value

# The daily history an operator gets by writing
# ``market_data.macro_trend_daily_lookback: 260``. Not applied as a default
# anywhere — the *code* default is 0 (feature off); this is the documented
# starting value for the config file and the tests. 260 = the 200 bars the
# slow average needs before it exists at all, plus 60 bars over which a change
# of ordering can actually be SEEN. At the floor (200) the section is still
# legal and still correct, but there is exactly one bar with both averages, so
# ``days_in_state`` is always 1 and always capped.
DEFAULT_DAILY_LOOKBACK: Final = 260

# How far behind the context's own as-of the newest daily bar may be. Daily
# bars close at 00:00 UTC and so does every sixth 4h bar, so a healthy feed
# sits anywhere in 0-20h — EXCEPT for the one case that lands exactly on the
# bound: a 4h bar that closed at 00:00 while the daily bar covering the day
# just ended has not been published yet is exactly 24h behind. Hence ``<=``,
# not ``<``. Anything beyond it means the daily feed is missing at least one
# whole bar, and a section that quietly averages a stale series is worse than
# no section.
MAX_DAILY_CANDLE_AGE_MS: Final = 24 * 60 * 60_000


def _bar_date(candle: Candle) -> date:
    """The UTC calendar day a daily bar covers.

    Taken from ``open_time``, which is 00:00 UTC of that day, rather than from
    ``close_time``. Hyperliquid stamps a bar's close as the next bar's open
    minus one millisecond, so ``close_time`` also lands on the right day — but
    only under that convention. The open is the bar's own day under either,
    and this date is printed in the prompt.
    """
    return from_epoch_ms(candle.open_time).date()


def compute_macro_trend(daily_candles: Sequence[Candle], *, as_of_ms: int) -> MacroTrend | None:
    """The daily SMA pair's ordering and age, or ``None`` if it cannot be stated.

    ``daily_candles`` is a separate ``1d`` series, oldest first. ``as_of_ms``
    is the instant the context is dated to — the newest ``4h`` bar's close
    (:func:`.context_builder.context_as_of`) — and is what the daily feed's
    freshness is measured against.

    Three refusals, each logged as a WARNING because each one is a section
    silently missing from the prompt, and each returning ``None`` for the
    WHOLE section:

    1. fewer than :data:`MACRO_SLOW_PERIOD` daily bars — no slow average
       exists at any bar. This is the permanent state for a coin listed less
       than 200 days ago, so the warning then repeats every cycle: that is the
       signal to turn the switch off for that coin, not a fault to fix.
    2. the newest daily bar is more than :data:`MAX_DAILY_CANDLE_AGE_MS` old,
       or closes AFTER ``as_of_ms``. One check with two bounds. The future
       side is unreachable from a live fetch — both windows are cut at the
       same exchange clock — and exists for the paths that do not fetch. A
       series handed in newest-first also lands here, since its last element
       is then the oldest bar.
    3. the two averages are EXACTLY equal at the newest bar: there is no
       ordering to report.

    Note what is NOT a refusal: a window SHORTER than the configured lookback
    but at least :data:`MACRO_SLOW_PERIOD` bars long. That is a correct,
    narrower answer — the age of the ordering is simply capped sooner — and
    the adapter already warns about the short read itself.
    """
    count = len(daily_candles)
    if count < MACRO_SLOW_PERIOD:
        logger.warning(
            "macro trend needs %d daily candles for SMA(%d) but only %d are available; "
            "skipping the macro-trend section for this cycle (raise "
            "market_data.macro_trend_daily_lookback, or turn it off for a coin with less "
            "than %d days of history)",
            MACRO_SLOW_PERIOD,
            MACRO_SLOW_PERIOD,
            count,
            MACRO_SLOW_PERIOD,
        )
        return None

    newest = daily_candles[-1]
    age_ms = as_of_ms - newest.close_time
    if not 0 <= age_ms <= MAX_DAILY_CANDLE_AGE_MS:
        logger.warning(
            "the newest daily candle closed at %d, which is %d ms from the context's as-of "
            "%d — outside the [0, %d] ms a healthy daily feed sits in; skipping the "
            "macro-trend section for this cycle",
            newest.close_time,
            age_ms,
            as_of_ms,
            MAX_DAILY_CANDLE_AGE_MS,
        )
        return None

    closes = [c.close for c in daily_candles]

    def _sma(end_index: int, period: int) -> Decimal:
        """The simple average of ``period`` closes ending at ``end_index``."""
        return sum(closes[end_index - period + 1 : end_index + 1], Decimal(0)) / period

    with localcontext(DECIMAL_CONTEXT):
        # The oldest index at which BOTH averages exist. The slow one is the
        # binding constraint (it is the longer period), so this is its warm-up.
        oldest_comparable = MACRO_SLOW_PERIOD - 1
        alignments = [
            derive_macro_alignment(_sma(i, MACRO_FAST_PERIOD), _sma(i, MACRO_SLOW_PERIOD))
            for i in range(oldest_comparable, count)
        ]
        alignment = alignments[-1]
        if alignment is None:
            logger.warning(
                "the daily SMA(%d) and SMA(%d) are exactly equal at %s; there is no ordering "
                "to report, so the macro-trend section is skipped for this cycle",
                MACRO_FAST_PERIOD,
                MACRO_SLOW_PERIOD,
                _sma(count - 1, MACRO_SLOW_PERIOD),
            )
            return None

        # Walk back over the run of bars carrying the CURRENT ordering. A bar
        # where the two averages were exactly equal breaks the run rather than
        # continuing it — the conservative reading, since it can only make the
        # reported age shorter than the truth, never longer.
        run_start = len(alignments) - 1
        while run_start > 0 and alignments[run_start - 1] is alignment:
            run_start -= 1
        days_in_state = len(alignments) - run_start
        # A run reaching the oldest comparable bar means no change of ordering
        # is visible inside the window: the true age is at least this, and the
        # bar it started on is not in the window at all.
        capped = run_start == 0
        last_change_date = (
            None if capped else _bar_date(daily_candles[oldest_comparable + run_start])
        )

        sma_fast = _sma(count - 1, MACRO_FAST_PERIOD)
        sma_slow = _sma(count - 1, MACRO_SLOW_PERIOD)
        latest_close = newest.close
        # Both percentages are OF the slow average, which is positive because
        # every candle price is (``Candle``'s own guard) — an average of
        # positives cannot be zero. ``MacroTrend`` checks it again anyway.
        separation_pct = float((sma_fast - sma_slow) / sma_slow * 100)
        close_vs_slow_pct = float((latest_close - sma_slow) / sma_slow * 100)

    return MacroTrend(
        sma_fast=sma_fast,
        sma_slow=sma_slow,
        alignment=alignment,
        separation_pct=separation_pct,
        days_in_state=days_in_state,
        state_age_capped=capped,
        last_change_date=last_change_date,
        as_of_date=_bar_date(newest),
        latest_close=latest_close,
        close_vs_slow_pct=close_vs_slow_pct,
        candle_count=count,
        fast_period=MACRO_FAST_PERIOD,
        slow_period=MACRO_SLOW_PERIOD,
    )
