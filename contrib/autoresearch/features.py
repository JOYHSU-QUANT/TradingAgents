"""Feature values, bar by bar, computed only from what had closed by that bar.

Plan §3.6 asks for no look-ahead as a CONSTRUCTION guarantee rather than as
something a test hopes to catch afterwards, and that is the whole shape of
this module. Every feature is a function of bars ``0..t`` and of the daily
bars and funding settlements that had already closed at ``bars[t].close_time``
— there is no path here that reads ``t+1``, because none is written. The
property is still tested (``tests/test_features.py`` recomputes every feature
on a bundle truncated at ``t`` and demands the same answer), but the test is
a check on the construction, not the guarantee itself.

The three borrowed analytics are used the way the live path uses them, and
that is a decision about COMPARABILITY, taken down to the window each one
sees. The indicator engine is handed the last
:data:`LIVE_CANDLE_LOOKBACK` closed bars — the count the trader's own fetch
asks for every cycle, read off that config's default rather than written down
again here — and asked for the latest value. The regime label comes from the
borrowed classifier over those indicators. The funding z-score is the
borrowed function over the stored settlements, with the rate being scored
kept OUT of the window it is scored against, because the live caller passes a
rate that is not in its history (the unsettled current one) and including it
folds the value into its own mean. A research number computed a slightly
different way would score strategies against a market the live path never
saw, and the difference would be invisible in the results.

Feeding the engine a fixed trailing window rather than the whole prefix is
also what keeps the cost linear: the engine rebuilds its frame on every call,
so an expanding prefix grows with the series — measured on this box at
12 ms a bar by bar 5000, against about 2.5 ms flat here.

Two places where mirroring the live path means NOT passing its value through:

- **The regime during warm-up.** ``classify_regime`` answers ``RANGING`` when
  its indicators are missing, and the live path never shows that answer to
  anyone: ``context_guards.context_refusal`` refuses the whole cycle when any
  of the regime trio is unusable, precisely so nothing trades on a
  fabricated-calm label. The faithful mirror of that is ``None`` — the
  feature is not available yet — so a rule filtering on ``regime == ranging``
  does not fire during warm-up instead of firing on a default.
- **A stale funding rate.** The settlement series can end before the bars do
  (a store filled at different times, a venue gap). Carrying the last known
  rate forward would quietly price weeks of carry off one observation, so a
  settlement older than one interval plus its posting jitter is not this
  bar's rate at all, and the feature is ``None``.

A windowed number carries the name it is filed under or it is not reported: a
``funding_cum_24`` whose window is missing settlements would say the carry was
small when the settlements were simply absent, and a ``funding_zscore_30``
standardised against one day would be a different measurement wearing the same
name. So the occupancy of each window is COUNTED (see
:func:`_window_is_covered`), not inferred from where the series begins — the
front-edge test that preceded it could see only a series starting inside the
window, and was blind to every hole that did not touch the edge.

This is one place the live path is deliberately NOT mirrored: live fetches its
own thirty-day funding window every cycle and prints the sample count into the
prompt beside the z-score, so a thin window is visible to whoever reads it. A
research feature has no such channel.

And a feature that is unavailable at EVERY bar is refused rather than
reported, because ``None`` everywhere is indistinguishable from a rule that
never fired — which is a strategy scored as tried when the bundle could never
have answered it. Whether the store is fit to measure on is still the
evaluator's call (plan PR A3); what this module owes it is to be unable to
hand it a column of silence.
"""

from __future__ import annotations

import math
import statistics
from bisect import bisect_left, bisect_right
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final

from .constants import (
    CANDLE_STAMP_TOLERANCE_MS,
    FUNDING_INTERVAL_MS,
    FUNDING_STAMP_TOLERANCE_MS,
    MS_PER_DAY,
)
from .upstream import (
    REGIME_INDICATORS,
    Candle,
    FundingPoint,
    MarketDataConfig,
    MarketRegime,
    context_analytics,
    from_epoch_ms,
    required_candles,
)
from .vocabulary import FeatureKind, FeatureRef, SeriesSource, spec_of

__all__ = [
    "LIVE_CANDLE_LOOKBACK",
    "FeatureError",
    "FeatureFrame",
    "FeatureValue",
    "SeriesBundle",
]

# How many closed bars the indicator engine is shown at each bar — the count
# the live path's own fetch asks for, taken from that config's declared
# DEFAULT rather than repeated as a number here, so the two cannot drift apart
# without ``tests/test_pins.py`` saying so.
#
# The default is as far as this reaches, and the limit is worth stating: the
# running value is a YAML key an operator may set, in a file that is
# deliberately not in the repository. So "the same bar gets the same
# indicator here and there" holds while the server sits at the default, and a
# server fetching a different number would make research indicators
# incomparable with nothing going red. Closing that needs the lookback to
# become an argument to the frame rather than a constant, which is PR A3's to
# do when it learns where an experiment's parameters come from.
LIVE_CANDLE_LOOKBACK: Final = MarketDataConfig().candle_lookback

# Bound once, at import: ``context_analytics`` imports pandas and stockstats
# inside the call, so the cost lands on whatever imports THIS module and on
# nothing else (the store and gap commands never do).
_ANALYTICS: Final = context_analytics()

# The upstream indicator names this vocabulary is built on, plus the trio the
# regime classifier reads. Derived from the vocabulary rather than written out,
# so adding ``ema_100`` to the table is the only edit that needs making — and
# unioned with ``REGIME_INDICATORS`` because the regime needs its three whether
# or not a spec refers to them.
_INDICATOR_KINDS: Final = (FeatureKind.EMA, FeatureKind.RSI, FeatureKind.ATR)
_FEATURE_INDICATORS: Final[dict[str, tuple[FeatureKind, int]]] = {
    f"{kind.value}_{period}": (kind, period)
    for kind in _INDICATOR_KINDS
    for period in spec_of(kind).periods
}
_INDICATOR_NAMES: Final[tuple[str, ...]] = tuple(
    dict.fromkeys(list(_FEATURE_INDICATORS) + list(REGIME_INDICATORS))
)

# How old the last settlement may be and still be THIS bar's funding rate: one
# interval, plus the jitter the venue posts with (see ``constants``). A bar
# closing at 04:00:00.000 whose 04:00 settlement was stamped 57 ms late reads
# the 03:00 one, which is exactly one interval old and correct.
#
# This answers one question only — is the last settlement still this bar's
# rate. It was briefly also the slack allowed at a window's START, on the
# reasoning that a half-open window opening at T first contains the
# settlement at T + interval. That reasoning was wrong: settlements are
# stamped just AFTER the hour, so a window opening on the bar grid at T first
# contains the one at T + jitter, and allowing a whole interval let a
# four-settlement sum be reported with three. Counting the window's occupancy
# asks the question directly and needs no boundary reasoning at all.
_FUNDING_STALE_MS: Final = FUNDING_INTERVAL_MS + FUNDING_STAMP_TOLERANCE_MS

# The same question for the daily backdrop, with a different answer at the
# boundary. Carrying a daily close forward is the same defect as carrying a
# funding rate forward — a trend filter frozen at one number, permanently on
# or permanently off, scored as a filter that was live — and ``fetch`` walks
# 4h and 1d in separate invocations, so "4h current, 1d months behind" is the
# default consequence of running one and not the other.
#
# Stale AT one day rather than past it, which is where this differs from
# funding, and the reason is the stamps. Bar stamps are exact
# (``CANDLE_STAMP_TOLERANCE_MS`` is 0, measured), so a 4h bar closing at the
# same instant as a daily bar SEES that daily bar, at an age of zero; on a
# complete daily series the oldest a close can be is one bar short of a day
# (20h on the 4h grid). An age of exactly one day is therefore only reachable
# when the daily bar due at that instant is missing — the case this exists to
# catch — and it would land on the 00:00 UTC bar, where a daily trend filter
# is most likely to be read. Funding cannot use the same rule: its
# settlements are stamped AFTER the hour, so on a complete series a bar
# legitimately reads a rate a full interval old.
_DAILY_STALE_MS: Final = MS_PER_DAY - CANDLE_STAMP_TOLERANCE_MS

# How much of a window has to be there for the number over it to carry the
# name it is filed under. A fraction rather than exact equality, because the
# venue itself occasionally skips a settlement and a research feature that
# went ``None`` on every such bar would be measuring the venue's uptime; a
# fraction rather than nothing, because the alternative is a ``funding_cum_24``
# that says the carry was small when the settlements were simply absent, and a
# ``funding_zscore_30`` standardised against one day (the borrowed function's
# own floor is 24 samples).
#
# It is a POLICY, not a fact about the market, and it is the only one in this
# module: a hole big enough to breach it is a store problem, which the gap
# scan reports and a re-fetch fixes.
_MIN_WINDOW_COVERAGE: Final = 0.9


def _window_is_covered(observed: int, span_ms: int) -> bool:
    """Does a window spanning ``span_ms`` hold enough of its hourly settlements?

    Counted rather than inferred from where the series starts. The front-edge
    test this replaces could only see a series that BEGINS inside the window,
    and it had to reason about the boundary to do even that — it allowed a
    whole extra settlement of slack, which understated a four-settlement sum
    by a quarter, and it was blind to every hole that did not touch the edge.

    Rounded UP, which is the whole of the difference between a policy and an
    accident: a four-settlement window floored at 90% requires three, so
    ``funding_cum_1`` would have gone on reporting the same quarter-understated
    sum this function was written to stop — and a window shorter than about an
    hour would have required none at all, reporting a carry of zero from no
    settlements whatever.
    """
    return observed >= math.ceil(span_ms / FUNDING_INTERVAL_MS * _MIN_WINDOW_COVERAGE)


# What a computed feature is: a number, a regime label, or "not available at
# this bar". ``None`` is never a zero and never a NaN — the same rule the perp
# package's indicators follow, for the same reason.
FeatureValue = float | MarketRegime | None


class FeatureError(RuntimeError):
    """A feature was asked for that this bundle cannot answer at all.

    Distinct from a ``None`` value, and the distinction is the point: ``None``
    is a feature that is not available AT THAT BAR (warm-up, a settlement that
    has not happened), which is normal and which the evaluator reads as "this
    rule does not fire here". This is a feature whose whole source series is
    missing — a daily backdrop nothing fetched, a funding series nothing
    walked — where every bar would be ``None`` and the strategy would be
    scored as tried-and-found-wanting when it was never tried at all.
    """


def _require_ascending(stamps: Sequence[int], *, what: str) -> None:
    """Refuse a series that is not strictly ascending, naming the pair that is not.

    Checked here rather than assumed from the store's ``ORDER BY``, because a
    bundle is also assembled by hand — by a test, by a later phase slicing a
    window — and every alignment below (the daily pointer, the funding
    bisects, every lookback) is a statement about an ordered series. An
    out-of-order stamp would not fail: it would quietly make ``sma_20`` a mean
    of twenty prices in no particular order.
    """
    for earlier, later in zip(stamps, stamps[1:], strict=False):
        if later <= earlier:
            raise FeatureError(
                f"{what} must be strictly ascending by timestamp; {later} follows {earlier}"
            )


def _require_daily_cadence(daily: Sequence[Candle]) -> None:
    """Refuse a "daily" series whose bars are not a day apart.

    Ordering is checked above; this checks the cadence, because nothing else
    does and everything downstream assumes it. ``SeriesBundle(bars, daily=bars)``
    — one positional slip in the bundle builder PR A3 will write — was
    accepted, and then ``sma_1d_200`` was a 200-BAR mean of 4h candles (33
    days) wearing a 200-day name, while the staleness rule that exists to
    catch a stalled daily series was measuring against a cadence it had never
    verified.

    Closer together than a day is the slip; FURTHER apart is a hole, which is
    a fact about the venue's history and is the coverage check's business, not
    this one's.
    """
    for earlier, later in zip(daily, daily[1:], strict=False):
        if later.close_time - earlier.close_time < MS_PER_DAY - CANDLE_STAMP_TOLERANCE_MS:
            raise FeatureError(
                f"the daily series has bars {later.close_time - earlier.close_time} ms apart, "
                f"which is less than a day — this is not a 1d series, and every feature "
                f"reading it would be measured over a window shorter than its name"
            )


@dataclass(frozen=True, init=False)
class SeriesBundle:
    """The stored history one experiment measures on: bars, daily bars, funding.

    Sequences are copied into tuples at construction. Not tidiness: the frame
    caches each feature's array the first time it is asked for, so a caller
    holding a list it later appends to would have a bundle whose contents no
    longer match the numbers already computed from it.

    ``daily`` and ``funding`` may be empty, and an empty one is not an error
    until a feature that needs it is asked for — a price-only experiment
    should not have to fetch funding it never refers to.
    """

    bars: tuple[Candle, ...]
    daily: tuple[Candle, ...]
    funding: tuple[FundingPoint, ...]

    def __init__(
        self,
        bars: Sequence[Candle],
        daily: Sequence[Candle] = (),
        funding: Sequence[FundingPoint] = (),
    ) -> None:
        object.__setattr__(self, "bars", tuple(bars))
        object.__setattr__(self, "daily", tuple(daily))
        object.__setattr__(self, "funding", tuple(funding))
        if not self.bars:
            raise FeatureError("a feature bundle needs at least one bar to measure on")
        # Both stamps, because two different halves of this module read two
        # different ones: the lookbacks index bars in ``open_time`` order,
        # while every alignment (the daily pointer, the funding bisects) is
        # cut at ``close_time``. Venue rows have the second following the first
        # by one interval, so on real data the two checks agree — and a
        # hand-assembled bundle mixing intervals is exactly the case where they
        # would not, and where the daily pointer would stall silently rather
        # than refuse.
        _require_ascending([bar.open_time for bar in self.bars], what="bars")
        _require_ascending([bar.close_time for bar in self.bars], what="bar closes")
        _require_ascending([bar.open_time for bar in self.daily], what="daily bars")
        _require_ascending([bar.close_time for bar in self.daily], what="daily bar closes")
        _require_daily_cadence(self.daily)
        _require_ascending([point.time for point in self.funding], what="funding settlements")


@dataclass(frozen=True)
class FeatureFrame:
    """Every feature of one bundle, computed on demand and kept.

    One frame per experiment, not one per trial: the arrays depend on the
    history alone, so a hypothesis loop scoring fifty specs over the same
    bundle computes them once. That matters most for the borrowed indicator
    engine, which is asked for its latest value once per bar — measured at
    about 2.5 ms a bar on this box, so a 5000-bar series is some thirteen
    seconds paid once rather than thirteen seconds a trial.

    Frozen so the bundle cannot be swapped out from under the cache. The cache
    itself is a plain dict that is mutated, never rebound: an assignable
    ``bundle`` meant every already-computed column silently belonged to a
    different history, and ``_require_source`` — consulted only on a cache
    miss — would not have looked again.
    """

    bundle: SeriesBundle
    _cache: dict[tuple[FeatureKind, int | None], tuple[FeatureValue, ...]] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )
    # The alignments every column is built on: the close series, which daily
    # bar is usable at each bar, the settlement in force, and the settlement
    # stamps. Per-bundle facts like the columns themselves, so they are kept
    # the same way.
    _alignments: dict[str, list] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )

    # -- the two public verbs ---------------------------------------------

    def series(self, ref: FeatureRef) -> tuple[FeatureValue, ...]:
        """The whole per-bar array for one feature, index-aligned with ``bundle.bars``.

        Takes a :class:`FeatureRef` rather than a loose ``(kind, period)``
        pair, so the closed vocabulary is enforced on the public verb and not
        only on the parsed path. With the pair, ``series(SMA, 7)`` computed and
        cached a period the vocabulary refuses by name, and ``series(EMA, 9)``
        came back as a bare ``KeyError`` out of the cache — neither of which an
        evaluator's "this trial is unevaluable" lane would catch.

        The ref's OFFSET is not consulted here: an offset shifts which bar a
        value is read at, which is :meth:`value_at`'s business, and it would be
        a second cache key for one identical column.
        """
        key = (ref.kind, ref.period)
        cached = self._cache.get(key)
        if cached is None:
            self._require_source(ref.kind)
            cached = self._compute(ref.kind, ref.period)
            self._cache[key] = cached
        # Checked on every return, not only after computing: the indicator
        # walk fills the cache for five features at once, so four of them
        # reached their first caller through the hit above and skipped the
        # guard entirely. That is the likeliest order too — ``spec.features``
        # sorts the regime last, so an evaluator walking it asks a working
        # indicator first and would have been handed the column of silence
        # this refuses.
        self._require_an_answer_somewhere(ref, cached)
        return cached

    def value_at(self, ref: FeatureRef, index: int) -> FeatureValue:
        """``ref``'s value for the decision taken at ``bars[index]``'s close.

        The offset is applied HERE rather than inside each computation, so
        every feature gets the same reading of what "two bars ago" means, and
        a reach past the start of the series is ``None`` — the bar it names
        does not exist, which is the same answer as a feature that has not
        warmed up.
        """
        if not 0 <= index < len(self.bundle.bars):
            raise FeatureError(
                f"bar index {index} is outside the bundle's {len(self.bundle.bars)} bars"
            )
        shifted = index - ref.offset
        if shifted < 0:
            return None
        return self.series(ref)[shifted]

    # -- guards ------------------------------------------------------------

    def _require_an_answer_somewhere(
        self, ref: FeatureRef, column: tuple[FeatureValue, ...]
    ) -> None:
        """Refuse a feature this bundle cannot answer at ANY bar.

        ``_require_source`` asks the narrow version of this question — is the
        source series there at all — and the narrow version turned out to be
        the rare one. A daily series that ends before the bars begin, a
        funding walk resumed from a later ``--since``, a spec naming
        ``sma_1d_200`` over a store holding ninety days: each leaves a column
        that is ``None`` at every bar, which is the value warm-up produces and
        which an evaluator therefore reads as "this rule does not fire here",
        bar after bar, to the end. The strategy is then filed as tried and
        found wanting, having never been tried — the one outcome this package
        exists to prevent.

        A column empty everywhere is the same fact whatever produced it, so
        the check is on the column rather than on any of the reasons. The
        engine's own failure keeps its more specific sentence by being checked
        first.
        """
        if any(value is not None for value in column):
            return
        bars = self.bundle.bars
        span = f"{from_epoch_ms(bars[0].close_time):%Y-%m-%d} to {from_epoch_ms(bars[-1].close_time):%Y-%m-%d}"
        raise FeatureError(
            f"{ref.name} has no value at any of this bundle's {len(bars)} bars ({span}), so a "
            f"spec reading it cannot be evaluated here — it would score as a strategy that "
            f"never fired. Fetch more history, or measure a spec this store can answer."
        )

    def _require_source(self, kind: FeatureKind) -> None:
        source = spec_of(kind).source
        if source is SeriesSource.DAILY and not self.bundle.daily:
            raise FeatureError(
                f"{kind.value} is computed from the daily series, and this bundle has no "
                f"daily bars — fetch --interval 1d before measuring a spec that refers to it"
            )
        if source is SeriesSource.FUNDING and not self.bundle.funding:
            raise FeatureError(
                f"{kind.value} is computed from the funding series, and this bundle has no "
                f"settlements — fetch without --skip-funding before measuring a spec that "
                f"refers to it"
            )

    # -- the computations --------------------------------------------------

    def _compute(self, kind: FeatureKind, period: int | None) -> tuple[FeatureValue, ...]:
        if kind in _INDICATOR_KINDS or kind is FeatureKind.REGIME:
            self._indicator_pass()
            return self._cache[(kind, period)]
        return _BUILDERS[kind](self, period)

    def _indicator_pass(self) -> None:
        """Fill every borrowed-indicator feature, and the regime, in one walk.

        One walk, for every name, whatever was asked for. The shared part of a
        call is smaller than that sounds — measured on a 200-bar window, the
        frame build is about 0.55 ms of a 2.4 ms four-name call — so a frame
        asked for one indicator does pay for three it will not read.

        It is still the right default HERE, and the reason is the caller: one
        frame serves a whole experiment (plan §5's A4 scores many specs
        against one bundle), so the union is wanted sooner or later, while a
        demand-driven set that re-walks per newly-requested name costs twice
        the single walk in the worst case. A frame told its feature set up
        front could have both, and that is A3's to give — it knows every
        spec's features before scoring starts.

        The regime rides along because it is a function of three of those same
        values at the same bar.
        """
        bars = self.bundle.bars
        columns: dict[str, list[FeatureValue]] = {name: [] for name in _INDICATOR_NAMES}
        regimes: list[FeatureValue] = []
        for index, bar in enumerate(bars):
            # The window ENDS at this bar, which is the whole no-look-ahead
            # guarantee: the engine cannot see further because it was never
            # given further. It BEGINS a fixed lookback earlier, which is the
            # comparability one — the live path fetches exactly that many bars
            # per cycle, so the same bar gets the same indicator here and
            # there.
            window = bars[max(0, index + 1 - LIVE_CANDLE_LOOKBACK) : index + 1]
            values = _ANALYTICS.compute_indicators(window, _INDICATOR_NAMES)
            for name in _INDICATOR_NAMES:
                columns[name].append(values.get(name))
            regimes.append(_regime_of(values, bar.close))
        self._require_a_working_engine(columns)
        # Only the names that ARE features get cached as features. The walk
        # also computes whatever the regime classifier reads, and those two
        # sets are not the same one: a trio member that is not in the
        # vocabulary (upstream is free to add one) has no key to be filed
        # under, and inferring one from its spelling would turn that addition
        # into a crash here.
        for name, key in _FEATURE_INDICATORS.items():
            self._cache[key] = tuple(columns[name])
        self._cache[(FeatureKind.REGIME, None)] = tuple(regimes)

    def _require_a_working_engine(self, columns: dict[str, list[FeatureValue]]) -> None:
        """Refuse a column that is empty everywhere despite having warmed up.

        The borrowed engine answers ``None`` for an indicator it could not
        compute — a stockstats or pandas breakage is caught per indicator and
        logged, not raised. On the live path that is one warning an operator
        sees each cycle; here the engine is called once per bar, so the same
        failure is a column of five thousand ``None``s that reads exactly like
        warm-up, and every strategy touching it would be scored as tried.

        Warm-up is the one thing that CAN legitimately empty a column, so the
        threshold is the engine's own minimum for that indicator (borrowed,
        not guessed). Past it, an all-empty column is not a market fact.
        """
        bar_count = len(self.bundle.bars)
        dead = sorted(
            name
            for name, column in columns.items()
            if bar_count >= required_candles([name]) and all(value is None for value in column)
        )
        if dead:
            raise FeatureError(
                f"the indicator engine returned nothing for {dead} across all {bar_count} "
                f"bars, well past the warm-up each of them needs — this is the engine "
                f"(stockstats/pandas) failing, not a market without a value, and every "
                f"feature built on it would otherwise read as permanently warming up"
            )

    def _close(self, _period: int | None) -> tuple[FeatureValue, ...]:
        return tuple(self._closes())

    def _ret(self, period: int | None) -> tuple[FeatureValue, ...]:
        closes = self._closes()
        assert period is not None
        return tuple(
            None
            if index < period or closes[index - period] <= 0
            else closes[index] / closes[index - period] - 1.0
            for index in range(len(closes))
        )

    def _sma(self, period: int | None) -> tuple[FeatureValue, ...]:
        closes = self._closes()
        assert period is not None
        return tuple(
            None if index + 1 < period else statistics.fmean(closes[index + 1 - period : index + 1])
            for index in range(len(closes))
        )

    def _atr_pct(self, period: int | None) -> tuple[FeatureValue, ...]:
        atr = self.series(FeatureRef(FeatureKind.ATR, period))
        closes = self._closes()
        return tuple(
            None if value is None or closes[index] <= 0 else float(value) / closes[index]
            for index, value in enumerate(atr)
        )

    def _donchian_high(self, period: int | None) -> tuple[FeatureValue, ...]:
        return self._prior_window([float(bar.high) for bar in self.bundle.bars], period, max)

    def _donchian_low(self, period: int | None) -> tuple[FeatureValue, ...]:
        return self._prior_window([float(bar.low) for bar in self.bundle.bars], period, min)

    def _prior_window(
        self,
        values: list[float],
        period: int | None,
        reduce: Callable[[Sequence[float]], float],
    ) -> tuple[FeatureValue, ...]:
        """``reduce`` over the ``period`` bars BEFORE each bar; this bar excluded.

        Excluding the current bar is what makes a breakout rule expressible at
        all. With this bar inside the window, ``close > donchian_high_20`` can
        only be true when the close IS the high of the window, so the rule
        reads as a plausible breakout and fires almost never — a strategy
        scored as tried when it was structurally dead. The vocabulary's own
        summary says "the N bars BEFORE this one" for the same reason.
        """
        assert period is not None
        return tuple(
            None if index < period else reduce(values[index - period : index])
            for index in range(len(values))
        )

    def _realized_vol(self, period: int | None) -> tuple[FeatureValue, ...]:
        closes = self._closes()
        assert period is not None
        steps: list[float | None] = [None]
        for index in range(1, len(closes)):
            previous = closes[index - 1]
            steps.append(None if previous <= 0 else closes[index] / previous - 1.0)
        out: list[FeatureValue] = []
        for index in range(len(closes)):
            # One warm-up test, not two: past the guard the slice holds exactly
            # ``period`` steps by construction, so a second length check could
            # never disagree with it and only makes a reader work out that it
            # cannot.
            window = steps[index + 1 - period : index + 1] if index + 1 >= period else None
            # Sample stdev (Bessel's n-1), the same estimator the borrowed
            # funding z-score uses on its own window: a lookback is a sample of
            # an ongoing process, not the whole of one.
            out.append(
                None
                if window is None or any(step is None for step in window)
                else statistics.stdev(window)
            )
        return tuple(out)

    def _close_1d(self, _period: int | None) -> tuple[FeatureValue, ...]:
        closes = [float(bar.close) for bar in self.bundle.daily]
        return tuple(None if count == 0 else closes[count - 1] for count in self._daily_counts())

    def _sma_1d(self, period: int | None) -> tuple[FeatureValue, ...]:
        """The mean of the daily closes inside the last ``period`` DAYS.

        Not "the last ``period`` daily closes", which is the same thing on a
        complete series and a different thing on one with a hole: twenty
        closes drawn from twenty-five calendar days is a twenty-five-day mean
        reported under a name that says twenty. Counted the way a funding
        window is, and refused below the same coverage.
        """
        daily = self.bundle.daily
        closes = [float(bar.close) for bar in daily]
        assert period is not None
        out: list[FeatureValue] = []
        for bar, count in zip(self.bundle.bars, self._daily_counts(), strict=True):
            opened = bar.close_time - period * MS_PER_DAY
            first = bisect_right([day.close_time for day in daily[:count]], opened)
            observed = count - first
            if count == 0 or observed < int(period * _MIN_WINDOW_COVERAGE):
                out.append(None)
                continue
            out.append(statistics.fmean(closes[first:count]))
        return tuple(out)

    def _funding_rate(self, _period: int | None) -> tuple[FeatureValue, ...]:
        points = self.bundle.funding
        return tuple(
            None if index is None else float(points[index].rate)
            for index in self._funding_indices()
        )

    def _funding_zscore(self, period: int | None) -> tuple[FeatureValue, ...]:
        points = self.bundle.funding
        times = self._funding_times()
        assert period is not None
        out: list[FeatureValue] = []
        for bar, found in zip(self.bundle.bars, self._funding_indices(), strict=True):
            if found is None:
                # No current rate means there is nothing to standardise. The
                # borrowed function would happily z-score a rate carried over
                # from last week against a window that does not contain it.
                out.append(None)
                continue
            as_of_ms = bar.close_time
            cutoff = as_of_ms - period * MS_PER_DAY
            # Sliced to the borrowed function's OWN window (it keeps
            # ``cutoff <= p.time < as_of_ms``), so the filter it runs is a
            # no-op. Without the slice this walks the whole settlement history
            # once per bar, which on a full store is tens of millions of
            # conversions for a thirty-day window.
            #
            # With ONE point dropped from it: the rate being scored. The live
            # caller passes the venue's current, unsettled rate, which is not
            # a member of the history it is scored against — and upstream says
            # why that matters, at the boundary it uses to exclude it:
            # folding the value into its own mean and deviation makes a
            # genuine outlier read as less extreme than it is. Here the rate
            # IS a settled point, so keeping the arrangement the same means
            # cutting the window at it rather than after it.
            #
            # ``found`` IS that upper bound: the stamps ascend, so the last
            # settlement at or before the close is either the last one inside
            # the borrowed function's half-open window or the one sitting
            # exactly on its edge, and slicing to it covers both.
            score, samples = _ANALYTICS.funding_zscore(
                points[bisect_left(times, cutoff) : found], points[found].rate, as_of_ms, period
            )
            # The sample count the borrowed function hands back, used rather
            # than discarded: its own floor is 24 — one day — so a window with
            # a month-long hole in the middle still answers, and answers a
            # one-day z-score under a name that says thirty. The live path
            # prints that count into the prompt beside the number; here there
            # is no reader to print it to.
            out.append(score if _window_is_covered(samples, period * MS_PER_DAY) else None)
        return tuple(out)

    def _funding_cum(self, period: int | None) -> tuple[FeatureValue, ...]:
        points = self.bundle.funding
        times = self._funding_times()
        totals = [0.0]
        for point in points:
            totals.append(totals[-1] + float(point.rate))
        bars = self.bundle.bars
        assert period is not None
        out: list[FeatureValue] = []
        for index, found in enumerate(self._funding_indices()):
            if index < period or found is None:
                # ``found is None`` means the series does not reach this bar;
                # summing to a window whose end is uncovered understates the
                # carry without looking wrong.
                out.append(None)
                continue
            opened = bars[index - period].close_time
            # ``found`` is the last settlement at or before this bar's close,
            # so ``found + 1`` is where the running total stands there — the
            # same point the rate feature reads, said once rather than
            # re-derived with a second bisect that could drift from it.
            first = bisect_right(times, opened)
            if not _window_is_covered(found + 1 - first, bars[index].close_time - opened):
                out.append(None)
                continue
            out.append(totals[found + 1] - totals[first])
        return tuple(out)

    # -- shared alignment --------------------------------------------------

    def _closes(self) -> list[float]:
        """The close series, cached like the columns it feeds.

        The alignments below are per-bundle facts exactly as a feature column
        is, and seventeen of the vocabulary's forty-two columns start by
        rebuilding this list. Cached here rather than at each caller so
        "compute it once per bundle" is one decision in one place.
        """
        cached = self._alignments.get("closes")
        if cached is None:
            cached = [float(bar.close) for bar in self.bundle.bars]
            self._alignments["closes"] = cached
        return cached

    def _daily_counts(self) -> list[int]:
        """How many daily bars are USABLE at each bar: closed, and not stale.

        Two rules, one walk, because they are two halves of one question and
        reading them apart is how they drift. The first is the alignment plan
        §4 states — a daily bar is visible to a 4h bar only once the day is
        over, measured on the daily bar's own ``close_time`` so the venue's
        statement of where the day ended is what is compared. The second is
        freshness: once the newest closed day is more than a day behind, the
        series has stopped (or has a hole) and nothing in it describes this
        bar's day.

        Zero rather than a shorter count for the stale case, because a stale
        daily series does not make ``sma_1d_50`` a 49-day mean — it makes
        every daily feature at that bar unavailable.
        """
        cached = self._alignments.get("daily")
        if cached is not None:
            return cached
        closes = [bar.close_time for bar in self.bundle.daily]
        counts: list[int] = []
        pointer = 0
        for bar in self.bundle.bars:
            while pointer < len(closes) and closes[pointer] <= bar.close_time:
                pointer += 1
            stale = pointer == 0 or bar.close_time - closes[pointer - 1] >= _DAILY_STALE_MS
            counts.append(0 if stale else pointer)
        self._alignments["daily"] = counts
        return counts

    def _funding_indices(self) -> list[int | None]:
        """The settlement in force at each bar's close, or ``None`` if there is none.

        ``None`` covers both ends of the problem: before the first settlement
        the series has nothing to say, and after the last one a rate older
        than :data:`_FUNDING_STALE_MS` is not this bar's rate — see the module
        docstring on why carrying it forward is worse than admitting the gap.
        """
        cached = self._alignments.get("funding")
        if cached is not None:
            return cached
        times = self._funding_times()
        out: list[int | None] = []
        for bar in self.bundle.bars:
            found = bisect_right(times, bar.close_time) - 1
            if found < 0 or bar.close_time - times[found] > _FUNDING_STALE_MS:
                out.append(None)
            else:
                out.append(found)
        self._alignments["funding"] = out
        return out

    def _funding_times(self) -> list[int]:
        """The settlement stamps, cached: three features bisect this same list."""
        cached = self._alignments.get("funding_times")
        if cached is None:
            cached = [point.time for point in self.bundle.funding]
            self._alignments["funding_times"] = cached
        return cached


# Which method computes each self-computed kind. A module-level table rather
# than a dict rebuilt inside ``_compute``, so the assert below can run: a kind
# added to the vocabulary with nothing to compute it now fails at IMPORT, the
# way ``vocabulary.py``'s own totality check does, instead of as a ``KeyError``
# in the middle of a trial.
_BUILDERS: Final[dict[FeatureKind, Callable[[FeatureFrame, int | None], tuple[FeatureValue, ...]]]] = {
    FeatureKind.CLOSE: FeatureFrame._close,
    FeatureKind.RET: FeatureFrame._ret,
    FeatureKind.SMA: FeatureFrame._sma,
    FeatureKind.ATR_PCT: FeatureFrame._atr_pct,
    FeatureKind.DONCHIAN_HIGH: FeatureFrame._donchian_high,
    FeatureKind.DONCHIAN_LOW: FeatureFrame._donchian_low,
    FeatureKind.REALIZED_VOL: FeatureFrame._realized_vol,
    FeatureKind.CLOSE_1D: FeatureFrame._close_1d,
    FeatureKind.SMA_1D: FeatureFrame._sma_1d,
    FeatureKind.FUNDING_RATE: FeatureFrame._funding_rate,
    FeatureKind.FUNDING_ZSCORE: FeatureFrame._funding_zscore,
    FeatureKind.FUNDING_CUM: FeatureFrame._funding_cum,
}

if set(_BUILDERS) | set(_INDICATOR_KINDS) | {FeatureKind.REGIME} != set(FeatureKind):
    # Raised rather than asserted — see the note in ``vocabulary``: under
    # ``python -O`` an assert here would let a kind with nothing to compute it
    # reach a trial as a ``KeyError``.
    raise RuntimeError("every feature kind needs something that computes it")


def _regime_of(indicators: dict[str, float | None], close: Decimal) -> MarketRegime | None:
    """The live path's regime label, or ``None`` while its inputs are missing.

    The borrowed classifier answers ``RANGING`` when the trio is unavailable,
    and that answer is one the live path structurally never shows: its
    pre-engine guard refuses the cycle instead, so nothing reasons over a
    fabricated-calm market. Mirroring the guard rather than the default is
    what keeps a research regime filter measuring the same thing the trader
    would have seen.
    """
    if any(indicators.get(name) is None for name in REGIME_INDICATORS):
        return None
    return _ANALYTICS.classify_regime(indicators, close)
