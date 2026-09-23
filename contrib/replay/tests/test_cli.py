"""``python -m contrib.replay score`` end to end: the report, the files, the refusals."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from contrib.replay import __main__ as entry
from contrib.replay.cli import main
from contrib.replay.paper_store import REPORTS_SUFFIX
from contrib.replay.upstream import payload_dir

from .conftest import (
    ROWS,
    RUN_ID,
    payload_name,
    run_config,
    write_paper_store,
    write_research_store,
)


@pytest.fixture
def store(tmp_path: Path) -> Path:
    return write_paper_store(
        tmp_path / "paper_trading.db",
        payload_root=payload_dir(tmp_path / "paper_trading.db", RUN_ID),
        config=run_config(),
    )


def _score(store: Path, *extra: str) -> list[str]:
    return ["score", "--db", str(store), "--run-id", RUN_ID, *extra]


def test_score_prints_the_scorecard_over_train_and_validation(store, capsys):
    assert main(_score(store)) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == (
        f"scorecard: run {RUN_ID} (BTC, 4h cycle; costs from the run's recorded config)"
    )
    assert "decisions: 9 questions, 9 answered, 0 unanswered" in out
    assert "segments scored: train 6, validation 3 (holdout not read)" in out
    # Without the research store the missing slot's later mark is unavailable:
    # row 3 drops out of the next-close count.
    assert "  executed hit 57.1% (4/7); model hit 33.3% (2/6)" in out
    assert "  flat band +/-1.923%; later marks from the research store: 0" in out


def test_the_research_store_fills_the_missing_cycle(store, tmp_path, capsys):
    research = write_research_store(tmp_path / "autoresearch.sqlite")
    assert main(_score(store, "--research-db", str(research))) == 0
    out = capsys.readouterr().out.splitlines()
    assert "  executed hit 50.0% (4/8); model hit 33.3% (2/6)" in out
    assert any(line.endswith("later marks from the research store: 1") for line in out)


def test_out_writes_one_csv_row_per_decision_and_the_summary(store, tmp_path, capsys):
    out_dir = tmp_path / "out"
    assert main(_score(store, "--out", str(out_dir))) == 0
    captured = capsys.readouterr()
    decisions = out_dir / f"{RUN_ID}-decisions.csv"
    summary = out_dir / f"{RUN_ID}-summary.txt"
    with decisions.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.reader(fh))
    assert rows[0][:3] == ["input_id", "attempt_id", "at"]
    assert len(rows) == 1 + 9
    assert summary.read_text(encoding="utf-8") == captured.out
    assert f"wrote {decisions}" in captured.err


def test_holdout_scores_every_row_and_says_so(store, capsys):
    assert main(_score(store, "--holdout")) == 0
    out = capsys.readouterr().out.splitlines()
    assert "decisions: 11 questions, 10 answered, 1 unanswered" in out
    assert "segments scored: train 6, validation 3, holdout 2 -- HOLDOUT READ" in out


def test_reports_are_counted_beside_the_store_by_default_or_under_payload_root(
    store, tmp_path, capsys
):
    beside = payload_dir(store, RUN_ID)
    beside.mkdir(parents=True)
    (beside / payload_name(0)).with_suffix(REPORTS_SUFFIX).write_text("{}", encoding="utf-8")
    assert main(_score(store)) == 0
    assert "questions with a .reports.json beside the payload: 1/9" in capsys.readouterr().out
    elsewhere = tmp_path / "copied"
    elsewhere.mkdir()
    for slot in (0, 7):
        (elsewhere / payload_name(slot)).with_suffix(REPORTS_SUFFIX).write_text("{}", encoding="utf-8")
    assert main(_score(store, "--payload-root", str(elsewhere))) == 0
    assert "questions with a .reports.json beside the payload: 2/9" in capsys.readouterr().out


def test_without_a_payload_directory_the_count_is_skipped(store, capsys):
    assert main(_score(store)) == 0
    assert ".reports.json" not in capsys.readouterr().out


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (("--research-db", "missing.sqlite"), "--research-db 'missing.sqlite' does not exist"),
        (("--payload-root", "missing-dir"), "--payload-root 'missing-dir' is not a directory"),
        (("--payload-root", ""), "--payload-root needs a directory, got ''"),
    ],
)
def test_a_flag_pointing_nowhere_is_a_named_exit_1(
    store, tmp_path, monkeypatch, capsys, extra, message
):
    monkeypatch.chdir(tmp_path)
    assert main(_score(store, *extra)) == 1
    assert f"error: {message}" in capsys.readouterr().err
    assert not (tmp_path / "missing.sqlite").exists()  # never created on the way to refusing


def test_a_missing_store_or_run_is_a_named_exit_1(store, tmp_path, capsys):
    assert main(["score", "--db", str(tmp_path / "none.db"), "--run-id", RUN_ID]) == 1
    assert "does not exist" in capsys.readouterr().err
    assert main(["score", "--db", str(store), "--run-id", "paper-NONE"]) == 1
    assert "run 'paper-NONE' not found" in capsys.readouterr().err


def test_a_live_run_is_refused(tmp_path, capsys):
    live = write_paper_store(
        tmp_path / "live_trading.db", payload_root=tmp_path / "p", config=run_config(), mode="live"
    )
    assert main(_score(live)) == 1
    assert f"run {RUN_ID!r} is a live run" in capsys.readouterr().err


def test_a_run_too_short_to_split_is_refused_with_the_reason(tmp_path, capsys):
    short = write_paper_store(
        tmp_path / "short.db", payload_root=tmp_path / "p", config=run_config(), rows=ROWS[:2]
    )
    assert main(_score(short)) == 1
    err = capsys.readouterr().err
    assert "too short to cut into train / validation / holdout" in err
    assert "at least four 4h bars" in err


def test_a_run_with_no_rows_is_refused(tmp_path, capsys):
    empty = write_paper_store(
        tmp_path / "empty.db", payload_root=tmp_path / "p", config=run_config(), rows=()
    )
    assert main(_score(empty)) == 1
    assert "has no decision attempt with an input row" in capsys.readouterr().err


def test_cycles_that_are_not_decisions_are_counted_apart(store, capsys):
    assert main(_score(store)) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[1:5] == [
        "cycles that failed before an input row was written (not questions): 1",
        "cycles still in progress when the store was read (left out): 1",
        "attempts retried: 1 (1 extra tries); the fail-closed rate below counts final answers only",
        "decisions: 9 questions, 9 answered, 0 unanswered",
    ]


def test_a_research_store_without_the_series_is_a_warning_not_a_silence(store, tmp_path, capsys):
    from contrib.replay.upstream import ResearchStore

    with ResearchStore(tmp_path / "empty.sqlite"):
        pass
    assert main(_score(store, "--research-db", str(tmp_path / "empty.sqlite"))) == 0
    captured = capsys.readouterr()
    assert "warning: --research-db" in captured.err and "holds no BTC 4h candles" in captured.err
    assert "later marks from the research store: 0" in captured.out


def test_no_command_is_a_usage_error():
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 2


def test_the_module_entry_hands_argv_to_the_cli():
    assert entry.main is main
