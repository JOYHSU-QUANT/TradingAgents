"""``score --replay-db``: a variant's replayed answers, scored the way the paper trader's are.

The anchor claim: a variant that says, for each question, what the paper
trader's model said (:class:`~.papers.Echo`) gets the paper trader's own
card, line for line. The answers go through the gate, into
``replay.sqlite``, back out as scorecard records and through the one
``score_run``; any step that bends a field shows up as a differing line.
"""

from __future__ import annotations

import csv
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from contrib.replay import cli
from contrib.replay.replay import Completion
from contrib.replay.replay_store import ReplayStore

from .papers import (
    RUN_ID,
    TRAIN,
    VALIDATION,
    Echo,
    decision_text,
    replay_argv,
    variant as make_variant,
    write_gate_store,
    write_variant,
)

ASKED = len(TRAIN) + len(VALIDATION)
SHORT_30 = decision_text("set_target", "short", 30, 0.8)


@pytest.fixture
def store(tmp_path: Path) -> Path:
    return write_gate_store(tmp_path / "paper_trading.db")


def _ask(store: Path, variant: Path, model, monkeypatch, *segments: str) -> None:
    """Replay ``segments`` (train and validation by default), two repeats each."""
    monkeypatch.setattr(cli, "_build_model", lambda _variant: model)
    for segment in segments or ("train", "validation"):
        assert cli.main(replay_argv(store, variant, "--repeats", "2", "--segment", segment)) == 0


def _score(store: Path, *extra: str) -> list[str]:
    return ["score", "--db", str(store), "--run-id", RUN_ID, *extra]


def _replay_score(store: Path, variant: str, *extra: str) -> list[str]:
    replay_db = str(store.parent / "replay.sqlite")
    return _score(store, "--replay-db", replay_db, "--variant", variant, *extra)


def _section(lines: list[str], start: str) -> list[str]:
    """The lines after ``start``, up to the next repeat banner or the across-repeats banner."""
    begin = lines.index(start) + 1
    end = next(
        (i for i in range(begin, len(lines)) if lines[i].startswith(("== repeat", "-- across"))),
        len(lines),
    )
    return lines[begin:end]


class _SecondTimeShort(Echo):
    """An echo that, the second time it is asked slot 0, calls it short instead."""

    def __init__(self) -> None:
        super().__init__()
        self.slot0 = 0

    def __call__(self, system: str, human: str) -> Completion:
        if "question 00:" in human:
            self.slot0 += 1
            if self.slot0 == 2:
                self.calls.append((system, human))
                return Completion(text=SHORT_30)
        return super().__call__(system, human)


def test_an_echo_of_the_paper_traders_answers_scores_as_the_paper_trader(
    store, tmp_path, monkeypatch, capsys
):
    _ask(store, write_variant(tmp_path, "echo"), Echo(), monkeypatch)
    capsys.readouterr()
    assert cli.main(_score(store)) == 0
    paper = capsys.readouterr().out.splitlines()
    assert cli.main(_replay_score(store, "echo")) == 0
    out = capsys.readouterr().out.splitlines()
    for repeat in (0, 1):
        assert _section(out, f"== repeat {repeat}: {ASKED} question(s) scored ==") == paper[1:]
    assert out[1].startswith("variant echo (")
    assert out[2] == (
        "model_cutoff 2027-01-14 (echo): 0 of the run's questions are decided on or before it; "
        "none of them is scored"
    )
    assert out[3].startswith("split: pinned ")


def test_an_echo_differs_from_the_paper_trader_on_no_question(
    store, tmp_path, monkeypatch, capsys
):
    _ask(store, write_variant(tmp_path, "echo"), Echo(), monkeypatch)
    capsys.readouterr()
    assert cli.main(_replay_score(store, "echo")) == 0
    out = capsys.readouterr().out.splitlines()
    paired = out[out.index("-- paired with paper (model reading; McNemar exact p) --") + 1 :]
    assert len(paired) == 4  # two repeats x two horizons
    assert all(", echo only 0, paper only 0, p 1.000" in line for line in paired)


def test_the_across_repeats_line_is_the_median_and_the_range(
    store, tmp_path, monkeypatch, capsys
):
    # Slot 0 moves from 100 to 102: its long (repeat 0) is a hit, and the
    # short its repeat 1 turns into is a miss. Nothing else differs.
    _ask(store, write_variant(tmp_path, "flaky"), _SecondTimeShort(), monkeypatch)
    capsys.readouterr()
    assert cli.main(_replay_score(store, "flaky")) == 0
    out = capsys.readouterr().out.splitlines()

    def model_hits(repeat: int) -> tuple[int, int]:
        card = _section(out, f"== repeat {repeat}: {ASKED} question(s) scored ==")
        line = next(line for line in card if line.startswith("  executed hit"))
        hits, n = line.rsplit("(", 1)[1].rstrip(")").split("/")
        return int(hits), int(n)

    (hits0, n0), (hits1, n1) = model_hits(0), model_hits(1)
    assert (n0, hits0 - hits1) == (n1, 1)
    low, high = hits1 / n1, hits0 / n0
    across = out[out.index("-- across 2 repeat(s) --") + 1]
    assert across.startswith(
        f"  4h: model hit median {(low + high) / 2:.1%} (range {low:.1%} to {high:.1%})"
    )
    assert f"  repeat 1, 4h: n {n1}, flaky only 0, paper only 1, p 1.000" in out
    assert f"  repeat 0, 4h: n {n0}, flaky only 0, paper only 0, p 1.000" in out


def test_questions_up_to_the_models_cutoff_are_left_out(store, tmp_path, monkeypatch, capsys):
    # The fixture's first five questions are decided on 2027-01-15, the rest on the 16th.
    _ask(store, write_variant(tmp_path, "dated", cutoff="2027-01-15"), Echo(), monkeypatch)
    capsys.readouterr()
    assert cli.main(_replay_score(store, "dated")) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[2] == (
        "model_cutoff 2027-01-15 (dated): 5 of the run's questions are decided on or before it; "
        "none of them is scored"
    )
    assert f"== repeat 0: {ASKED - 5} question(s) scored ==" in out
    assert cli.main(_replay_score(store, "dated", "--include-pre-cutoff")) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[2] == (
        "model_cutoff 2027-01-15 (dated): 5 of the run's questions are decided on or before it; "
        "they are scored too (--include-pre-cutoff)"
    )
    assert f"== repeat 0: {ASKED} question(s) scored ==" in out


def test_against_takes_the_later_cutoff_of_the_two(store, tmp_path, monkeypatch, capsys):
    """The rival may have seen 2027-01-15's prices: those questions count for neither side."""
    _ask(store, write_variant(tmp_path, "echo"), Echo(), monkeypatch)
    _ask(store, write_variant(tmp_path, "rival", cutoff="2027-01-15"), Echo(), monkeypatch)
    capsys.readouterr()
    assert cli.main(_replay_score(store, "echo", "--against", "rival")) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[2].startswith("against variant rival (")
    assert out[3] == (
        "model_cutoff 2027-01-15 (rival): 5 of the run's questions are decided on or before it; "
        "none of them is scored"
    )
    assert out[4].startswith("split: pinned ")
    assert f"== repeat 0: {ASKED - 5} question(s) scored ==" in out
    paired = out[out.index("-- paired with rival (model reading; McNemar exact p) --") + 1 :]
    assert all(int(line.split(": n ")[1].split(",")[0]) <= ASKED - 5 for line in paired)


def test_a_repeat_with_nothing_priced_has_no_pnl_figure(store, tmp_path, monkeypatch, capsys):
    # Validation's questions (slots 6-7) have no question six bars on: no 24h price.
    _ask(store, write_variant(tmp_path, "late"), Echo(), monkeypatch, "validation")
    capsys.readouterr()
    assert cli.main(_replay_score(store, "late")) == 0
    out = capsys.readouterr().out.splitlines()
    (day,) = [line for line in out if line.startswith("  24h: model hit")]
    assert day.endswith("executed pnl mean n/a")


def test_only_the_questions_a_variant_was_asked_are_scored(store, tmp_path, monkeypatch, capsys):
    _ask(store, write_variant(tmp_path, "trainonly"), Echo(), monkeypatch, "train")
    capsys.readouterr()
    assert cli.main(_replay_score(store, "trainonly")) == 0
    out = capsys.readouterr().out.splitlines()
    card = _section(out, f"== repeat 0: {len(TRAIN)} question(s) scored ==")
    assert card[0] == f"decisions: {len(TRAIN)} questions, {len(TRAIN)} answered, 0 unanswered"


def test_against_pairs_two_variants_repeat_by_repeat(store, tmp_path, monkeypatch, capsys):
    _ask(store, write_variant(tmp_path, "echo"), Echo(), monkeypatch)
    # The rival calls slot 0 short every time: a miss where the echo hits.
    _ask(store, write_variant(tmp_path, "rival"), Echo(text_for={0: SHORT_30}), monkeypatch)
    capsys.readouterr()
    assert cli.main(_replay_score(store, "echo", "--against", "rival")) == 0
    out = capsys.readouterr().out.splitlines()
    paired = out[out.index("-- paired with rival (model reading; McNemar exact p) --") + 1 :]
    four_hour = [line.split(": ", 1)[1] for line in paired if ", 4h:" in line]
    assert [line.split(", ", 1)[1] for line in four_hour] == [
        "echo only 1, rival only 0, p 1.000",
        "echo only 1, rival only 0, p 1.000",
    ]


def test_against_a_variant_with_no_answers_for_the_run_is_refused(
    store, tmp_path, monkeypatch, capsys
):
    _ask(store, write_variant(tmp_path, "echo"), Echo(), monkeypatch)
    with ReplayStore(store.parent / "replay.sqlite") as replay_store:
        idle = make_variant(name="idle", model_cutoff=date(2027, 1, 14))
        replay_store.register(idle, now=datetime.now(timezone.utc))
    capsys.readouterr()
    assert cli.main(_replay_score(store, "echo", "--against", "idle")) == 1
    expected = f"variant 'idle' has no answers for run {RUN_ID!r} to compare with"
    assert expected in capsys.readouterr().err


def test_a_repeat_the_other_variant_does_not_have_is_named(
    store, tmp_path, monkeypatch, capsys
):
    _ask(store, write_variant(tmp_path, "echo"), Echo(), monkeypatch)
    rival = write_variant(tmp_path, "rival")
    for segment in ("train", "validation"):
        argv = replay_argv(store, rival, "--repeats", "1", "--segment", segment)
        assert cli.main(argv) == 0
    capsys.readouterr()
    assert cli.main(_replay_score(store, "echo", "--against", "rival")) == 0
    out = capsys.readouterr().out.splitlines()
    assert "  repeat 1: rival has no repeat 1" in out
    assert any(line.startswith("  repeat 0, 4h: n ") for line in out)


def test_scoring_the_holdout_records_the_look_and_shows_the_earlier_ones(
    store, tmp_path, monkeypatch, capsys
):
    _ask(store, write_variant(tmp_path, "echo"), Echo(), monkeypatch)
    capsys.readouterr()
    assert cli.main(_replay_score(store, "echo", "--holdout")) == 0
    assert "holdout looks recorded for this run before this one: 0" in (
        capsys.readouterr().out.splitlines()
    )
    assert cli.main(_replay_score(store, "echo", "--holdout")) == 0
    out = capsys.readouterr().out.splitlines()
    assert "holdout looks recorded for this run before this one: 1" in out
    (look,) = [line for line in out if line.endswith(" score echo (2 question(s))")]
    assert look.startswith("  20")
    conn = sqlite3.connect(store.parent / "replay.sqlite")
    try:
        assert conn.execute("SELECT count(*) FROM ledger WHERE action = 'score'").fetchone() == (2,)
    finally:
        conn.close()


def test_scoring_without_the_holdout_records_no_look(store, tmp_path, monkeypatch):
    _ask(store, write_variant(tmp_path, "echo"), Echo(), monkeypatch)
    assert cli.main(_replay_score(store, "echo")) == 0
    conn = sqlite3.connect(store.parent / "replay.sqlite")
    try:
        assert conn.execute("SELECT count(*) FROM ledger").fetchone() == (0,)
    finally:
        conn.close()


def test_out_writes_every_repeat_into_one_csv(store, tmp_path, monkeypatch, capsys):
    _ask(store, write_variant(tmp_path, "echo"), Echo(), monkeypatch)
    capsys.readouterr()
    out_dir = tmp_path / "out"
    assert cli.main(_replay_score(store, "echo", "--out", str(out_dir))) == 0
    with (out_dir / f"{RUN_ID}-echo-decisions.csv").open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert [row["repeat"] for row in rows] == ["0"] * ASKED + ["1"] * ASKED
    summary = (out_dir / f"{RUN_ID}-echo-summary.txt").read_text(encoding="utf-8")
    assert summary.splitlines() == capsys.readouterr().out.splitlines()


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (("--variant", "echo"), "error: --variant only apply with --replay-db"),
        (
            ("--against", "x", "--include-pre-cutoff"),
            "error: --against, --include-pre-cutoff only apply with --replay-db",
        ),
    ],
    ids=["variant", "against-and-cutoff"],
)
def test_replay_flags_without_a_replay_store_are_refused(store, capsys, extra, message):
    assert cli.main(_score(store, *extra)) == 1
    assert message in capsys.readouterr().err


def test_a_missing_replay_store_is_refused_and_not_created(store, capsys):
    missing = store.parent / "nope.sqlite"
    assert cli.main(_score(store, "--replay-db", str(missing), "--variant", "x")) == 1
    assert f"--replay-db {str(missing)!r} does not exist" in capsys.readouterr().err
    assert not missing.exists()


def test_an_unknown_variant_is_refused_naming_the_known_ones(store, tmp_path, monkeypatch, capsys):
    _ask(store, write_variant(tmp_path, "echo"), Echo(), monkeypatch)
    capsys.readouterr()
    assert cli.main(_replay_score(store, "ghost")) == 1
    err = capsys.readouterr().err
    assert "no variant named 'ghost'" in err
    assert err.rstrip().endswith("it holds 'echo'")


def test_a_variant_with_no_answers_for_the_run_is_refused(store, tmp_path, monkeypatch, capsys):
    _ask(store, write_variant(tmp_path, "echo"), Echo(), monkeypatch)
    conn = sqlite3.connect(store.parent / "replay.sqlite")
    try:
        conn.execute("DELETE FROM answers")
        conn.commit()
    finally:
        conn.close()
    assert cli.main(_replay_score(store, "echo")) == 1
    assert f"variant 'echo' has no answers for run {RUN_ID!r}" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (("--against", "echo"), "--against only apply with --variant"),
        (("--variant", "echo", "--against", "echo"), "--against names the variant being scored"),
    ],
    ids=["against-without-variant", "against-itself"],
)
def test_the_variant_flags_are_checked_up_front(
    store, tmp_path, monkeypatch, capsys, extra, message
):
    _ask(store, write_variant(tmp_path, "echo"), Echo(), monkeypatch)
    argv = _score(store, "--replay-db", str(store.parent / "replay.sqlite"), *extra)
    assert cli.main(argv) == 1
    assert message in capsys.readouterr().err


# -- decided 2026-09-24: a cutoff is required, the split is pinned, refusals count ---------


def test_a_variant_with_no_cutoff_is_refused_unless_every_question_is_asked_for(
    store, tmp_path, monkeypatch, capsys
):
    _ask(store, write_variant(tmp_path, "undated", cutoff=None), Echo(), monkeypatch)
    capsys.readouterr()
    assert cli.main(_replay_score(store, "undated")) == 1
    err = capsys.readouterr().err
    assert "'undated' record(s) no model_cutoff" in err
    assert "run `register`, or pass --include-pre-cutoff" in err
    assert cli.main(_replay_score(store, "undated", "--include-pre-cutoff")) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[2] == "model_cutoff unknown: the questions are not split at the model's cutoff"
    assert f"== repeat 0: {ASKED} question(s) scored ==" in out


def test_register_records_a_cutoff_without_asking_anything(store, tmp_path, monkeypatch, capsys):
    _ask(store, write_variant(tmp_path, "later", cutoff=None), Echo(), monkeypatch)
    dated = write_variant(tmp_path, "later", cutoff="2027-01-15")
    monkeypatch.setattr(cli, "_build_model", _no_client)
    capsys.readouterr()
    argv = ["register", "--variant", str(dated), "--replay-db", str(store.parent / "replay.sqlite")]
    assert cli.main(argv) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[1] == "model_cutoff: 2027-01-15"
    assert out[2].startswith("note: model_cutoff of 'later' changed from None to 2027-01-15")
    assert cli.main(_replay_score(store, "later")) == 0
    assert f"== repeat 0: {ASKED - 5} question(s) scored ==" in capsys.readouterr().out


def _no_client(variant):
    raise AssertionError("register must not build a client")


def test_questions_the_run_gained_after_the_pin_are_not_part_of_the_exam(
    tmp_path, monkeypatch, capsys
):
    """Pinned over eight questions (5 / 1 / 2), the split stays put when the run gains two."""
    from .papers import PAPERS

    store = write_gate_store(tmp_path / "paper_trading.db", papers=PAPERS[:8])
    variant = write_variant(tmp_path, "echo")
    _ask(store, variant, Echo(), monkeypatch, "train")
    write_gate_store(store, papers=PAPERS[8:], grow=True)
    capsys.readouterr()
    assert cli.main(_replay_score(store, "echo")) == 0
    out = capsys.readouterr().out.splitlines()
    (split_line,) = [line for line in out if line.startswith("split: pinned ")]
    assert split_line.endswith("; 2 question(s) decided after its end are not part of this exam")
    card = out[out.index("== repeat 0: 5 question(s) scored ==") + 1 :]
    # Five train bars, one validation bar: the eight-question cut, not the ten-question one.
    assert "split (4h): train: 2027-01-15 04:00 .. 2027-01-16 00:00" in card
    assert "split (4h): validation: 2027-01-16 00:00 .. 2027-01-16 04:00" in card
    assert "segments (questions): train 5 (holdout not read)" in card
    # Unpinned, the ten questions would have cut 6 / 2 / 2: a sixth train question.
    argv = replay_argv(store, variant, "--repeats", "2", "--dry-run")
    assert cli.main(argv) == 0
    dry = capsys.readouterr().out.splitlines()
    assert dry[0].endswith("train segment: 5 question(s) x 2 repeat(s)")


def test_a_refused_question_scores_as_unanswered(store, tmp_path, monkeypatch, capsys):
    model = _RefusesSlot(2)
    _ask(store, write_variant(tmp_path, "picky"), model, monkeypatch, "train")
    capsys.readouterr()
    assert cli.main(_replay_score(store, "picky")) == 0
    out = capsys.readouterr().out.splitlines()
    card = out[out.index(f"== repeat 0: {len(TRAIN)} question(s) scored ==") + 1 :]
    assert card[0] == (
        f"decisions: {len(TRAIN)} questions, {len(TRAIN) - 1} answered, 1 unanswered"
    )


class _ContextTooLong(Exception):
    status_code = 400


class _RefusesSlot(Echo):
    """An echo whose provider refuses one question with a 400 every time it is asked."""

    def __init__(self, slot: int) -> None:
        super().__init__()
        self.slot = slot

    def __call__(self, system: str, human: str) -> Completion:
        if f"question {self.slot:02d}:" in human:
            self.calls.append((system, human))
            raise _ContextTooLong("maximum context length exceeded")
        return super().__call__(system, human)


def test_looking_at_the_paper_traders_holdout_is_recorded(store, capsys):
    assert cli.main(_score(store, "--holdout")) == 1
    assert "--holdout needs --replay-db PATH" in capsys.readouterr().err
    replay_db = store.parent / "looks.sqlite"
    assert cli.main(_score(store, "--holdout", "--replay-db", str(replay_db))) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[1].startswith("split: pinned ")
    assert "holdout looks recorded for this run before this one: 0" in out
    assert any(line.endswith("-- HOLDOUT READ") for line in out)
    assert cli.main(_score(store, "--holdout", "--replay-db", str(replay_db))) == 0
    out = capsys.readouterr().out.splitlines()
    assert "holdout looks recorded for this run before this one: 1" in out
    assert any(line.endswith(" score paper (2 question(s))") for line in out)


def test_a_missing_replay_store_is_not_created_to_score_a_variant_or_the_paper_unlooked(
    store, capsys
):
    missing = store.parent / "nope.sqlite"
    for extra in ((), ("--variant", "x")):
        assert cli.main(_score(store, "--replay-db", str(missing), *extra)) == 1
        assert f"--replay-db {str(missing)!r} does not exist" in capsys.readouterr().err
    assert not missing.exists()
