"""The controls' asks and the plan section 5 bar, on hand-built paths."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from contrib.replay.account import Path, Step, Target
from contrib.replay.gate import (
    ALPHA,
    CONTROLS,
    POOLED_MIN_QUESTIONS,
    Readings,
    answer_targets,
    control_targets,
    decisions,
    describe_paths,
    judge,
    pooled_sharpe,
)
from contrib.replay.paper_store import InputFacts
from contrib.replay.score import build_split, sharpe
from contrib.replay.upstream import Penalty, SegmentName, TargetSide

from .conftest import (
    ROWS,
    STEP_MS,
    at_ms,
    fixture_answers,
    fixture_questions,
    input_id,
    terms,
)

LONG = TargetSide.LONG
SHORT = TargetSide.SHORT
FLAT = TargetSide.FLAT
BARS_PER_YEAR = 365 * 24 / 4


def _target(side: TargetSide, margin: int) -> Target:
    return Target(side, Decimal(margin))


# -- the asks -------------------------------------------------------------------------------


def test_answer_targets_follow_the_targets_the_gate_approved():
    questions = fixture_questions()
    asks = answer_targets(questions, {a.input_id: a for a in fixture_answers()})
    by_slot = {q.input_id: ask for q, ask in zip(questions, asks, strict=True)}
    assert by_slot[input_id(0)] == _target(LONG, 30)  # approved from flat
    assert by_slot[input_id(1)] is None  # a maintain
    assert by_slot[input_id(2)] == _target(SHORT, 20)  # clamped: the APPROVED margin
    assert by_slot[input_id(3)] is None  # fail-closed
    assert by_slot[input_id(5)] == _target(FLAT, 0)  # a flat target
    assert by_slot[input_id(6)] is None  # rejected
    assert by_slot[input_id(7)] == _target(LONG, 30)  # inside PAPER's deadband: still an ask
    assert by_slot[input_id(9)] == _target(SHORT, 50)
    assert by_slot[input_id(11)] is None  # unanswered


def test_buy_hold_asks_once_and_flat_never():
    questions = fixture_questions()
    held = control_targets("buy_hold", questions)
    assert held[0] == _target(LONG, 60)  # the fixture's cap
    assert held[1:] == [None] * (len(questions) - 1)
    assert control_targets("flat", questions) == [None] * len(questions)


def test_research_bias_asks_whenever_the_rules_side_changes():
    questions = fixture_questions()
    asks = control_targets("research_bias", questions)
    biases = [row.bias for row in ROWS]
    assert biases[:5] == ["long", "long", "short", None, "flat"]
    assert asks[0] == _target(LONG, 60)
    assert asks[1] is None  # the same side again
    assert asks[2] == _target(SHORT, 60)
    assert asks[3] is None  # no bias: hold
    assert asks[4] == _target(FLAT, 0)  # slot 5's flat bias
    assert asks[5] == _target(LONG, 60)
    assert asks[6] is None
    assert asks[7] == _target(SHORT, 60)
    assert asks[8:] == [None, None, None]


def test_an_unknown_control_is_refused():
    with pytest.raises(ValueError, match="no control named 'random'"):
        control_targets("random", fixture_questions())


def _facts(question, *, leverage: str = "2") -> InputFacts:
    return InputFacts(
        input_id=question.input_id,
        payload_name=None,
        payload_hash=None,
        mark=Decimal(str(int(question.mark))),
        account_equity=Decimal(1000),
        side=question.current_side,
        size=None,
        margin_pct=None,
        leverage=Decimal(leverage),
        guardrail_side=None,
    )


def test_decisions_take_the_exact_facts_and_drop_the_holdout_unless_asked():
    everyone = fixture_questions()
    split = build_split(everyone, interval="4h", step_ms=STEP_MS)
    first = everyone[:3]
    inputs = {q.input_id: _facts(q) for q in first}
    walked = decisions(first, inputs, [None, None, None], split=split, step_ms=STEP_MS)
    assert [d.segment for d in walked] == [SegmentName.TRAIN] * 3
    assert walked[0].mark == Decimal(100) and walked[0].leverage == Decimal(2)
    last = everyone[-1:]
    facts = {last[0].input_id: _facts(last[0])}
    assert decisions(last, facts, [None], split=split, step_ms=STEP_MS) == []
    (holdout,) = decisions(last, facts, [None], split=split, step_ms=STEP_MS, holdout=True)
    assert holdout.segment is SegmentName.HOLDOUT
    with pytest.raises(ValueError, match="2 asks for 3 questions"):
        decisions(first, inputs, [None, None], split=split, step_ms=STEP_MS)


# -- the bar --------------------------------------------------------------------------------


def _path(returns: list[float], segment: SegmentName = SegmentName.VALIDATION) -> Path:
    """A path whose steps carry ``returns`` in ``segment``; the other fields are not read."""
    steps = tuple(
        Step(
            input_id=f"q{i}",
            at_ms=at_ms(i),
            segment=segment,
            mark=Decimal(100),
            target=None,
            carried=False,
            filled=None,
            fill_price=None,
            fee=Decimal(0),
            funding=Decimal(0),
            funding_missing=0,
            size=Decimal(0),
            entry=None,
            wallet=Decimal(1000),
            equity=Decimal(1000),
            ret=r,
        )
        for i, r in enumerate(returns)
    )
    return Path(terms(), steps)


GOOD = [0.01, 0.02, 0.01, 0.03, 0.02]
BAD = [-0.01, 0.02, -0.03, 0.01, -0.02]
NONE = [0.0, 0.0, 0.0, 0.0, 0.0]


def _readings(candidate_only: int, paper_only: int, both: int) -> tuple[Readings, Readings]:
    """Two readings over one run's validation questions with those discordant and concordant counts."""
    mine = [True] * candidate_only + [False] * paper_only + [True] * both
    theirs = [False] * candidate_only + [True] * paper_only + [True] * both
    return (
        Readings({f"q{i}": h for i, h in enumerate(mine)}, fail_closed=1, answered=20),
        Readings({f"q{i}": h for i, h in enumerate(theirs)}, fail_closed=2, answered=20),
    )


def test_pooled_sharpe_concatenates_the_runs_returns():
    value, n = pooled_sharpe([_path(GOOD), _path(BAD)], SegmentName.VALIDATION, BARS_PER_YEAR)
    assert n == 10
    assert value == pytest.approx(sharpe(GOOD + BAD, BARS_PER_YEAR))
    assert pooled_sharpe([_path(GOOD)], SegmentName.TRAIN, BARS_PER_YEAR) == (0.0, 0)


def _judge(**overrides):
    mine, theirs = _readings(40, 10, 60)
    fields = {
        "candidate": "cand",
        "candidate_paths": [_path(GOOD)],
        "control_paths": {name: [_path(BAD if name != "flat" else NONE)] for name in CONTROLS},
        "trials": 1,
        "candidate_readings": [mine],
        "paper_readings": [theirs],
        "bars_per_year": BARS_PER_YEAR,
    }
    fields.update(overrides)
    return judge(**fields)


def test_a_candidate_that_clears_all_four_is_eligible():
    verdict = _judge()
    assert [c.passed for c in verdict.checks] == [True, True, True, True]
    assert verdict.eligible
    lines = verdict.describe()
    assert lines[0] == "-- plan section 5 bar: cand on the validation segment --"
    assert lines[1].startswith("  (1) net sharpe above every control: cand ")
    assert lines[1].endswith("-> pass")
    assert "1 + 0.25 x ln 1; 1 variant repeat(s) tried" in lines[2]
    assert f"n 110 (needs >= {POOLED_MIN_QUESTIONS}), cand only 40, paper only 10" in lines[3]
    assert "cand 1/20 (5.0%) vs paper 2/20 (10.0%)" in lines[4]
    assert lines[-1].endswith("eligible for --holdout (one look, recorded in the ledger)")


def test_the_sharpe_checks_read_only_the_eligible_questions():
    # Keep q0..q2 (the first three returns of GOOD and BAD) and the
    # comparison is over three returns, not five.
    verdict = _judge(eligible={"q0", "q1", "q2"})
    assert "(n 3)" in verdict.checks[0].detail
    assert pooled_sharpe([_path(GOOD)], SegmentName.VALIDATION, BARS_PER_YEAR, only={"q0"}) == (
        0.0,
        1,
    )


def test_a_ruined_candidate_cannot_pass_check_one():
    ruined = _path(GOOD)
    last = ruined.steps[-1]
    ruined = Path(ruined.terms, (*ruined.steps[:-1], replace(last, equity=Decimal(0), ret=None)))
    assert ruined.ruined
    verdict = _judge(candidate_paths=[ruined])
    assert not verdict.checks[0].passed
    assert "RUINED: a path that stopped cannot win" in verdict.checks[0].detail


def test_readings_hold_their_counts_and_the_verdict_says_when_the_holdout_was_walked():
    with pytest.raises(ValueError, match="is a count of the answered"):
        Readings({}, fail_closed=3, answered=2)
    walked = _judge().describe(holdout_walked=True)
    assert walked[-1].endswith("the holdout was walked above and the look recorded")
    mine, _ = _readings(40, 10, 60)
    locked = _judge(candidate_readings=[mine], paper_readings=[mine]).describe(holdout_walked=True)
    assert locked[-1].endswith("the holdout above was walked on the paper trader's look")


def test_check_one_needs_strictly_more_than_every_control():
    controls = {name: [_path(BAD)] for name in CONTROLS}
    controls["paper"] = [_path(GOOD)]
    verdict = _judge(control_paths=controls)
    assert not verdict.checks[0].passed  # equal to paper is not above it
    assert not verdict.eligible
    assert verdict.describe()[-1].endswith("does not pass; the holdout stays locked")


def test_check_two_is_the_research_ledgers_threshold_at_the_trials_tried():
    # Unannualised, GOOD's sharpe is about 2.1: above the base of 1.0 for
    # one trial, below the 3.88 that 100,000 trials raise the bar to.
    assert _judge(trials=1, bars_per_year=1.0).checks[1].passed
    many = _judge(trials=100_000, bars_per_year=1.0)
    assert not many.checks[1].passed
    assert f"vs {Penalty().threshold(100_000):.2f}" in many.checks[1].detail
    with pytest.raises(ValueError, match="at least one"):
        _judge(trials=0)


def test_check_three_needs_the_pooled_sample_the_direction_and_the_significance():
    few = _readings(20, 2, 50)
    assert not _judge(candidate_readings=[few[0]], paper_readings=[few[1]]).checks[2].passed
    behind = _readings(10, 40, 60)
    verdict = _judge(candidate_readings=[behind[0]], paper_readings=[behind[1]])
    assert not verdict.checks[2].passed
    close = _readings(26, 24, 60)  # n 110, p far above ALPHA
    verdict = _judge(candidate_readings=[close[0]], paper_readings=[close[1]])
    assert not verdict.checks[2].passed
    assert f"(needs < {ALPHA})" in verdict.checks[2].detail
    # A question one side has no reading for is dropped from the pairs, and
    # a question only one side holds is not paired at all.
    mine, theirs = _readings(40, 10, 60)
    unread = Readings({**mine.hits, "q0": None, "extra": True}, mine.fail_closed, mine.answered)
    verdict = _judge(candidate_readings=[unread], paper_readings=[theirs])
    assert "n 109" in verdict.checks[2].detail
    # Two runs pool their questions.
    verdict = _judge(candidate_readings=[mine, mine], paper_readings=[theirs, theirs])
    assert "n 220" in verdict.checks[2].detail


def test_check_four_compares_the_fail_closed_rates():
    mine, _ = _readings(40, 10, 60)
    equal = Readings(mine.hits, fail_closed=2, answered=20)
    assert _judge(candidate_readings=[equal]).checks[3].passed  # equal is not above
    worse = Readings(mine.hits, fail_closed=3, answered=20)
    assert not _judge(candidate_readings=[worse]).checks[3].passed
    silent = Readings(mine.hits, fail_closed=0, answered=0)
    unread = _judge(candidate_readings=[silent])
    assert not unread.checks[3].passed
    assert "no answered validation question to read a rate from" in unread.checks[3].detail


def test_the_paper_trader_against_itself_fails_one_and_three_and_passes_four():
    mine, _ = _readings(40, 10, 60)
    verdict = _judge(
        candidate="paper",
        candidate_paths=[_path(BAD)],
        control_paths={name: [_path(BAD)] for name in CONTROLS},
        candidate_readings=[mine],
        paper_readings=[mine],
    )
    assert [c.passed for c in verdict.checks] == [False, verdict.checks[1].passed, False, True]
    assert "paper only 0, McNemar p 1.000" in verdict.checks[2].detail


def test_readings_reduce_a_scorecards_validation_rows():
    from contrib.replay.score import build_split, score_run
    from contrib.replay.upstream import CostModel

    questions = fixture_questions()
    split = build_split(questions, interval="4h", step_ms=STEP_MS)
    card = score_run(questions, fixture_answers(), step_ms=STEP_MS, costs=CostModel(), split=split)
    readings = Readings.of(card)
    validation = [row for row in card.rows if row.segment is SegmentName.VALIDATION]
    assert set(readings.hits) == {row.question.input_id for row in validation}
    assert readings.answered == sum(1 for row in validation if row.answer is not None)
    assert readings.fail_closed == sum(1 for row in validation if row.fail_closed)


def test_describe_paths_prints_each_run_and_the_pooled_sharpe_per_segment():
    lines = describe_paths(
        "flat", {"r1": _path(NONE), "r2": _path(NONE)}, bars_per_year=BARS_PER_YEAR, holdout=False
    )
    assert lines[0] == "-- flat --"
    assert lines[1] == "  run r1:"
    assert lines[-1] == "  pooled: train sharpe 0.00 (n 0); validation sharpe 0.00 (n 10)"
    with_holdout = describe_paths("flat", {"r1": _path(NONE)}, bars_per_year=1.0, holdout=True)
    assert with_holdout[-1].endswith("; holdout sharpe 0.00 (n 0)")
    assert replace(_path(NONE).steps[0], ret=None).ret is None  # the record stays replaceable
