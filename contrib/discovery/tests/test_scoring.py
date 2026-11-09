"""The scorer (plan §4.6, PR 4): gold figures for the three readings, the record's rules, one real world scored.

The readings are pure and tested on hand-written trades and specs, where
the right answer can be worked out by eye. The one real world is the
suite's ``searched`` fixture, copied once and scored once (``scored``), so
the record, the re-run, the untouched store and the table read off one
scoring.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from contrib.autoresearch.dsl import Side, parse_spec
from contrib.autoresearch.evaluator import ExitReason, Trade
from contrib.autoresearch.ledger import Ledger
from contrib.autoresearch.store import ResearchStore
from contrib.discovery import scoring
from contrib.discovery.bench import (
    ARMS_DIRNAME,
    Arm,
    BenchError,
    Searched,
    read_searched,
    store_path,
)
from contrib.discovery.landing import artifact_digest
from contrib.discovery.manifest import Recovery, Scoring, SeedSet, WorldKind
from contrib.discovery.scoring import (
    SCORES_FILENAME,
    ArmScore,
    HoldoutRead,
    ReportEntry,
    ReportRow,
    ScoreError,
    Scores,
    direction_agreement,
    positions,
    read_scores,
    report_table,
    score_records,
    score_world,
    structural_distance,
)
from contrib.discovery.verdict import Ended

from .conftest import PLANTED, SIZING, bench_manifest, land_run

RECOVERY = Recovery(theta_behavior=0.8, require_positive_holdout=True)


# -- positions ------------------------------------------------------------------------------------


def _trade(side: Side, entry: int, exit_: int, reason: ExitReason) -> Trade:
    return Trade(
        side=side,
        entry_index=entry,
        exit_index=exit_,
        entry_price=100.0,
        exit_price=101.0,
        size=1.0,
        notional=100.0,
        fees=0.0,
        slippage=0.0,
        funding=0.0,
        exit_reason=reason,
    )


@pytest.mark.parametrize(
    ("reason", "held"),
    [
        (ExitReason.MAX_BARS, (0, 1, 1, 1, 0, 0)),  # fills at bar 14's open: bar 14 is not held
        (ExitReason.EXIT_RULE, (0, 1, 1, 1, 0, 0)),
        (ExitReason.SEGMENT_END, (0, 1, 1, 1, 1, 0)),  # fills at bar 14's close: bar 14 is held
        (ExitReason.RUIN, (0, 1, 1, 1, 1, 0)),  # as segment_end; flat from the next bar on
    ],
    ids=lambda value: value.value if isinstance(value, ExitReason) else None,
)
def test_a_trade_holds_the_bars_its_exit_reason_says(reason, held):
    trade = _trade(Side.LONG, 11, 14, reason)
    assert positions([trade], first=10, count=6) == held
    assert sum(held) == trade.bars_held


def test_a_short_is_minus_one_and_a_reversal_s_exit_bar_belongs_to_the_new_position():
    trades = [
        _trade(Side.LONG, 10, 13, ExitReason.REVERSAL),
        _trade(Side.SHORT, 13, 15, ExitReason.MAX_BARS),
    ]
    assert positions(trades, first=10, count=6) == (1, 1, 1, -1, -1, 0)


def test_a_ruin_on_the_entry_bar_still_held_that_bar():
    assert positions([_trade(Side.LONG, 12, 12, ExitReason.RUIN)], first=10, count=4) == (
        0,
        0,
        1,
        0,
    )


def test_trades_the_evaluator_would_not_write_are_refused():
    with pytest.raises(ScoreError, match="two trades hold bar 12"):
        positions(
            [
                _trade(Side.LONG, 10, 13, ExitReason.MAX_BARS),
                _trade(Side.SHORT, 12, 14, ExitReason.MAX_BARS),
            ],
            first=10,
            count=6,
        )
    with pytest.raises(ScoreError, match="outside the window"):
        positions([_trade(Side.LONG, 9, 11, ExitReason.MAX_BARS)], first=10, count=6)
    with pytest.raises(ScoreError, match="outside the window"):
        positions([_trade(Side.LONG, 15, 17, ExitReason.MAX_BARS)], first=10, count=6)


# -- direction agreement ----------------------------------------------------------------------------


def test_agreement_counts_only_the_bars_at_least_one_rule_holds():
    selected = (1, 1, 0, 0, -1)
    planted = (1, 0, 0, 0, 1)
    # Bars 0, 1 and 4 are compared; they agree on bar 0 alone. Over every
    # bar it would read 3/5, the both-flat bars agreeing by doing nothing.
    assert direction_agreement(selected, planted) == (pytest.approx(1 / 3), 3)


def test_opposite_positions_disagree_and_identical_ones_agree():
    assert direction_agreement((1, 1), (-1, -1)) == (0.0, 2)
    assert direction_agreement((1, -1, 0), (1, -1, 0)) == (1.0, 2)


def test_agreement_is_undefined_when_neither_rule_holds_a_position():
    assert direction_agreement((0, 0), (0, 0)) == (None, 0)


def test_agreement_refuses_positions_over_different_bars():
    with pytest.raises(ScoreError, match="positions over 2 and 3 bars"):
        direction_agreement((1, 1), (1, 1, 1))


# -- structural distance ---------------------------------------------------------------------------


def _spec(*, entry, filters=(), side="long", extra_side=None):
    document = {
        "family": "breakout",
        "entry": {side: list(entry)},
        "exit": {"max_bars": 6},
        "sizing": SIZING,
    }
    if extra_side is not None:
        document["entry"][extra_side[0]] = list(extra_side[1])
    if filters:
        document["filters"] = list(filters)
    return parse_spec(document)


def _clause(left, op, right):
    return {"left": left, "op": op, "right": right}


Z = _clause("funding_zscore_14", "<", -1.5)
VOL = _clause("realized_vol_20", ">", 0.0095)


def test_the_same_rule_is_at_distance_zero():
    assert structural_distance(parse_spec(PLANTED), parse_spec(PLANTED)) == 0.0


def test_a_clause_under_filters_and_under_entry_are_one_condition():
    under_filters = _spec(entry=[Z], filters=[VOL])
    under_entry = _spec(entry=[Z, VOL])
    assert structural_distance(under_filters, under_entry) == 0.0


def test_a_rule_on_the_other_side_or_with_nothing_in_common_is_at_distance_one():
    planted = _spec(entry=[Z], filters=[VOL])
    assert structural_distance(_spec(entry=[Z], filters=[VOL], side="short"), planted) == 1.0
    assert structural_distance(_spec(entry=[_clause("rsi_14", "<", 30.0)]), planted) == 1.0


def test_a_threshold_gap_is_read_relative_to_the_larger_threshold():
    # The volatility clause matches outright (similarity 1); the z-score
    # clause has the period and op right and thresholds -2.0 against -1.5,
    # a closeness of 1 - 0.5/2 = 0.75, so (1 + 1 + 0.75)/3. Two clauses:
    # distance = 1 - (1 + 2.75/3)/2 = 1/24.
    selected = _spec(entry=[_clause("funding_zscore_14", "<", -2.0)], filters=[VOL])
    planted = _spec(entry=[Z], filters=[VOL])
    assert structural_distance(selected, planted) == pytest.approx(1 / 24)
    assert structural_distance(planted, selected) == pytest.approx(1 / 24)


def test_a_different_period_or_op_is_a_third_of_a_clause():
    planted = _spec(entry=[Z])
    period = _spec(entry=[_clause("funding_zscore_7", "<", -1.5)])
    assert structural_distance(period, planted) == pytest.approx(1 / 3)
    op = _spec(entry=[_clause("funding_zscore_14", "<=", -1.5)])
    assert structural_distance(op, planted) == pytest.approx(1 / 3)


def test_thresholds_of_opposite_sign_are_nothing_alike_but_the_clause_still_pairs():
    selected = _spec(entry=[_clause("funding_zscore_14", "<", 1.5)])
    assert structural_distance(selected, _spec(entry=[Z])) == pytest.approx(1 / 3)


def test_a_clause_only_one_rule_has_counts_against_it_whichever_rule_it_is_on():
    planted = _spec(entry=[Z], filters=[VOL])
    extra = _spec(entry=[Z], filters=[VOL, _clause("rsi_14", "<", 30.0)])
    assert structural_distance(extra, planted) == pytest.approx(1 / 3)
    assert structural_distance(planted, extra) == pytest.approx(1 / 3)


def test_a_feature_comparison_written_from_either_side_is_one_condition():
    one = _spec(entry=[_clause("close", ">", "sma_20")])
    other = _spec(entry=[_clause("sma_20", "<", "close")])
    assert structural_distance(one, other) == 0.0


def test_one_clause_written_two_ways_in_one_rule_is_one_condition():
    both = _spec(entry=[_clause("close", ">", "sma_20")], filters=[_clause("sma_20", "<", "close")])
    assert structural_distance(both, _spec(entry=[_clause("close", ">", "sma_20")])) == 0.0


def test_a_side_only_one_rule_enters_lowers_the_similarity_by_its_share():
    planted = _spec(entry=[Z])
    both = _spec(entry=[Z], extra_side=("short", [_clause("funding_zscore_14", ">", 1.5)]))
    assert structural_distance(both, planted) == pytest.approx(0.5)


# -- the score of one arm -------------------------------------------------------------------------


def _score(**changes) -> ArmScore:
    fields = {
        "arm": Arm.GRID,
        "replicate": 0,
        "ended": Ended.BUDGET_SPENT,
        "selected": 3,
        "structural_distance": 0.25,
        "agreement": 0.9,
        "bars_compared": 40,
        "holdout_sharpe": 1.5,
        "holdout_ruined": False,
        "recovery": RECOVERY,
    }
    return ArmScore(**{**fields, **changes})


NOTHING = {
    "selected": None,
    "structural_distance": None,
    "agreement": None,
    "bars_compared": 0,
    "holdout_sharpe": None,
}


def test_a_rule_at_or_over_theta_with_a_profitable_holdout_is_recovered():
    assert _score(agreement=0.8).recovered is True
    assert _score(agreement=0.79).recovered is False


def test_a_holdout_that_is_not_positive_is_not_a_recovery_when_the_manifest_requires_one():
    assert _score(holdout_sharpe=0.0).recovered is False
    assert _score(holdout_sharpe=-1.0).recovered is False
    loose = Recovery(theta_behavior=0.8, require_positive_holdout=False)
    assert _score(holdout_sharpe=-1.0, recovery=loose).recovered is True


def test_a_holdout_the_rule_ruined_is_read_as_no_ratio_at_all():
    ruined = _score(holdout_ruined=True)
    assert ruined.holdout_sharpe == 1.5 and ruined.readable_holdout is None
    assert ruined.recovered is False  # a positive Sharpe of a ruined run is not a profit
    loose = Recovery(theta_behavior=0.8, require_positive_holdout=False)
    assert _score(holdout_ruined=True, recovery=loose).recovered is True
    assert "(ruined)" in ruined.describe()


def test_a_score_whose_seam_failed_says_so():
    assert _score(**NOTHING, ended=Ended.SEAM_FAILED).describe() == (
        "grid-0: nothing selected; not recovered; its seam failed, so the report does not count it"
    )


def test_nothing_selected_and_an_undefined_agreement_are_not_recoveries():
    nothing = _score(**NOTHING)
    assert nothing.recovered is False and not nothing.found and not nothing.undefined
    undefined = _score(agreement=None, bars_compared=0)
    assert undefined.recovered is False and undefined.found and undefined.undefined


def test_a_null_world_recovers_nothing_and_reads_a_selection_as_a_false_call():
    call = _score(structural_distance=None, agreement=None, bars_compared=0, recovery=None)
    assert call.recovered is None and call.found
    assert call.describe() == "grid-0: false call, trial #3, holdout net sharpe 1.5000"
    assert call.describe(holdout=False) == "grid-0: false call, trial #3"
    with pytest.raises(ScoreError, match="nothing is planted"):
        call.recovered_at(0.5)
    assert _score(**NOTHING, recovery=None).describe() == "grid-0: no false call"


def test_a_score_describes_itself_and_keeps_the_holdout_figure_off_when_asked():
    line = _score().describe()
    assert line == (
        "grid-0: trial #3: direction agreement 0.9000 over 40 bar(s), structural distance 0.2500, "
        "holdout net sharpe 1.5000; recovered"
    )
    # A test seed: nothing read from the holdout reaches the console.
    hidden = _score().describe(holdout=False)
    assert hidden == (
        "grid-0: trial #3: structural distance 0.2500; the rest is not shown on a test seed"
    )
    assert "undefined" in _score(agreement=None, bars_compared=0).describe()
    assert _score(**NOTHING).describe() == "grid-0: nothing selected; not recovered"


@pytest.mark.parametrize(
    ("changes", "fragment"),
    [
        ({"selected": None}, "if and only if a trial was selected"),
        ({"holdout_sharpe": None}, "if and only if a trial was selected"),
        (
            {"selected": None, "holdout_sharpe": None},
            "compared with the planted one only when one was selected",
        ),
        ({"recovery": None}, "compared with the planted one only when one was selected"),
        ({"structural_distance": None}, "has a structural distance"),
        ({"bars_compared": 0}, "undefined exactly when no bar is compared"),
        ({"agreement": None}, "undefined exactly when no bar is compared"),
        ({"agreement": 1.1}, "score.agreement: expected a value between 0 and 1"),
        ({"structural_distance": -0.1}, "score.structural_distance"),
        ({"arm": "oracle"}, "score.arm"),
        ({"ended": "nope"}, "score.ended"),
        ({"selected": -1}, "score.selected"),
        ({"replicate": -1}, "score.replicate"),
        ({"bars_compared": -1}, "score.bars_compared"),
        ({"holdout_sharpe": float("nan")}, "score.holdout_sharpe"),
        ({"holdout_ruined": 1}, "score.holdout_ruined"),
        ({**NOTHING, "holdout_ruined": True}, "nothing was selected to be ruined"),
    ],
)
def test_a_score_that_could_not_have_been_computed_is_refused(changes, fragment):
    with pytest.raises(ScoreError, match=fragment):
        _score(**changes)


# -- the record ------------------------------------------------------------------------------------


def _scores(**changes) -> Scores:
    fields = {
        "world_id": "SYN-EB3BC030",
        "manifest_digest": "0badf00d",
        "scoring_version": "1",
        "planted_holdout_sharpe": 2.0,
        "recovery": RECOVERY,
        "results": (_score(),),
    }
    return Scores(**{**fields, **changes})


def test_the_record_comes_back_equal_and_a_recovered_flag_that_lies_is_refused():
    scores = _scores()
    assert Scores.loads(scores.dumps()) == scores
    tampered = json.loads(scores.dumps())
    tampered["results"][0]["recovered"] = False
    with pytest.raises(ScoreError, match="the record says recovered"):
        Scores.from_dict(tampered)


def test_the_record_refuses_another_version_and_unknown_keys():
    document = _scores().to_dict()
    with pytest.raises(ScoreError, match="scores_version: this build reads version 1"):
        Scores.from_dict({**document, "scores_version": 2})
    with pytest.raises(ScoreError, match="expected exactly the keys"):
        Scores.from_dict({**document, "extra": 1})
    with pytest.raises(ScoreError, match="scoring.recovery"):
        Scores.from_dict({**document, "recovery": {"theta_behavior": 2, "x": 1}})


@pytest.mark.parametrize(
    ("changes", "fragment"),
    [
        ({"planted_holdout_sharpe": None}, "both present on a world that plants"),
        ({"recovery": None}, "both present on a world that plants"),
        ({"results": ()}, "at least one ArmScore"),
        ({"results": (_score(), _score())}, "each arm and replicate once"),
        ({"manifest_digest": "x"}, "manifest_digest"),
        (
            {"results": (_score(recovery=Recovery(theta_behavior=0.5)),)},
            "one recovery rule",
        ),
    ],
)
def test_a_record_that_could_not_have_been_written_is_refused(changes, fragment):
    with pytest.raises(ScoreError, match=fragment):
        _scores(**changes)


def test_the_world_describes_itself_with_the_holdout_figures_or_without():
    lines = _scores().describe()
    assert lines[0] == "planted rule: holdout net sharpe 2.0000"
    assert not any("holdout" in line for line in _scores().describe(holdout=False))


# -- one world, scored ----------------------------------------------------------------------------


@pytest.fixture(scope="session")
def scored(searched, tmp_path_factory):
    """A copy of the searched world, scored once: the directory, what came back, the grid store's digest before."""
    source, *_ = searched
    run = tmp_path_factory.mktemp("scored") / source.name
    shutil.copytree(source, run)
    before = artifact_digest(store_path(run, Arm.GRID, 0))
    return run, score_world(run), before


def test_scoring_writes_the_record_every_arm_in_it_and_leaves_the_store_alone(scored):
    run, scores, before = scored
    searched = read_searched(run)
    assert searched is not None
    assert Scores.loads((run / SCORES_FILENAME).read_text(encoding="utf-8")) == scores
    assert (scores.world_id, scores.manifest_digest) == (
        searched.verdicts.world_id,
        searched.verdicts.manifest_digest,
    )
    assert scores.recovery == searched.manifest.scoring.recovery
    assert searched.oracle.planted is not None and searched.oracle.planted.holdout is not None
    assert scores.planted_holdout_sharpe == searched.oracle.planted.holdout.net.sharpe
    assert [s.experiment_id for s in scores.results] == [
        r.experiment_id for r in searched.verdicts.results
    ]
    for score, result in zip(scores.results, searched.verdicts.results, strict=True):
        assert score.selected == result.verdict.selected
        assert score.ended is result.snapshot.ended
        if result.holdout is None:
            assert score.holdout_sharpe is None and score.agreement is None
        else:
            assert score.holdout_sharpe == result.holdout.net.sharpe
            assert score.structural_distance is not None
    # Something was selected, so this world really exercised the comparison.
    assert any(score.found for score in scores.results)
    assert artifact_digest(store_path(run, Arm.GRID, 0)) == before
    assert sorted(p.name for p in run.iterdir() if p.name.startswith(".")) == []


def test_a_rule_scored_against_itself_agrees_on_every_bar_it_held(scored):
    run, *_ = scored
    searched = read_searched(run)
    assert searched is not None
    grid = next(r for r in searched.verdicts.results if r.arm is Arm.GRID)
    planted = searched.manifest.world.planted
    assert planted is not None and searched.oracle.planted is not None
    with ResearchStore(store_path(run, Arm.GRID, 0)) as store:
        read = scoring._holdout_reader(Ledger(store), grid.snapshot.experiment)
        got = read(planted.spec)
        assert read(planted.spec) is got  # one read per distinct rule
    held = sum(1 for position in got.positions if position)
    assert held > 0
    assert direction_agreement(got.positions, got.positions) == (1.0, held)
    assert got.metrics == searched.oracle.planted.holdout


def test_scoring_again_writes_the_same_bytes(scored):
    run, *_ = scored
    first = (run / SCORES_FILENAME).read_bytes()
    score_world(run)
    assert (run / SCORES_FILENAME).read_bytes() == first


def test_only_a_development_seed_s_holdout_reaches_the_console(scored, monkeypatch):
    run, scores, _ = scored
    lines: list[str] = []
    score_world(run, on_line=lines.append)
    assert lines == scores.describe()
    # The same world filed as a test seed: the seed set is the only input
    # to what is shown, and it is not in the record.
    searched = read_searched(run)
    assert searched is not None
    world = replace(searched.manifest.world, seed_set=SeedSet.TEST)
    filed = replace(searched, manifest=replace(searched.manifest, world=world))
    monkeypatch.setattr(scoring, "read_searched", lambda run_dir: filed)
    lines.clear()
    score_world(run, on_line=lines.append)
    assert lines == scores.describe(holdout=False)
    assert not any("agreement" in line and "bar(s)" in line for line in lines)


def test_a_world_nobody_searched_cannot_be_scored_and_a_store_that_is_gone_is_named(
    scored, landed_world, tmp_path
):
    artifact, _landing = landed_world
    fresh = land_run(tmp_path / "fresh", artifact, bench_manifest())
    with pytest.raises(ScoreError, match="has not been searched"):
        score_world(fresh)
    run, *_ = scored
    copy = _copy(run, tmp_path)
    store_path(copy, Arm.GRID, 0).unlink()
    with pytest.raises(ScoreError, match="for the grid arm"):
        score_world(copy)


def test_a_manifest_asking_for_another_scorer_is_refused(scored, monkeypatch):
    run, *_ = scored
    monkeypatch.setattr(scoring, "SCORING_VERSION", "2")
    with pytest.raises(ScoreError, match="this build scores by version 2"):
        score_world(run)


def _reader_returning(searched: Searched, *, planted=None, chosen=None):
    """A reader of the records' own figures, with either one swapped: the store disagreeing with its record."""
    assert searched.oracle.planted is not None and searched.manifest.world.planted is not None
    planted_spec = searched.manifest.world.planted.spec

    def read(spec) -> HoldoutRead:
        if spec == planted_spec:
            figure = searched.oracle.planted.holdout if planted is None else planted
            return HoldoutRead(figure, (1, 0))
        held = next(
            r.holdout for r in searched.verdicts.results if scoring._selected_spec(r) == spec
        )
        return HoldoutRead(held if chosen is None else chosen, (1, 1))

    return read


def test_a_store_that_does_not_reproduce_its_records_is_refused(scored):
    run, *_ = scored
    searched = read_searched(run)
    assert searched is not None and searched.oracle.planted is not None
    held = searched.oracle.planted.holdout
    assert held is not None
    other = replace(held, trades=held.trades + 1)
    with pytest.raises(ScoreError, match="planted rule's holdout read from the store"):
        score_records(searched, read=_reader_returning(searched, planted=other))
    with pytest.raises(ScoreError, match="holdout read from the store is not the figure its"):
        score_records(searched, read=_reader_returning(searched, chosen=other))


def test_a_selection_on_a_world_that_plants_a_rule_needs_a_reader(scored):
    run, *_ = scored
    searched = read_searched(run)
    assert searched is not None
    with pytest.raises(ScoreError, match="no reader was given"):
        score_records(searched)


def test_the_reader_s_positions_are_what_the_arms_are_compared_on(scored):
    run, *_ = scored
    searched = read_searched(run)
    assert searched is not None
    scores = score_records(searched, read=_reader_returning(searched))
    selected = [s for s in scores.results if s.found]
    assert selected
    # Planted (1, 0) against chosen (1, 1): bars 0 and 1 compared, bar 0 agrees.
    assert all((s.agreement, s.bars_compared) == (0.5, 2) for s in selected)


def test_a_null_world_needs_no_store_and_scores_a_selection_as_a_false_call(scored):
    run, *_ = scored
    searched = read_searched(run)
    assert searched is not None
    null = Searched(
        manifest=bench_manifest(kind=WorldKind.NULL_V1),
        verdicts=searched.verdicts,
        oracle=replace(searched.oracle, planted=None),
    )
    scores = score_records(null)
    assert scores.recovery is None and scores.planted_holdout_sharpe is None
    assert all(s.recovered is None and s.agreement is None for s in scores.results)
    assert [s.found for s in scores.results] == [r.verdict.found for r in searched.verdicts.results]
    assert Scores.loads(scores.dumps()) == scores


def test_a_conditional_world_where_nothing_was_selected_needs_no_store(scored):
    run, *_ = scored
    searched = read_searched(run)
    assert searched is not None
    empty = replace(
        searched.verdicts,
        results=tuple(
            replace(
                result,
                snapshot=replace(
                    result.snapshot, first_eligible_round=None, trials=(), distinct_rules=0
                ),
                holdout=None,
            )
            for result in searched.verdicts.results
        ),
    )
    scores = score_records(replace(searched, verdicts=empty))
    assert all(s.recovered is False and not s.found for s in scores.results)


# -- the table ----------------------------------------------------------------------------------


def _entry(score: ArmScore, *, first=None, passed=True, planted=2.0) -> ReportEntry:
    return ReportEntry(
        score=score,
        first_eligible_round=first,
        oracle_passed=passed,
        planted_holdout_sharpe=planted,
    )


def _row(*entries, **changes) -> ReportRow:
    fields = {
        "kind": WorldKind.CONDITIONAL_V1,
        "null_definition_version": "v1",
        "seed_set": SeedSet.DEV,
        "conditions": "0badf00d",
        "arm": Arm.GRID,
        "entries": entries,
        "failed": 0,
    }
    return ReportRow(**{**fields, **changes})


def test_a_conditional_row_keeps_every_failure_in_the_denominator():
    row = _row(
        _entry(_score(agreement=0.9, holdout_sharpe=1.0), first=3),
        _entry(_score(agreement=0.5, holdout_sharpe=0.5, structural_distance=0.75), first=5),
        _entry(_score(**NOTHING), passed=False),
        _entry(_score(agreement=None, bars_compared=0, holdout_sharpe=0.5), first=1, passed=False),
    )
    assert (row.searches, row.found, row.recovered, row.oracle_passed, row.recovered_with_room) == (
        4,
        3,
        1,
        2,
        1,
    )
    assert row.describe() == [
        "conditional-v1 (v1) dev [0badf00d] grid: recovered 1/4 = 25.0%; the oracle had room on "
        "2/4 = 50.0%; recovered where it had room 1/2 = 50.0%",
        "  nothing selected 1, agreement undefined 1; direction agreement median 0.7000, "
        "structural distance mean 0.4167",
        "  holdout net sharpe: selected mean 0.6667, the planted rule on those worlds mean "
        "2.0000; first eligible round median 3 (a diagnostic)",
        "  recovered at theta (development seeds): 0.5: 2, 0.6: 1, 0.7: 1, 0.8: 1, 0.9: 1",
    ]


def test_a_ruined_holdout_stays_in_the_denominator_and_out_of_the_means():
    row = _row(_entry(_score(holdout_ruined=True)), _entry(_score(holdout_sharpe=1.0)))
    assert (row.searches, row.found, row.recovered) == (2, 2, 1)
    assert row.describe()[2].startswith(
        "  holdout net sharpe: selected mean 1.0000 (1 ruined, not averaged), the planted rule on "
        "those worlds mean 2.0000"
    )


def test_a_test_seed_row_does_not_print_the_theta_sensitivity():
    lines = _row(_entry(_score()), seed_set=SeedSet.TEST).describe()
    assert len(lines) == 3 and not any("theta" in line for line in lines)


def test_a_null_row_reads_false_calls_and_a_failed_seam_is_counted_apart():
    clean = _score(**NOTHING, recovery=None)
    call = _score(
        structural_distance=None,
        agreement=None,
        bars_compared=0,
        holdout_sharpe=-0.5,
        recovery=None,
    )
    row = _row(
        _entry(call, first=2, passed=False, planted=None),
        _entry(clean, passed=False, planted=None),
        kind=WorldKind.NULL_V1,
        failed=1,
    )
    assert row.describe() == [
        "null-v1 (v1) dev [0badf00d] grid: false calls 1/2 = 50.0%; 1 seam failure(s) not counted",
        "  first eligible round median 2 (a diagnostic); holdout net sharpe of the false calls, "
        "mean -0.5000",
    ]


def test_a_row_with_no_finished_search_says_so_rather_than_dividing():
    row = _row(failed=2)
    assert row.describe()[0] == (
        "conditional-v1 (v1) dev [0badf00d] grid: recovered 0/0 = n/a; the oracle had room on "
        "0/0 = n/a; recovered where it had room 0/0 = n/a; 2 seam failure(s) not counted"
    )


def _copy(run: Path, tmp_path: Path, name: str = "copy") -> Path:
    target = tmp_path / name / run.name
    shutil.copytree(run, target)
    return target


def test_the_table_reads_the_world_and_no_store(scored, tmp_path):
    run, scores, _ = scored
    copy = _copy(run, tmp_path)
    shutil.rmtree(copy / ARMS_DIRNAME / Arm.GRID.value)
    # ``arms/`` is present (what the guard reads) but the Grid store is gone.
    rows = report_table([copy, copy])
    assert [row.arm for row in rows] == [Arm.RANDOM, Arm.GRID]
    by_arm = {score.arm: score for score in scores.results}
    assert all(row.searches == 1 and row.entries[0].score == by_arm[row.arm] for row in rows)


def test_the_table_refuses_a_searched_world_nobody_scored(scored, tmp_path):
    run, *_ = scored
    copy = _copy(run, tmp_path)
    (copy / SCORES_FILENAME).unlink()
    with pytest.raises(ScoreError, match="run `score` on it"):
        report_table([copy])


def _edit_scores(run: Path, edit) -> None:
    document = json.loads((run / SCORES_FILENAME).read_text(encoding="utf-8"))
    edit(document)
    (run / SCORES_FILENAME).write_text(json.dumps(document), encoding="utf-8")


@pytest.mark.parametrize(
    ("edit", "fragment"),
    [
        (lambda d: d.update(world_id="SYN-00000000"), "the records beside it are for"),
        (lambda d: d.update(manifest_digest="00000000"), "the records beside it are for"),
        (lambda d: d.update(scoring_version="9"), "is not the one the manifest and oracle"),
        (lambda d: d.update(planted_holdout_sharpe=99.0), "is not the one the manifest and oracle"),
        (lambda d: d["results"].pop(), "does not score the arms"),
        (lambda d: d["results"][0].update(replicate=7), "does not score the arms"),
    ],
    ids=["world", "digest", "version", "planted", "missing-arm", "other-arm"],
)
def test_the_table_refuses_scores_that_are_not_the_records_beside_them(
    scored, tmp_path, edit, fragment
):
    run, *_ = scored
    copy = _copy(run, tmp_path)
    _edit_scores(copy, edit)
    with pytest.raises(ScoreError, match=fragment):
        report_table([copy])


def test_the_table_refuses_an_arm_scored_otherwise_than_it_ended(scored, tmp_path):
    run, *_ = scored
    copy = _copy(run, tmp_path)
    _edit_scores(copy, lambda d: d["results"][0].update(ended="exhausted"))
    with pytest.raises(ScoreError, match="is not scored as verdicts.json records it"):
        report_table([copy])


def test_the_table_refuses_a_structural_distance_the_records_do_not_give(scored, tmp_path):
    run, scores, _ = scored
    copy = _copy(run, tmp_path)
    index = next(i for i, score in enumerate(scores.results) if score.found)
    wrong = 0.5 if scores.results[index].structural_distance != 0.5 else 0.25
    _edit_scores(copy, lambda d: d["results"][index].update(structural_distance=wrong))
    with pytest.raises(ScoreError, match="is not scored as verdicts.json records it"):
        report_table([copy])


def test_the_table_names_the_file_of_a_record_it_cannot_read(scored, tmp_path):
    run, *_ = scored
    copy = _copy(run, tmp_path)
    (copy / SCORES_FILENAME).write_text("{", encoding="utf-8")
    with pytest.raises(BenchError, match=rf"{SCORES_FILENAME}: scores is not valid JSON"):
        report_table([copy])


def test_a_failed_seam_is_set_apart_by_the_table(scored, monkeypatch):
    run, scores, _ = scored
    dead = replace(
        scores,
        results=tuple(
            replace(s, ended=Ended.SEAM_FAILED) if s.arm is Arm.GRID else s for s in scores.results
        ),
    )
    monkeypatch.setattr(scoring, "read_scores", lambda run_dir, searched: dead)
    rows = {row.arm: row for row in report_table([run])}
    assert (rows[Arm.GRID].searches, rows[Arm.GRID].failed) == (0, 1)
    assert (rows[Arm.RANDOM].searches, rows[Arm.RANDOM].failed) == (1, 0)


def test_read_scores_holds_the_record_to_the_manifest_s_scorer_and_recovery_rule(scored):
    run, *_ = scored
    searched = read_searched(run)
    assert searched is not None
    recovery = searched.manifest.scoring.recovery
    other_scorer = replace(
        searched, manifest=replace(searched.manifest, scoring=Scoring("2", recovery))
    )
    with pytest.raises(ScoreError, match="this build scores by version 1"):
        read_scores(run, other_scorer)
    other_rule = replace(
        searched,
        manifest=replace(searched.manifest, scoring=Scoring("1", Recovery(theta_behavior=0.1))),
    )
    with pytest.raises(ScoreError, match="is not the one the manifest and oracle"):
        read_scores(run, other_rule)
