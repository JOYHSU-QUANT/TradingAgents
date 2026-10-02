"""The trend guardrail's shadow report: the verdict table, the rows, and the command.

The fixture run's eleven questions are read against a rule whose side can be
followed by eye: long above 102, short below 98, flat in between. The
research store holds one bar per slot, each closing exactly at a question's
instant, so the side a question reads is the one decided at the bar that
closed there.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from contrib.hyperliquid_perp.domains.perp.schema import Candle
from contrib.replay import guardrail as guardrail_module
from contrib.replay.cli import main
from contrib.replay.guardrail import (
    BLOCK,
    CLOSE,
    PASS,
    UNKNOWN,
    describe_shadow,
    held_after,
    interventions,
    shadow,
    shadow_table,
    verdict_of,
)
from contrib.replay.score import Answer
from contrib.replay.upstream import (
    ResearchStore,
    TargetSide,
    build_timeline,
    from_epoch_ms,
    load_rule,
    payload_dir,
)

from .conftest import (
    ANCHOR_MS,
    COIN,
    ROWS,
    RUN_ID,
    STEP_MS,
    at_ms,
    fixture_answers,
    fixture_questions,
    input_id,
    run_config,
    write_paper_store,
)
from .papers import RUN_ID as GATE_RUN_ID, write_gate_store

LONG, SHORT, FLAT = TargetSide.LONG, TargetSide.SHORT, TargetSide.FLAT

_BAND = {
    "family": "breakout",
    "entry": {
        "long": [{"left": "close", "op": ">", "right": 102}],
        "short": [{"left": "close", "op": "<", "right": 98}],
    },
    "exit": {
        "long": [{"left": "close", "op": "<", "right": 101}],
        "short": [{"left": "close", "op": ">", "right": 99}],
    },
    "sizing": {"mode": "fixed_margin_fraction", "fraction": 0.5},
}

# One bar per slot, the first closing at slot 0's instant. The side the rule
# holds after each: long, long, long, flat, flat, flat, short, short, flat,
# flat, short, short — which slots 0..11 read in that order.
_CLOSES = (105, 106, 104, 100, 100, 100, 95, 96, 100, 100, 95, 94)

# The same history ending in a rally: the rule is long at slots 10 and 11,
# where the book is short on a maintain and on a round with no answer.
_RALLY = (*_CLOSES[:10], 105, 106)

# What each fixture question reads, by slot: the rule's side, the side the
# book held once the decision was applied, and the verdict.
_EXPECTED = {
    0: (LONG, LONG, PASS),  # an order with the rule
    1: (LONG, LONG, PASS),  # a maintain with the rule
    2: (LONG, SHORT, BLOCK),  # a clamped flip to short against a long rule
    3: (FLAT, SHORT, CLOSE),  # fail-closed, still short under a flat rule
    5: (FLAT, FLAT, PASS),  # a flat target
    6: (SHORT, FLAT, PASS),  # a rejected long: the book stays flat
    7: (SHORT, LONG, CLOSE),  # inside the deadband: no order, still long
    8: (FLAT, LONG, CLOSE),  # fail-closed, still long under a flat rule
    9: (FLAT, SHORT, BLOCK),  # an order under a flat rule
    10: (SHORT, SHORT, PASS),
    11: (SHORT, SHORT, PASS),  # no answer: the book stays where it was
}


def _candles(closes=_CLOSES, *, shift: int = 0) -> list[Candle]:
    """One bar per close, the first closing at slot ``shift``'s instant."""
    made = []
    for index, close in enumerate(closes, start=shift):
        price = Decimal(close)
        made.append(
            Candle(
                open_time=ANCHOR_MS + (index - 1) * STEP_MS,
                close_time=at_ms(index),
                open=price,
                high=price + 10,
                low=price - 10,
                close=price,
                volume=Decimal("1"),
            )
        )
    return made


def _rule_file(tmp_path: Path) -> Path:
    path = tmp_path / "band.json"
    path.write_text(json.dumps(_BAND), encoding="utf-8")
    return path


def _research(tmp_path: Path, closes=_CLOSES, *, shift: int = 0) -> Path:
    path = tmp_path / "autoresearch.sqlite"
    with ResearchStore(path) as store:
        store.upsert_candles(COIN, "4h", _candles(closes, shift=shift))
    return path


def _rows(tmp_path: Path, closes=_CLOSES):
    with ResearchStore(_research(tmp_path, closes)) as store:
        timeline = build_timeline(store, load_rule(_rule_file(tmp_path)), coin=COIN)
    return shadow(fixture_questions(), fixture_answers(), timeline)


def _at(slot: int) -> str:
    return f"{from_epoch_ms(at_ms(slot)):%Y-%m-%d %H:%M}"


# -- the verdict table ----------------------------------------------------------


@pytest.mark.parametrize(
    ("rule_side", "held", "order_created", "verdict"),
    [
        # A flat book passes under anything, an unknown rule side included.
        (LONG, FLAT, False, PASS),
        (SHORT, FLAT, True, PASS),
        (FLAT, FLAT, True, PASS),
        (None, FLAT, False, PASS),
        # The rule's own side passes, held or just ordered.
        (LONG, LONG, False, PASS),
        (LONG, LONG, True, PASS),
        (SHORT, SHORT, False, PASS),
        (SHORT, SHORT, True, PASS),
        # Against the rule: an order is blocked, a held position is closed.
        (LONG, SHORT, True, BLOCK),
        (LONG, SHORT, False, CLOSE),
        (SHORT, LONG, True, BLOCK),
        (SHORT, LONG, False, CLOSE),
        # A flat rule lets nothing be held.
        (FLAT, LONG, True, BLOCK),
        (FLAT, LONG, False, CLOSE),
        (FLAT, SHORT, True, BLOCK),
        (FLAT, SHORT, False, CLOSE),
        # A sized book under a rule side nobody knows is not judged.
        (None, LONG, True, UNKNOWN),
        (None, SHORT, False, UNKNOWN),
    ],
)
def test_the_verdict_table(rule_side, held, order_created, verdict):
    assert verdict_of(rule_side, held, order_created=order_created) == verdict


def test_the_book_holds_the_approved_target_only_when_an_order_was_created():
    answers = {answer.input_id: answer for answer in fixture_answers()}
    held = {
        row.slot: held_after(question, answers.get(question.input_id))
        for row, question in zip(ROWS, fixture_questions(), strict=True)
    }
    assert held == {slot: expected[1] for slot, expected in _EXPECTED.items()}


def test_an_order_for_no_margin_leaves_the_book_flat_whatever_side_it_names():
    # The gate's shape allows it (a sized set_target of zero margin), and the
    # fixture's only zero-margin order names ``flat``, which would read flat
    # either way. A long target of nothing is not a long book.
    nothing = Answer(
        input_id=input_id(0),
        decision_mode="set_target",
        target_side="long",
        requested_margin_pct=0.0,
        approved_margin_pct=0.0,
        risk_action="approved",
        risk_reason=None,
        confidence=0.8,
        order_created=True,
    )
    assert held_after(fixture_questions()[0], nothing) is FLAT


# -- the rows -----------------------------------------------------------------------


def test_every_question_is_read_against_the_rule_side_at_its_instant(tmp_path):
    rows = _rows(tmp_path)
    assert {
        row.question.input_id: (row.rule_side, row.held, row.verdict) for row in rows
    } == {input_id(slot): expected for slot, expected in _EXPECTED.items()}
    # The side read is the one decided at the bar that closed at the question.
    assert all(row.rule_decided_ms == row.question.at_ms for row in rows)


def test_a_question_the_research_store_does_not_reach_reads_unknown(tmp_path):
    # The store ends at slot 4's instant: two bars on the side is still read
    # (the live reader's bound), three bars on it is not.
    rows = {row.question.input_id: row for row in _rows(tmp_path, _CLOSES[:5])}
    assert rows[input_id(6)].rule_side is FLAT
    assert rows[input_id(6)].rule_decided_ms == at_ms(4)
    for slot in (7, 8, 9, 10, 11):
        row = rows[input_id(slot)]
        assert (row.rule_side, row.rule_decided_ms, row.verdict) == (None, None, UNKNOWN)
    # In the CSV such a row has no rule side and no bar it was read from.
    header, table = shadow_table(list(rows.values()))
    unread = dict(zip(header, next(r for r in table if r[0] == input_id(7)), strict=True))
    assert (unread["rule_side"], unread["rule_decided_at"]) == (None, None)
    assert (unread["held_side"], unread["verdict"]) == ("long", UNKNOWN)


@pytest.mark.parametrize(
    ("verdicts", "count"),
    [
        ((PASS, PASS), 0),
        ((BLOCK, BLOCK), 2),  # every blocked order is one
        ((CLOSE, CLOSE, CLOSE), 1),  # one position, however long it stays held
        ((BLOCK, CLOSE, CLOSE), 1),  # the paper book kept what was blocked: the same position
        ((CLOSE, BLOCK), 2),  # closed, then an order refused
        ((CLOSE, PASS, CLOSE), 2),  # a pass ends the stretch
        ((CLOSE, UNKNOWN, CLOSE), 1),  # a question with no reading does not end one
        ((UNKNOWN, CLOSE), 1),  # nor start one: the close after it is the first found
        ((CLOSE, PASS, UNKNOWN, CLOSE), 2),  # nor reopen one a pass has ended
    ],
)
def test_interventions_count_orders_and_positions_not_questions(tmp_path, verdicts, count):
    row = _rows(tmp_path)[0]
    assert interventions([replace(row, verdict=verdict) for verdict in verdicts]) == count


# -- the summary -----------------------------------------------------------------------


def test_describe_counts_the_verdicts_and_lists_each_refused_question(tmp_path):
    lines = describe_shadow(_rows(tmp_path))
    assert lines[:4] == [
        "questions: 11 (10 answered)",
        "rule side at the decisions: long 3, flat 4, short 4, unknown 0",
        "verdicts: pass 6, block_to_flat 2, close_position 3, rule_unknown 0",
        "orders created: 4, of which the guardrail refuses 2",
    ]
    assert lines[4].startswith("questions on which the book was outside the guardrail: 5 (")
    # Slot 2's order, the position found at slot 7 (still held at slot 8),
    # and slot 9's order; slot 3's close is the position slot 2's order opened.
    assert lines[5].startswith("interventions: 3 (")
    # Slot 7 is a decision that created no order (a target inside the
    # deadband); slots 3 and 8 are fail-closed rounds.
    assert lines[6] == (
        "close_position by cause: a decision that created no order 1, no decision 2"
    )
    assert lines[7:] == [
        f"  {_at(2)} rule long; set_target short 20% -> block_to_flat",
        f"  {_at(3)} rule flat; holds short (invalid_fail_closed) -> close_position",
        f"  {_at(7)} rule short; holds long (within_deadband) -> close_position",
        f"  {_at(8)} rule flat; holds long (invalid_fail_closed) -> close_position",
        f"  {_at(9)} rule flat; set_target short 50% -> block_to_flat",
    ]


def test_a_round_with_no_answer_is_judged_on_the_position_it_left_held(tmp_path):
    # Decided 2026-10-02: the guardrail reads the book, not the answer. Under
    # the rally the rule is long at slots 10 (a maintain, short) and 11 (no
    # answer, short): both are closes, and they are one position.
    lines = describe_shadow(_rows(tmp_path, _RALLY))
    assert "verdicts: pass 4, block_to_flat 2, close_position 5, rule_unknown 0" in lines
    assert any(line.startswith("interventions: 3 (") for line in lines)
    assert "close_position by cause: a decision that created no order 2, no decision 3" in lines
    assert lines[-2:] == [
        f"  {_at(10)} rule long; holds short (maintain_current) -> close_position",
        f"  {_at(11)} rule long; no answer, holds short -> close_position",
    ]


def test_describe_stops_listing_at_the_cap_and_says_how_many_it_left_out(tmp_path, monkeypatch):
    monkeypatch.setattr(guardrail_module, "_LISTED", 2)
    lines = describe_shadow(_rows(tmp_path))
    assert lines[7:] == [
        f"  {_at(2)} rule long; set_target short 20% -> block_to_flat",
        f"  {_at(3)} rule flat; holds short (invalid_fail_closed) -> close_position",
        "  ... and 3 more (the CSV lists every question)",
    ]


def test_describe_says_so_when_the_guardrail_changes_nothing(tmp_path):
    passing = [row for row in _rows(tmp_path) if row.verdict == PASS]
    assert describe_shadow(passing)[-1] == "the guardrail would have changed nothing in this run"


def test_describe_does_not_conclude_past_the_questions_it_could_not_read(tmp_path):
    # The store ends at slot 4: slots 7 to 11 have no rule side. They are
    # counted and named before anything is concluded, and "changed nothing"
    # is never said over them.
    rows = _rows(tmp_path, _CLOSES[:5])
    lines = describe_shadow(rows)
    assert lines[:5] == [
        "questions: 11 (10 answered)",
        "rule side at the decisions: long 3, flat 3, short 0, unknown 5",
        "verdicts: pass 4, block_to_flat 1, close_position 1, rule_unknown 5",
        # Slot 9's order sits at a side nobody knows: not refused, and said.
        "orders created: 4, of which the guardrail refuses 1 (1 more at a rule side not known)",
        "rule side not known at 5 question(s): the research store does not reach them, or the "
        "rule could not be evaluated there; a position held there is not judged (rule_unknown), "
        "and a flat book passes unread",
    ]
    assert lines[6].startswith("interventions: 1 (")
    unrefused = describe_shadow([row for row in rows if row.verdict in (PASS, UNKNOWN)])
    assert unrefused[-1] == "no question that could be read was refused"
    assert "the guardrail would have changed nothing in this run" not in unrefused


def test_describe_judges_nothing_when_no_question_has_a_rule_side(tmp_path):
    # Every row unread: the counts, and one sentence in place of a conclusion.
    unread = [
        replace(
            row,
            rule_side=None,
            rule_decided_ms=None,
            verdict=verdict_of(
                None, row.held, order_created=row.answer is not None and row.answer.order_created
            ),
        )
        for row in _rows(tmp_path)
    ]
    lines = describe_shadow(unread)
    # Slots 5 and 6 hold nothing, so they pass unread; the other nine hold a
    # position nobody can judge. All four orders are among the unread.
    assert lines == [
        "questions: 11 (10 answered)",
        "rule side at the decisions: long 0, flat 0, short 0, unknown 11",
        "verdicts: pass 2, block_to_flat 0, close_position 0, rule_unknown 9",
        "orders created: 4, of which the guardrail refuses 0 (3 more at a rule side not known)",
        "no question has a rule side: the research store does not reach this run, and nothing "
        "was judged",
    ]


def test_the_csv_has_one_row_per_question(tmp_path):
    header, table = shadow_table(_rows(tmp_path))
    assert header == [
        "input_id", "at", "mark", "rule_side", "rule_decided_at", "current_side",
        "decision_mode", "target_side", "approved_margin_pct", "order_created",
        "no_order_reason", "held_side", "verdict",
    ]  # fmt: skip
    assert len(table) == 11
    by_input = {row[0]: row[1:] for row in table}

    def stamp(slot: int) -> str:
        return from_epoch_ms(at_ms(slot)).isoformat()

    # A blocked order, a close with no order, and the question nobody answered.
    assert by_input[input_id(2)] == [
        stamp(2), 99.0, "long", stamp(2), "long",
        "set_target", "short", 20.0, True, None, "short", BLOCK,
    ]  # fmt: skip
    assert by_input[input_id(7)] == [
        stamp(7), 104.0, "short", stamp(7), "long",
        "set_target", "long", 30.0, False, "within_deadband", "long", CLOSE,
    ]  # fmt: skip
    assert by_input[input_id(11)] == [
        stamp(11), 109.0, "short", stamp(11), "short",
        None, None, None, None, None, "short", PASS,
    ]  # fmt: skip


# -- the command ------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> Path:
    return write_paper_store(
        tmp_path / "paper_trading.db",
        payload_root=payload_dir(tmp_path / "paper_trading.db", RUN_ID),
        config=run_config(),
    )


def _guardrail(store: Path, tmp_path: Path, *extra: str) -> list[str]:
    command = ["guardrail", "--db", str(store), "--run-id", RUN_ID]
    command += ["--research-db", str(_research(tmp_path))]
    command += ["--rule", str(_rule_file(tmp_path))]
    return [*command, *extra]


def test_guardrail_prints_the_rule_and_the_run_under_it(store, tmp_path, capsys):
    assert main(_guardrail(store, tmp_path)) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == (
        f"guardrail shadow: run {RUN_ID} (BTC, 4h cycle; interval from the run's recorded config)"
    )
    assert out[1].startswith("guardrail rule: band@")
    # The fixture's two cycles that are not questions are said, not dropped.
    assert "cycles that failed before an input row was written (not questions): 1" in out
    assert "cycles still in progress when the store was read (left out): 1" in out
    assert "verdicts: pass 6, block_to_flat 2, close_position 3, rule_unknown 0" in out
    assert "orders created: 4, of which the guardrail refuses 2" in out


def test_out_writes_one_csv_row_per_question_and_the_summary(store, tmp_path, capsys):
    out_dir = tmp_path / "out"
    assert main(_guardrail(store, tmp_path, "--out", str(out_dir))) == 0
    captured = capsys.readouterr()
    decisions = out_dir / f"{RUN_ID}-guardrail-decisions.csv"
    summary = out_dir / f"{RUN_ID}-guardrail-summary.txt"
    with decisions.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.reader(fh))
    assert rows[0][-2:] == ["held_side", "verdict"]
    assert len(rows) == 1 + 11
    assert summary.read_text(encoding="utf-8") == captured.out
    assert f"wrote {decisions}" in captured.err


def test_several_runs_are_read_in_one_call_each_with_its_own_report(tmp_path, capsys):
    # The gate-written fixture, twice in one store: the rule is replayed once
    # and each run gets its own block on stdout and its own pair of files.
    other = "paper-GATE-B"
    papers_store = write_gate_store(tmp_path / "paper_trading.db")
    write_gate_store(papers_store, run_id=other)
    out_dir = tmp_path / "out"
    command = _guardrail(papers_store, tmp_path, "--run-id", other, "--out", str(out_dir))
    command[command.index(RUN_ID)] = GATE_RUN_ID
    assert main(command) == 0
    blocks = capsys.readouterr().out.split("\n\n")
    assert len(blocks) == 2
    for block, run_id in zip(blocks, (GATE_RUN_ID, other), strict=True):
        assert block.splitlines()[0].startswith(f"guardrail shadow: run {run_id} (BTC, 4h cycle; ")
    # Same decisions, same rule, same history: the two reports differ in the
    # run's name alone.
    assert blocks[0].splitlines()[1:] == blocks[1].strip().splitlines()[1:]
    assert "questions: 10 (10 answered)" in blocks[0].splitlines()
    for run_id in (GATE_RUN_ID, other):
        assert (out_dir / f"{run_id}-guardrail-decisions.csv").is_file()
        assert (out_dir / f"{run_id}-guardrail-summary.txt").is_file()


def test_a_run_the_research_store_does_not_reach_at_all_is_refused(store, tmp_path, capsys):
    # The same twelve bars, closing twenty slots before the run begins: no
    # question has a rule side, so there is no report to print and no exit 0.
    research = _research(tmp_path, shift=-20)
    command = ["guardrail", "--db", str(store), "--run-id", RUN_ID]
    command += ["--research-db", str(research), "--rule", str(_rule_file(tmp_path))]
    assert main(command) == 1
    captured = capsys.readouterr()
    assert (
        f"error: run {RUN_ID!r}: the research store has no rule side for any of its 11 "
        f"question(s), decided {_at(0)} to {_at(11)} UTC, while the rule's history there closes "
        f"{_at(-20)} to {_at(-9)}"
    ) in captured.err
    assert captured.out == ""


def test_questions_read_before_the_rule_first_took_a_side_are_said(store, tmp_path, capsys):
    # The replay starts flat. Here the rule first takes a side at the bar
    # closing at slot 2, so slots 0 and 1 read a flat that may be a position
    # opened before the store begins. In the main fixture the first bar is
    # already long, and nothing is read before it.
    research = _research(tmp_path, (100, 100, *_CLOSES[2:]))
    command = ["guardrail", "--db", str(store), "--run-id", RUN_ID]
    command += ["--research-db", str(research), "--rule", str(_rule_file(tmp_path))]
    assert main(command) == 0
    out = capsys.readouterr().out.splitlines()
    assert (
        "  the replay starts flat at the store's first bar; the rule first takes a side at the "
        f"bar closing {_at(2)}"
    ) in out
    assert (
        "2 question(s) were read before the rule first took a side in this store: a flat rule "
        "there may be a position it opened before the store begins"
    ) in out
    assert main(_guardrail(store, tmp_path)) == 0
    assert not any("were read before" in line for line in capsys.readouterr().out.splitlines())


def test_the_committed_rule_is_the_default_and_a_short_store_refuses_it(store, tmp_path, capsys):
    # No --rule: the research package's own file, whose 120-bar channel the
    # twelve fixture bars cannot answer.
    args = ["guardrail", "--db", str(store), "--run-id", RUN_ID]
    assert main([*args, "--research-db", str(_research(tmp_path))]) == 1
    captured = capsys.readouterr()
    assert "cannot say which side btc-20d-breakout@" in captured.err
    assert captured.out == ""


def test_a_research_store_that_is_not_one_is_a_named_exit_1(store, tmp_path, capsys):
    # A SQLite file that belongs to something else: the store's own refusal,
    # which is not one of the errors ``main`` catches for every command.
    foreign = tmp_path / "other.sqlite"
    conn = sqlite3.connect(foreign)
    conn.execute("CREATE TABLE something_else (x)")
    conn.commit()
    conn.close()
    assert main(_guardrail(store, tmp_path, "--research-db", str(foreign))) == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("error: ")
    assert "is a SQLite database but not an AutoResearch store" in captured.err
    assert captured.out == ""


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (("--research-db", "missing.sqlite"), "--research-db 'missing.sqlite' does not exist"),
        (("--out", ""), "--out needs a directory, got ''"),
        (("--run-id", RUN_ID), f"--run-id names {RUN_ID} more than once"),
        (("--rule", "missing.json"), "could not read the guardrail rule 'missing.json'"),
    ],
)
def test_a_flag_that_cannot_be_used_is_a_named_exit_1(
    store, tmp_path, monkeypatch, capsys, extra, message
):
    monkeypatch.chdir(tmp_path)
    assert main(_guardrail(store, tmp_path, *extra)) == 1
    captured = capsys.readouterr()
    assert f"error: {message}" in captured.err
    assert captured.out == ""
    assert not (tmp_path / "missing.sqlite").exists()  # never created on the way to refusing


def test_a_missing_store_or_run_is_a_named_exit_1(store, tmp_path, capsys):
    args = _guardrail(store, tmp_path)
    assert main(["guardrail", "--db", str(tmp_path / "none.db"), *args[3:]]) == 1
    assert "does not exist" in capsys.readouterr().err
    assert main([*args[:4], "paper-NONE", *args[5:]]) == 1
    assert "run 'paper-NONE' not found" in capsys.readouterr().err


def test_a_live_run_and_a_run_on_another_interval_are_refused(tmp_path, capsys):
    live = write_paper_store(
        tmp_path / "live_trading.db", payload_root=tmp_path / "p", config=run_config(), mode="live"
    )
    assert main(_guardrail(live, tmp_path)) == 1
    assert "is a live run; the guardrail report reads paper runs" in capsys.readouterr().err
    daily = write_paper_store(
        tmp_path / "daily.db", payload_root=tmp_path / "p", config=run_config(interval="1d")
    )
    assert main(_guardrail(daily, tmp_path)) == 1
    assert "the guardrail rule is written in 4h bars" in capsys.readouterr().err
