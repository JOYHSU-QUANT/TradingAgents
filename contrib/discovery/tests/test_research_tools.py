"""The training-window look-ups: the line they read behind, and the arithmetic of each (tools plan §4, T1 a–h).

Two groups. The LINE: a view is read no further than the last training
bar, takes its window from the experiment's split and refuses a frame
that holds anything later; rewriting everything after that bar changes no
answer; no answer carries a word that would name the world's kind; the
arm's store is byte-identical around the calls. And the READING: a
training backtest is a trial's own training figures, a rule and a list of
conditions are read by the research package's parser and refused in its
words, a feature with no value at the window's start is not read, and two of
the answers are computed by hand on a history short enough to redo on
paper — the non-overlapping samples of ``conditional_returns`` above all,
since a t-statistic over overlapping windows is the number these tools
exist not to hand a model.

The view is built over the suite's hand-written history (``conftest``),
through an experiment opened on it: its split is what makes a figure here
comparable with a trial's.
"""

from __future__ import annotations

import ast
import copy
import json
import math
import statistics
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from contrib.autoresearch.constants import FUNDING_STAMP_TOLERANCE_MS, bar_span_ms
from contrib.autoresearch.costs import CostModel
from contrib.autoresearch.dsl import Side, parse_conditions, parse_spec
from contrib.autoresearch.evaluator import EvaluationError
from contrib.autoresearch.features import FeatureFrame, SeriesBundle
from contrib.autoresearch.ledger import Ledger
from contrib.autoresearch.metrics import SegmentMetrics
from contrib.autoresearch.split import Segment, SegmentName, Split
from contrib.autoresearch.store import ResearchStore
from contrib.autoresearch.upstream import Candle, FundingPoint, MarketRegime
from contrib.autoresearch.vocabulary import FeatureKind, FeatureRef, parse_feature_name
from contrib.discovery import research_tools
from contrib.discovery.landing import artifact_digest
from contrib.discovery.manifest import WorldKind
from contrib.discovery.oracle import evaluate_unfiled, frame_for
from contrib.discovery.research_tools import (
    MAX_HORIZON,
    QUANTILES,
    SIGNIFICANT_DIGITS,
    TOOL_NAMES,
    ConditionalReturns,
    LabelCounts,
    NumberStats,
    ToolRefusal,
    TrainView,
    backtest_train,
    call,
    conditional_returns,
    feature_stats,
)

from .conftest import (
    BUY,
    BUY_12,
    COIN,
    LOOKBACK,
    MS_PER_HOUR,
    PLANTED,
    SHORT,
    START_MS,
    STEP_MS,
    arm_store,
    open_arm,
)

_ANY_BAR = [{"left": "close", "op": ">", "right": 0}]

# Each look-up on more than one kind of input, and between them a feature
# of each stored series — the bars, the daily bars, the settlements — so
# "every answer" below reads everything a view holds.
CALLS = (
    ("feature_stats", {"feature": "close"}),
    ("feature_stats", {"feature": "funding_zscore_14"}),
    ("feature_stats", {"feature": "funding_cum_6"}),
    ("feature_stats", {"feature": "regime"}),
    ("feature_stats", {"feature": "close_1d"}),
    (
        "conditional_returns",
        {
            "conditions": [{"left": "funding_zscore_14", "op": "<", "right": 0}],
            "horizon": 6,
            "side": "long",
        },
    ),
    (
        "conditional_returns",
        {
            "conditions": [
                {"left": "close", "op": ">", "right": {"feature": "close", "offset": 1}}
            ],
            "horizon": 2,
            "side": "short",
        },
    ),
    ("backtest_train", {"rule": PLANTED}),
    ("backtest_train", {"rule": BUY_12}),
    # Held on every bar but the first, so whatever the last training bar
    # pays is in an answer whichever bar training ends on.
    ("backtest_train", {"rule": BUY}),
)


# The calls that need no indicator walk: still a feature of each stored
# series, which is all a control has to show is read.
QUICK = tuple(
    (tool, arguments)
    for tool, arguments in CALLS
    if tool != "backtest_train" and arguments.get("feature") != "regime"
)


def _load(path: Path, experiment, split: Split | None = None) -> TrainView:
    """The view as a server would build it: the store opened, read to the line, and closed."""
    with ResearchStore(path) as store:
        return TrainView.load(
            store,
            coin=experiment.coin,
            split=experiment.split if split is None else split,
            costs=experiment.costs,
            indicator_lookback=experiment.indicator_lookback,
        )


def _answers(view: TrainView, calls=CALLS) -> str:
    return json.dumps([call(view, tool, arguments) for tool, arguments in calls], sort_keys=True)


@pytest.fixture(scope="module")
def opened_arm(history, tmp_path_factory):
    """One arm's store with its experiment opened, nothing filed: the path and the experiment."""
    path = tmp_path_factory.mktemp("tools") / "arm.sqlite"
    with arm_store(history, path) as store:
        experiment = open_arm(Ledger(store))
    return path, experiment


@pytest.fixture(scope="module")
def view(opened_arm) -> TrainView:
    return _load(*opened_arm)


@pytest.fixture(scope="module")
def measured(opened_arm) -> FeatureFrame:
    """The frame a trial of that experiment is measured on: read to the validation window's end."""
    path, experiment = opened_arm
    with ResearchStore(path) as store:
        return frame_for(Ledger(store), experiment, holdout=False)


def _hand_split(bars: int, train_from: int = 0) -> Split:
    """A split whose training window is bars ``train_from`` .. ``bars`` of a history from ``START_MS``.

    The two windows after it are a bar each and hold nothing: a view is
    read off the training window alone.
    """
    edges = [START_MS + index * STEP_MS for index in (train_from, bars, bars + 1, bars + 2)]
    return Split(
        "4h",
        Segment(SegmentName.TRAIN, edges[0], edges[1]),
        Segment(SegmentName.VALIDATION, edges[1], edges[2]),
        Segment(SegmentName.HOLDOUT, edges[2], edges[3]),
    )


def _hand_view(
    closes, *, train_from: int = 0, funding_hours: int = 0, missed_hours=()
) -> TrainView:
    """A view over closes written out by hand: 4h bars from ``START_MS``, training from bar ``train_from``.

    Each bar opens at the previous close, as a generated world's does. No
    daily bars; ``funding_hours`` hourly settlements from the first hour,
    stamped as the venue stamps them (a few ms after the hour), for the
    tests that read funding or score a rule — less the ones due at
    ``missed_hours`` (hours after ``START_MS``), which the venue skipped.
    """
    opens = [closes[0], *closes[:-1]]
    bars = [
        Candle(
            open_time=START_MS + index * STEP_MS,
            close_time=START_MS + index * STEP_MS + bar_span_ms(STEP_MS),
            open=Decimal(str(opened)),
            high=Decimal(str(max(opened, close))),
            low=Decimal(str(min(opened, close))),
            close=Decimal(str(close)),
            volume=Decimal("1"),
        )
        for index, (opened, close) in enumerate(zip(opens, closes, strict=True))
    ]
    funding = [
        FundingPoint(time=START_MS + (hour + 1) * MS_PER_HOUR + 57, rate=Decimal("0.0001"))
        for hour in range(funding_hours)
        if hour + 1 not in missed_hours
    ]
    return TrainView(
        frame=FeatureFrame(SeriesBundle(bars, funding=funding), indicator_lookback=LOOKBACK),
        split=_hand_split(len(closes), train_from),
        costs=CostModel(),
    )


def _above(level: float) -> tuple:
    return parse_conditions([{"left": "close", "op": ">", "right": level}])


# -- the line: what a view holds -------------------------------------------------------------


def test_a_view_is_read_no_further_than_the_last_training_bar(opened_arm, monkeypatch):
    path, experiment = opened_arm
    bounds: list[int | None] = []
    real_load = research_tools.load_bundle

    def spying_load(store, **kwargs):
        bounds.append(kwargs.get("until_ms"))
        return real_load(store, **kwargs)

    monkeypatch.setattr(research_tools, "load_bundle", spying_load)
    view = _load(path, experiment)
    train = experiment.split.train
    assert bounds == [train.end_ms - 1]
    # The window is the split's own, not one the caller named.
    assert view.train is train and view.interval == "4h"
    bundle = view.frame.bundle
    last = bundle.bars[-1]
    # The last bar held is the last training bar; beside it the daily bars
    # stop at its close, and the settlements at the one due at it.
    assert last.open_time + STEP_MS == train.end_ms
    assert max(day.close_time for day in bundle.daily) <= last.close_time
    assert (
        max(point.time for point in bundle.funding) <= last.close_time + FUNDING_STAMP_TOLERANCE_MS
    )
    first, stop = view.window
    assert stop == len(bundle.bars) and bundle.bars[first].open_time == train.start_ms
    # And the store does hold more: the bound is what kept it out.
    with ResearchStore(path) as store:
        assert store.count_candles(COIN, "4h") > len(bundle.bars)
        assert any(store.iter_funding(COIN, since_ms=train.end_ms + STEP_MS))


def test_a_view_refuses_a_frame_that_holds_anything_after_training(opened_arm, view, measured):
    _path, experiment = opened_arm
    shape = {"split": experiment.split, "costs": experiment.costs}
    # The frame a trial is measured on reaches the validation window's end.
    with pytest.raises(ValueError, match="no bar after the last training bar"):
        TrainView(frame=measured, **shape)

    # The two series beside the bars, each on its own: the bars end where
    # they should, and a daily bar or a settlement from later is still held.
    bundle = view.frame.bundle
    for late in (
        SeriesBundle(bundle.bars, daily=measured.bundle.daily, funding=bundle.funding),
        SeriesBundle(bundle.bars, daily=bundle.daily, funding=measured.bundle.funding),
    ):
        with pytest.raises(ValueError, match="holds nothing later than that close"):
            TrainView(frame=FeatureFrame(late, indicator_lookback=LOOKBACK), **shape)

    # A frame that stops a bar short is a window measured over fewer bars
    # than it names.
    short = SeriesBundle(bundle.bars[:-1], daily=bundle.daily, funding=bundle.funding)
    with pytest.raises(ValueError, match="fewer bars than the window names"):
        TrainView(frame=FeatureFrame(short, indicator_lookback=LOOKBACK), **shape)


def test_a_view_takes_its_window_from_the_split_and_not_from_the_caller(opened_arm, view, measured):
    # A window is not handed over: a segment, however it is named, is not a
    # split, so a caller cannot hand over a bare stretch and call it training.
    # (Which split it is handed is the caller's to get right: the experiment's.)
    _path, experiment = opened_arm
    split = experiment.split
    wider = Segment(SegmentName.TRAIN, split.train.start_ms, split.validation.end_ms)
    with pytest.raises(ValueError, match="the experiment's split, got FeatureFrame and Segment"):
        TrainView(frame=measured, split=wider, costs=experiment.costs)
    with pytest.raises(ValueError, match="takes a feature frame"):
        TrainView(frame=view.frame.bundle, split=split, costs=experiment.costs)
    # And swapping the split under a built view runs the checks again: the
    # frame that ended with the old training window does not cover a wider one.
    stretched = Split(
        "4h",
        wider,
        replace(split.holdout, name=SegmentName.VALIDATION),
        Segment(SegmentName.HOLDOUT, split.holdout.end_ms, split.holdout.end_ms + STEP_MS),
    )
    with pytest.raises(ValueError, match="fewer bars than the window names"):
        replace(view, split=stretched)


def test_a_view_built_in_code_is_held_to_the_grid_a_loaded_one_is(view):
    # A bar missing from the middle: a horizon counted across it would span
    # more time than its bars, and a windowed feature more calendar than its
    # name. The check a measurement makes of its history, made of a view's.
    bundle = view.frame.bundle
    holed = SeriesBundle(
        bundle.bars[:40] + bundle.bars[41:], daily=bundle.daily, funding=bundle.funding
    )
    with pytest.raises(EvaluationError, match="are not a grid"):
        TrainView(
            frame=FeatureFrame(holed, indicator_lookback=LOOKBACK),
            split=view.split,
            costs=view.costs,
        )


def test_the_look_ups_import_the_borrow_and_never_the_manifest():
    # The world's kind and what is planted in it live in the manifest. The
    # module that answers a model reaches this package through the borrow
    # alone: no other relative import, and no absolute one of the package.
    tree = ast.parse(Path(research_tools.__file__).read_text(encoding="utf-8"))
    froms = [node for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
    plain = [
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    ]
    assert {node.module for node in froms if node.level > 0} == {"upstream"}
    absolute = [node.module or "" for node in froms if node.level == 0] + plain
    assert absolute and not [name for name in absolute if name.startswith("contrib")]


# -- (a) nothing after training reaches an answer ---------------------------------------------


def _rewrite(store: ResearchStore, *, since_ms: int, series=("4h", "1d", "funding")) -> int:
    """Every 4h bar and daily bar closing, and every settlement posted, from ``since_ms`` on — replaced.

    ``series`` narrows it to some of the three.
    """
    changed = 0
    for interval in ("4h", "1d"):
        later = [
            replace(
                bar,
                open=bar.open * 2,
                high=bar.high * 3,
                low=bar.low / 2,
                close=bar.close * Decimal("1.7"),
            )
            for bar in store.iter_candles(COIN, interval)
            if interval in series and bar.close_time >= since_ms
        ]
        changed += store.upsert_candles(COIN, interval, later)
    settlements = [
        replace(point, rate=point.rate * -7 + Decimal("0.0003"))
        for point in store.iter_funding(COIN, since_ms=since_ms)
        if "funding" in series
    ]
    return changed + store.upsert_funding(COIN, settlements)


def test_rewriting_everything_after_training_changes_no_answer(opened_arm, view, history, tmp_path):
    _path, experiment = opened_arm
    before = _answers(view)
    bundle = view.frame.bundle
    last = bundle.bars[-1]

    # From the first instant after the last training bar's close: every bar
    # that opens at the window's end or later, every daily bar that closes
    # after that close, and every settlement posted after it. That takes in
    # the one settlement the view HOLDS from past the close — the one due at
    # it, which the venue posts a few ms late — so its rate is rewritten
    # too, and no answer may move: its stamp is held, its rate is not read.
    path = tmp_path / "rewritten.sqlite"
    with arm_store(history, path) as store:
        held = len(list(store.iter_candles(COIN, "4h")))
        rewritten = _rewrite(store, since_ms=last.close_time + 1)
        assert rewritten > held - len(bundle.bars) > 0
    assert last.close_time < bundle.funding[-1].time <= last.close_time + FUNDING_STAMP_TOLERANCE_MS
    assert _answers(_load(path, experiment)) == before

    # The controls, one series at a time: the same rewrite reaching just
    # INSIDE training is seen, so the equality above is not a view that
    # never read that series — the last training bar, the last daily bar
    # the view holds, the settlements inside the last training bar. On the
    # calls that need no indicator walk, which read all three series.
    quick = _answers(view, QUICK)
    for series, since_ms in (
        ("4h", last.close_time),
        ("1d", bundle.daily[-1].close_time),
        ("funding", last.close_time - STEP_MS),
    ):
        control = tmp_path / f"control-{series}.sqlite"
        with arm_store(history, control) as store:
            assert _rewrite(store, since_ms=since_ms, series=(series,)) > 0
        assert _answers(_load(control, experiment), QUICK) != quick, series


def test_a_day_that_closes_after_training_is_not_held_though_it_opened_inside(
    opened_arm, history, tmp_path
):
    # The fixture's training window ends on a midnight, where no day
    # straddles the line. Ended one bar earlier it does not: the day that
    # holds the last training bar closes four hours after it.
    path, experiment = opened_arm
    split = experiment.split
    cut = split.train.end_ms - STEP_MS
    earlier = replace(
        split,
        train=replace(split.train, end_ms=cut),
        validation=replace(split.validation, start_ms=cut),
    )
    view = _load(path, experiment, earlier)
    last = view.frame.bundle.bars[-1]
    with ResearchStore(path) as store:
        straddling = [
            day
            for day in store.iter_candles(COIN, "1d")
            if day.open_time <= last.open_time and day.close_time > last.close_time
        ]
    assert len(straddling) == 1
    assert max(day.close_time for day in view.frame.bundle.daily) <= last.close_time
    # And rewriting that day, with every later one, moves no answer.
    rewritten = tmp_path / "days.sqlite"
    with arm_store(history, rewritten) as store:
        assert _rewrite(store, since_ms=last.close_time + 1, series=("1d",)) > 0
    assert _answers(_load(rewritten, experiment, earlier), QUICK) == _answers(view, QUICK)


# -- (b) no answer names the world -------------------------------------------------------------


def _strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for key, item in value.items() for text in (*_strings(key), *_strings(item))]
    if isinstance(value, list):
        return [text for item in value for text in _strings(item)]
    return []


def test_no_answer_names_the_world_s_kind_or_what_is_planted_in_it(view):
    # Every key and every string value of every answer. (A figure that is
    # not defined is JSON's ``null``, which is not a string and names
    # nothing.) The keys are fixed and the values are feature names, sides
    # and regime labels, so this is a check on the words those are spelled in.
    shown = [
        text.lower() for tool, arguments in CALLS for text in _strings(call(view, tool, arguments))
    ]
    assert shown
    for word in ("null", "planted", "cond", "holdout", "validation", *(k.value for k in WorldKind)):
        assert not any(word in text for text in shown), word


# -- (c) a training backtest is a trial's training figures ---------------------------------------


@pytest.mark.parametrize(
    "document", [BUY, BUY_12, SHORT, PLANTED], ids=["buy", "buy12", "short", "planted"]
)
def test_a_training_backtest_is_the_figures_a_trial_s_training_window_carries(
    opened_arm, view, measured, document
):
    path, experiment = opened_arm
    spec = parse_spec(document)
    with ResearchStore(path) as store:
        trial = evaluate_unfiled(Ledger(store), experiment, spec, holdout=False, frame=measured)
    # The same record, field for field — scored there on a frame that runs
    # to the validation window's end, here on one that ends with training.
    assert backtest_train(view, spec) == SegmentMetrics.from_result(trial.train)


# -- (d) nothing is written ---------------------------------------------------------------------


def test_the_look_ups_leave_the_arm_s_store_byte_identical(opened_arm):
    path, experiment = opened_arm
    before = artifact_digest(path)
    _answers(_load(path, experiment))
    assert artifact_digest(path) == before
    with ResearchStore(path) as store:
        ledger = Ledger(store)
        # Nothing filed and nothing counted: a look is not a trial.
        assert ledger.rules_tried(COIN) == 0
        assert ledger.trials(experiment.experiment_id) == []


# -- (f) (g) one parser, and its own sentences ---------------------------------------------------


def _rounded(value: float) -> float:
    return float(f"{value:.{SIGNIFICANT_DIGITS}g}")


def test_a_rule_is_handed_to_the_parser_as_it_was_written(view):
    arguments = {"rule": copy.deepcopy(BUY_12)}
    answer = call(view, "backtest_train", arguments)
    # Untouched, so what was tried can be proposed as it stands; and what
    # was scored is what ``parse_spec`` makes of that very object.
    assert arguments == {"rule": BUY_12}
    scored = backtest_train(view, parse_spec(arguments["rule"]))
    assert answer == {
        "bars": scored.bars,
        "bars_unevaluable": 0,
        "trades": scored.trades,
        "exposure": _rounded(scored.exposure),
        "turnover": _rounded(scored.turnover),
        "net_sharpe": _rounded(scored.net.sharpe),
        "net_return": _rounded(scored.net.total_return),
        "ruined": False,
    }
    assert scored.trades > 0  # a rule that traded: the figures above are not all zeros


_UNKNOWN_KEY = {**BUY, "stop_loss": 0.02}
_CROSS_UNIT = [{"left": "close", "op": ">", "right": "rsi_14"}]
_FUTURE = [{"left": {"feature": "close", "offset": -1}, "op": ">", "right": 0}]


@pytest.mark.parametrize(
    ("tool", "arguments", "parser"),
    [
        (
            "feature_stats",
            {"feature": "rsi_9"},
            lambda: parse_feature_name("rsi_9", path="feature"),
        ),
        (
            "feature_stats",
            {"feature": "momentum"},
            lambda: parse_feature_name("momentum", path="feature"),
        ),
        ("feature_stats", {"feature": 14}, lambda: parse_feature_name(14, path="feature")),
        ("backtest_train", {"rule": _UNKNOWN_KEY}, lambda: parse_spec(_UNKNOWN_KEY)),
        ("backtest_train", {"rule": "buy the dip"}, lambda: parse_spec("buy the dip")),
        (
            "conditional_returns",
            {"conditions": _CROSS_UNIT, "horizon": 3, "side": "long"},
            lambda: parse_conditions(_CROSS_UNIT),
        ),
        (
            "conditional_returns",
            {"conditions": _FUTURE, "horizon": 3, "side": "long"},
            lambda: parse_conditions(_FUTURE),
        ),
        (
            "conditional_returns",
            {"conditions": {"left": "close"}, "horizon": 3, "side": "long"},
            lambda: parse_conditions({"left": "close"}),
        ),
    ],
)
def test_what_the_parser_refuses_is_refused_in_the_parser_s_own_sentence(
    view, tool, arguments, parser
):
    with pytest.raises(ValueError) as said:
        parser()
    with pytest.raises(ToolRefusal) as refused:
        call(view, tool, arguments)
    assert str(refused.value) == str(said.value)


@pytest.mark.parametrize(
    ("tool", "arguments", "sentence"),
    [
        ("promote", {}, "'promote' is not a tool; the tools are"),
        (None, {}, "None is not a tool"),
        ("feature_stats", "rsi_14", "feature_stats: expected an object, got str"),
        ("feature_stats", {}, r"feature_stats: missing required key\(s\) \['feature'\]"),
        (
            "feature_stats",
            {"feature": "close", "offset": 1},
            r"feature_stats: unknown key\(s\) \['offset'\]; this tool accepts \['feature'\]",
        ),
        (
            "backtest_train",
            BUY,
            r"backtest_train: unknown key\(s\) \['entry', 'family', 'sizing'\]",
        ),
        (
            "conditional_returns",
            {"conditions": [], "horizon": 3, "side": "long"},
            "conditions: at least one condition",
        ),
        (
            "conditional_returns",
            {"conditions": _ANY_BAR, "side": "long"},
            r"conditional_returns: missing required key\(s\) \['horizon'\]",
        ),
    ],
)
def test_an_argument_no_look_up_reads_is_refused_by_name(view, tool, arguments, sentence):
    with pytest.raises(ToolRefusal, match=sentence):
        call(view, tool, arguments)


@pytest.mark.parametrize("horizon", [0, MAX_HORIZON + 1, 2.5, True, "6"])
def test_a_horizon_is_refused_in_the_sentence_the_language_refuses_a_hold_in(view, horizon):
    # A horizon is the hold a rule would write under ``exit.max_bars``: the
    # same bounds, and the parser's sentence with the path swapped. (The
    # parser's check is private, so the tool restates it; this holds the two
    # together, bound included.)
    with pytest.raises(ValueError) as said:
        parse_spec({**BUY, "exit": {"max_bars": horizon}})
    arguments = {"conditions": _ANY_BAR, "horizon": horizon, "side": "long"}
    with pytest.raises(ToolRefusal) as refused:
        call(view, "conditional_returns", arguments)
    assert str(refused.value) == str(said.value).replace("spec.exit.max_bars", "horizon", 1)


@pytest.mark.parametrize("side", ["both", 1, None, ["long"]])
def test_a_side_is_refused_in_the_vocabulary_s_own_sentence(view, side):
    with pytest.raises(ValueError) as said:
        Side(side)
    with pytest.raises(ToolRefusal) as refused:
        call(view, "conditional_returns", {"conditions": _ANY_BAR, "horizon": 3, "side": side})
    assert str(refused.value) == f"side: {said.value}"


def test_the_tools_are_the_plan_s_three():
    assert TOOL_NAMES == ("feature_stats", "conditional_returns", "backtest_train")


# -- a feature with no value at the window's start is not read ------------------------------------


def test_a_feature_with_no_value_at_the_first_training_bar_is_refused_by_all_three():
    # Training from the first bar: a twenty-bar mean has no value on the
    # first nineteen. The evaluator does not measure a rule reading it on
    # this window, and a look-up answering anyway would describe the last
    # eleven bars as if they were the thirty. One rule, all three look-ups —
    # and it is the store's fact, so the evaluator's error and not a refusal.
    closes = [100.0 + i for i in range(30)]
    view = _hand_view(closes, funding_hours=30 * 4)
    reading = [{"left": "close", "op": ">", "right": "sma_20"}]
    rule = parse_spec({**BUY, "entry": {"long": reading}})
    unready = "sma_20 has no value at the first training bar"
    with pytest.raises(EvaluationError, match=unready) as refused:
        feature_stats(view, FeatureRef(FeatureKind.SMA, 20))
    # The sentence names no instant, as nothing a model is shown does.
    assert not any(character.isdigit() for character in str(refused.value).replace("sma_20", ""))
    with pytest.raises(EvaluationError, match=unready):
        conditional_returns(view, parse_conditions(reading), horizon=3, side=Side.LONG)
    with pytest.raises(EvaluationError, match=unready):
        call(view, "feature_stats", {"feature": "sma_20"})
    with pytest.raises(EvaluationError, match="sma_20 has no value at the first bar"):
        backtest_train(view, rule)

    # Training from the bar the mean first has a value at, all three answer.
    ready = _hand_view(closes, train_from=19, funding_hours=30 * 4)
    stats = feature_stats(ready, FeatureRef(FeatureKind.SMA, 20))
    assert (stats.bars, stats.valued, stats.minimum) == (11, 11, pytest.approx(109.5))
    read = conditional_returns(ready, parse_conditions(reading), horizon=3, side=Side.LONG)
    assert (read.bars, read.holding, read.unevaluable) == (11, 11, 0)
    assert backtest_train(ready, rule).trades == 1


def test_bars_further_in_where_a_feature_loses_its_value_are_counted():
    closes = [100.0 + i for i in range(12)]
    reading = [{"left": "funding_rate", "op": ">", "right": 0}]
    # The venue skips three of the settlements inside bar 5 (hours 21 to 23):
    # at that bar's close the newest rate is four hours old, which is stale,
    # and at every other bar it is fresh. The window is still one the
    # evaluator measures — three settlements short of forty-eight is within
    # its coverage — and the bar is counted, here as the scored loop counts
    # it, and not read as "does not hold".
    view = _hand_view(closes, funding_hours=12 * 4, missed_hours=(21, 22, 23))
    stats = feature_stats(view, FeatureRef(FeatureKind.FUNDING_RATE))
    assert (stats.bars, stats.valued) == (12, 11)
    read = conditional_returns(view, parse_conditions(reading), horizon=2, side=Side.LONG)
    assert (read.bars, read.holding, read.unevaluable) == (12, 11, 1)
    rule = {**BUY, "entry": {"long": reading}}
    scored = backtest_train(view, parse_spec(rule))
    assert (scored.bars_unevaluable, scored.funding_settlements_missing) == (1, 3)
    # And each of the three says so in what a model is shown.
    asked = {"conditions": reading, "horizon": 2, "side": "long"}
    assert call(view, "conditional_returns", asked)["bars_unevaluable"] == 1
    assert call(view, "backtest_train", {"rule": rule})["bars_unevaluable"] == 1
    assert call(view, "feature_stats", {"feature": "funding_rate"})["bars_with_value"] == 11

    # Where the settlements stop altogether the feature is stale to the end,
    # the window's last bar included — a bar of the window like the others,
    # though a scored window takes no decision on it.
    stopped = _hand_view(closes, funding_hours=6 * 4)
    read = conditional_returns(stopped, parse_conditions(reading), horizon=2, side=Side.LONG)
    assert (read.bars, read.holding, read.unevaluable) == (12, 6, 6)


# -- feature_stats, by hand ---------------------------------------------------------------------


def test_feature_stats_are_the_ordered_training_values_cut_by_interpolation():
    # Eleven closes, the last nine of them training bars: 3..11 once
    # ordered, so a cut at q sits at 3 + 8q.
    view = _hand_view([50, 60, 7, 3, 11, 5, 9, 4, 10, 6, 8], train_from=2)
    stats = feature_stats(view, FeatureRef(FeatureKind.CLOSE))
    assert isinstance(stats, NumberStats)
    assert (stats.feature, stats.bars, stats.valued) == ("close", 9, 9)
    assert (stats.minimum, stats.maximum) == (3.0, 11.0)
    assert list(stats.quantiles) == pytest.approx([3 + 8 * q for q in QUANTILES])
    assert call(view, "feature_stats", {"feature": "close"}) == {
        "feature": "close",
        "bars": 9,
        "bars_with_value": 9,
        "min": 3.0,
        "p05": 3.4,
        "p25": 5.0,
        "p50": 7.0,
        "p75": 9.0,
        "p95": 10.6,
        "max": 11.0,
    }
    # Read as a condition reads it: an offset looks a bar back, so the
    # first training bar reads the warm-up's last close.
    lagged = feature_stats(view, FeatureRef(FeatureKind.CLOSE, offset=1))
    assert (lagged.feature, lagged.minimum, lagged.maximum) == ("close[1]", 3.0, 60.0)


def test_the_regime_is_counted_by_label_with_every_label_listed(view):
    counts = feature_stats(view, FeatureRef(FeatureKind.REGIME))
    assert isinstance(counts, LabelCounts)
    first, stop = view.window
    column = view.frame.series(FeatureRef(FeatureKind.REGIME))[first:stop]
    labelled = [value.value for value in column if value is not None]
    assert counts.bars == stop - first
    assert counts.valued == len(labelled) > 0
    # Every label of the vocabulary, in its order: one that never occurred
    # reads 0, not as a label that does not exist.
    assert counts.counts == tuple(
        (regime.value, labelled.count(regime.value)) for regime in MarketRegime
    )
    assert call(view, "feature_stats", {"feature": "regime"}) == {
        "feature": "regime",
        "bars": counts.bars,
        "bars_with_value": counts.valued,
        "counts": dict(counts.counts),
    }


def test_the_stats_records_refuse_figures_that_are_not_one_window_s():
    # Built by one function today, and by the fakes of the later steps
    # tomorrow: the gate is on the record.
    cuts = (2.0, 3.0, 4.0, 5.0, 6.0)
    assert NumberStats("close", 9, 9, 1.0, 7.0, cuts).valued == 9
    for bars, valued, low, high, quantiles in (
        (9, 10, 1.0, 7.0, cuts),  # more bars with a value than bars
        (9, 0, 1.0, 7.0, cuts),  # a description of no value
        (9, 9, 3.0, 7.0, cuts),  # a cut under the minimum
        (9, 9, 1.0, 5.0, cuts),  # a cut over the maximum
        (9, 9, 1.0, 7.0, cuts[:4]),  # a cut missing
        (9, 9, 1.0, 7.0, cuts[::-1]),  # the cuts out of order
    ):
        with pytest.raises(ValueError, match="close: "):
            NumberStats("close", bars, valued, low, high, quantiles)

    with pytest.raises(ValueError, match="finite and in ascending order"):
        NumberStats("close", 9, 9, 1.0, math.nan, cuts)

    vocabulary = [regime.value for regime in MarketRegime]
    counted = tuple(zip(vocabulary, (4, 3, 0, 0, 0), strict=False))
    assert len(counted) == len(vocabulary)
    labels = LabelCounts("regime", bars=9, counts=list(counted))
    assert labels.valued == 7  # the counts added up, so it cannot disagree with them
    assert labels.counts == counted  # and held as a tuple, whatever it was handed
    assert NumberStats("close", 9, 9, 1.0, 7.0, list(cuts)).quantiles == cuts
    for wrong in (
        counted[:-1],  # a label of the vocabulary left out
        counted[::-1] if len(counted) > 1 else (),  # out of the vocabulary's order
        (*counted, counted[0]),  # a label twice
        (("sideways", 1), *counted[1:]),  # a label the vocabulary does not have
        ((vocabulary[0], -1), *counted[1:]),  # a count below zero
        ((vocabulary[0], 3.5), *counted[1:]),  # a count that is not a whole number
        ((vocabulary[0], True), *counted[1:]),
    ):
        with pytest.raises(ValueError, match="every label of the vocabulary once"):
            LabelCounts("regime", bars=9, counts=wrong)
    with pytest.raises(ValueError, match="count 13 bar"):  # ten and three, of nine
        LabelCounts("regime", bars=9, counts=((vocabulary[0], 10), *counted[1:]))
    with pytest.raises(ValueError, match="count 0 bar"):  # a description of no label
        LabelCounts("regime", bars=9, counts=tuple((label, 0) for label in vocabulary))


# -- (h) conditional_returns, by hand -------------------------------------------------------------


def test_conditional_returns_are_taken_on_samples_that_share_no_bar():
    #         bar:   0    1    2    3    4    5    6    7    8    9   10   11
    closes = [100, 110, 121, 100, 120, 126, 100, 100, 150, 100, 130, 100]
    # ``close > 105`` holds at bars 1, 2, 4, 5, 8 and 10. With a horizon of
    # two: bar 1 is a sample (to bar 3), bar 2 is inside its horizon, bar 4
    # is a sample (to bar 6), bar 5 inside it, bar 8 a sample (to bar 10),
    # and bar 10 would need bar 12, which the window does not hold.
    view = _hand_view(closes)
    read = conditional_returns(view, _above(105), horizon=2, side=Side.LONG)
    samples = [100 / 110 - 1, 100 / 120 - 1, 130 / 150 - 1]
    assert (read.bars, read.holding, read.unevaluable, read.samples) == (12, 6, 0, 3)
    assert read.mean == pytest.approx(statistics.fmean(samples))
    assert read.std == pytest.approx(statistics.stdev(samples))
    assert read.t_stat == pytest.approx(
        statistics.fmean(samples) / (statistics.stdev(samples) / math.sqrt(3))
    )
    # The baseline is the same move with no condition: every second bar
    # from the first — bars 0, 2, 4, 6 and 8, each to the bar two on.
    baseline = [121 / 100 - 1, 120 / 121 - 1, 100 / 120 - 1, 150 / 100 - 1, 130 / 150 - 1]
    assert read.baseline_samples == 5
    assert read.baseline_mean == pytest.approx(statistics.fmean(baseline))
    # The other side is the same moves, signed for it — the baseline's too.
    short = conditional_returns(view, _above(105), horizon=2, side=Side.SHORT)
    assert short.mean == pytest.approx(-read.mean)
    assert short.std == pytest.approx(read.std)
    assert short.t_stat == pytest.approx(-read.t_stat)
    assert short.baseline_mean == pytest.approx(-read.baseline_mean)


def test_a_signal_that_holds_at_every_bar_is_sampled_once_a_horizon():
    # The overlap at its worst: twenty-one training bars, the signal on at
    # each, a horizon of four. Seventeen bars have a whole horizon inside
    # the window and every neighbouring pair of those shares three bars of
    # return; five samples share none (bars 0, 4, 8, 12, 16).
    view = _hand_view([100.0 * 1.01**i for i in range(21)])
    read = conditional_returns(view, _above(0), horizon=4, side=Side.LONG)
    assert (read.holding, read.samples) == (21, 5)
    assert read.mean == pytest.approx(1.01**4 - 1)
    # A signal on at every bar IS the baseline: the same samples.
    assert (read.baseline_samples, read.baseline_mean) == (5, read.mean)
    # Five equal moves, which floats do not make bit-equal: the deviation
    # between them is rounding (1e-16 here), and the mean over it would be
    # a t-statistic of about 1e15 for a signal with no spread to test. None
    # is given — as when the samples are exactly equal, where it would be
    # a division by zero.
    assert 0 < read.std < 1e-12
    assert read.t_stat is None
    asked = {"conditions": _ANY_BAR, "horizon": 4, "side": "long"}
    assert call(view, "conditional_returns", asked)["t_stat"] is None
    flat = _hand_view([100.0] * 21)
    still = conditional_returns(flat, _above(0), horizon=4, side=Side.LONG)
    assert (still.samples, still.mean, still.std, still.t_stat) == (5, 0.0, 0.0, None)


def test_training_from_a_later_bar_reads_no_signal_of_the_warm_up():
    closes = [200, 200, 200, 100, 110, 100, 121, 100]
    # Training is bars 3..7. The signal holds on the three warm-up bars and
    # on bars 4 and 6; only the last two are counted, and only bar 4 has
    # its horizon inside the window (bar 6 would need bar 8). The baseline
    # is bars 3 and 5, each from a close of 100 to a close of 100.
    view = _hand_view(closes, train_from=3)
    read = conditional_returns(view, _above(105), horizon=2, side=Side.LONG)
    assert (read.bars, read.holding, read.samples) == (5, 2, 1)
    assert read.mean == pytest.approx(121 / 110 - 1)
    # One sample has a mean and nothing to deviate from.
    assert read.std is None and read.t_stat is None
    asked = {
        "conditions": [{"left": "close", "op": ">", "right": 105}],
        "horizon": 2,
        "side": "long",
    }
    assert call(view, "conditional_returns", asked) == {
        "bars": 5,
        "bars_holding": 2,
        "bars_unevaluable": 0,
        "horizon": 2,
        "side": "long",
        "samples": 1,
        "mean_return": 0.1,
        "std_return": None,
        "t_stat": None,
        "baseline_samples": 2,
        "baseline_mean_return": 0.0,
    }


def test_a_signal_too_late_for_its_horizon_or_never_on_gives_no_sample():
    view = _hand_view([100, 100, 100, 100, 100, 120])
    late = conditional_returns(view, _above(105), horizon=1, side=Side.LONG)
    assert (late.holding, late.samples, late.mean, late.std, late.t_stat) == (
        1,
        0,
        None,
        None,
        None,
    )
    # The window still has its own move to report: five one-bar steps.
    assert (late.baseline_samples, late.baseline_mean) == (5, pytest.approx(0.04))
    never = conditional_returns(view, _above(500), horizon=1, side=Side.LONG)
    assert (never.holding, never.samples, never.mean) == (0, 0, None)
    # A horizon longer than the window is legal and samples nothing at all.
    beyond = conditional_returns(view, _above(0), horizon=50, side=Side.LONG)
    assert (beyond.samples, beyond.baseline_samples, beyond.baseline_mean) == (0, 0, None)


def test_the_function_refuses_what_the_json_layer_refuses():
    # The control arm of a later step (plan T5) will call the functions directly.
    view = _hand_view([100, 110, 120])
    with pytest.raises(ToolRefusal, match="at least one condition"):
        conditional_returns(view, (), horizon=1, side=Side.LONG)
    with pytest.raises(ToolRefusal, match="between 1 and"):
        conditional_returns(view, _above(0), horizon=0, side=Side.LONG)
    with pytest.raises(ToolRefusal, match="expected a whole number, got True"):
        conditional_returns(view, _above(0), horizon=True, side=Side.LONG)
    with pytest.raises(ToolRefusal, match="side: unsupported side 'both'"):
        conditional_returns(view, _above(0), horizon=1, side="both")
    # And reads a side as the word a rule writes it in, like the member.
    assert conditional_returns(view, _above(0), horizon=1, side="short") == conditional_returns(
        view, _above(0), horizon=1, side=Side.SHORT
    )


def test_a_returns_record_refuses_figures_that_are_not_one_window_s():
    good = {
        "bars": 12,
        "holding": 6,
        "unevaluable": 0,
        "horizon": 2,
        "side": "long",
        "samples": 3,
        "mean": 0.01,
        "std": 0.02,
        "t_stat": 0.01 / (0.02 / math.sqrt(3)),
        "baseline_samples": 5,
        "baseline_mean": 0.0,
    }
    assert ConditionalReturns(**good).side is Side.LONG  # the word is read as the member
    # No t-statistic beside a deviation is legal: the samples may differ by
    # rounding alone, which is read off the samples and not off the record.
    assert ConditionalReturns(**{**good, "t_stat": None}).t_stat is None
    counts, figures = "are not counts of one window", "do not go with"
    for changed, sentence in (
        ({"samples": 7}, counts),  # more samples than bars the signal holds at
        ({"holding": 13}, counts),  # more holding bars than bars
        ({"unevaluable": 13}, counts),
        ({"unevaluable": 7}, counts),  # six holding and seven that could not be asked, of twelve
        ({"baseline_samples": 13}, counts),
        ({"baseline_samples": 4}, counts),  # five two-bar samples fit in twelve bars, not four
        ({"baseline_samples": 6}, counts),
        ({"samples": 6}, counts),  # more samples than the baseline, which takes all that fit
        ({"horizon": 0}, "is not one a look-up takes"),
        ({"horizon": True}, "is not one a look-up takes"),
        ({"mean": None}, figures),  # samples and no mean
        ({"samples": 0}, figures),  # a mean and no sample
        ({"std": None}, figures),  # three samples and no deviation
        ({"samples": 1, "std": 0.02}, figures),  # a deviation of one sample
        ({"samples": 1, "std": None}, figures),  # a t-statistic with no deviation
        ({"baseline_mean": None}, figures),  # baseline samples and no baseline
        ({"mean": math.inf}, "have to be finite"),
        ({"std": -0.02}, "have to be finite"),
        ({"t_stat": -99.0}, "is not the mean"),  # derived, so held to what it derives from
        ({"t_stat": 0.866}, "is not the mean"),  # and closely: the figure is 0.8660254…
        ({"samples": 5, "std": 5e-324}, "is not the mean"),  # no error of the mean to divide by
        ({"std": 0.0}, "is not the mean"),  # a t-statistic over no deviation at all
        ({"side": "both"}, "unsupported side"),
    ):
        with pytest.raises(ValueError, match=sentence):
            ConditionalReturns(**{**good, **changed})
    # A window no longer than the horizon has no baseline sample, and so no baseline.
    short = {**good, "bars": 2, "holding": 0, "samples": 0, "baseline_samples": 0}
    short.update(mean=None, std=None, t_stat=None)
    with pytest.raises(ValueError, match=figures):
        ConditionalReturns(**short)
    assert ConditionalReturns(**{**short, "baseline_mean": None}).baseline_samples == 0


def test_a_figure_is_shown_to_six_significant_digits():
    # Significant, not decimal: a mean nine places down keeps its digits,
    # and the exact figure stays on the record the function returns.
    view = _hand_view([100.0, 100.0012345678, 100.0, 100.0098765432, 100.0])
    exact = conditional_returns(view, _above(0), horizon=1, side=Side.LONG)
    shown = call(
        view, "conditional_returns", {"conditions": _ANY_BAR, "horizon": 1, "side": "long"}
    )
    assert SIGNIFICANT_DIGITS == 6
    assert shown["mean_return"] == float(f"{exact.mean:.6g}") != exact.mean
    assert shown["samples"] == exact.samples == 4
