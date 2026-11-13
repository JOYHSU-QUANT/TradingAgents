"""The three look-ups a tool-using arm may make before it proposes — pure functions over the TRAINING window (tools plan §2.1, §3.1).

The question the tools plan asks (§0.1) is whether a model that can look
at the training window before it proposes recovers a planted rule more
often, and makes one up in noise less often, than a model that proposes
blind. These are the looks: what a feature's values are
(:func:`feature_stats`), what the price did after a signal
(:func:`conditional_returns`), and what a whole rule would have scored
(:func:`backtest_train`). Nothing here talks to a model or to MCP — the
server that offers these (plan T2) and the hypothesist that calls it (T3)
are wrappers over this module, and the control arm that reads the same
window without a model (T5) calls it directly.

**The line (plan §2.2).** Everything is computed from a :class:`TrainView`,
and a view holds a frame read no further than the training window's last
bar: :meth:`TrainView.load` bounds the read there, and the view refuses a
frame that holds anything later. So the validation and holdout bars are
not filtered out of an answer — they are not in the process. A look-up
whose window would pass the end (the forward return of a signal on the
last training bars) is dropped, never padded. And a view is built from a
store, a coin, a window, costs and a lookback: no manifest, so the world's
kind and whatever is planted in it have no path into an output.

**One reading (plan §2.1.1, §3.2).** A rule is read by the research
package's own ``parse_spec``, a list of conditions by its
``parse_conditions``, a feature name by its ``parse_feature_name``;
whether conditions hold at a bar is its evaluator's ``condition_bars``,
and a rule's training figures are its ``evaluate_segment`` over the view's
window. Nothing is re-implemented, so a look-up cannot accept what the
search loop would refuse, nor say a condition holds where the scored loop
says it does not. A refusal carries the parser's own sentence
(:class:`ToolRefusal`).

**Two layers.** The three functions take parsed values and return frozen
records, exact. :func:`call` is the JSON layer a server stands on: it
takes a tool's name and its decoded arguments, and returns a ``dict`` of
fixed keys with every figure rounded to :data:`SIGNIFICANT_DIGITS` —
what a model is shown is short, and what a test compares is not rounded.

Nothing here writes: a view is read once from the store and held in
memory, and ``tests/test_research_tools.py`` holds the arm's store
byte-identical around the calls.
"""

from __future__ import annotations

import math
import statistics
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final

from .upstream import (
    FUNDING_STAMP_TOLERANCE_MS,
    MAX_HOLD_BARS,
    Condition,
    CostModel,
    FeatureFrame,
    FeatureRef,
    FeatureUnit,
    ResearchStore,
    Segment,
    SegmentMetrics,
    SegmentName,
    Side,
    SpecError,
    StrategySpec,
    condition_bars,
    evaluate_segment,
    from_epoch_ms,
    interval_to_ms,
    load_bundle,
    parse_conditions,
    parse_feature_name,
    parse_spec,
    require_clean_history,
    studied_interval,
)

__all__ = [
    "MAX_HORIZON",
    "MIN_HORIZON",
    "QUANTILES",
    "SIGNIFICANT_DIGITS",
    "TOOL_NAMES",
    "ConditionalReturns",
    "LabelCounts",
    "NumberStats",
    "ToolRefusal",
    "TrainView",
    "backtest_train",
    "call",
    "conditional_returns",
    "feature_stats",
]

# The five cuts ``feature_stats`` reports between the minimum and the
# maximum. Fixed, so two looks at two features are read on one scale.
QUANTILES: Final = (0.05, 0.25, 0.5, 0.75, 0.95)

# How far ahead ``conditional_returns`` may look, in bars. The language's
# own bound on a hold (``exit.max_bars``): a horizon is the hold a rule
# would write, so a look-up cannot ask about one no rule could take.
MIN_HORIZON: Final = 1
MAX_HORIZON: Final = MAX_HOLD_BARS

# What the JSON layer rounds a figure to. Significant digits, not decimal
# places: a funding rate lives five places down and a price five places up.
SIGNIFICANT_DIGITS: Final = 6


class ToolRefusal(ValueError):
    """The arguments are not ones a look-up reads, and the sentence says which and why.

    The caller's mistake, never the store's: an unknown feature, a rule the
    language refuses, a horizon out of range. Where the research package's
    parser is what refused, the sentence is that parser's own, unchanged —
    it is what the model would be told had it proposed the same thing, so
    a look-up teaches the language rather than a second dialect of it. A
    history that cannot answer (``FeatureError``, ``EvaluationError``) is
    not one of these and is left to propagate: it is a fact about the
    store, as it is to the search loop.
    """


# -- the view -----------------------------------------------------------------------------


@dataclass(frozen=True)
class TrainView:
    """What a look-up may read: a frame that ends where training ends, and what a trial is scored under.

    ``frame`` holds the warm-up and the training bars and nothing after
    them; ``train`` is the window; ``costs`` and ``interval`` are the
    experiment's, so :func:`backtest_train` scores a rule as a trial's
    training window is scored. Checked at construction — a view built in
    code meets the line :meth:`load` draws — on all three series: no
    decision bar opening at or after the window's end, no daily bar closing
    after the last decision bar's close, no settlement past the one due at
    that close. Those are the bounds ``load_bundle`` reads under, restated,
    so a frame it bounded at the window's end passes and one bounded any
    later does not.
    """

    frame: FeatureFrame
    train: Segment
    costs: CostModel
    interval: str
    # ``(first, stop)``: the training bars' indices into the frame's bars.
    # Derived at construction, as the frame and the window it is read off
    # are both fixed there.
    window: tuple[int, int] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "interval", studied_interval(self.interval))
        train = self.train
        if train.name is not SegmentName.TRAIN:
            raise ValueError(
                f"a train view is over the training window, got the {train.name.value} one"
            )
        bundle = self.frame.bundle
        bars = bundle.bars
        last = bars[-1]
        if last.open_time >= train.end_ms:
            raise ValueError(
                f"{train}: the frame holds bars up to the one opening at "
                f"{from_epoch_ms(last.open_time).isoformat()}, at or after the window's end; a "
                f"train view holds no bar after the last training bar — what opens later is "
                f"the validation window's"
            )
        first, stop = train.bar_range([bar.open_time for bar in bars])
        step = interval_to_ms(self.interval)
        if (
            first == len(bars)
            or bars[first].open_time != train.start_ms
            or last.open_time + step != train.end_ms
        ):
            raise ValueError(
                f"{train}: the frame's bars do not run from the window's first bar to its "
                f"last, so a look-up would read fewer bars than the window names"
            )
        late_daily = sum(day.close_time > last.close_time for day in bundle.daily)
        late_funding = sum(
            point.time > last.close_time + FUNDING_STAMP_TOLERANCE_MS for point in bundle.funding
        )
        if late_daily or late_funding:
            raise ValueError(
                f"{train}: the frame holds {late_daily} daily bar(s) closing and "
                f"{late_funding} funding settlement(s) posted after the last training bar's "
                f"close; a train view holds nothing later than that close"
            )
        object.__setattr__(self, "window", (first, stop))

    @classmethod
    def load(
        cls,
        store: ResearchStore,
        *,
        coin: str,
        interval: str,
        train: Segment,
        costs: CostModel,
        indicator_lookback: int,
    ) -> TrainView:
        """Read ``coin``'s history up to ``train``'s last bar and no further, and build the view on it.

        The bound is the one ``Split.loadable_until`` hands the store for a
        measurement, moved one window earlier: the last bar read is the one
        that opens just before the window ends. The history is held to the
        grid as a measurement's is (``require_clean_history``), so a
        look-up never reads a feature across a hole a trial would refuse.
        (The read is ``oracle.frame_for``'s with the bound moved, and is
        kept in step with it by hand, as that one is with the research
        package's private one.)
        The store is read here and not kept: the view is in memory, and the
        caller may close the store — the arm's search is writing its ledger
        to the same file.
        """
        bundle = load_bundle(store, coin=coin, interval=interval, until_ms=train.end_ms - 1)
        require_clean_history(bundle, interval)
        return cls(
            frame=FeatureFrame(bundle, indicator_lookback=indicator_lookback),
            train=train,
            costs=costs,
            interval=interval,
        )


# -- feature_stats -------------------------------------------------------------------------


@dataclass(frozen=True)
class NumberStats:
    """One numeric feature over the training bars: how many have a value, and where the values lie.

    ``quantiles`` are at :data:`QUANTILES`, by linear interpolation between
    the two nearest ordered values. With no value on any training bar
    (``valued`` 0) the three figures are ``None``.
    """

    feature: str
    bars: int
    valued: int
    minimum: float | None
    maximum: float | None
    quantiles: tuple[float, ...] | None


@dataclass(frozen=True)
class LabelCounts:
    """The one categorical feature (the regime) over the training bars: bars per label."""

    feature: str
    bars: int
    valued: int
    counts: tuple[tuple[str, int], ...]


def feature_stats(view: TrainView, ref: FeatureRef) -> NumberStats | LabelCounts:
    """Where ``ref``'s values lie on the training bars — what a threshold on it would be set against.

    Read at each training bar as a condition reads it, the reference's
    offset applied. A feature the store cannot answer at any bar is the
    frame's refusal, not an empty answer.
    """
    first, stop = view.window
    values = [view.frame.value_at(ref, index) for index in range(first, stop)]
    name = str(ref)
    if ref.unit is FeatureUnit.REGIME:
        labels = Counter(
            value.value for value in values if value is not None and not isinstance(value, float)
        )
        return LabelCounts(
            feature=name,
            bars=stop - first,
            valued=sum(labels.values()),
            counts=tuple(sorted(labels.items())),
        )
    numbers = sorted(float(value) for value in values if isinstance(value, (int, float)))
    return NumberStats(
        feature=name,
        bars=stop - first,
        valued=len(numbers),
        minimum=numbers[0] if numbers else None,
        maximum=numbers[-1] if numbers else None,
        quantiles=tuple(_quantile(numbers, q) for q in QUANTILES) if numbers else None,
    )


def _quantile(ordered: Sequence[float], q: float) -> float:
    """The ``q`` cut of ``ordered`` (ascending, not empty), interpolated between its two neighbours."""
    position = q * (len(ordered) - 1)
    below = math.floor(position)
    above = min(below + 1, len(ordered) - 1)
    return ordered[below] + (ordered[above] - ordered[below]) * (position - below)


# -- conditional_returns ----------------------------------------------------------------------


@dataclass(frozen=True)
class ConditionalReturns:
    """What the close did over the ``horizon`` bars after the conditions held, on the training bars.

    ``holding`` counts every training bar the conditions hold at.
    ``samples`` counts the ones the three figures are computed from, which
    are fewer on purpose: taken in bar order, each at least ``horizon`` bars
    after the one before it, and only where the whole ``horizon`` lies
    inside the training window. So no two samples share a bar of return —
    forward returns of neighbouring bars overlap in all but one bar, and a
    t-statistic over them counts one move many times — and none reads past
    the window. They are NOT a rule's trades: a rule entering on this signal
    and holding ``horizon`` bars fills at the next open and does not enter
    again on the bar it leaves, so its entries fall a bar later with every
    trade. What such a rule scores is :func:`backtest_train`'s to say.

    A sample's return is ``close[t + horizon] / close[t] - 1``, negated for
    ``side`` short: the price move in the position's favour, before costs.
    ``mean`` and ``std`` (the sample deviation, ``n - 1``) are over the
    samples, and ``t_stat`` is ``mean / (std / sqrt(samples))``. ``mean`` is
    ``None`` with no sample, the other two with fewer than two, and
    ``t_stat`` when the samples do not vary — a deviation that is float
    rounding (:data:`_ROUNDING` of the largest sample, or less) is no
    variation: a t-statistic over it measures the rounding, at any size.
    """

    bars: int
    holding: int
    unevaluable: int
    horizon: int
    side: Side
    samples: int
    mean: float | None
    std: float | None
    t_stat: float | None


def conditional_returns(
    view: TrainView, conditions: Sequence[Condition], *, horizon: int, side: Side
) -> ConditionalReturns:
    """Whether the close moved ``side``'s way over the ``horizon`` bars after ``conditions`` held.

    The conditions are ANDed and read at each training bar by the
    evaluator's own reader, so "holds" here is "holds" in a scored rule.
    This is a signal's reading, before costs and without a position: what a
    rule built on it would score is :func:`backtest_train`'s to say.
    """
    conditions = tuple(conditions)
    if not conditions:
        raise ToolRefusal("conditions: at least one condition — an empty list holds at no bar")
    horizon = _horizon(horizon)
    side = _side(side)
    first, stop = view.window
    read = condition_bars(conditions, view.frame)
    closes = [float(bar.close) for bar in view.frame.bundle.bars]
    sign = 1.0 if side is Side.LONG else -1.0
    returns: list[float] = []
    index = first
    while index < stop - horizon:
        # ``stop - horizon``: past the last bar whose whole horizon closes
        # inside the window. A signal later than that is counted in
        # ``holding`` and is no sample — its return would need a bar the
        # view does not hold. A sample steps over its own horizon, so the
        # next one shares no bar with it.
        if read.holds[index]:
            returns.append(sign * (closes[index + horizon] / closes[index] - 1))
            index += horizon
        else:
            index += 1
    mean = statistics.fmean(returns) if returns else None
    std = statistics.stdev(returns) if len(returns) > 1 else None
    t_stat = None
    if mean is not None and std is not None and std > _ROUNDING * max(map(abs, returns)):
        t_stat = mean / (std / math.sqrt(len(returns)))
    return ConditionalReturns(
        bars=stop - first,
        holding=sum(read.holds[first:stop]),
        unevaluable=sum(read.unevaluable[first:stop]),
        horizon=horizon,
        side=side,
        samples=len(returns),
        mean=mean,
        std=std,
        t_stat=t_stat,
    )


# The share of the largest sample at or under which a deviation between the
# samples is float rounding. Equal moves do not come out bit-equal — five
# samples of one compounding step deviate by about 1e-16 — and dividing a
# mean by that is a t-statistic of 1e14 for a signal with no spread to test.
# (The rounding is absolute, about 1e-16 on a ratio near one; measured
# against the samples it is caught for any move larger than about 1e-7,
# which every priced move is.)
_ROUNDING: Final = 1e-9


def _horizon(value: object) -> int:
    """A horizon as the language reads a hold: a whole number of bars, inside the hold's bounds.

    The parser's check of ``exit.max_bars`` (``dsl._whole``), which is
    private and so restated; the suite holds the two sentences equal.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolRefusal(f"horizon: expected a whole number, got {value!r}")
    if not MIN_HORIZON <= value <= MAX_HORIZON:
        raise ToolRefusal(
            f"horizon: expected a whole number between {MIN_HORIZON} and {MAX_HORIZON}, got {value}"
        )
    return value


def _side(value: object) -> Side:
    """A side, as the member or as the word a rule writes it in — refused in the vocabulary's sentence."""
    try:
        return Side(value)
    except (TypeError, ValueError) as exc:
        raise ToolRefusal(f"side: {exc}") from exc


# -- backtest_train ----------------------------------------------------------------------------


def backtest_train(view: TrainView, spec: StrategySpec) -> SegmentMetrics:
    """``spec`` scored on the training window alone, under the experiment's costs — filed nowhere.

    The research package's own measurement of one window
    (``evaluate_segment``), so the figures are the ones a trial's training
    window carries: the evaluator is deterministic and a bar's features
    never read past it, so the window scores the same on this frame as on
    the longer one a trial is measured on. Nothing is written — no trial,
    no proposal — and so nothing is counted against the search's budget.
    """
    result = evaluate_segment(spec, view.frame, view.train, view.costs, interval=view.interval)
    return SegmentMetrics.from_result(result)


def _backtest_document(metrics: SegmentMetrics) -> dict[str, object]:
    """The figures a model is shown of a training backtest: the ones the prompt shows of a validation one."""
    return {
        "bars": metrics.bars,
        "trades": metrics.trades,
        "exposure": _shown(metrics.exposure),
        "turnover": _shown(metrics.turnover),
        "net_sharpe": _shown(metrics.net.sharpe),
        "net_return": _shown(metrics.net.total_return),
        "ruined": metrics.ruined,
    }


# -- the JSON layer ----------------------------------------------------------------------------


def _shown(value: float | None) -> float | None:
    return None if value is None else float(f"{value:.{SIGNIFICANT_DIGITS}g}")


def _stats_document(stats: NumberStats | LabelCounts) -> dict[str, object]:
    head: dict[str, object] = {
        "feature": stats.feature,
        "bars": stats.bars,
        "bars_with_value": stats.valued,
    }
    if isinstance(stats, LabelCounts):
        return {**head, "counts": dict(stats.counts)}
    cuts = stats.quantiles if stats.quantiles is not None else (None,) * len(QUANTILES)
    return {
        **head,
        "min": _shown(stats.minimum),
        **{f"p{round(q * 100):02d}": _shown(cut) for q, cut in zip(QUANTILES, cuts, strict=True)},
        "max": _shown(stats.maximum),
    }


def _returns_document(read: ConditionalReturns) -> dict[str, object]:
    return {
        "bars": read.bars,
        "bars_holding": read.holding,
        "bars_unevaluable": read.unevaluable,
        "horizon": read.horizon,
        "side": read.side.value,
        "samples": read.samples,
        "mean_return": _shown(read.mean),
        "std_return": _shown(read.std),
        "t_stat": _shown(read.t_stat),
    }


def _arguments(tool: str, arguments: object, keys: tuple[str, ...]) -> Mapping[str, object]:
    """``arguments`` as an object carrying exactly ``keys`` — nothing defaulted, nothing ignored.

    The parser's own rule for a rule's keys, for its reason: an argument
    that is silently dropped is one its author believes was read. And the
    parser's two sentences rather than :mod:`.records`' exact-key reader,
    which says what the key set should have been: a model is better told
    which key to drop and which to add.
    """
    if not isinstance(arguments, Mapping):
        raise ToolRefusal(f"{tool}: expected an object, got {type(arguments).__name__}")
    unknown = sorted(str(key) for key in arguments if key not in keys)
    if unknown:
        raise ToolRefusal(f"{tool}: unknown key(s) {unknown}; this tool accepts {list(keys)}")
    missing = [key for key in keys if key not in arguments]
    if missing:
        raise ToolRefusal(f"{tool}: missing required key(s) {missing}")
    return arguments


def _feature_stats(view: TrainView, body: Mapping[str, object]) -> dict[str, object]:
    kind, period = parse_feature_name(body["feature"], path="feature")
    return _stats_document(feature_stats(view, FeatureRef(kind, period)))


def _conditional_returns(view: TrainView, body: Mapping[str, object]) -> dict[str, object]:
    conditions = parse_conditions(body["conditions"], path="conditions")
    return _returns_document(
        conditional_returns(
            view, conditions, horizon=_horizon(body["horizon"]), side=_side(body["side"])
        )
    )


def _backtest_train(view: TrainView, body: Mapping[str, object]) -> dict[str, object]:
    return _backtest_document(backtest_train(view, parse_spec(body["rule"])))


# Each look-up: the keys its arguments carry, and what reads them.
_TOOLS: Final[
    dict[
        str, tuple[tuple[str, ...], Callable[[TrainView, Mapping[str, object]], dict[str, object]]]
    ]
] = {
    "feature_stats": (("feature",), _feature_stats),
    "conditional_returns": (("conditions", "horizon", "side"), _conditional_returns),
    "backtest_train": (("rule",), _backtest_train),
}

# The look-ups by name, in the order a caller would reach for them: what a
# feature's values are, whether a signal on it has anything after it, what a
# whole rule scores.
TOOL_NAMES: Final = tuple(_TOOLS)


def call(view: TrainView, tool: str, arguments: object) -> dict[str, object]:
    """One look-up by name with its decoded JSON arguments; the answer as a ``dict`` of fixed keys.

    ``feature_stats`` takes ``{"feature": <a feature name>}``;
    ``conditional_returns`` takes ``{"conditions": [...], "horizon": <bars>,
    "side": "long" | "short"}``, the conditions written as a rule writes
    them under ``entry.long``; ``backtest_train`` takes ``{"rule": {...}}``,
    the rule being the object a proposal is — it is handed to ``parse_spec``
    untouched, so a rule that was tried here can be proposed as it stands.

    Every argument is required and no other is accepted. A mistake in them
    is a :class:`ToolRefusal` whose sentence is the parser's own wherever
    the parser is what refused.

    A history that cannot answer is NOT turned into a refusal here: a
    ``FeatureError`` or an ``EvaluationError`` passes through as itself. It
    is not the caller's mistake, and its sentence names calendar instants,
    which nothing a model is shown carries — so whoever stands between
    this function and a model (the server, plan T2) has to catch it and
    must not hand the sentence on.
    """
    known = _TOOLS.get(tool) if isinstance(tool, str) else None
    if known is None:
        raise ToolRefusal(f"{tool!r} is not a tool; the tools are {list(TOOL_NAMES)}")
    keys, reader = known
    try:
        return reader(view, _arguments(tool, arguments, keys))
    except SpecError as exc:
        raise ToolRefusal(str(exc)) from exc
