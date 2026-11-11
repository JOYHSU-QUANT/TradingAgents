"""Whether one more section moves the decision (discovery plan §4.7): the report, the four arms, the command.

The pure cases build :class:`~contrib.replay.effect.Reading` records by
hand, so every printed count can be re-derived from the lines above it.

The command cases replay :mod:`.papers`' fixture run twice: ``plain`` says
what the paper trader's model said, and ``told`` says the same except on
two questions, where its answer also mentions the research section:

- slot 5 (held short at 63% of equity): ``plain`` asks for flat, ``told``
  for long at 30%, which the gate approves. The model reading moves from
  0 to +0.30, and so does the executed one.
- slot 7 (flat): both ask for long at 30%; ``plain`` at confidence 0.2,
  which the gate refuses, ``told`` at 0.8, which it approves. The model
  reading is +0.30 either way; the executed one moves from 0 to +0.30.

Slot 6 is prose from both (fail-closed: no claim, the book untouched), so
of the eight train and validation questions seven carry a readable claim
from both.
"""

from __future__ import annotations

import csv
import shutil
import sqlite3
from pathlib import Path

import pytest

from contrib.replay import cli
from contrib.replay.effect import (
    MENTION,
    Arm,
    ArmRun,
    Reading,
    arm_run,
    coverage,
    describe_effect,
    differences,
    mentions,
    read_rows,
    shift_interval,
    verdict,
)
from contrib.replay.paper_store import gate_config, load_decisions, run_facts
from contrib.replay.replay import human_message, prepare, select
from contrib.replay.score import ScoreError, build_split, score_run
from contrib.replay.upstream import Database, SegmentName, TargetSide, payload_dir
from contrib.replay.variant import load_variant

from . import papers
from .papers import (
    FORMAT,
    RUN_ID,
    Echo,
    decision_text,
    replay_argv,
    variant,
    write_gate_store,
    write_graph_variant,
    write_variant,
)

STEP_MS = 4 * 3_600_000
BASE_MS = 1_800_000_000_000  # 2027-01-15T08:00:00Z
OTHER_RUN = "paper-GATE2"
LONG, SHORT, FLAT = TargetSide.LONG, TargetSide.SHORT, TargetSide.FLAT
VARIANTS = Path(__file__).resolve().parents[1] / "variants"
ARMS = ("none", "neutral", "long", "short")
# How the report names the default phrases an answer is searched for.
SEARCHED = (
    "mentioning 'research signal' or 'research rule' or 'research radar' or 'btc-4h-maker'"
)


def _covered(readable: int, asked: int) -> str:
    """How every verdict line ends: what the comparison was read on, of what was asked."""
    return (
        f"; {readable} of {asked} question-repeat(s) put to either variant carry a readable "
        "claim from both"
    )


def _reading(
    slot: int,
    side: TargetSide | None,
    exposure: float | None,
    *,
    executed: float | None = None,
    confidence: float | None = None,
    said: bool = False,
) -> Reading:
    """Question ``slot`` read as ``side`` at ``exposure``; executed as asked unless told otherwise."""
    if executed is None:
        assert exposure is not None
        executed = exposure
    at_ms = BASE_MS + slot * STEP_MS
    return Reading(f"q{slot}", at_ms, side, exposure, executed, confidence, "approved", said)


def _arm(
    name: str, *runs: dict[int, list[Reading]], extra: str | None = "A section.", **fields
) -> Arm:
    """An arm named ``name`` whose runs are ``run-0``, ``run-1``, ... each ``{repeat: readings}``."""
    return Arm(
        variant(name=name, extra_context=extra, **fields),
        [
            ArmRun(f"run-{index}", {r: {x.input_id: x for x in xs} for r, xs in repeats.items()})
            for index, repeats in enumerate(runs)
        ],
        MENTION,
    )


def _describe(treatment: Arm, control: Arm) -> list[str]:
    report = describe_effect(treatment, control, block=6, draws=200, seed=0)
    return [*report.preamble, *report.body]


# -- the pieces --------------------------------------------------------------------


def test_a_mention_is_found_whatever_its_case():
    assert mentions("The Research signal holds long.", MENTION)
    assert mentions("per the RADAR", ["research", "radar"])
    assert not mentions("Funding is flat and the trend is up.", MENTION)
    # The shipped system prompt's own words: every arm is told the debate is absent.
    assert not mentions("Decided without the research debate.", MENTION)


def test_the_differences_name_what_reaches_the_model():
    base = variant(name="a")
    assert differences(base, variant(name="b")) == []
    assert differences(base, variant(name="b", extra_context="A section.")) == ["extra context"]
    assert differences(
        base, variant(name="b", model="c/d", temperature=0.2, max_tokens=1, system_prompt="No.")
    ) == ["model", "system prompt", "temperature", "max tokens"]


def test_two_constant_runs_always_draw_their_pooled_mean():
    """Each run is redrawn on its own circle and keeps its size, so the pooled mean cannot move."""
    assert shift_interval([[0.0] * 5, [1.0] * 5], block=2, draws=50, seed=3) == (0.5, 0.5)


def test_the_interval_brackets_the_mean_and_repeats_under_one_seed():
    runs = [[0.0, 0.3, 0.0, 0.0, 0.6, 0.0, 0.0], [0.1, 0.0, 0.0]]
    found = shift_interval(runs, block=2, draws=400, seed=7)
    assert found == shift_interval(runs, block=2, draws=400, seed=7)
    assert found is not None
    low, high = found
    assert low < 0.1 < high  # the mean shift of the ten questions
    assert high - low < 0.6  # an interval of the mean, not the range of the shifts
    assert found != shift_interval(runs, block=2, draws=400, seed=8)
    assert found != shift_interval(runs, block=5, draws=400, seed=7)
    assert shift_interval([[], []], block=2, draws=10, seed=0) is None
    with pytest.raises(ValueError, match="at least one draw"):
        shift_interval(runs, block=2, draws=0, seed=0)


# -- the report --------------------------------------------------------------------


def test_the_report_counts_the_moves_on_the_questions_both_read():
    told = _arm(
        "told",
        {
            0: [
                _reading(0, LONG, 0.3, confidence=0.8, said=True),  # flat -> long
                _reading(1, LONG, 0.5, executed=0.4, confidence=0.6),  # a larger long, cut back
                _reading(2, FLAT, 0.0),  # short -> flat
                _reading(3, SHORT, -0.2),  # long -> short
                _reading(4, LONG, 0.3),  # the same
                _reading(5, None, None, executed=0.2),  # fail-closed
                _reading(7, LONG, 0.3),  # the other was not asked this one
            ]
        },
    )
    plain = _arm(
        "plain",
        {
            0: [
                _reading(0, FLAT, 0.0, confidence=0.4),
                _reading(1, LONG, 0.3, confidence=0.7),
                _reading(2, SHORT, -0.2),
                _reading(3, LONG, 0.3),
                _reading(4, LONG, 0.3),
                _reading(5, LONG, 0.2),
                _reading(6, LONG, 0.3),
            ]
        },
        extra=None,
    )
    lines = _describe(told, plain)
    assert lines[0] == "decision effect: told against plain"
    assert lines[3] == "the two differ in: extra context"
    assert lines[4] == "extra context of told: 10 character(s) in 1 line(s), opening 'A section.'"
    assert lines[5] == "extra context of plain: none"
    assert lines[6] == "-- answers (train and validation; the holdout is never read) --"
    assert lines[7] == (
        "  told: 7 answer(s) in 1 repeat(s): long 4, short 1, flat 1, fail-closed 1; 0 refused "
        f"by the provider; {SEARCHED}: 1/7 (14.3%)"
    )
    assert lines[8] == (
        "  plain: 7 answer(s) in 1 repeat(s): long 5, short 1, flat 1, fail-closed 0; 0 refused "
        f"by the provider; {SEARCHED}: 0/7 (0.0%)"
    )
    # Six questions both answered (q0-q5); q5 is fail-closed on one side.
    assert lines[9] == (
        "-- repeat 0: 6 question(s) answered by both, 5 with a readable claim from both --"
    )
    # Shifts +0.3, +0.2, +0.2, -0.5, 0: three up, one down, two-sided p = 2 * 5/16.
    assert lines[10] == (
        "  model reading: toward long 3, toward short 1, unchanged 1 (sign test p 0.625); "
        "mean shift +0.040"
    )
    assert lines[11] == (
        "  sides, plain -> told: long -> long 2, long -> short 1, short -> flat 1, "
        "flat -> long 1"
    )
    # Executed: +0.3, +0.1, +0.2, -0.5, 0, 0 (the fail-closed round holds what plain holds).
    assert lines[12] == (
        "  executed reading: toward long 3, toward short 1, unchanged 2 (sign test p 0.625); "
        "mean shift +0.017"
    )
    assert lines[13] == "  confidence where both state one: n 2, mean 0.700 against 0.550"
    assert lines[14] == "-- per question, repeats averaged --"
    assert lines[15] == (
        "  5 question(s) read by both: mean requested exposure +0.180 against +0.140, "
        "shift +0.040"
    )
    assert lines[16].startswith("  90% interval of the shift [")
    assert lines[16].endswith(
        "]: 1 circular block(s) of 6 question(s) within each run, 200 draws, seed 0"
    )
    assert lines[17] == "  toward long 3, toward short 1, unchanged 1 (sign test p 0.625)"
    assert lines[18] == "  by run: run-0 5"
    assert lines[19] == (
        "-- how far apart two answers to one question are (readable claims on the questions "
        "read by both; exposure as a multiple of equity) --"
    )
    # Same repeat: the side differs on q0, q2 and q3; |shift| sums to 1.2 over five pairs.
    assert lines[20:] == [
        "  two repeats of told: fewer than two repeats hold an answer, so there is nothing to compare",
        "  two repeats of plain: fewer than two repeats hold an answer, so there is nothing to compare",
        "  told against plain, same repeat: side differs in 3 of 5 pair(s) (60.0%), mean absolute "
        "exposure difference 0.240",
        "verdict: cannot be judged: coverage is under 80%" + _covered(5, 8),
    ]


def test_a_question_counts_once_however_often_it_was_asked():
    """Repeats are averaged per question before the shift is taken, and each run keeps its own."""
    told = _arm(
        "told",
        {0: [_reading(0, LONG, 0.4), _reading(1, LONG, 0.2)], 1: [_reading(0, LONG, 0.2)]},
        {0: [_reading(0, LONG, 0.5)], 1: [_reading(0, None, None, executed=0.0)]},
    )
    plain = _arm(
        "plain",
        {0: [_reading(0, LONG, 0.2), _reading(1, LONG, 0.1)], 1: [_reading(0, LONG, 0.2)]},
        {0: [_reading(0, LONG, 0.4)], 1: [_reading(0, LONG, 0.4)]},
        extra="B section.",
    )
    lines = _describe(told, plain)
    start = lines.index("-- per question, repeats averaged --")
    # run-0: q0 (0.4 + 0.2) / 2 against 0.2, q1 0.2 against 0.1; run-1: q0 0.5 (its one
    # readable repeat) against 0.4. Every shift is +0.1, so every draw's mean is.
    assert lines[start + 1] == (
        "  3 question(s) read by both: mean requested exposure +0.333 against +0.233, "
        "shift +0.100"
    )
    assert lines[start + 2] == (
        "  90% interval of the shift [+0.100, +0.100]: 2 circular block(s) of 6 question(s) "
        "within each run, 200 draws, seed 0"
    )
    assert lines[start + 3] == "  toward long 3, toward short 0, unchanged 0 (sign test p 0.250)"
    assert lines[start + 4] == "  by run: run-0 2, run-1 1"
    # Between two repeats: told's q0 of run-0 (0.4 against 0.2) is its one pair both read;
    # plain has that pair (equal) and run-1's q0 (equal).
    assert lines[start + 6 :] == [
        "  two repeats of told: side differs in 0 of 1 pair(s) (0.0%), mean absolute exposure "
        "difference 0.200",
        "  two repeats of plain: side differs in 0 of 2 pair(s) (0.0%), mean absolute exposure "
        "difference 0.000",
        "  told against plain, same repeat: side differs in 0 of 4 pair(s) (0.0%), mean absolute "
        "exposure difference 0.100",
        # Five question-repeats asked; told's fail-closed one is the fifth: exactly 80%.
        "verdict: cannot be judged: 2 block(s), fewer than the 5 an interval is read with"
        + _covered(4, 5),
    ]


def test_a_repeat_one_side_lacks_on_a_run_is_named_and_not_paired_there():
    long = {0: [_reading(0, LONG, 0.3)], 1: [_reading(0, LONG, 0.3)]}
    flat = {0: [_reading(0, FLAT, 0.0)], 1: [_reading(0, FLAT, 0.0)]}
    told = _arm("told", long, long)
    plain = _arm("plain", flat, {0: flat[0]}, extra=None)
    lines = _describe(told, plain)
    whole = lines.index(
        "-- repeat 0: 2 question(s) answered by both, 2 with a readable claim from both --"
    )
    assert lines[whole + 1].startswith("  model reading: toward long 2, toward short 0, ")
    # Repeat 1 is paired on run-0 alone, and its block says why it is half of repeat 0's.
    half = lines.index(
        "-- repeat 1: 1 question(s) answered by both, 1 with a readable claim from both --"
    )
    assert lines[half + 1] == "  run-1: no answer read on repeat 1 from plain, so nothing is paired there"
    assert lines[half + 2].startswith("  model reading: toward long 1, toward short 0, ")
    # ``plain`` has two repeats on run-0 alone, and that is where its spread is read.
    assert lines[-3] == (
        "  two repeats of plain: side differs in 0 of 1 pair(s) (0.0%), mean absolute exposure "
        "difference 0.000"
    )


def test_a_repeat_only_the_other_variant_holds_gets_its_own_block():
    one = {0: [_reading(0, LONG, 0.3)]}
    told = _arm("told", one, one)
    plain = _arm("plain", {**one, 2: [_reading(0, LONG, 0.3)]}, one, extra=None)
    lines = _describe(told, plain)
    start = lines.index(
        "-- repeat 2: 0 question(s) answered by both, 0 with a readable claim from both --"
    )
    assert lines[start + 1 : start + 3] == [
        "  run-0: no answer read on repeat 2 from told, so nothing is paired there",
        "  run-1: no answer read on repeat 2 from told or plain, so nothing is paired there",
    ]


def test_a_mean_of_equal_answers_is_not_a_move():
    """0.35 three times over does not average to exactly 0.35: rounding is not a shift."""
    same = [_reading(0, LONG, 0.35)]
    told = _arm("told", {0: same, 1: same, 2: same})
    plain = _arm("plain", {0: same}, extra=None)
    lines = _describe(told, plain)
    start = lines.index("-- per question, repeats averaged --")
    assert lines[start + 1].endswith(", shift +0.000")
    assert lines[start + 2].startswith("  90% interval of the shift [+0.000, +0.000]: ")
    assert lines[start + 3] == "  toward long 0, toward short 0, unchanged 1 (sign test p 1.000)"


def _verdict(shift: float, low: float, high: float, **changes) -> str:
    terms = {
        "spreads": [("told", 0.05), ("plain", 0.02)],
        "block": 6,
        "blocks": 5,
        "readable": 60,
        "asked": 60,
        **changes,
    }
    return verdict(shift, low, high, **terms)


def test_the_verdict_is_the_bar_decided_before_any_answer_was_bought():
    """The interval clear of 0 AND the shift larger than either variant's own spread."""
    moves = "the extra context moves the decision"
    assert _verdict(0.2, 0.1, 0.3) == f"{moves} toward long"
    assert _verdict(-0.2, -0.3, -0.1) == f"{moves} toward short"
    assert _verdict(0.2, 0.1, 0.3, spreads=[("told", 0.0), ("plain", 0.0)]) == f"{moves} toward long"
    holds = "no measurable effect: the interval of the shift holds 0"
    assert _verdict(0.2, -0.01, 0.3) == holds
    # An interval that touches 0, from either side, holds it.
    assert _verdict(0.2, 0.0, 0.3) == holds
    assert _verdict(-0.2, -0.3, 0.0) == holds
    small = (
        "no measurable effect: the shift is no larger than the spread between two repeats of "
        "one variant (0.050)"
    )
    assert _verdict(0.04, 0.03, 0.05) == small
    assert _verdict(-0.05, -0.06, -0.04) == small  # equal is not larger
    # The larger spread is the bar, whichever variant it is.
    assert _verdict(0.04, 0.03, 0.05, spreads=[("told", 0.02), ("plain", 0.05)]) == small


def test_the_verdict_is_not_judged_without_a_spread_or_off_the_bars_blocks():
    assert _verdict(0.2, 0.1, 0.3, spreads=[("told", 0.05), ("plain", None)]) == (
        "cannot be judged: plain has no question with a readable claim on two repeats to take a "
        "spread from"
    )
    # One question a block would draw neighbouring questions as if they were independent.
    assert _verdict(0.2, 0.1, 0.3, block=1, blocks=30) == (
        "cannot be judged: the bar is read with blocks of 6 question(s), not 1"
    )
    assert _verdict(0.2, 0.1, 0.3, blocks=4) == (
        "cannot be judged: 4 block(s), fewer than the 5 an interval is read with"
    )
    with pytest.raises(ValueError, match="none was given"):
        _verdict(0.2, 0.1, 0.3, spreads=[])
    # Coverage: 80% of what was asked, in whole numbers (0.8 * 130 is not 104 in floats).
    assert _verdict(0.2, 0.1, 0.3, readable=103, asked=130) == (
        "cannot be judged: coverage is under 80%"
    )
    assert _verdict(0.2, 0.1, 0.3, readable=104, asked=130).startswith("the extra context")
    # Each comes before the bar: an interval holding 0 is no finding where nothing is judged.
    for unjudged in (
        {"readable": 47, "asked": 60},
        {"spreads": [("told", None), ("plain", 0.02)]},
        {"block": 1, "blocks": 30},
        {"blocks": 4},
    ):
        assert _verdict(0.2, -0.01, 0.3, **unjudged).startswith("cannot be judged: ")


def test_the_report_ends_on_the_verdict():
    def asked(*exposures: float) -> dict[int, list[Reading]]:
        return {
            repeat: [_reading(slot, LONG, exposure) for slot in range(30)]
            for repeat, exposure in enumerate(exposures)
        }

    # Thirty questions are five blocks of six. ``plain`` says 0.1 then 0.2: a spread of 0.1
    # and a mean of 0.15, which ``told`` at 0.5 clears by 0.35 on every draw.
    plain = _arm("plain", asked(0.1, 0.2), extra=None)
    assert _describe(_arm("told", asked(0.5, 0.5)), plain)[-1] == (
        "verdict: the extra context moves the decision toward long" + _covered(60, 60)
    )
    # 0.2 against a mean of 0.15 is a shift every draw agrees on, and half plain's own spread.
    assert _describe(_arm("told", asked(0.2, 0.2)), plain)[-1] == (
        "verdict: no measurable effect: the shift is no larger than the spread between two "
        "repeats of one variant (0.100)" + _covered(60, 60)
    )


def test_the_spread_is_read_on_the_questions_the_shift_is():
    """Questions one variant alone answered must not move the bar the shift is held against."""

    def asked(slots: range, *exposures: float) -> dict[int, list[Reading]]:
        return {
            repeat: [_reading(slot, LONG, exposure) for slot in slots]
            for repeat, exposure in enumerate(exposures)
        }

    steady, plain = asked(range(30), 0.2, 0.2), asked(range(30), 0.1, 0.1)
    # Six more questions only one variant was asked, on which its two repeats are 1.0 apart:
    # over all thirty-six its spread would be 0.167, above the 0.1 shift; over the thirty
    # the shift is read on, it is 0.
    wild = asked(range(30, 36), 0.0, 1.0)
    for told, other in (
        ({repeat: steady[repeat] + wild[repeat] for repeat in (0, 1)}, plain),
        (steady, {repeat: plain[repeat] + wild[repeat] for repeat in (0, 1)}),
    ):
        lines = _describe(_arm("told", told), _arm("plain", other, extra=None))
        for line in lines[-4:-2]:
            assert ": side differs in 0 of 30 pair(s) (0.0%), mean absolute exposure " in line
        assert lines[-1] == (
            "verdict: the extra context moves the decision toward long" + _covered(60, 72)
        )


def test_coverage_counts_what_was_asked_of_either_variant():
    """Fail-closed and refused rounds leave the shift; the coverage is where they show."""
    one = {0: {"q0": _reading(0, LONG, 0.3)}}
    # q1 and q2 were put to ``told`` and refused: asked, with no reading.
    told = Arm(
        variant(name="told", extra_context="A section."),
        [ArmRun("run-0", one, refused=2, asked={0: frozenset({"q0", "q1", "q2"})})],
        MENTION,
    )
    plain = Arm(variant(name="plain"), [ArmRun("run-0", one)], MENTION)
    assert coverage(told, plain) == (1, 3)
    assert coverage(plain, told) == (1, 3)
    # A repeat only one variant was asked is asked and unpaired.
    twice = Arm(told.variant, [ArmRun("run-0", {**one, 1: one[0]})], MENTION)
    assert coverage(twice, plain) == (1, 2)


def test_a_variant_that_fails_closed_where_it_is_shown_the_section_is_not_called_no_effect():
    """Thirty-six questions twice; ``told`` claims nothing on most and agrees on the rest."""

    def asked(broken: range) -> dict[int, list[Reading]]:
        return {
            repeat: [
                _reading(slot, None, None, executed=0.0)
                if slot in broken
                else _reading(slot, LONG, 0.3)
                for slot in range(36)
            ]
            for repeat in (0, 1)
        }

    plain = _arm("plain", asked(range(0)), extra=None)
    lines = _describe(_arm("told", asked(range(12, 36))), plain)
    assert "  12 question(s) read by both: " in lines[lines.index("-- per question, repeats averaged --") + 1]
    assert lines[-1] == "verdict: cannot be judged: coverage is under 80%" + _covered(24, 72)
    # Six broken questions in thirty-six leave 83% read, on thirty questions (five blocks):
    # judged, and nothing moved on what was read.
    assert _describe(_arm("told", asked(range(30, 36))), plain)[-1] == (
        "verdict: no measurable effect: the interval of the shift holds 0" + _covered(60, 72)
    )


def test_a_run_with_no_question_in_common_shows_as_zero():
    """Both variants answered it, on different questions: the pooled count alone would hide it."""
    told = _arm("told", {0: [_reading(0, LONG, 0.3)]}, {0: [_reading(0, LONG, 0.3)]})
    plain = _arm(
        "plain", {0: [_reading(0, LONG, 0.1)]}, {0: [_reading(1, LONG, 0.1)]}, extra=None
    )
    lines = _describe(told, plain)
    start = lines.index("-- per question, repeats averaged --")
    assert lines[start + 1].startswith("  1 question(s) read by both: ")
    assert lines[start + 4] == "  by run: run-0 1, run-1 0"


def test_arms_that_do_not_line_up_are_refused():
    """Paired run by run: another order would put one run's questions against another's."""
    first, second = {0: [_reading(0, LONG, 0.3)]}, {0: [_reading(1, LONG, 0.3)]}
    told, plain = _arm("told", first, second), _arm("plain", first, second, extra=None)
    swapped = Arm(plain.variant, list(reversed(plain.runs)), MENTION)
    with pytest.raises(ScoreError, match="compared run by run, in one order"):
        _describe(told, swapped)
    with pytest.raises(ScoreError, match="compared run by run, in one order"):
        _describe(told, Arm(plain.variant, plain.runs[:1], MENTION))
    with pytest.raises(ScoreError, match="only under the same phrases"):
        _describe(told, Arm(plain.variant, plain.runs, ["radar"]))


@pytest.mark.parametrize("phrases", ["research", [], ["research", " "]])
def test_an_arm_is_searched_for_phrases_not_for_letters_or_blanks(phrases):
    with pytest.raises(ScoreError, match="non-blank texts"):
        Arm(variant(name="told"), [], phrases)


def test_a_reading_claims_a_side_at_an_exposure_or_neither():
    with pytest.raises(ScoreError, match="q0: a reading claims a side at an exposure, or neither"):
        _reading(0, LONG, None, executed=0.0)
    with pytest.raises(ScoreError, match="or neither"):
        _reading(0, None, 0.3)


def test_the_same_variant_twice_is_said_to_differ_in_nothing():
    asked = {0: [_reading(0, LONG, 0.3)]}
    lines = _describe(_arm("a", asked), _arm("b", asked))
    assert lines[3] == "the two differ in nothing that reaches the model"
    assert lines[-1] == (
        "verdict: cannot be judged: the variants differ in nothing that reaches the model"
        + _covered(1, 1)
    )


def test_the_opening_of_a_long_extra_context_is_cut_at_sixty_characters():
    told = _arm("told", {0: [_reading(0, LONG, 0.3)]}, extra="x" * 70 + "\nsecond line")
    plain = _arm("plain", {0: [_reading(0, LONG, 0.3)]}, extra=None)
    assert _describe(told, plain)[4] == (
        "extra context of told: 82 character(s) in 2 line(s), opening '" + "x" * 60 + "'"
    )


def test_nothing_read_by_both_says_so():
    told = _arm("told", {0: [_reading(0, None, None, executed=0.0)]})
    plain = _arm("plain", {0: [_reading(0, LONG, 0.3)]}, extra=None)
    lines = _describe(told, plain)
    assert lines[9:] == [
        "-- repeat 0: 1 question(s) answered by both, 0 with a readable claim from both --",
        "  executed reading: toward long 0, toward short 1, unchanged 0 (sign test p 1.000); "
        "mean shift -0.300",
        "-- per question, repeats averaged --",
        "  no question has a readable claim from both",
        "  by run: run-0 0",
        "-- how far apart two answers to one question are (readable claims on the questions "
        "read by both; exposure as a multiple of equity) --",
        "  two repeats of told: fewer than two repeats hold an answer, so there is nothing to compare",
        "  two repeats of plain: fewer than two repeats hold an answer, so there is nothing to compare",
        "  told against plain, same repeat: no pair with a readable claim from both",
        "verdict: cannot be judged: no question has a readable claim from both" + _covered(0, 1),
    ]


def test_variants_that_differ_in_more_are_said_to():
    told = _arm("told", {0: [_reading(0, LONG, 0.3)]})
    other = _arm("other", {0: [_reading(0, LONG, 0.3)]}, extra=None, model="c/d")
    lines = _describe(told, other)
    assert lines[3] == (
        "the two differ in: model, extra context; a shift between them cannot be put down to "
        "the extra context"
    )
    # And the verdict, whose sentence is about the extra context, is not given.
    assert lines[-1] == (
        "verdict: cannot be judged: the variants differ in model, extra context, not in their "
        "extra context alone" + _covered(1, 1)
    )
    # Nor for two variants shown the same extra context by different models.
    lines = _describe(told, _arm("another", {0: [_reading(0, LONG, 0.3)]}, model="c/d"))
    assert lines[3] == (
        "the two differ in: model; a shift between them cannot be put down to the extra context"
    )
    assert lines[-1] == (
        "verdict: cannot be judged: the variants differ in model, not in their extra context "
        "alone" + _covered(1, 1)
    )


def test_the_table_holds_one_row_per_pair():
    told = _arm("told", {0: [_reading(0, LONG, 0.3, confidence=0.8, said=True)]})
    plain = _arm("plain", {0: [_reading(0, None, None, executed=0.1)]}, extra=None)
    header, rows = describe_effect(told, plain, block=6, draws=10, seed=0).table
    assert header[:4] == ["run_id", "input_id", "repeat", "at"]
    assert header[4:10] == [
        "variant_side",
        "variant_exposure",
        "variant_executed_exposure",
        "variant_confidence",
        "variant_risk_action",
        "variant_mentioned",
    ]
    assert header[10:] == [name.replace("variant_", "against_") for name in header[4:10]]
    assert rows == [
        ["run-0", "q0", 0, "2027-01-15T08:00:00+00:00"]
        + ["long", 0.3, 0.3, 0.8, "approved", 1]
        + ["fail_closed", "", 0.1, "", "approved", 0]
    ]


# -- the four arms the experiment ships ---------------------------------------------


def _shipped(arm: str):
    return load_variant(VARIANTS / f"research-{arm}.yaml")


def _section(arm: str) -> str:
    """The extra context of an arm that has one."""
    extra = _shipped(arm).extra_context
    assert extra is not None
    return extra


def test_the_four_arms_are_one_variant_but_for_the_extra_context():
    none, *others = (_shipped(arm) for arm in ARMS)
    assert none.extra_context is None
    assert [v.name for v in (none, *others)] == [f"research-{arm}" for arm in ARMS]
    for other in others:
        assert differences(none, other) == ["extra context"]
        assert other.model_cutoff == none.model_cutoff
    assert len({v.sha for v in (none, *others)}) == 4


def test_the_two_sides_differ_in_the_side_alone():
    long, short = _section("long"), _section("short")
    line = "  Side the rule holds after its latest bar: "
    assert long.count(f"{line}long\n") == 1
    assert long.replace(f"{line}long\n", f"{line}short\n") == short
    # No timestamp: a fixed one would read as a stale signal on all questions but one.
    assert long.startswith(
        "Research signal (rule btc-4h-maker#9, decided on the research radar's own 4h bars, "
        "as of the close this context is as of):\n"
    )


def test_the_neutral_text_has_the_sections_shape_and_none_of_its_content():
    neutral, long = _section("neutral"), _section("long")
    widths = [len(line) for line in neutral.splitlines()]
    assert len(widths) == len(long.splitlines()) == 7
    assert all(
        abs(mine - theirs) <= 2
        for mine, theirs in zip(widths, map(len, long.splitlines()), strict=True)
    )
    assert abs(len(neutral) - len(long)) <= 2
    assert all(line.startswith("  ") for line in neutral.splitlines()[1:])
    # No side, no direction, and not the word the report searches answers for.
    words = set(neutral.casefold().replace("—", " ").replace(",", " ").replace(".", " ").split())
    assert not words & {"long", "short", "bullish", "bearish", "buy", "sell", "up", "down"}
    assert "research" not in neutral.casefold()
    # Nor does the prompt every arm shares hold a phrase the report searches for.
    assert not mentions(_shipped("none").system_prompt, MENTION)


@pytest.fixture
def store(tmp_path: Path) -> Path:
    return write_gate_store(tmp_path / "paper_trading.db")


def test_each_arms_section_is_in_the_message_between_the_context_and_the_format_block(store):
    """Exposure, with no model asked: what an arm adds is in the message, where it should be."""
    with Database(store, migrate=False) as db:
        decisions = load_decisions(db, RUN_ID)
        risk, _ = gate_config(db, RUN_ID)
    split = build_split(decisions.questions, interval="4h", step_ms=STEP_MS)
    chosen = select(
        decisions.questions,
        decisions.inputs,
        split=split,
        step_ms=STEP_MS,
        segments={SegmentName.TRAIN},
    )
    item = prepare(chosen, payload_root=payload_dir(store, RUN_ID), risk=risk)[0]
    context = papers.context_text(0)
    bare = f"## Perpetual market context\n{context}\n\n{FORMAT}"
    assert human_message(item, _shipped("none").extra_context) == bare
    for arm, opening in (
        ("neutral", "Layout note (template perp-context#9, "),
        ("long", "Research signal (rule btc-4h-maker#9, "),
        ("short", "Research signal (rule btc-4h-maker#9, "),
    ):
        extra = _section(arm)
        assert extra.startswith(opening)
        assert human_message(item, extra) == (
            f"## Perpetual market context\n{context}\n\n{extra}\n\n{FORMAT}"
        )


# -- reading the stored answers ------------------------------------------------------


def test_a_refused_question_is_counted_and_a_fail_closed_one_claims_nothing(store):
    with Database(store, migrate=False) as db:
        decisions = load_decisions(db, RUN_ID)
        facts = run_facts(db, RUN_ID)
    assert facts is not None
    split = build_split(decisions.questions, interval="4h", step_ms=STEP_MS)
    # The paper trader's own answers stand in for a repeat: slot 5's is withheld (a
    # question the provider refused), slot 6's is prose.
    clamped, withheld, prose = (papers.input_id(slot) for slot in (4, 5, 6))
    card = score_run(
        decisions.questions,
        [a for a in decisions.answers if a.input_id in (clamped, prose)],
        step_ms=STEP_MS,
        costs=facts.costs,
        split=split,
        only={clamped, withheld, prose},
    )
    responses = {clamped: "Per the Research rule.", prose: "No research debate, no call."}
    readings, refused = read_rows(card.rows, responses, MENTION)
    # The same card as a repeat of a variant that also holds one answer and one refusal
    # the lock leaves unread (two refusals recorded, one of them read), beside a repeat
    # with no row at all: one refused, two unread, one repeat.
    nothing = score_run(
        decisions.questions, [], step_ms=STEP_MS, costs=facts.costs, split=split, only=set()
    )
    stored = {0: {**responses, "a-holdout-question": "Long."}}
    part = arm_run(RUN_ID, {0: card, 1: nothing}, stored, MENTION, refusals=2)
    assert (part.refused, part.unread, list(part.repeats)) == (1, 2, [0])
    # What was put to it on repeat 0, answered or not; repeat 1 was put nothing.
    assert part.asked == {0: frozenset({clamped, withheld, prose})}
    assert part.repeats[0] == readings
    assert refused == 1
    assert set(readings) == {clamped, prose}
    # Slot 4 asks for short at 80%, which the gate clamps to the 60% cap.
    cut, said_nothing = readings[clamped], readings[prose]
    assert (cut.side, cut.exposure, cut.executed_exposure) == (SHORT, -0.8, -0.6)
    assert (cut.confidence, cut.risk_action, cut.mentioned) == (0.9, "clamped", True)
    assert (said_nothing.side, said_nothing.exposure) == (None, None)
    assert said_nothing.executed_exposure == 0.0
    assert (said_nothing.confidence, said_nothing.risk_action, said_nothing.mentioned) == (
        None,
        "invalid_fail_closed",
        False,
    )


# -- the command ---------------------------------------------------------------------


# Long at 30% and confident, with a rationale that names the research section.
TOLD = {
    slot: decision_text(
        "set_target", "long", 30, 0.8, rationale=f"Question {slot}: the Research signal holds long."
    )
    for slot in (5, 7)
}


def _variant_file(directory: Path, name: str, extra: str | None, model: str = "echo") -> Path:
    """A variant on ``fixture/<model>`` with no cutoff: the report reads no later price."""
    return write_variant(directory, name, cutoff=None, model=model, extra_context=extra)


def _replay(
    store: Path,
    variant_file: Path,
    run_id: str,
    monkeypatch,
    model: Echo,
    segments: tuple[str, ...] = ("train", "validation"),
) -> None:
    monkeypatch.setattr(cli, "_build_model", lambda _variant: model)
    for segment in segments:
        look = ("--holdout",) if segment == "holdout" else ()
        argv = replay_argv(
            store, variant_file, "--repeats", "2", "--segment", segment, *look, run_id=run_id
        )
        assert cli.main(argv) == 0


@pytest.fixture(scope="module")
def asked_once(tmp_path_factory) -> Path:
    """Two runs in one store; ``plain`` and ``told`` asked both, twice each. Built once."""
    root = tmp_path_factory.mktemp("asked")
    store = write_gate_store(root / "paper_trading.db")
    write_gate_store(store, run_id=OTHER_RUN)
    plain = _variant_file(root, "plain", None)
    told = _variant_file(root, "told", "Research signal: long.")
    with pytest.MonkeyPatch.context() as patch:
        for run_id in (RUN_ID, OTHER_RUN):
            _replay(store, plain, run_id, patch, Echo())
            _replay(store, told, run_id, patch, Echo(text_for=TOLD))
    return root


@pytest.fixture
def asked(asked_once: Path, tmp_path: Path) -> Path:
    """This test's own copy of the asked stores (some tests write to them): the paper store."""
    shutil.copytree(asked_once, tmp_path, dirs_exist_ok=True)
    return tmp_path / "paper_trading.db"


def _effect(store: Path, *extra: str, variants: tuple[str, ...] = ("told",)) -> list[str]:
    argv = ["effect", "--db", str(store), "--replay-db", str(store.parent / "replay.sqlite")]
    for name in variants:
        argv += ["--variant", name]
    return [*argv, "--against", "plain", *extra]


def test_the_command_reads_one_run(asked, capsys):
    capsys.readouterr()
    assert cli.main(_effect(asked, "--run-id", RUN_ID, "--block", "7", "--draws", "50")) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "decision effect: told against plain"
    assert out[1].startswith("variant told (") and out[1].endswith(", with extra context")
    assert out[2].startswith("against variant plain (")
    assert out[3] == "the two differ in: extra context"
    assert out[4] == (
        "extra context of told: 22 character(s) in 1 line(s), opening 'Research signal: long.'"
    )
    assert out[5] == "extra context of plain: none"
    assert out[6].startswith(f"runs: {RUN_ID} (split pinned ") and out[6].endswith(")")
    assert out[7] == "-- answers (train and validation; the holdout is never read) --"
    # Eight questions, two repeats; slot 5 is flat from plain and long from told.
    assert out[8] == (
        "  told: 16 answer(s) in 2 repeat(s): long 12, short 2, flat 0, fail-closed 2; 0 refused "
        f"by the provider; {SEARCHED}: 4/16 (25.0%)"
    )
    assert out[9] == (
        "  plain: 16 answer(s) in 2 repeat(s): long 10, short 2, flat 2, fail-closed 2; 0 "
        f"refused by the provider; {SEARCHED}: 0/16 (0.0%)"
    )
    block = [
        "8 question(s) answered by both, 7 with a readable claim from both --",
        # Slot 5 alone moves the model reading: +0.30 over seven pairs.
        "  model reading: toward long 1, toward short 0, unchanged 6 (sign test p 1.000); "
        "mean shift +0.043",
        "  sides, plain -> told: long -> long 5, short -> short 1, flat -> long 1",
        # Slots 5 and 7 move the executed one: +0.30 each over eight pairs.
        "  executed reading: toward long 2, toward short 0, unchanged 6 (sign test p 0.500); "
        "mean shift +0.075",
        # 0.8 .6 .9 .5 .9 .8 .8 against 0.8 .6 .9 .5 .9 .7 .2; the prose states none.
        "  confidence where both state one: n 7, mean 0.757 against 0.657",
    ]
    assert out[10:20] == [
        f"-- repeat 0: {block[0]}",
        *block[1:],
        f"-- repeat 1: {block[0]}",
        *block[1:],
    ]
    assert out[20:] == [
        "-- per question, repeats averaged --",
        # 0.30 + 0.306 + 0.32 + 0.50 - 0.80 + 0.30 + 0.30 over seven, against the same with
        # slot 5 at 0.
        "  7 question(s) read by both: mean requested exposure +0.175 against +0.132, "
        "shift +0.043",
        # One block as long as the run is the run, whatever its start.
        "  90% interval of the shift [+0.043, +0.043]: 1 circular block(s) of 7 question(s) "
        "within each run, 50 draws, seed 0",
        "  toward long 1, toward short 0, unchanged 6 (sign test p 1.000)",
        f"  by run: {RUN_ID} 7",
        "-- how far apart two answers to one question are (readable claims on the questions "
        "read by both; exposure as a multiple of equity) --",
        "  two repeats of told: side differs in 0 of 7 pair(s) (0.0%), mean absolute exposure "
        "difference 0.000",
        "  two repeats of plain: side differs in 0 of 7 pair(s) (0.0%), mean absolute exposure "
        "difference 0.000",
        "  told against plain, same repeat: side differs in 2 of 14 pair(s) (14.3%), mean absolute "
        "exposure difference 0.043",
        # Sixteen question-repeats; the prose (slot 6) claims nothing on either repeat.
        "verdict: cannot be judged: the bar is read with blocks of 6 question(s), not 7"
        + _covered(14, 16),
    ]


def test_the_command_pools_runs_and_reads_each_variant_against_the_one_control(
    asked, tmp_path, monkeypatch, capsys
):
    other = _variant_file(tmp_path, "other", "Research signal: long.", model="other")
    for run_id in (RUN_ID, OTHER_RUN):
        _replay(asked, other, run_id, monkeypatch, Echo())
    capsys.readouterr()
    argv = _effect(asked, "--run-id", RUN_ID, "--run-id", OTHER_RUN, variants=("told", "other"))
    assert cli.main(argv) == 0
    out = capsys.readouterr().out.splitlines()
    first, second = out[: out.index("")], out[out.index("") + 1 :]
    assert first[0] == "decision effect: told against plain"
    assert first[6].startswith(f"runs: {RUN_ID} (split pinned ")
    assert f"), {OTHER_RUN} (split pinned " in first[6]
    assert first[8].startswith("  told: 32 answer(s) in 2 repeat(s): long 24, ")
    assert first[10] == (
        "-- repeat 0: 16 question(s) answered by both, 14 with a readable claim from both --"
    )
    assert first[21].startswith("  14 question(s) read by both: ")
    assert first[22].endswith(
        "]: 4 circular block(s) of 6 question(s) within each run, 10000 draws, seed 0"
    )
    # ``other`` says what ``plain`` says, on another model: nothing moves, and the report
    # says the two differ in more than the section.
    assert second[0] == "decision effect: other against plain"
    assert second[3] == (
        "the two differ in: model, extra context; a shift between them cannot be put down to "
        "the extra context"
    )
    assert second[11] == (
        "  model reading: toward long 0, toward short 0, unchanged 14 (sign test p 1.000); "
        "mean shift +0.000"
    )
    assert second[-1] == (
        "verdict: cannot be judged: the variants differ in model, extra context, not in their "
        "extra context alone" + _covered(28, 32)
    )


def test_a_run_recorded_with_the_section_already_in_its_context_is_noted(asked, capsys):
    """An extra context there is a second section beside the recorded one: said, not refused."""
    conn = sqlite3.connect(asked)
    try:
        with conn:
            conn.execute(
                "UPDATE ai_inputs SET context_shape = 'price|autoresearch|position' "
                "WHERE run_id = ? AND input_id IN (?, ?, ?, ?)",
                # Slot 8 is a holdout question: the report does not read it, or count it.
                (OTHER_RUN, *(f"{OTHER_RUN}-{papers.input_id(slot)}" for slot in (0, 1, 2, 8))),
            )
    finally:
        conn.close()
    capsys.readouterr()
    argv = _effect(asked, "--run-id", RUN_ID, "--run-id", OTHER_RUN, "--draws", "20")
    assert cli.main(argv) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[6].startswith(f"runs: {RUN_ID} (split pinned ")
    assert out[7] == (
        f"note: 3 of 8 question(s) of {OTHER_RUN} were recorded with a research section "
        "already in the context; an extra context is one more section beside it"
    )
    assert out[8] == "-- answers (train and validation; the holdout is never read) --"


def test_the_phrases_searched_for_are_the_operators_to_name(asked, capsys):
    capsys.readouterr()
    argv = _effect(asked, "--run-id", RUN_ID, "--mention", "Question 5", "--mention", "radar")
    assert cli.main(argv) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[8].endswith("; mentioning 'Question 5' or 'radar': 2/16 (12.5%)")
    assert out[9].endswith("; mentioning 'Question 5' or 'radar': 0/16 (0.0%)")


def test_the_command_writes_nothing_to_either_store(asked):
    replay_db = asked.parent / "replay.sqlite"
    before = replay_db.read_bytes(), asked.read_bytes()
    assert cli.main(_effect(asked, "--run-id", RUN_ID, "--draws", "20")) == 0
    assert (replay_db.read_bytes(), asked.read_bytes()) == before


def test_out_writes_the_summary_and_one_row_per_pair(asked, tmp_path, capsys):
    out_dir = tmp_path / "out"
    capsys.readouterr()
    argv = _effect(asked, "--run-id", RUN_ID, "--draws", "20", "--out", str(out_dir))
    assert cli.main(argv) == 0
    printed = capsys.readouterr().out
    stem = "effect-told-against-plain"
    assert (out_dir / f"{stem}-summary.txt").read_text(encoding="utf-8") == printed
    with (out_dir / f"{stem}-decisions.csv").open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 16  # eight questions, two repeats
    moved = [row for row in rows if row["variant_side"] != row["against_side"]]
    assert {(row["input_id"], row["repeat"]) for row in moved} == {
        (papers.input_id(5), "0"),
        (papers.input_id(5), "1"),
    }
    assert {(row["variant_side"], row["against_side"]) for row in moved} == {("long", "flat")}
    assert {row["run_id"] for row in rows} == {RUN_ID}
    assert sum(int(row["variant_mentioned"]) for row in rows) == 4


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (("--run-id", RUN_ID, "--run-id", RUN_ID), f"--run-id names {RUN_ID} more than once"),
        (("--run-id", RUN_ID, "--block", "0"), "--block must be at least 1, got 0"),
        (("--run-id", RUN_ID, "--draws", "0"), "--draws must be at least 1, got 0"),
        (("--run-id", RUN_ID, "--mention", ""), "--mention needs a phrase, got ''"),
        (("--run-id", RUN_ID, "--mention", " "), "--mention needs a phrase, got ' '"),
        (("--run-id", RUN_ID, "--out", ""), "--out needs a directory, got ''"),
        (("--run-id", "paper-NONE"), "run 'paper-NONE' not found"),
        (("--run-id", RUN_ID, "--variant", "plain"), "name plain more than once"),
        (("--run-id", RUN_ID, "--variant", "told"), "name told more than once"),
        (("--run-id", RUN_ID, "--variant", "nobody"), "no variant named 'nobody'"),
    ],
)
def test_the_command_refuses_by_name(asked, capsys, extra, message):
    capsys.readouterr()
    assert cli.main(_effect(asked, *extra)) == 1
    assert message in capsys.readouterr().err


def test_a_missing_store_is_refused_not_created(asked, tmp_path, capsys):
    argv = _effect(asked, "--run-id", RUN_ID)
    argv[argv.index("--replay-db") + 1] = str(tmp_path / "typo.sqlite")
    assert cli.main(argv) == 1
    assert "does not exist" in capsys.readouterr().err
    assert not (tmp_path / "typo.sqlite").exists()


def test_a_run_never_replayed_or_a_variant_never_asked_it_is_refused(
    asked, tmp_path, monkeypatch, capsys
):
    write_gate_store(asked, run_id="paper-GATE3")
    assert cli.main(_effect(asked, "--run-id", "paper-GATE3")) == 1
    assert "run 'paper-GATE3' has no split pinned in " in capsys.readouterr().err
    late = _variant_file(tmp_path, "late", "Research signal: short.")
    _replay(asked, late, RUN_ID, monkeypatch, Echo())
    argv = _effect(asked, "--run-id", RUN_ID, "--run-id", OTHER_RUN, variants=("late",))
    assert cli.main(argv) == 1
    assert f"variant 'late' has no answers for run {OTHER_RUN!r} in " in capsys.readouterr().err


def test_a_graph_variant_is_refused(asked, tmp_path, capsys):
    graph = write_graph_variant(tmp_path, "graph")
    replay_db = str(asked.parent / "replay.sqlite")
    assert cli.main(["register", "--variant", str(graph), "--replay-db", replay_db]) == 0
    capsys.readouterr()
    assert cli.main(_effect(asked, "--run-id", RUN_ID, variants=("graph",))) == 1
    assert "'graph': a graph variant is asked each question from its own simulated book" in (
        capsys.readouterr().err
    )


def test_answers_to_holdout_questions_are_counted_as_unread_not_dropped(
    asked, tmp_path, monkeypatch, capsys
):
    told = tmp_path / "told.yaml"
    _replay(asked, told, RUN_ID, monkeypatch, Echo(text_for=TOLD), segments=("holdout",))
    capsys.readouterr()
    assert cli.main(_effect(asked, "--run-id", RUN_ID, "--draws", "20")) == 0
    out = capsys.readouterr().out.splitlines()
    # Two holdout questions, two repeats: stored, and behind the lock.
    assert out[8] == (
        "  told: 16 answer(s) in 2 repeat(s): long 12, short 2, flat 0, fail-closed 2; 0 refused "
        f"by the provider; {SEARCHED}: 4/16 (25.0%); 4 more answer(s) or refusal(s) recorded outside train and validation, not read"
    )
    assert out[9].endswith(f"{SEARCHED}: 0/16 (0.0%)")


def test_a_variant_asked_only_the_holdout_is_refused_not_reported_empty(
    asked, tmp_path, monkeypatch, capsys
):
    held = _variant_file(tmp_path, "held", "Research signal: short.")
    _replay(asked, held, RUN_ID, monkeypatch, Echo(), segments=("holdout",))
    capsys.readouterr()
    assert cli.main(_effect(asked, "--run-id", RUN_ID, variants=("held",))) == 1
    assert (
        f"variant 'held' was asked only holdout questions of run {RUN_ID!r}, which effect never "
        "reads"
    ) in capsys.readouterr().err


def test_a_variant_refused_throughout_is_reported_not_called_holdout_only(asked, tmp_path, capsys):
    """Refused questions are train and validation questions it WAS asked: nothing is behind the lock."""
    refused = _variant_file(tmp_path, "refused", "Research signal: flat.")
    replay_db = asked.parent / "replay.sqlite"
    assert cli.main(["register", "--variant", str(refused), "--replay-db", str(replay_db)]) == 0
    conn = sqlite3.connect(replay_db)
    try:
        with conn:
            (sha,) = conn.execute(
                "SELECT variant_sha FROM variants WHERE name = 'refused'"
            ).fetchone()
            conn.executemany(
                "INSERT INTO failures (variant_sha, run_id, input_id, repeat, failed_at, error) "
                "VALUES (?, ?, ?, 0, '2027-01-20T00:00:00+00:00', 'HTTP 400: too long')",
                # Two train questions, and one of the holdout's.
                [(sha, RUN_ID, papers.input_id(slot)) for slot in (0, 1, 8)],
            )
    finally:
        conn.close()
    capsys.readouterr()
    argv = _effect(asked, "--run-id", RUN_ID, "--draws", "20", variants=("refused",))
    assert cli.main(argv) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[8] == (
        "  refused: 0 answer(s) in 0 repeat(s): long 0, short 0, flat 0, fail-closed 0; 2 refused "
        f"by the provider; {SEARCHED}: 0/0; 1 more answer(s) or refusal(s) recorded outside "
        "train and validation, not read"
    )
    # ``plain`` was asked sixteen question-repeats; the two refused ones are among them.
    assert out[-1] == (
        "verdict: cannot be judged: no question has a readable claim from both" + _covered(0, 16)
    )
