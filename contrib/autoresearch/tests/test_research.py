"""An experiment from creation to promotion, on a store, through the verbs the commands run.

The store is synthetic and built once per module: 400 midnight-anchored 4h
bars on a rising random walk, 240 days of daily bars before them, and hourly
funding stamped 57 ms late from forty days before — the venue's shapes,
``close_time`` a millisecond short of the next open included. Each test
works on its own copy, because every one of them writes to the ledger.

What is under test is what no unit test of the ledger or the evaluator can
see: which rows a measurement READS (the holdout lock is a property of the
read, not of the report), that the train window starts where every legal spec
can be measured, that a promotion re-measures what it was gated on, and that
the commands render one measurement the same way twice.
"""

from __future__ import annotations

import dataclasses
import json
import random
import shutil
from decimal import Decimal

import pytest

from contrib.autoresearch import research
from contrib.autoresearch.cli import main
from contrib.autoresearch.costs import CostModel
from contrib.autoresearch.dsl import parse_spec
from contrib.autoresearch.evaluator import EvaluationError, load_bundle
from contrib.autoresearch.features import FeatureFrame, SeriesBundle
from contrib.autoresearch.ledger import Ledger, LedgerError, Penalty, TrialStatus
from contrib.autoresearch.research import (
    calibrate,
    first_measurable_index,
    measure,
    open_experiment,
    plan_split,
    promote,
    require_clean_history,
)
from contrib.autoresearch.split import SplitError
from contrib.autoresearch.store import ResearchStore
from contrib.autoresearch.upstream import FundingPoint
from contrib.autoresearch.vocabulary import (
    MAX_OFFSET_BARS,
    FeatureRef,
    feature_names,
    parse_feature_name,
)

from .conftest import MS_PER_HOUR, candles

_STEP = 4 * MS_PER_HOUR
_DAY = 24 * MS_PER_HOUR
_START = 1_700_000_000_000 - 1_700_000_000_000 % _DAY  # a UTC midnight
_BARS = 400
_LOOKBACK = 50  # the engine's floor: the indicator walk is what every frame here pays for
_SIZING = {"mode": "fixed_margin_fraction", "fraction": 0.5}
_BUY = {
    "family": "breakout",
    "entry": {"long": [{"left": "close", "op": ">", "right": 0}]},
    "sizing": _SIZING,
}
_FLAT = {
    "family": "breakout",
    "entry": {"long": [{"left": "close", "op": ">", "right": 1e9}]},
    "sizing": _SIZING,
}


def _prices(count: int, *, seed: int, start: float, drift: float) -> tuple[list, list]:
    rng = random.Random(seed)
    closes = [start]
    for _ in range(count - 1):
        closes.append(round(closes[-1] * (1 + drift + rng.uniform(-0.01, 0.01)), 4))
    return closes, [closes[0], *closes[:-1]]


def _venue(bars):
    return [dataclasses.replace(bar, close_time=bar.close_time - 1) for bar in bars]


def _fill(store: ResearchStore) -> None:
    closes, opens = _prices(_BARS, seed=1, start=30000.0, drift=0.002)
    store.upsert_candles("BTC", "4h", _venue(candles(closes, opens=opens, start_ms=_START)))
    days = 240 + _BARS // 6 + 1
    closes, opens = _prices(days, seed=2, start=20000.0, drift=0.001)
    store.upsert_candles(
        "BTC",
        "1d",
        _venue(candles(closes, opens=opens, start_ms=_START - 240 * _DAY, step_ms=_DAY)),
    )
    first = _START - 40 * _DAY
    store.upsert_funding(
        "BTC",
        [
            FundingPoint(
                time=first + (i + 1) * MS_PER_HOUR + 57,
                rate=Decimal(str(round(0.00001 * ((i % 9) - 3), 6))),
            )
            for i in range(40 * 24 + _BARS * 4)
        ],
    )


def _open(ledger: Ledger, name: str = "btc-4h", **overrides):
    body = {
        "experiment_id": name,
        "coin": "BTC",
        "interval": "4h",
        "costs": CostModel(),
        "indicator_lookback": _LOOKBACK,
        "penalty": Penalty(),
    }
    body.update(overrides)
    return open_experiment(ledger, **body)


@pytest.fixture(scope="module")
def history(tmp_path_factory):
    path = tmp_path_factory.mktemp("history") / "autoresearch.sqlite"
    with ResearchStore(path) as store:
        _fill(store)
    return path


@pytest.fixture(scope="module")
def opened(history, tmp_path_factory):
    path = tmp_path_factory.mktemp("opened") / "autoresearch.sqlite"
    shutil.copy(history, path)
    with ResearchStore(path) as store:
        _open(Ledger(store))
    return path


def _copy(template, tmp_path) -> ResearchStore:
    copy = tmp_path / "autoresearch.sqlite"
    shutil.copy(template, copy)
    return ResearchStore(copy)


@pytest.fixture
def fresh(history, tmp_path):
    with _copy(history, tmp_path) as store:
        yield Ledger(store)


@pytest.fixture
def ledger(opened, tmp_path):
    with _copy(opened, tmp_path) as store:
        yield Ledger(store)


# -- opening an experiment ----------------------------------------------------------


def test_train_starts_at_the_first_bar_every_feature_has_a_value_plus_the_deepest_offset(fresh):
    experiment = _open(fresh)
    bundle = load_bundle(fresh.store, coin="BTC", interval="4h")
    frame = FeatureFrame(bundle, indicator_lookback=_LOOKBACK)
    columns = [frame.series(FeatureRef(*parse_feature_name(name))) for name in feature_names()]
    ready = next(i for i in range(len(bundle.bars)) if all(c[i] is not None for c in columns))
    assert any(column[ready - 1] is None for column in columns)
    assert experiment.split.train.start_ms == bundle.bars[ready + MAX_OFFSET_BARS].open_time
    assert experiment.split.holdout.start_ms % _DAY == 0
    assert experiment.split.holdout.end_ms == bundle.bars[-1].open_time + _STEP
    assert fresh.experiment("btc-4h") == experiment


def test_the_slowest_spec_the_language_can_write_is_not_refused_for_warm_up(ledger):
    def lagged(name):
        return {"feature": name, "offset": MAX_OFFSET_BARS}

    slow = parse_spec(
        {
            "family": "regime_filter",
            "entry": {
                "long": [
                    {"left": lagged("sma_200"), "op": ">", "right": lagged("sma_1d_200")},
                    {"left": lagged("ret_180"), "op": ">", "right": -0.9},
                    {"left": lagged("funding_zscore_30"), "op": ">", "right": -1000},
                    {"left": lagged("funding_cum_42"), "op": ">", "right": -1},
                    {"left": lagged("donchian_low_120"), "op": ">", "right": 0},
                    {"left": lagged("realized_vol_50"), "op": ">", "right": -1},
                    {"left": lagged("ema_50"), "op": ">", "right": 0},
                ]
            },
            "filters": [{"left": lagged("regime"), "op": "!=", "right": "volatile"}],
            "sizing": _SIZING,
        }
    )
    measurement = measure(ledger, ledger.experiment("btc-4h"), slow)
    assert measurement.trial.trial_id == 1


def _frame_with_a_hole(bars: int, *, hole_at: int):
    """A stand-in frame: every column warms up after five bars, and one loses a value at ``hole_at``."""
    from types import SimpleNamespace

    def series(ref):
        column = [None] * 5 + [1.0] * (bars - 5)
        if ref.name == "funding_cum_1":
            column[hole_at] = None
        return tuple(column)

    return SimpleNamespace(bundle=SimpleNamespace(bars=[None] * bars), series=series)


def test_train_starts_after_every_bar_an_offset_can_reach_has_every_value():
    """A hole inside the offset's reach moves the start past it, not just past the warm-up.

    Taking the first fully-valued bar (5) and adding the offset (24) gave 29,
    whose reach back to 5 crosses the hole at 20 — a lagged spec was refused at
    train's first bar on exactly the history this start was meant to rule out.
    """
    frame = _frame_with_a_hole(60, hole_at=20)
    assert first_measurable_index(frame) == 20 + 1 + MAX_OFFSET_BARS
    with pytest.raises(EvaluationError, match="consecutive bars"):
        first_measurable_index(_frame_with_a_hole(40, hole_at=20))


def test_every_later_experiment_withholds_the_holdout_the_first_one_pinned(ledger):
    first = ledger.experiment("btc-4h")
    second = _open(ledger, "btc-4h-wider-train", train_share=0.5, validation_share=0.3)
    assert second.split.holdout.start_ms == first.split.holdout.start_ms
    assert second.split.validation.end_ms == first.split.holdout.start_ms
    assert second.split.validation.start_ms != first.split.validation.start_ms


def test_a_first_holdout_is_cut_on_a_utc_midnight():
    split = plan_split(
        "4h",
        grid_origin_ms=_START,
        train_start_ms=_START + 7 * _STEP,
        end_ms=_START + 400 * _STEP,
        pinned_holdout_ms=None,
    )
    assert split.holdout.start_ms % _DAY == 0
    assert split.train.start_ms == _START + 7 * _STEP


def test_a_pinned_holdout_the_store_cannot_honour_is_refused_by_name():
    common = {"grid_origin_ms": _START, "train_start_ms": _START, "end_ms": _START + 400 * _STEP}
    with pytest.raises(SplitError, match="not on this store's 4h grid"):
        plan_split("4h", pinned_holdout_ms=_START + 200 * _STEP + 60_000, **common)
    with pytest.raises(SplitError, match="leaves a window with no bars .*given the pinned holdout"):
        plan_split("4h", pinned_holdout_ms=_START + 500 * _STEP, **common)


def test_a_store_with_nothing_in_it_is_refused_not_opened(store):
    with pytest.raises(EvaluationError, match="holds no 4h bars"):
        _open(Ledger(store))
    assert Ledger(store).experiments() == []


# -- the history a measurement reads ----------------------------------------------


def test_a_hole_in_the_warm_up_is_refused_though_no_window_touches_it(fresh):
    bundle = load_bundle(fresh.store, coin="BTC", interval="4h")
    bars = list(bundle.bars)
    del bars[10]
    with pytest.raises(EvaluationError, match=r"4h bars this measurement reads are not a grid: 1 hole"):
        require_clean_history(SeriesBundle(bars, daily=bundle.daily, funding=bundle.funding), "4h")


def test_a_daily_hole_is_refused_inside_the_reach_of_sma_1d_200_and_ignored_before_it(fresh):
    bundle = load_bundle(fresh.store, coin="BTC", interval="4h")
    reach = bundle.bars[0].close_time - 200 * _DAY
    inside = list(bundle.daily)
    del inside[next(i for i, day in enumerate(inside) if day.close_time > reach + 5 * _DAY)]
    with pytest.raises(EvaluationError, match=r"1d bars this measurement reads are not a grid"):
        require_clean_history(SeriesBundle(bundle.bars, daily=inside, funding=bundle.funding), "4h")
    older = list(bundle.daily)
    assert older[5].close_time < reach
    del older[5]
    clean = SeriesBundle(bundle.bars, daily=older, funding=bundle.funding)
    assert require_clean_history(clean, "4h") == 0


def test_a_funding_hole_is_counted_and_an_off_grid_settlement_is_refused(fresh):
    bundle = load_bundle(fresh.store, coin="BTC", interval="4h")
    holed = list(bundle.funding)
    del holed[-10]
    counted = SeriesBundle(bundle.bars, daily=bundle.daily, funding=holed)
    assert require_clean_history(counted, "4h") == 1
    stray = FundingPoint(time=bundle.funding[-20].time + 30 * 60_000, rate=Decimal("0"))
    strayed = sorted([*bundle.funding, stray], key=lambda point: point.time)
    with pytest.raises(EvaluationError, match=r"funding settlements .*1 off-grid"):
        require_clean_history(SeriesBundle(bundle.bars, daily=bundle.daily, funding=strayed), "4h")


# -- measuring ------------------------------------------------------------------------


def _spy_on_reads(monkeypatch) -> list[tuple[int | None, SeriesBundle]]:
    reads: list[tuple[int | None, SeriesBundle]] = []
    real = research.load_bundle

    def spy(store, **kwargs):
        bundle = real(store, **kwargs)
        reads.append((kwargs.get("until_ms"), bundle))
        return bundle

    monkeypatch.setattr(research, "load_bundle", spy)
    return reads


def _unreachable(*_args, **_kwargs):
    raise AssertionError("this path must not load a bundle")


def test_measuring_a_trial_reads_no_row_of_the_holdout(ledger, monkeypatch):
    reads = _spy_on_reads(monkeypatch)
    experiment = ledger.experiment("btc-4h")
    measurement = measure(ledger, experiment, parse_spec(_BUY))
    assert [until for until, _bundle in reads] == [experiment.split.loadable_until(holdout=False)]
    bundle = reads[0][1]
    holdout_start = experiment.split.holdout.start_ms
    assert bundle.bars[-1].open_time < holdout_start
    assert bundle.daily[-1].close_time < holdout_start
    # The settlement DUE at validation's last close posts just after it and is
    # read; the one due an hour into the holdout is not.
    assert holdout_start <= bundle.funding[-1].time < holdout_start + MS_PER_HOUR
    assert measurement.result is not None and measurement.result.holdout is None
    assert measurement.trial.holdout is None


def test_a_rule_measured_again_is_the_same_trial_and_reads_nothing(ledger, monkeypatch):
    experiment = ledger.experiment("btc-4h")
    first = measure(ledger, experiment, parse_spec(_BUY))
    monkeypatch.setattr(research, "load_bundle", _unreachable)
    again = measure(ledger, experiment, parse_spec({**_BUY, "family": "mean_reversion"}))
    assert again.duplicate and again.trial == first.trial
    assert ledger.count_trials("btc-4h") == 1


# -- promoting -------------------------------------------------------------------------


def test_promote_measures_the_holdout_once_past_the_gate(ledger, monkeypatch):
    experiment = ledger.experiment("btc-4h")
    measurement = measure(ledger, experiment, parse_spec(_BUY))
    assert measurement.verdict.eligible, measurement.verdict.blockers
    reads = _spy_on_reads(monkeypatch)
    promoted = promote(ledger, experiment, measurement.trial.trial_id)
    assert [until for until, _bundle in reads] == [experiment.split.loadable_until(holdout=True)]
    assert promoted.status is TrialStatus.PROMOTED
    assert promoted.holdout is not None and promoted.holdout.segment == experiment.split.holdout
    assert promoted.holdout.trades == 1
    with pytest.raises(LedgerError, match="already been promoted"):
        promote(ledger, experiment, measurement.trial.trial_id)


def test_a_trial_the_gate_refuses_never_reads_the_holdout(ledger, monkeypatch):
    experiment = ledger.experiment("btc-4h")
    flat = measure(ledger, experiment, parse_spec(_FLAT))
    monkeypatch.setattr(research, "load_bundle", _unreachable)
    with pytest.raises(LedgerError, match=r"cannot be promoted: .*made no trades"):
        promote(ledger, experiment, flat.trial.trial_id)


def test_promote_refuses_a_trial_whose_figures_the_store_no_longer_gives(ledger):
    experiment = ledger.experiment("btc-4h")
    measurement = measure(ledger, experiment, parse_spec(_BUY))
    bars = list(ledger.store.iter_candles("BTC", "4h"))
    start = experiment.split.validation.start_ms
    bar = bars[next(i for i, bar in enumerate(bars) if bar.open_time == start) + 3]
    moved = bar.close * Decimal("1.05")
    revised = dataclasses.replace(bar, close=moved, high=max(bar.high, moved))
    ledger.store.upsert_candles("BTC", "4h", [revised])
    with pytest.raises(LedgerError, match="no longer gives trial #1 the validation figures"):
        promote(ledger, experiment, measurement.trial.trial_id)
    assert ledger.trial("btc-4h", 1).status is TrialStatus.MEASURED


# -- calibrating ------------------------------------------------------------------------


def test_calibrate_scores_the_baselines_on_both_windows_and_files_nothing(ledger):
    experiment = ledger.experiment("btc-4h")
    rows = dict(calibrate(ledger, experiment))
    assert set(rows) == {"buy_and_hold", "always_flat", "high_turnover_noise"}
    assert ledger.count_trials("btc-4h") == 0
    windows = [experiment.split.train, experiment.split.validation]
    for segments in rows.values():
        assert [metrics.segment for metrics in segments] == windows
    for metrics in rows["always_flat"]:
        assert (metrics.trades, metrics.net.total_return) == (0, 0)
    for metrics in rows["buy_and_hold"]:
        assert metrics.exposure == pytest.approx((metrics.bars - 1) / metrics.bars)
    for metrics in rows["high_turnover_noise"]:
        assert metrics.net.total_return < metrics.gross.total_return


# -- the commands -----------------------------------------------------------------------


def test_the_commands_walk_an_experiment_from_creation_to_promotion(history, tmp_path, capsys):
    db = tmp_path / "autoresearch.sqlite"
    shutil.copy(history, db)
    buy, flat = tmp_path / "buy.json", tmp_path / "flat.json"
    buy.write_text(json.dumps(_BUY), encoding="utf-8")
    flat.write_text(json.dumps(_FLAT), encoding="utf-8")
    store = ["--db", str(db)]
    experiment = ["--experiment", "btc-4h"]

    assert main(["experiment", "--name", "btc-4h", "--indicator-lookback", str(_LOOKBACK), *store]) == 0
    assert "now pinned for every later experiment on BTC" in capsys.readouterr().out

    assert main(["evaluate", *experiment, "--spec", str(buy), *store]) == 0
    evaluated = capsys.readouterr().out
    assert "filed as trial #1 of btc-4h" in evaluated
    assert "holdout: withheld (not promoted)" in evaluated
    assert "promote threshold at 1 trial(s)" in evaluated
    assert "eligible: `promote` would measure its holdout" in evaluated

    assert main(["evaluate", *experiment, "--spec", str(buy), *store]) == 0
    assert "already measured in btc-4h as trial #1; nothing was measured" in capsys.readouterr().out

    # ``report`` renders the measurement ``evaluate`` printed, from the ledger alone.
    assert main(["report", *experiment, "--trial", "1", *store]) == 0
    reported = capsys.readouterr().out.splitlines()
    prefixes = ("train:", "validation:", "  gross", "  net", "  costs", "  by regime", "split (")
    measured = [line for line in evaluated.splitlines() if line.startswith(prefixes)]
    assert len(measured) >= 9 and all(line in reported for line in measured)

    assert main(["evaluate", *experiment, "--spec", str(flat), *store]) == 0
    assert "not eligible: it made no trades in validation" in capsys.readouterr().out
    assert main(["promote", *experiment, "--trial", "2", *store]) == 1
    assert "cannot be promoted" in capsys.readouterr().err

    assert main(["promote", *experiment, "--trial", "1", *store]) == 0
    promoted = capsys.readouterr().out
    assert "promoted trial #1" in promoted
    assert "withheld" not in promoted and "\nholdout: " in promoted

    assert main(["report", *experiment, *store]) == 0
    table = capsys.readouterr().out
    assert "promote threshold at 2 trial(s)" in table
    assert "by family: breakout 2" in table
    assert "(promoted)" in table

    assert main(["report", *store]) == 0
    assert "btc-4h: BTC 4h, 2 trial(s), 1 promoted" in capsys.readouterr().out

    assert main(["calibrate", *experiment, *store]) == 0
    calibrated = capsys.readouterr().out
    assert "high_turnover_noise validation:" in calibrated
    assert "baselines are not trials" in calibrated
    assert main(["report", *experiment, *store]) == 0
    assert "promote threshold at 2 trial(s)" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["experiment", "--name", "x"], "holds no 4h bars"),
        (["report", "--trial", "1"], "give --experiment too"),
        (["report", "--experiment", "nope"], "no experiment named 'nope'"),
        (["evaluate", "--experiment", "nope", "--spec", "absent.json"], "cannot read --spec"),
    ],
    ids=["an empty store", "a trial with no experiment", "an unknown experiment", "an unreadable spec"],
)
def test_a_refusal_is_named_and_exits_one(tmp_path, capsys, argv, message):
    assert main([*argv, "--db", str(tmp_path / "autoresearch.sqlite")]) == 1
    assert message in capsys.readouterr().err


def test_a_defect_is_not_dressed_as_a_named_refusal(tmp_path, monkeypatch):
    """Only the measurement refusals ride the exit-1 lane; any other RuntimeError is a bug."""
    from contrib.autoresearch import cli

    def broken(_args):
        raise RuntimeError("an actual defect")

    monkeypatch.setitem(cli._COMMANDS, "report", broken)
    with pytest.raises(RuntimeError, match="an actual defect"):
        main(["report", "--db", str(tmp_path / "autoresearch.sqlite")])
