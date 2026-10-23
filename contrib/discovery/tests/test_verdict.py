"""The final verdict is the ledger's own gate at the final count, snapshotted before promotion (plan §4.5).

Two kinds of test. The ones on a store run the research package's real
``search()`` with a scripted hypothesist and compare the verdict with what
``Ledger.verdict`` says of every trial — acceptance (d) — then promote the
selected trial and rebuild the verdict from the persisted snapshot —
acceptance (e). The hand-built ones hold the pure function's own rules
(the selection order, the empty case, the refusals) without a store.
"""

from __future__ import annotations

import json

import pytest

from contrib.autoresearch.costs import CostModel
from contrib.autoresearch.dsl import parse_spec, spec_hash
from contrib.autoresearch.hypothesis import SearchReport, search
from contrib.autoresearch.ledger import Experiment, Ledger, Penalty
from contrib.autoresearch.metrics import RegimeBucket, SegmentMetrics, Tally
from contrib.autoresearch.research import promote
from contrib.autoresearch.split import Segment, Split
from contrib.discovery.oracle import evaluate_unfiled
from contrib.discovery.verdict import (
    ArmSnapshot,
    ArmVerdict,
    Ended,
    TrialRecord,
    VerdictError,
    classify_ending,
    final_verdict,
    snapshot_arm,
)

from .conftest import (
    BUY,
    BUY_12,
    COIN,
    FLAT,
    SHORT,
    START_MS,
    STEP_MS,
    InterruptingHypothesist,
    ScriptedHypothesist,
    arm_store,
    open_arm,
)


def _json(document: dict) -> str:
    return json.dumps(document)


def _run(ledger, answers, *, penalty: Penalty, max_trials: int, runs_out: bool = False):
    experiment = open_arm(ledger, penalty=penalty)
    hypothesist = ScriptedHypothesist(list(answers), runs_out=runs_out)
    report = search(ledger, experiment, hypothesist, model="scripted", max_trials=max_trials)
    return experiment, hypothesist, report


@pytest.fixture(scope="module")
def opened(history, tmp_path_factory):
    """An arm with its experiment opened and nothing filed, shared by the tests that write nothing.

    Opening an experiment is the costly part of a fresh arm (the indicator
    walk); the tests on this fixture only read — the oracle path, a search
    that stops before it answers, a refused report.
    """
    with arm_store(history, tmp_path_factory.mktemp("opened") / "opened.sqlite") as store:
        ledger = Ledger(store)
        yield ledger, open_arm(ledger)


@pytest.fixture(scope="module")
def buy_sharpe(opened) -> float:
    """Buy-and-hold's validation net Sharpe on this world, read through the oracle path.

    The penalties below are set relative to it, so a test can put the
    first trial on either side of the threshold on purpose rather than
    by luck of the walk.
    """
    ledger, experiment = opened
    return evaluate_unfiled(ledger, experiment, parse_spec(BUY), holdout=False).validation.net.sharpe


@pytest.fixture(scope="module")
def one_buy(history, tmp_path_factory):
    """One search of one answer, spent in full; shared by the tests that only read it back."""
    with arm_store(history, tmp_path_factory.mktemp("one_buy") / "one_buy.sqlite") as store:
        ledger = Ledger(store)
        experiment, _hypothesist, report = _run(
            ledger, [_json(BUY)], penalty=Penalty(), max_trials=1
        )
        yield ledger, experiment, report


# -- on a store: acceptance (d) and (e) ------------------------------------------------


def test_the_verdict_is_the_ledger_s_gate_at_the_final_rule_count(arm, buy_sharpe):
    # A bar every profitable rule clears, so the gate's other blockers (no
    # trades, no gain) are what decide: the flat rule and the short rule on
    # a rising walk must be out, buy-and-hold in.
    penalty = Penalty(sharpe_base=buy_sharpe - 2.0, k=0.0)
    answers = [_json(BUY), "not a rule", _json(FLAT), _json(BUY), _json(SHORT), _json(BUY_12)]
    experiment, hypothesist, report = _run(arm, answers, penalty=penalty, max_trials=6)
    snapshot = snapshot_arm(arm, experiment, report, exhausted=hypothesist.exhausted)
    verdict = final_verdict(snapshot)

    assert snapshot.ended is Ended.BUDGET_SPENT
    assert snapshot.answers == 6  # a refusal and a duplicate both spent a round
    assert snapshot.distinct_rules == 4
    trials = arm.trials(experiment.experiment_id)
    by_rule = {trial.spec_hash: trial for trial in trials}
    expected = {trial.trial_id for trial in trials if arm.verdict(experiment, trial).eligible}
    assert set(verdict.final_eligible) == expected
    assert by_rule[spec_hash(parse_spec(BUY))].trial_id in expected
    assert by_rule[spec_hash(parse_spec(FLAT))].trial_id not in expected
    assert by_rule[spec_hash(parse_spec(SHORT))].trial_id not in expected
    assert verdict.threshold == experiment.penalty.threshold(4)
    # The selection is the fixed order: validation net Sharpe down, trial id up.
    best = max(
        expected,
        key=lambda i: (arm.trial(experiment.experiment_id, i).validation.net.sharpe, -i),
    )
    assert verdict.selected == best == verdict.final_eligible[0]
    assert verdict.found


def test_a_rule_that_passed_in_round_one_can_fail_the_final_count(arm, buy_sharpe):
    # threshold(1) sits just under buy-and-hold's Sharpe; threshold(3) a
    # whole ln 3 above it. The round-one pass is kept as a diagnostic and
    # is not a success (plan §1 #8).
    penalty = Penalty(sharpe_base=buy_sharpe - 0.01, k=1.0)
    experiment, hypothesist, report = _run(
        arm, [_json(BUY), _json(FLAT), _json(SHORT)], penalty=penalty, max_trials=3
    )
    snapshot = snapshot_arm(arm, experiment, report, exhausted=hypothesist.exhausted)
    verdict = final_verdict(snapshot)

    assert report.rounds[0].measurement.verdict.eligible
    assert snapshot.first_eligible_round == 1
    assert verdict.final_eligible == () and verdict.selected is None and not verdict.found
    buy = arm.trial(experiment.experiment_id, 1)
    assert not arm.verdict(experiment, buy).eligible


def test_the_verdict_rebuilt_from_the_snapshot_is_unchanged_by_the_promotion(arm, buy_sharpe):
    penalty = Penalty(sharpe_base=buy_sharpe - 2.0, k=0.0)
    experiment, hypothesist, report = _run(
        arm, [_json(BUY), _json(BUY_12), _json(FLAT)], penalty=penalty, max_trials=3
    )
    snapshot = snapshot_arm(arm, experiment, report, exhausted=hypothesist.exhausted)
    verdict = final_verdict(snapshot)
    text = snapshot.dumps()  # what verdicts.json holds

    promoted = promote(arm, experiment, verdict.selected)
    assert promoted.holdout is not None

    # The ledger now refuses the trial it chose — the reason the snapshot
    # exists — while the record read back from disk gives the same verdict.
    blockers = arm.verdict(experiment, promoted).blockers
    assert any("already been promoted" in blocker for blocker in blockers)
    rebuilt = ArmSnapshot.loads(text)
    assert rebuilt == snapshot
    assert final_verdict(rebuilt) == verdict
    # And a snapshot is not re-taken after a promotion: nothing is re-asked.
    with pytest.raises(VerdictError, match="already promoted"):
        snapshot_arm(arm, experiment, report, exhausted=False)


def test_a_search_that_filed_nothing_is_an_empty_verdict_and_asks_no_threshold(arm):
    experiment, hypothesist, report = _run(
        arm, ["prose", "more prose"], penalty=Penalty(), max_trials=2
    )
    snapshot = snapshot_arm(arm, experiment, report, exhausted=hypothesist.exhausted)
    verdict = final_verdict(snapshot)
    assert snapshot.trials == () and snapshot.answers == 2 and snapshot.distinct_rules == 0
    assert verdict == ArmVerdict(
        ended=Ended.BUDGET_SPENT,
        answers=2,
        distinct_rules=0,
        threshold=None,
        final_eligible=(),
        first_eligible_round=None,
    )
    assert verdict.selected is None and not verdict.found
    # The research package's threshold is for a count of at least one.
    with pytest.raises(ValueError):
        experiment.penalty.threshold(0)


# -- how a search ended ----------------------------------------------------------------


def test_a_pool_walked_to_its_end_is_exhausted_not_a_seam_failure(arm):
    experiment, hypothesist, report = _run(
        arm, [_json(BUY)], penalty=Penalty(), max_trials=3, runs_out=True
    )
    assert report.stopped is not None and hypothesist.exhausted
    snapshot = snapshot_arm(arm, experiment, report, exhausted=hypothesist.exhausted)
    assert snapshot.ended is Ended.EXHAUSTED and snapshot.answers == 1


def test_a_seam_that_failed_is_not_exhausted(opened):
    ledger, experiment = opened
    hypothesist = ScriptedHypothesist([])
    report = search(ledger, experiment, hypothesist, model="scripted", max_trials=3)
    snapshot = snapshot_arm(ledger, experiment, report, exhausted=hypothesist.exhausted)
    assert snapshot.ended is Ended.SEAM_FAILED and snapshot.answers == 0


def test_an_interrupt_is_its_own_ending(opened):
    ledger, experiment = opened
    report = search(ledger, experiment, InterruptingHypothesist(), model="scripted", max_trials=3)
    assert snapshot_arm(ledger, experiment, report, exhausted=False).ended is Ended.INTERRUPTED


def test_an_exhausted_flag_against_a_spent_budget_is_a_contradiction(one_buy):
    ledger, experiment, report = one_buy
    assert classify_ending(report, exhausted=False) is Ended.BUDGET_SPENT
    with pytest.raises(VerdictError, match="spent all 1 round"):
        snapshot_arm(ledger, experiment, report, exhausted=True)


def test_a_report_that_stopped_short_without_saying_why_is_refused():
    # ``search()`` never returns one; a report built by hand can.
    report = SearchReport(experiment_id="arm", max_trials=2, rounds=())
    with pytest.raises(VerdictError, match="spent 0 of 2"):
        classify_ending(report, exhausted=False)


def test_a_report_for_another_experiment_is_refused(opened):
    ledger, experiment = opened
    other = SearchReport(experiment_id="other", max_trials=1, rounds=())
    with pytest.raises(VerdictError, match="the report is for other"):
        snapshot_arm(ledger, experiment, other, exhausted=False)


def test_a_report_that_does_not_account_for_the_ledger_s_answers_is_refused(one_buy):
    ledger, experiment, report = one_buy
    # A second invocation's report would number its rounds from one again.
    stale = SearchReport(experiment_id="arm", max_trials=1, rounds=(), stopped="the seam failed")
    with pytest.raises(VerdictError, match="spent 0 round.*holds 1 answer"):
        snapshot_arm(ledger, experiment, stale, exhausted=False)
    assert snapshot_arm(ledger, experiment, report, exhausted=False).answers == 1


# -- hand-built: the pure function's own rules ---------------------------------------------


def _experiment(**overrides) -> Experiment:
    body = {
        "experiment_id": "arm",
        "coin": COIN,
        "costs": CostModel(),
        "split": Split.by_shares("4h", start_ms=START_MS, end_ms=START_MS + 100 * STEP_MS),
        "indicator_lookback": 200,
        "penalty": Penalty(sharpe_base=1.0, k=0.25),
    }
    body.update(overrides)
    return Experiment(**body)


def _metrics(
    segment: Segment, *, sharpe: float, total: float = 0.1, trades: int = 4
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
        bars_unevaluable=0,
        bars_conflicting=0,
        funding_settlements_missing=0,
        regime_buckets=(RegimeBucket("trending", 20, 0.1),),
        ruined=False,
    )


def _record(
    experiment: Experiment, trial_id: int, *, sharpe: float, threshold: float = 30
) -> TrialRecord:
    spec = parse_spec(
        {
            "family": "mean_reversion",
            "entry": {"long": [{"left": "rsi_14", "op": "<", "right": threshold}]},
            "sizing": {"mode": "fixed_margin_fraction", "fraction": 0.5},
        }
    )
    return TrialRecord(
        trial_id=trial_id,
        spec=spec,
        train=_metrics(experiment.split.train, sharpe=sharpe),
        validation=_metrics(experiment.split.validation, sharpe=sharpe),
    )


def _snapshot(experiment: Experiment, *records: TrialRecord, distinct_rules: int | None = None):
    return ArmSnapshot(
        experiment=experiment,
        ended=Ended.BUDGET_SPENT,
        answers=len(records),
        distinct_rules=len(records) if distinct_rules is None else distinct_rules,
        first_eligible_round=None,
        trials=records,
    )


def test_the_selection_ranks_by_validation_sharpe_then_by_trial_id():
    experiment = _experiment()
    snapshot = _snapshot(
        experiment,
        _record(experiment, 1, sharpe=2.0, threshold=30),
        _record(experiment, 2, sharpe=3.0, threshold=25),
        _record(experiment, 3, sharpe=3.0, threshold=20),
        _record(experiment, 4, sharpe=1.0, threshold=35),  # below 1 + 0.25 ln 4
    )
    verdict = final_verdict(snapshot)
    assert verdict.final_eligible == (2, 3, 1) and verdict.selected == 2
    assert verdict.threshold == pytest.approx(experiment.penalty.threshold(4))


def test_the_count_the_gate_divides_by_is_the_snapshot_s_not_the_record_count():
    # Another experiment on the coin raised the bar for every trial; the
    # ledger's ``rules_tried`` counts it and the snapshot carries that.
    experiment = _experiment()
    tight = _snapshot(experiment, _record(experiment, 1, sharpe=1.2), distinct_rules=5)
    assert final_verdict(tight).final_eligible == ()  # 1.2 < 1 + 0.25 ln 5
    loose = _snapshot(experiment, _record(experiment, 1, sharpe=1.2))
    assert final_verdict(loose).final_eligible == (1,)  # 1.2 >= 1 + 0.25 ln 1
    # Nothing filed here, but rules tried elsewhere on the coin: the
    # threshold they set is still a fact of the arm.
    empty = _snapshot(experiment, distinct_rules=3)
    assert final_verdict(empty).threshold == pytest.approx(experiment.penalty.threshold(3))


def test_a_snapshot_round_trips_through_its_record():
    experiment = _experiment()
    snapshot = _snapshot(experiment, _record(experiment, 1, sharpe=2.0))
    assert ArmSnapshot.from_dict(json.loads(snapshot.dumps())) == snapshot


@pytest.mark.parametrize(
    ("edit", "fragment"),
    [
        (lambda r: r.__setitem__("distinct_rules", 0), "below the 1 distinct"),
        (lambda r: r.__setitem__("ended", "gave_up"), "snapshot.ended"),
        (lambda r: r.__setitem__("extra", 1), "snapshot: expected exactly the keys"),
        (lambda r: r.__setitem__("answers", -1), "snapshot.answers"),
        (lambda r: r["trials"][0].__setitem__("spec_hash", "0" * 64), "not the hash of its spec"),
        (lambda r: r["trials"][0]["spec"].__setitem__("family", "momentum"), "trial:"),
        (lambda r: r["trials"][0].__setitem__("trial_id", -1), "trial.trial_id"),
    ],
)
def test_a_record_that_cannot_be_read_is_refused_naming_where(edit, fragment):
    experiment = _experiment()
    record = _snapshot(experiment, _record(experiment, 1, sharpe=2.0)).to_dict()
    edit(record)
    with pytest.raises(VerdictError, match=fragment):
        ArmSnapshot.from_dict(record)


def test_a_bad_ending_built_directly_is_refused_on_the_same_lane_as_one_read():
    experiment = _experiment()
    with pytest.raises(VerdictError, match="snapshot.ended"):
        ArmSnapshot(
            experiment=experiment,
            ended="gave_up",  # type: ignore[arg-type]
            answers=0,
            distinct_rules=0,
            first_eligible_round=None,
            trials=(),
        )


def test_a_snapshot_lists_each_rule_once():
    experiment = _experiment()
    with pytest.raises(VerdictError, match="each rule once"):
        _snapshot(
            experiment,
            _record(experiment, 1, sharpe=2.0),
            _record(experiment, 2, sharpe=2.0),
        )
