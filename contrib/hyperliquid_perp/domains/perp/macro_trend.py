"""Macro trend — how the DAILY SMA(50)/SMA(200) pair is ordered, and for how long.

Adds the one thing the ``4h`` context cannot express: a multi-month backdrop.
``context_builder.classify_regime`` already labels the trend, but it does so
from EMA(20)/EMA(50) over the ``4h`` series — the slower of those two averages
spans about eight days, so the widest thing it can see is a swing. (The 4h
FETCH is 200 candles ≈ 33 days; the indicators cut from it see less than that.)
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

from ...common.constants import MACRO_FAST_PERIOD, MIN_MACRO_TREND_LOOKBACK
from ...common.decimal_context import DECIMAL_CONTEXT
from ...common.instants import from_epoch_ms
from .schema import Candle, CandleInterval, MacroTrend, derive_macro_alignment

logger = logging.getLogger(__name__)

__all__ = [
    "MACRO_CANDLE_INTERVAL",
    "MACRO_FAST_PERIOD",
    "MACRO_SLOW_PERIOD",
    "MAX_DAILY_CANDLE_AGE_MS",
    "compute_macro_trend",
]

# The slow period, under this module's own name. Bound to the config floor
# rather than written out again: the floor exists BECAUSE a window shorter
# than the slow period has no slow average at any bar, so the two are one
# number. ``MACRO_FAST_PERIOD`` is imported from the same place for the same
# reason — ``MacroTrend`` pins BOTH period fields to those constants, which is
# what makes the binding checkable instead of a claim in a comment.
MACRO_SLOW_PERIOD: Final = MIN_MACRO_TREND_LOOKBACK

# The interval the daily series is fetched at, derived from the vocabulary
# rather than typed as a bare ``"1d"`` at the call site in ``engine_bridge``,
# so the spelling is decided in exactly one place. Taking it from the enum
# also means a typo cannot compile: ``engine_bridge`` catches the adapter's
# identity-echo refusal and turns it into an omitted section, so a hand-typed
# interval would NOT surface as a loud fetch failure — it would be a section
# that never appears, which is the silent-degradation mode the config layer
# spends two refusals preventing.
MACRO_CANDLE_INTERVAL: Final[str] = CandleInterval.D1.value

# How far behind the context's own as-of the newest daily bar may be. Daily
# bars close at 00:00 UTC and so does every sixth 4h bar, so a healthy feed
# sits anywhere in 0-20h — EXCEPT for the one case that lands exactly on the
# bound: a 4h bar that closed at 00:00 while the daily bar covering the day
# just ended has not been published yet is exactly 24h behind. Hence ``<=``,
# not ``<``. Anything beyond it means the daily feed is missing at least one
# whole bar, and a section that quietly averages a stale series is worse than
# no section.
MAX_DAILY_CANDLE_AGE_MS: Final = 24 * 60 * 60_000


def _gap(ms: int) -> str:
    """A duration rendered at a scale that never reads as no duration at all.

    A fixed unit makes a small gap vanish: at hours to one decimal, a bar one
    millisecond early prints as ``0.0h`` — no gap, in a sentence about a gap.
    So the unit is the LARGEST whose figure is at least 1.0, and milliseconds
    are printed as an integer when even seconds would not reach that.
    (``value >= 1.0`` is the same test as ``ms >= scale``; either spelling is
    fine.)

    Both halves of that are load-bearing, and this helper got each of them
    wrong once before settling here:

    - **The SECONDS tier.** The first version went milliseconds, minutes,
      hours, so 1.5 s landed in minutes and printed ``0.0 min`` — the same
      vanishing gap one unit down from where it was first found.
    - **The unrounded comparison.** The second version selected on
      ``round(value, 1) >= 1.0``, which promotes from 0.95 of a unit upward,
      so a stalled feed at 57 minutes printed ``1.0h`` — overstating the one
      number that sizes the outage by about 5%.

    The cost of not rounding is that the top of a unit is not normalised:
    59.999 s prints as ``60.0 s`` rather than ``1.0 min``. That is
    unidiomatic, and the figure is still rounded to the printed grid (a tenth
    of a second here). What it does not do is promote a figure into a unit it
    has not reached, which is the error that misleads.

    What this does NOT fix, because no choice of unit can: a value just past a
    bound still prints as that bound. 24h + 1 ms is ``24.0h`` at any sane
    precision, and in a sentence saying the 24h limit was exceeded that reads
    as a contradiction. The caller closes that by printing the EXCESS as a
    second figure — ``24.0h ... 1 ms past the 24h`` — which this helper only
    supplies the formatting for.
    """
    for scale, unit in ((3_600_000, "h"), (60_000, " min"), (1000, " s")):
        value = ms / scale
        if value >= 1.0:
            return f"{value:.1f}{unit}"
    return f"{ms} ms"


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

    Four refusals, each logged as a WARNING because each one is a section
    silently missing from the prompt, and each returning ``None`` for the
    WHOLE section:

    1. fewer than :data:`MACRO_SLOW_PERIOD` daily BARS — no slow average
       exists at any bar. For a coin listed less than 200 days ago this is
       permanent and the warning repeats every cycle: that is the signal to
       turn the switch off for that coin, not a fault to fix. (Bars, not days:
       nothing here checks the series for gaps, so 200 bars can span longer.)
    2. the newest daily bar is more than :data:`MAX_DAILY_CANDLE_AGE_MS` old
       — the daily feed has stopped publishing.
    3. the newest daily bar closes AFTER ``as_of_ms``. A separate refusal from
       2 with its own message, because it is a different fault pointing at a
       different feed: ``as_of_ms`` is the newest closed bar of the caller's
       own shorter series, not a clock, so this says THAT series is lagging.
       Both windows being cut at the same exchange clock makes it rare, NOT
       impossible — a tail gap in the short series pulls ``as_of_ms`` back
       below a current daily close while still clearing the freshness guard's
       own 3-interval tolerance. A series handed in newest-first also lands
       here, since its last element is then the oldest bar.
    4. the two averages are EXACTLY equal at the newest bar: there is no
       ordering to report.

    Note what is NOT a refusal: a window SHORTER than the configured lookback
    but at least :data:`MACRO_SLOW_PERIOD` bars long. That is a correct,
    narrower answer — the age of the ordering is simply capped sooner — and
    the adapter already warns about the short read itself.
    """
    count = len(daily_candles)
    if count < MACRO_SLOW_PERIOD:
        # The fix that WORKS in the common case is named first. A coin listed
        # less than 200 days ago can never satisfy this however high the
        # lookback goes, and that is the case an operator meets most often —
        # leading with "raise the lookback" sent them to a setting that cannot
        # help. The adapter's own short-read WARNING names what was requested.
        logger.warning(
            "macro trend needs %d daily candles for SMA(%d) but only %d are available; "
            "skipping the macro-trend section for this cycle (a coin the venue has fewer "
            "than %d daily bars for can never fill it — turn "
            "market_data.macro_trend_daily_lookback off for that coin; otherwise raise it)",
            MACRO_SLOW_PERIOD,
            MACRO_SLOW_PERIOD,
            count,
            MACRO_SLOW_PERIOD,
        )
        return None

    newest = daily_candles[-1]
    age_ms = as_of_ms - newest.close_time
    if age_ms > MAX_DAILY_CANDLE_AGE_MS:
        # A readable scale, not raw epoch-ms: this repeats every cycle while
        # a feed is down, and an operator should not have to subtract two
        # 13-digit integers to learn that it is two days stale. Two figures,
        # because the age alone cannot say the bound was passed when it
        # rounds to the bound (``24.0h`` against a 24h limit) — the second
        # is the excess, and it can be milliseconds. The date is printed
        # for the same readability reason.
        logger.warning(
            "the newest daily candle is dated %s and closed %s before this context's "
            "as-of — %s past the %.0fh a healthy daily feed stays within; the daily feed "
            "has stopped publishing, so the macro-trend section is skipped for this cycle "
            "(turn market_data.macro_trend_daily_lookback off if it stays down)",
            _bar_date(newest).isoformat(),
            _gap(age_ms),
            _gap(age_ms - MAX_DAILY_CANDLE_AGE_MS),
            MAX_DAILY_CANDLE_AGE_MS / 3_600_000,
        )
        return None
    if age_ms < 0:
        # The OTHER fault, and it is not the daily feed's: ``as_of_ms`` is the
        # newest CLOSED 4h bar, so a negative age means the short series is
        # behind the daily one. Saying "the daily feed is stale" here would
        # send an operator to inspect the healthy feed.
        logger.warning(
            "the newest daily candle (dated %s) closes %s AFTER this context's as-of — "
            "and that as-of is the newest CLOSED bar of the context's own shorter series, "
            "not a clock, so it is that series lagging rather than the daily one; "
            "skipping the macro-trend section for this cycle",
            _bar_date(newest).isoformat(),
            _gap(-age_ms),
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
        bars_in_state = len(alignments) - run_start
        # A run reaching the oldest comparable bar has nothing before it to
        # have begun from, so the window cannot date its start: the length is
        # then a lower bound and the date is withheld. That is the ONLY cause,
        # which is what lets ``MacroTrend`` check the flag as an exact
        # equivalence against the run length.
        #
        # The date names the bar the run BEGAN on, and that is all it is
        # allowed to name. It is the first bar carrying the current alignment,
        # so the bar before it carried something else — the opposite
        # alignment, or (rarely, and only on constructed flat prices) an exact
        # tie. Calling it "the bar the alignment changed on" would read as a
        # turn in both cases, and in the tie case nothing turned: the pair
        # merely touched equality between two stretches of the same ordering.
        # The renderer says "began", which is true of both.
        capped = run_start == 0
        run_started_date = (
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

        # Constructed INSIDE the pinned context: ``MacroTrend`` re-derives both
        # percentages to check them, and doing that under whatever precision
        # the thread happens to carry can disagree with what was computed here
        # — a pure function raising ValueError mid-cycle.
        return MacroTrend(
            sma_fast=sma_fast,
            sma_slow=sma_slow,
            alignment=alignment,
            separation_pct=separation_pct,
            bars_in_state=bars_in_state,
            state_age_capped=capped,
            run_started_date=run_started_date,
            as_of_date=_bar_date(newest),
            latest_close=latest_close,
            close_vs_slow_pct=close_vs_slow_pct,
            candle_count=count,
            fast_period=MACRO_FAST_PERIOD,
            slow_period=MACRO_SLOW_PERIOD,
        )
