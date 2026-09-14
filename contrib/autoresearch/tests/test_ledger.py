"""The ledger: experiments written once, one trial per rule, a threshold that rises, a holdout seen once.

Every figure here is fabricated on purpose. What is under test is what the
ledger does with a measurement — file it, count it, gate it, lock it, read it
back — and none of that depends on a measurement being real; the end-to-end
tests in ``test_research.py`` pay for real ones.
"""

from __future__ import annotations

import dataclasses
import json
import math
import sqlite3

import pytest

from contrib.autoresearch.costs import CostModel, FillRole
from contrib.autoresearch.dsl import parse_spec
from contrib.autoresearch.ledger import (
    PENALTY_K,
    SHARPE_BASE,
    Experiment,
    Ledger,
    LedgerError,
    Penalty,
    TrialStatus,
    promotion_verdict,
)
from contrib.autoresearch.metrics import RegimeBucket, SegmentMetrics, Tally
from contrib.autoresearch.schema import MIGRATIONS, SCHEMA_VERSION, SCHEMA_VERSION_DDL
from contrib.autoresearch.split import Segment, SegmentName, Split
from contrib.autoresearch.store import ResearchStore

from .conftest import ANCHOR_MS

_STEP = 4 * 60 * 60_000


def _split(offset_bars: int = 0, bars: int = 100) -> Split:
    return Split.by_shares(
        "4h",
        start_ms=ANCHOR_MS + offset_bars * _STEP,
        end_ms=ANCHOR_MS + (offset_bars + bars) * _STEP,
    )


def _experiment(name: str = "btc-4h", **overrides) -> Experiment:
    body = {
        "experiment_id": name,
        "coin": "btc",
        "costs": CostModel(),
        "split": _split(),
        "indicator_lookback": 200,
    }
    body.update(overrides)
    return Experiment(**body)


def _metrics(
    segment: Segment,
    *,
    sharpe: float = 2.0,
    total: float = 0.1,
    trades: int = 4,
    ruined: bool = False,
) -> SegmentMetrics:
    tally = Tally(total_return=total, sharpe=sharpe, max_drawdown=0.05, hit_rate=0.5)
    return SegmentMetrics(
        segment=segment,
        bars=20,
        bars_per_year=2190.0,
        gross=tally,
        net=tally,
        trades=trades,
        exposure=0.5,
        turnover=2.0,
        fees_paid=0.001,
        slippage_paid=0.001,
        funding_paid=-0.0002,
        bars_unevaluable=1,
        bars_conflicting=0,
        funding_settlements_missing=0,
        regime_buckets=(RegimeBucket("trending", 12, 0.07), RegimeBucket("unlabelled", 8, 0.03)),
        ruined=ruined,
    )


def _spec(threshold: float = 30, family: str = "breakout"):
    return parse_spec(
        {
            "family": family,
            "entry": {"long": [{"left": "rsi_14", "op": "<", "right": threshold}]},
            "sizing": {"mode": "fixed_margin_fraction", "fraction": 0.5},
        }
    )


def _file(ledger: Ledger, experiment: Experiment, spec=None, **figures):
    return ledger.record_trial(
        experiment,
        spec or _spec(),
        _metrics(experiment.split.train, **figures),
        _metrics(experiment.split.validation, **figures),
    )


@pytest.fixture
def ledger(store):
    return Ledger(store)


# -- the store -------------------------------------------------------------------


def test_a_v1_store_migrates_forward_and_keeps_its_history(tmp_path):
    path = tmp_path / "written-by-a3.sqlite"
    conn = sqlite3.connect(path)
    conn.execute(SCHEMA_VERSION_DDL)
    for statement in MIGRATIONS[1]:
        conn.execute(statement)
    conn.execute("INSERT INTO schema_version VALUES (1, '2026-09-12T00:00:00+00:00')")
    conn.execute(
        "INSERT INTO candles VALUES ('BTC', '4h', ?, ?, '100', '101', '99', '100', '1.5')",
        (ANCHOR_MS, ANCHOR_MS + _STEP),
    )
    conn.commit()
    conn.close()
    with ResearchStore(path) as opened:
        assert opened.version == SCHEMA_VERSION == 2
        assert opened.count_candles("BTC", "4h") == 1
        tables = {
            row[0]
            for row in opened.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"experiments", "trials"} <= tables
        assert Ledger(opened).experiments() == []


# -- experiments ---------------------------------------------------------------


def test_an_experiment_reads_back_as_the_conditions_it_was_written_with(ledger):
    written = ledger.create_experiment(
        _experiment(
            costs=CostModel(fill_role=FillRole.MAKER, slippage_bps=2),
            penalty=Penalty(sharpe_base=0.5, k=0.1),
            indicator_lookback=120,
            notes="maker lane, run 5",
        )
    )
    assert written.created_at and written.coin == "BTC"
    assert ledger.experiment("btc-4h") == written
    assert ledger.experiments() == [written]


def test_an_experiment_is_written_once(ledger):
    ledger.create_experiment(_experiment())
    with pytest.raises(LedgerError, match="already has an experiment named 'btc-4h'"):
        ledger.create_experiment(_experiment(split=_split(bars=120)))


def test_an_unknown_experiment_is_named(ledger):
    with pytest.raises(LedgerError, match="no experiment named 'nope'"):
        ledger.experiment("nope")


@pytest.mark.parametrize("name", ["", "has space", "-leading", "x" * 65, 7])
def test_an_experiment_name_is_one_a_command_line_can_carry(name):
    with pytest.raises(ValueError, match="an experiment is named"):
        _experiment(name)


def test_every_experiment_on_a_coin_withholds_the_same_window(ledger):
    """Plan §11: a split cut over a grown store would slide validation over the old holdout."""
    first = ledger.create_experiment(_experiment("first"))
    start = first.split.holdout.start_ms
    assert ledger.holdout_pin("btc") == start
    grown = _split(offset_bars=10)  # the same shares over a later span
    assert grown.holdout.start_ms > start
    with pytest.raises(LedgerError, match="pinned by the first experiment"):
        ledger.create_experiment(_experiment("later", split=grown))
    earlier = _split(offset_bars=-10)
    assert earlier.holdout.start_ms < start
    with pytest.raises(LedgerError, match="pinned by the first experiment"):
        ledger.create_experiment(_experiment("earlier", split=earlier))
    # The same start, a holdout that has grown at its far end: accepted.
    longer = dataclasses.replace(
        first.split,
        holdout=Segment(SegmentName.HOLDOUT, start, first.split.holdout.end_ms + 10 * _STEP),
    )
    ledger.create_experiment(_experiment("longer", split=longer))
    # Another coin is another calendar.
    ledger.create_experiment(_experiment("eth", coin="ETH", split=grown))
    assert ledger.holdout_pin("ETH") == grown.holdout.start_ms


# -- trials ------------------------------------------------------------------------


def test_a_trial_reads_back_as_the_spec_and_the_figures_that_were_filed(ledger):
    experiment = ledger.create_experiment(_experiment())
    trial = _file(ledger, experiment)
    assert trial.trial_id == 1
    assert trial.status is TrialStatus.MEASURED
    assert (trial.holdout, trial.promoted_at) == (None, None)
    assert trial.spec == _spec() and trial.family == "breakout"
    assert trial.train == _metrics(experiment.split.train)
    assert trial.validation == _metrics(experiment.split.validation)
    assert ledger.trials("btc-4h") == [trial]
    assert ledger.count_trials("btc-4h") == 1


def test_the_same_rule_is_one_trial_whatever_it_is_labelled(ledger):
    experiment = ledger.create_experiment(_experiment())
    first = _file(ledger, experiment)
    with pytest.raises(LedgerError, match=r"already measured in btc-4h as trial #1"):
        _file(ledger, experiment, _spec(family="mean_reversion"))
    assert ledger.count_trials("btc-4h") == 1
    assert ledger.trial_by_hash("btc-4h", first.spec_hash) == first
    # Another experiment is another set of looks.
    other = ledger.create_experiment(_experiment("again"))
    assert _file(ledger, other).trial_id == 2


def test_figures_for_another_window_are_refused(ledger):
    experiment = ledger.create_experiment(_experiment())
    with pytest.raises(LedgerError, match="the experiment's own windows"):
        ledger.record_trial(
            experiment,
            _spec(),
            _metrics(experiment.split.validation),
            _metrics(experiment.split.validation),
        )


def test_an_unknown_trial_is_named(ledger):
    ledger.create_experiment(_experiment())
    with pytest.raises(LedgerError, match="has no trial #9"):
        ledger.trial("btc-4h", 9)


# -- the penalty and the gate (plan §3.10, §6.7) ---------------------------------------


def test_the_threshold_rises_with_the_log_of_the_trial_count():
    penalty = Penalty()
    thresholds = [penalty.threshold(n) for n in range(1, 21)]
    assert thresholds[0] == SHARPE_BASE
    assert thresholds == pytest.approx(
        [SHARPE_BASE + PENALTY_K * math.log(n) for n in range(1, 21)]
    )
    assert all(a < b for a, b in zip(thresholds, thresholds[1:], strict=False))
    with pytest.raises(ValueError, match="at least one trial"):
        penalty.threshold(0)


def test_a_trial_that_clears_the_bar_alone_does_not_clear_it_after_more_were_tried(ledger):
    """``n`` is the experiment's count at promote time, not the trial's ordinal."""
    experiment = ledger.create_experiment(_experiment())
    trial = _file(ledger, experiment, sharpe=1.2)
    assert promotion_verdict(experiment, trial, ledger.count_trials("btc-4h")).eligible
    for threshold in (40, 50):
        _file(ledger, experiment, _spec(threshold), sharpe=0.1)
    verdict = promotion_verdict(experiment, trial, ledger.count_trials("btc-4h"))
    assert verdict.trials == 3
    assert verdict.threshold == pytest.approx(1 + 0.25 * math.log(3))
    assert verdict.blockers == (
        "its validation net sharpe 1.20 is below 1.27 (1 + 0.25 × ln 3)",
    )


def test_the_gate_lists_every_blocker(ledger):
    experiment = ledger.create_experiment(_experiment())
    trial = _file(ledger, experiment, sharpe=-0.5, total=-0.2, trades=0, ruined=True)
    blockers = promotion_verdict(experiment, trial, 1).blockers
    assert len(blockers) == 4
    text = "\n".join(blockers)
    for fragment in ("ruined", "no trades", "not positive", "below 1.00"):
        assert fragment in text


def test_a_lowered_bar_still_does_not_promote_a_rule_that_never_traded(ledger):
    """A Sharpe of 0 is neither "did nothing" nor "did well": the return and the trades are read too."""
    experiment = ledger.create_experiment(_experiment(penalty=Penalty(sharpe_base=-5, k=0)))
    trial = _file(ledger, experiment, sharpe=0.0, total=0.0, trades=0)
    verdict = promotion_verdict(experiment, trial, 1)
    assert not verdict.eligible
    assert len(verdict.blockers) == 2


@pytest.mark.parametrize(
    ("penalty", "message"),
    [
        (lambda: Penalty(k=-0.1), "Penalty.k must be a number >= 0"),
        (lambda: Penalty(sharpe_base=float("nan")), "Penalty.sharpe_base"),
        (lambda: Penalty.from_dict({"sharpe_base": 1.0}), "exactly the keys"),
    ],
    ids=["a k that lowers the bar", "a base that is not a number", "a record that lost a key"],
)
def test_a_penalty_that_cannot_mean_what_it_says_is_refused(penalty, message):
    with pytest.raises(ValueError, match=message):
        penalty()


# -- the holdout ----------------------------------------------------------------------


def test_a_trial_is_promoted_once(ledger):
    experiment = ledger.create_experiment(_experiment())
    trial = _file(ledger, experiment)
    holdout = _metrics(experiment.split.holdout, sharpe=0.7)
    promoted = ledger.promote(experiment, trial, holdout)
    assert promoted.status is TrialStatus.PROMOTED
    assert promoted.holdout == holdout and promoted.promoted_at
    assert promoted.segments == (trial.train, trial.validation, holdout)
    with pytest.raises(LedgerError, match="sees its holdout once"):
        ledger.promote(experiment, promoted, holdout)
    assert promotion_verdict(experiment, promoted, 1).blockers[0] == (
        "trial #1 has already been promoted"
    )


def test_holdout_figures_for_another_window_are_refused(ledger):
    experiment = ledger.create_experiment(_experiment())
    trial = _file(ledger, experiment)
    with pytest.raises(LedgerError, match="the experiment's own windows"):
        ledger.promote(experiment, trial, _metrics(experiment.split.validation))
    assert ledger.trial("btc-4h", trial.trial_id).status is TrialStatus.MEASURED


def test_the_store_itself_refuses_holdout_figures_on_a_trial_that_was_not_promoted(ledger, store):
    """The lock as a constraint, so a writer that is not this module cannot half-promote a row."""
    experiment = ledger.create_experiment(_experiment())
    _file(ledger, experiment)
    for statement in (
        "UPDATE trials SET holdout_metrics_json = '{}' WHERE trial_id = 1",
        "UPDATE trials SET status = 'promoted' WHERE trial_id = 1",
        "UPDATE trials SET status = 'promoted', holdout_metrics_json = '{}' WHERE trial_id = 1",
    ):
        with pytest.raises(sqlite3.IntegrityError):
            store.conn.execute(statement)


# -- reading rows back ------------------------------------------------------------


def test_a_row_this_build_cannot_read_is_named_not_rendered(ledger, store):
    experiment = ledger.create_experiment(_experiment())
    _file(ledger, experiment)
    _file(ledger, experiment, _spec(40))
    unknown_feature = {
        "family": "breakout",
        "entry": {"long": [{"left": "ema_9", "op": ">", "right": 1}]},
        "sizing": {"mode": "fixed_margin_fraction", "fraction": 0.5},
    }
    store.conn.execute(
        "UPDATE trials SET spec_json = ? WHERE trial_id = 1", (json.dumps(unknown_feature),)
    )
    with pytest.raises(
        LedgerError, match=r"trial #1 of btc-4h cannot be read .*'ema' has no period '9'"
    ):
        ledger.trials("btc-4h")
    record = _metrics(experiment.split.validation).to_dict()
    del record["ruined"]
    store.conn.execute(
        "UPDATE trials SET validation_metrics_json = ? WHERE trial_id = 2", (json.dumps(record),)
    )
    with pytest.raises(
        LedgerError, match=r"trial #2 of btc-4h .*metrics: expected exactly the keys"
    ):
        ledger.trial("btc-4h", 2)


def test_metrics_round_trip_and_refuse_a_record_that_does_not_say_what_it_should():
    """A lost ``ruined`` read back as False would pass the one trial the gate exists to stop."""
    metrics = _metrics(_split().validation)
    assert SegmentMetrics.from_dict(json.loads(json.dumps(metrics.to_dict()))) == metrics
    for field, bad in (
        ("ruined", 0),
        ("trades", True),
        ("trades", -1),
        ("exposure", "0.5"),
        ("bars_per_year", float("inf")),
    ):
        record = metrics.to_dict()
        record[field] = bad
        with pytest.raises(ValueError, match=f"metrics.{field}"):
            SegmentMetrics.from_dict(record)
    record = metrics.to_dict()
    record["net"]["extra"] = 1
    with pytest.raises(ValueError, match="metrics.net"):
        SegmentMetrics.from_dict(record)
    record = metrics.to_dict()
    record["segment"]["name"] = "test"
    with pytest.raises(ValueError, match="metrics.segment.name"):
        SegmentMetrics.from_dict(record)
