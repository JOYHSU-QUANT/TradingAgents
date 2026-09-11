"""The closed feature vocabulary — every name a strategy spec may refer to.

A hypothesis reaches this package as data, never as code (plan §3.5), so the
set of things it can say has to be finite and enumerable. This module is that
set: a feature is a ``kind`` (``ret``, ``ema``, ``funding_zscore``, ...) plus
the period it is measured over, and both halves are declared here rather than
parsed out of whatever an LLM happened to write. ``ema_9`` is not a feature
with an unusual period — it is refused by name, because nothing in this repo
computes it.

Three properties of the vocabulary are load-bearing, and each one is a
decision rather than a detail:

**It is finite.** Every kind declares the exact periods it accepts, so
:func:`feature_names` can list the whole language. An open vocabulary
(``ret_n`` for any ``n``) would make the search space unbounded in a
direction that carries no information — ``ret_37`` against ``ret_38`` is not
a hypothesis — while plan §3.10's trial penalty charges the same for it as
for a real idea.

**It is closed under what can actually be computed.** The periods declared
for ``ema``, ``rsi`` and ``atr`` are the ones the perp package's indicator
engine supports, and ``tests/test_pins.py`` checks that claim against
``supported_indicators()`` upstream. A vocabulary that admitted names the
engine returns ``None`` for would not be a stricter parser; it would be a
strategy that never fires, scored as if it had been tried.

**It describes itself.** :func:`describe_vocabulary` renders the listing the
``vocab`` command prints and the plan's PR B1 prompt hands the LLM, from the
same table the parser resolves against. A hand-written listing beside a table
is two vocabularies that agree until one of them is edited.

Units exist for one reason: a comparison between two features is only
meaningful if both sides measure the same sort of thing. ``close > rsi_14``
parses as a well-formed condition and means nothing — the close of BTC is
five digits and an oscillator is bounded at a hundred, so the rule is
"always", dressed as an idea. :class:`FeatureUnit` is what lets the parser
say so (see :mod:`~contrib.autoresearch.dsl`).

Two of the units exist because a first cut of that table was too coarse and
re-admitted its own motivating example. ``atr_14`` is quote currency like a
close is, but it is a DISTANCE rather than a LEVEL, so ``close > atr_14``
passed the check and is true at essentially every bar — the same defect as
``close > rsi_14``, one unit later. ``PRICE_SPAN`` separates them. Likewise
``funding_cum_24`` sums about ninety-six settlements and ``funding_rate`` is
one of them: measured on BTC-shaped funding, ``funding_cum_24 >
funding_rate`` fired at 276 bars out of 276. ``RATE_SUM`` separates those.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from typing import Final

from .upstream import VocabEnum

__all__ = [
    "MAX_OFFSET_BARS",
    "FeatureKind",
    "FeatureRef",
    "FeatureUnit",
    "SeriesSource",
    "SpecError",
    "describe_vocabulary",
    "feature_names",
    "parse_feature_name",
    "periods_for",
    "spec_of",
]


class SpecError(ValueError):
    """A hypothesis said something this package's language does not contain.

    One type for the whole parser — feature names, operators, families,
    sizing, structure — because every caller does the same thing with it: an
    LLM-written spec is discarded and the sentence becomes the failure note
    the next round is shown (plan §3.11), and a human-written one is printed
    by the CLI. What differs between refusals is the SENTENCE, which names the
    path inside the spec and the legal alternatives, not the class.

    A ``ValueError`` because that is what it is — a value outside its domain —
    and because the CLI's existing exit-1 lane already catches that family.
    """


# How far back a condition may reach with an explicit offset, in bars.
#
# Offsets exist so a rule can compare a bar with an earlier one (``close >
# close[1]``); they are NOT a second way to spell a lookback window, which is
# what the periods below are for. The bound keeps a spec's warm-up derivable
# from the spec (plan §3.6: no look-ahead is a construction guarantee, and a
# reach of unbounded depth would make the first evaluable bar unbounded too),
# and 24 is four days of 4h bars — past the point where "the bar before this
# one" is still the idea being expressed.
MAX_OFFSET_BARS: Final = 24


class FeatureUnit(VocabEnum, noun="feature unit"):
    """What a feature's number MEANS, so two of them can be compared.

    The parser refuses a comparison between two different units. That is not
    style: the point of the check is that ``close > sma_50`` and ``close >
    rsi_14`` look identical to a generator that has only seen the grammar.
    """

    PRICE = "price"  # a LEVEL in quote currency: a close, a mean, a channel edge
    PRICE_SPAN = "price_span"  # a DISTANCE in quote currency: how far, not where
    RETURN = "return"  # a fraction of price: 0.01 is one percent
    RATE = "rate"  # ONE funding settlement, as a fraction
    RATE_SUM = "rate_sum"  # settlements ADDED UP over a window
    SCORE = "score"  # standardised and dimensionless (a z-score)
    OSCILLATOR = "oscillator"  # bounded 0..100
    REGIME = "regime"  # categorical, not a number at all


class SeriesSource(VocabEnum, noun="feature source series"):
    """Which stored series a feature is computed from.

    Carried so :mod:`~contrib.autoresearch.features` can refuse a feature
    whose series is absent BY NAME instead of returning ``None`` for every
    bar. The two failures are opposites: ``None`` from warm-up is the feature
    working, while ``None`` from a missing series is a store that was never
    filled, and a strategy that silently never fires would be scored as one
    that was tried and found wanting.
    """

    BARS = "bars"  # the decision series itself
    DAILY = "daily"  # the 1d backdrop
    FUNDING = "funding"  # the hourly settlement series


class FeatureKind(VocabEnum, noun="feature"):
    """The stems of the vocabulary, in the order the listing prints them.

    Declaration order is the order of :func:`feature_names` and of the
    rendered listing, so the price and momentum family reads first and
    funding last — the order plan §4 sets them out in. It is not
    alphabetical, for the same reason ``CandleInterval``'s members are not
    (PR #165).
    """

    CLOSE = "close"
    RET = "ret"
    EMA = "ema"
    SMA = "sma"
    RSI = "rsi"
    ATR = "atr"
    ATR_PCT = "atr_pct"
    DONCHIAN_HIGH = "donchian_high"
    DONCHIAN_LOW = "donchian_low"
    REALIZED_VOL = "realized_vol"
    CLOSE_1D = "close_1d"
    SMA_1D = "sma_1d"
    FUNDING_RATE = "funding_rate"
    FUNDING_ZSCORE = "funding_zscore"
    FUNDING_CUM = "funding_cum"
    REGIME = "regime"


@dataclass(frozen=True)
class FeatureSpec:
    """One kind's declaration: what it accepts, what it means, where it comes from."""

    kind: FeatureKind
    unit: FeatureUnit
    source: SeriesSource
    periods: tuple[int, ...]  # empty means the kind takes no period at all
    period_noun: str  # what a period COUNTS — "bars" and "days" are different windows
    summary: str  # one line, shown to an operator and to the hypothesis LLM

    @property
    def parameterised(self) -> bool:
        return bool(self.periods)


# The vocabulary itself. Every period listed here is one this package can
# actually compute today; see the module docstring on why that is a
# correctness rule rather than a conservative default.
_SPECS: Final[tuple[FeatureSpec, ...]] = (
    FeatureSpec(
        kind=FeatureKind.CLOSE,
        unit=FeatureUnit.PRICE,
        source=SeriesSource.BARS,
        periods=(),
        period_noun="bars",
        summary="this bar's close.",
    ),
    FeatureSpec(
        kind=FeatureKind.RET,
        unit=FeatureUnit.RETURN,
        source=SeriesSource.BARS,
        # Up to thirty days. The short end (a bar, a day) is where a reversal
        # lives; the long end is where crypto time-series momentum has been
        # measured, and a ladder stopping at four days could not express it at
        # all. 180 bars of 4h fits inside the ~833 days the venue serves.
        periods=(1, 3, 6, 12, 24, 42, 180),
        period_noun="bars",
        summary="close-to-close return over the last N bars, as a fraction.",
    ),
    FeatureSpec(
        kind=FeatureKind.EMA,
        unit=FeatureUnit.PRICE,
        source=SeriesSource.BARS,
        periods=(20, 50),
        period_noun="bars",
        summary="exponential moving average of the close, from the live path's own engine.",
    ),
    FeatureSpec(
        kind=FeatureKind.SMA,
        unit=FeatureUnit.PRICE,
        source=SeriesSource.BARS,
        periods=(10, 20, 50, 100, 200),
        period_noun="bars",
        summary="simple mean of the last N closes, this bar included.",
    ),
    FeatureSpec(
        kind=FeatureKind.RSI,
        unit=FeatureUnit.OSCILLATOR,
        source=SeriesSource.BARS,
        periods=(14,),
        period_noun="bars",
        summary="relative strength index, 0..100, from the live path's own engine.",
    ),
    FeatureSpec(
        kind=FeatureKind.ATR,
        unit=FeatureUnit.PRICE_SPAN,
        source=SeriesSource.BARS,
        periods=(14,),
        period_noun="bars",
        summary="average true range in quote currency, from the live path's own engine.",
    ),
    FeatureSpec(
        kind=FeatureKind.ATR_PCT,
        unit=FeatureUnit.RETURN,
        source=SeriesSource.BARS,
        periods=(14,),
        period_noun="bars",
        summary="atr_N divided by this bar's close, as a fraction (0.04 is four percent).",
    ),
    FeatureSpec(
        kind=FeatureKind.DONCHIAN_HIGH,
        unit=FeatureUnit.PRICE,
        source=SeriesSource.BARS,
        # 55 bars of 4h is nine days; the channel lengths this idea is named
        # for are 20 and 55 DAYS, so 120 bars (twenty days) is the one that
        # reaches the horizon the family is about.
        periods=(10, 20, 55, 120),
        period_noun="bars",
        summary="highest high of the N bars BEFORE this one; this bar is excluded.",
    ),
    FeatureSpec(
        kind=FeatureKind.DONCHIAN_LOW,
        unit=FeatureUnit.PRICE,
        source=SeriesSource.BARS,
        periods=(10, 20, 55, 120),
        period_noun="bars",
        summary="lowest low of the N bars BEFORE this one; this bar is excluded.",
    ),
    FeatureSpec(
        kind=FeatureKind.REALIZED_VOL,
        unit=FeatureUnit.RETURN,
        source=SeriesSource.BARS,
        periods=(10, 20, 50),
        period_noun="bars",
        summary="sample standard deviation of the last N bar returns; PER BAR, not annualised.",
    ),
    FeatureSpec(
        kind=FeatureKind.CLOSE_1D,
        unit=FeatureUnit.PRICE,
        source=SeriesSource.DAILY,
        periods=(),
        period_noun="days",
        summary="close of the most recent DAILY bar that had closed by this bar's close.",
    ),
    FeatureSpec(
        kind=FeatureKind.SMA_1D,
        unit=FeatureUnit.PRICE,
        source=SeriesSource.DAILY,
        periods=(20, 50, 200),
        period_noun="days",
        summary="simple mean of the last N daily closes that had closed by this bar's close.",
    ),
    FeatureSpec(
        kind=FeatureKind.FUNDING_RATE,
        unit=FeatureUnit.RATE,
        source=SeriesSource.FUNDING,
        periods=(),
        period_noun="hours",
        summary="the most recent hourly settlement rate at this bar's close.",
    ),
    FeatureSpec(
        kind=FeatureKind.FUNDING_ZSCORE,
        unit=FeatureUnit.SCORE,
        source=SeriesSource.FUNDING,
        periods=(7, 14, 30),
        period_noun="days",
        summary="that rate against the trailing N DAYS of settlements; the live path's z-score.",
    ),
    FeatureSpec(
        kind=FeatureKind.FUNDING_CUM,
        unit=FeatureUnit.RATE_SUM,
        source=SeriesSource.FUNDING,
        periods=(1, 6, 24, 42),
        period_noun="bars",
        summary="settlement rates summed over the last N BARS (a 4h bar holds four of them).",
    ),
    FeatureSpec(
        kind=FeatureKind.REGIME,
        unit=FeatureUnit.REGIME,
        source=SeriesSource.BARS,
        periods=(),
        period_noun="bars",
        summary="trending / ranging / volatile, the live path's own label. Compare with == or !=.",
    ),
)

_BY_KIND: Final[dict[FeatureKind, FeatureSpec]] = {spec.kind: spec for spec in _SPECS}

# Every kind declared exactly once, and every kind declared: a member added to
# the enum without a row above would be a name the parser accepts and nothing
# computes, and the reverse is a row nothing can reach.
assert tuple(_BY_KIND) == tuple(FeatureKind), "the vocabulary and its enum disagree"


def spec_of(kind: FeatureKind) -> FeatureSpec:
    """The declaration for ``kind``."""
    return _BY_KIND[kind]


def periods_for(kind: FeatureKind) -> tuple[int, ...]:
    """The periods ``kind`` accepts; empty if it takes none."""
    return _BY_KIND[kind].periods


def _spell(kind: FeatureKind, period: int | None) -> str:
    return kind.value if period is None else f"{kind.value}_{period}"


def _no_such_period(kind: FeatureKind, shown: object) -> str:
    """The one sentence for "that stem, that period, no".

    Written once because it is raised from two places — the name parser and
    :class:`FeatureRef`'s own guard — and those two audiences are the model
    writing a spec and the code assembling one. Two copies drifted the moment
    one of them was improved.
    """
    spec = _BY_KIND[kind]
    return (
        f"{kind.value!r} has no period {shown!r}; this package computes "
        f"{kind.value} over {list(spec.periods)} {spec.period_noun}"
    )


def _takes_no_period(kind: FeatureKind, shown: object) -> str:
    return f"{kind.value!r} takes no period — write it as {kind.value!r}, not {shown!r}"


# Every legal spelling, resolved to its ``(kind, period)`` — built from the
# table so the two can never disagree, and used as the parser's fast path.
_BY_NAME: Final[dict[str, tuple[FeatureKind, int | None]]] = {
    _spell(spec.kind, period): (spec.kind, period)
    for spec in _SPECS
    for period in (spec.periods or (None,))
}

# Stems longest-first, which is what makes ``close_1d`` a kind of its own
# rather than ``close`` wearing a period, and ``sma_1d_50`` a daily mean
# rather than ``sma`` with a period of ``1d``.
_STEMS: Final[tuple[FeatureKind, ...]] = tuple(
    sorted(FeatureKind, key=lambda kind: len(kind.value), reverse=True)
)


def feature_names() -> tuple[str, ...]:
    """Every legal feature spelling, in the vocabulary's declaration order."""
    return tuple(_BY_NAME)


def parse_feature_name(text: object, *, path: str = "feature") -> tuple[FeatureKind, int | None]:
    """Resolve one feature spelling, or raise :class:`SpecError` saying what is wrong.

    Three refusals, and they are different sentences on purpose, because they
    call for three different edits. A known stem with an impossible period is
    told the periods that stem has (``ema`` is real, ``9`` is not); a known
    stem that takes no period is told to drop it; and a name with no stem at
    all is told the vocabulary is closed, with the nearest legal name if there
    is one. A single "unknown feature" sentence for all three would answer the
    rarest of the questions and leave the common ones to guesswork.
    """
    if not isinstance(text, str):
        raise SpecError(f"{path}: a feature is named by a string, got {text!r}")
    found = _BY_NAME.get(text)
    if found is not None:
        return found
    for kind in _STEMS:
        if not text.startswith(f"{kind.value}_"):
            continue
        suffix = text[len(kind.value) + 1 :]
        if not _BY_KIND[kind].parameterised:
            raise SpecError(f"{path}: {_takes_no_period(kind, text)}")
        raise SpecError(f"{path}: {_no_such_period(kind, suffix)}")
    nearest = difflib.get_close_matches(text, _BY_NAME, n=1)
    hint = f" Did you mean {nearest[0]!r}?" if nearest else ""
    raise SpecError(
        f"{path}: {text!r} is not a feature.{hint} The vocabulary is closed — "
        f"{len(_BY_NAME)} names, listed by `python -m contrib.autoresearch vocab`."
    )


@dataclass(frozen=True)
class FeatureRef:
    """One reference to a feature: a kind, its period, and how many bars back.

    Validated at construction rather than at the parser, so a reference built
    in code — by a test, by a later phase assembling a spec itself — cannot
    name a period the vocabulary does not have. ``offset`` counts bars BACK
    from the bar being decided: ``0`` is this bar, ``1`` the one before it.
    There is no negative offset, and refusing it is the leakage guard plan
    §3.6(a) asks for, stated where every reference passes.
    """

    kind: FeatureKind
    period: int | None = None
    offset: int = 0

    def __post_init__(self) -> None:
        spec = _BY_KIND[self.kind]
        if self.period is not None and (
            not isinstance(self.period, int) or isinstance(self.period, bool)
        ):
            # Checked the way ``offset`` is, and for a reason ``offset`` does
            # not have: ``True == 1`` and ``20.0 == 20``, so both would pass
            # the membership test below and then render as ``ret_True`` and
            # ``sma_20.0`` — names ``parse_feature_name`` refuses, so a spec
            # written back out by the ledger could not be read in again.
            raise SpecError(
                f"{self.kind.value!r}: a period is a whole number, got {self.period!r}"
            )
        if spec.parameterised and self.period not in spec.periods:
            raise SpecError(_no_such_period(self.kind, self.period))
        if not spec.parameterised and self.period is not None:
            raise SpecError(_takes_no_period(self.kind, _spell(self.kind, self.period)))
        if not isinstance(self.offset, int) or isinstance(self.offset, bool):
            raise SpecError(
                f"{self.name}: offset must be a whole number of bars, got {self.offset!r}"
            )
        if self.offset < 0:
            raise SpecError(
                f"{self.name}: offset {self.offset} refers to a bar that has not closed "
                f"when the decision is made. Offsets count backwards: 0 is this bar, 1 the "
                f"one before it."
            )
        if self.offset > MAX_OFFSET_BARS:
            raise SpecError(
                f"{self.name}: offset {self.offset} reaches further back than "
                f"{MAX_OFFSET_BARS} bars; use a feature with a longer period instead"
            )

    @property
    def name(self) -> str:
        """The feature's spelling, without the offset."""
        return _spell(self.kind, self.period)

    @property
    def unit(self) -> FeatureUnit:
        return _BY_KIND[self.kind].unit

    @property
    def source(self) -> SeriesSource:
        return _BY_KIND[self.kind].source

    def __str__(self) -> str:
        return self.name if self.offset == 0 else f"{self.name}[{self.offset}]"


def describe_vocabulary() -> list[str]:
    """The vocabulary as lines to print — one row per kind, generated from the table.

    Generated, never transcribed: this listing is what the ``vocab`` command
    shows an operator and what plan §3.11's prompt hands the hypothesis LLM,
    so a hand-kept copy would be a second vocabulary that drifts from the one
    the parser enforces — and it would drift in the direction that costs most,
    since the model would be asked for names the parser then refuses.
    """
    rows = [
        (
            f"{spec.kind.value}_N" if spec.parameterised else spec.kind.value,
            spec.unit.value,
            spec.source.value,
            f"{spec.summary} N: {', '.join(str(p) for p in spec.periods)} {spec.period_noun}."
            if spec.parameterised
            else spec.summary,
        )
        for spec in _SPECS
    ]
    name_width = max(len(row[0]) for row in rows)
    unit_width = max(len(row[1]) for row in rows)
    source_width = max(len(row[2]) for row in rows)
    lines = [
        f"{'feature'.ljust(name_width)}  {'unit'.ljust(unit_width)}  "
        f"{'from'.ljust(source_width)}  meaning"
    ]
    lines += [
        f"{name.ljust(name_width)}  {unit.ljust(unit_width)}  {source.ljust(source_width)}  {text}"
        for name, unit, source, text in rows
    ]
    lines.append(
        f'Any feature may be written as an object with an offset in bars, 0..{MAX_OFFSET_BARS}: '
        f'{{"feature": "close", "offset": 1}} is the previous bar\'s close. '
        f"Comparisons need both sides in the same unit."
    )
    return lines
