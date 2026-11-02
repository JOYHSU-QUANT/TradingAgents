"""The asking walk's pieces that stand alone: the guardrail reader and the report.

The walk itself (resuming, the book check, the refusals) is exercised end to
end through the command in ``test_backtest_cli.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import pytest

from contrib.replay.paper_store import InputFacts
from contrib.replay.upstream import GuardrailSide, TargetSide
from contrib.replay.walk import WalkReport, guardrail_reader

from .conftest import fixture_questions


@dataclass(frozen=True)
class _Side:
    value: str


@dataclass(frozen=True)
class _Reading:
    side: _Side | None
    close_time: int


class _Timeline:
    """A rule timeline that answers by instant: a side, a flat rule, or no reading."""

    def __init__(self, readings: dict[int, _Reading | None]) -> None:
        self.readings = readings

    def reading_at(self, at_ms: int) -> _Reading | None:
        return self.readings[at_ms]


def _facts(side: GuardrailSide | None) -> InputFacts:
    return InputFacts(
        input_id="q",
        payload_name=None,
        payload_hash=None,
        mark=Decimal(100),
        account_equity=Decimal(1000),
        side=TargetSide.FLAT,
        size=None,
        margin_pct=None,
        leverage=Decimal(1),
        guardrail_side=side,
    )


def test_recorded_reads_the_rows_side_and_off_reads_none():
    question = fixture_questions()[0]
    assert (
        guardrail_reader("recorded")(question, _facts(GuardrailSide.SHORT)) is GuardrailSide.SHORT
    )
    assert guardrail_reader("recorded")(question, _facts(None)) is None
    assert guardrail_reader("off")(question, _facts(GuardrailSide.SHORT)) is None


def test_rule_reads_the_timeline_as_the_live_gate_reads_the_signal():
    questions = fixture_questions()[:3]
    timeline = _Timeline(
        {
            questions[0].at_ms: _Reading(_Side("long"), 1),
            questions[1].at_ms: _Reading(None, 2),  # a flat rule
            questions[2].at_ms: None,  # no reading: before the history, or a stale bar
        }
    )
    read = guardrail_reader("rule", timeline)
    assert [read(q, _facts(None)) for q in questions] == [
        GuardrailSide.LONG,
        GuardrailSide.FLAT,
        GuardrailSide.UNKNOWN,
    ]


def test_the_rule_needs_its_timeline_and_an_unknown_mode_is_refused():
    with pytest.raises(ValueError, match="needs the rule's timeline"):
        guardrail_reader("rule")
    with pytest.raises(ValueError, match="no guardrail mode named 'always'"):
        guardrail_reader("always")


def test_the_walk_report_says_what_was_asked_what_it_cost_and_what_stopped_it():
    full = WalkReport(
        asked=5,
        already_stored=2,
        refused=1,
        fail_closed=1,
        input_tokens=500,
        output_tokens=50,
        graph_input_tokens=2500,
        graph_output_tokens=250,
        completions=25,
        unreported=1,
        ruined=1,
        stopped_at_limit=True,
    )
    lines = full.describe()
    assert lines[0] == "asked: 5 new answer(s); already stored, skipped: 2"
    assert lines[1] == "fail-closed among the new answers: 1"
    assert lines[2] == (
        "tokens reported: portfolio manager 500 in, 50 out; whole graph 2500 in, 250 out over "
        "25 completion(s)"
    )
    assert lines[3].startswith("questions the provider refused for their own sake: 1")
    assert lines[4].startswith(
        "answers whose portfolio manager call the usage collector recorded nothing"
    )
    assert lines[5].startswith("repeat(s) ruined on the way: 1")
    assert lines[6] == "stopped at --limit; the same command continues from here"
    quiet = WalkReport(8, 0, 0, 0, 800, 80, 4000, 400, 40, 0, 0, False)
    assert len(quiet.describe()) == 3
    costly = WalkReport(8, 0, 0, 0, 800, 80, 4000, 400, 40, 0, 0, False, 2, 900, 30)
    lines = costly.describe()
    assert lines[3] == (
        "tokens spent on graph runs that failed or were refused: 900 in, 30 out (paid for, "
        "not in the figures above)"
    )
    assert lines[4].startswith("funding settlements missing from the research store")
