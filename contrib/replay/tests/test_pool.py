"""The direction probe pooled across runs (plan PR 2.2): the skill, the block bootstrap, the bar.

The CLI cases put two runs of :mod:`.papers`' fixture in one store. Only
slot 6 of each run's validation segment has a 4h outcome (an up: slot 7's
next mark is in the holdout), and each run's train base rate is up 50% /
down 0% / flat 50% (see ``test_probe``). Run one is probed with h4 up .6 /
down .1 / flat .3, whose Brier on an up is 0.26; run two with up .2 / down
.5 / flat .3, whose Brier on an up is .8² + .5² + .3² = 0.98; the base rate
costs 0.5 on either. Pooled: 1 - (0.26 + 0.98) / (0.5 + 0.5) = -0.24. Each
run is redrawn on its own circle, and a circle of one question always
redraws that question: every draw is -0.24, and two blocks are fewer than
the bar's five.
"""

from __future__ import annotations

import random
import sqlite3
from pathlib import Path

import pytest

from contrib.replay import cli
from contrib.replay.paper_store import load_decisions
from contrib.replay.pool import (
    BLOCK,
    MIN_BLOCKS,
    Interval,
    RunScores,
    _quantile,
    _verdict,
    block_count,
    bootstrap,
    circular_draw,
    describe_pool,
    pooled_skill,
)
from contrib.replay.probe_score import QuestionScore, headline_scores
from contrib.replay.replay_store import ReplayStore
from contrib.replay.score import build_split, score_run
from contrib.replay.upstream import CostModel, Database, SegmentName
from contrib.replay.variant import load_variant

from .papers import RUN_ID, VALIDATION, input_id, replay_argv, write_gate_store, write_variant
from .test_probe import Forecaster, forecast_text, write_probe

OTHER_RUN = "paper-GATE2"
STEP_MS = 4 * 3_600_000
A, B = (0.26, 0.5), (0.98, 0.5)  # an up forecast at .6 and one at .2, against the base rate


def _score(i: int, brier: float, base: float) -> QuestionScore:
    return QuestionScore(f"q{i}", i, brier, base, False, None)


class _Starts(random.Random):
    """A generator whose ``randrange`` hands out the starts it was given, in order."""

    def __init__(self, starts: list[int]) -> None:
        super().__init__(0)
        self.starts = list(starts)

    def randrange(self, *_args, **_kwargs) -> int:  # type: ignore[override]
        return self.starts.pop(0)


# -- the pure functions -------------------------------------------------------------


def test_the_pooled_skill_weighs_every_question_alike():
    assert pooled_skill([A, B]) == pytest.approx(1 - 1.24 / 1.0)
    assert pooled_skill([(0.2, 0.0)]) is None  # a base that scores perfectly
    assert pooled_skill([]) is None


def test_the_blocks_are_counted_per_run():
    # 7 questions make two blocks of 6 (the second wraps), 1 makes one, 0 none.
    assert block_count([[A] * 7, [B], []], 6) == 3
    assert block_count([[A] * 30], 6) == MIN_BLOCKS == 5
    assert BLOCK == 6
    with pytest.raises(ValueError, match="at least one question"):
        block_count([[A]], 0)


def test_a_circular_draw_wraps_within_the_run_and_keeps_its_size():
    run = [(float(i), 1.0) for i in range(5)]
    # Blocks of 2 starting at 4, 1 and 3: (4, 0), (1, 2), (3, 4) cut back to 5.
    drawn = circular_draw(run, 2, _Starts([4, 1, 3]))
    assert [m for m, _ in drawn] == [4.0, 0.0, 1.0, 2.0, 3.0]
    # A block as long as the run is a rotation of it: every question, once.
    whole = circular_draw(run, 5, _Starts([3]))
    assert [m for m, _ in whole] == [3.0, 4.0, 0.0, 1.0, 2.0]


def test_the_quantile_interpolates_between_order_statistics():
    ordered = [0.0, 1.0, 2.0, 3.0, 4.0]
    assert _quantile(ordered, 0.05) == pytest.approx(0.2)
    assert _quantile(ordered, 0.95) == pytest.approx(3.8)
    assert _quantile([7.0], 0.05) == 7.0


def test_each_run_is_redrawn_on_its_own_circle():
    # Two one-question runs: each draw keeps each run's only question.
    apart = bootstrap([[A], [B]], block=6, draws=500, seed=1)
    assert apart == Interval(
        2, 2, pytest.approx(-0.24), pytest.approx(-0.24), pytest.approx(-0.24), 0
    )
    # Runs of one and two questions, blocks of 1: run one always redraws A, run
    # two only B's, so every draw is 1 - (0.26 + 0.98 x 2) / 1.5 = -0.48; one
    # circle over all three would mix them.
    kept = bootstrap([[A], [B, B]], block=1, draws=500, seed=1)
    assert kept.low == kept.high == pytest.approx(-0.48)
    # The same two questions as one run, blocks of 1: a draw is AA (+0.48, a
    # quarter), BB (-0.96, a quarter) or one of each (-0.24, half).
    together = bootstrap([[A, B]], block=1, draws=4000, seed=7)
    assert together.skill == pytest.approx(-0.24)
    assert (together.low, together.high) == (pytest.approx(-0.96), pytest.approx(0.48))
    assert bootstrap([[A, B]], block=1, draws=4000, seed=7) == together  # seeded
    # Blocks of 2 on that run are rotations of it: every draw is the whole run.
    whole = bootstrap([[A, B]], block=2, draws=500, seed=3)
    assert whole.low == whole.high == pytest.approx(-0.24)


def test_the_interval_reads_the_5th_and_95th_percentiles():
    # One run of twenty questions, Brier 0.00, 0.05, ..., 0.95 against a base of
    # 1, blocks of 1: the skill is 1 minus the mean of 20 draws, whose mean is
    # 0.475 and standard error 0.2883 / sqrt(20) = 0.0645. The 90% interval is
    # about 0.525 -/+ 1.645 x 0.0645, [0.419, 0.631]; an 80% one would be
    # [0.442, 0.608].
    run = [(i / 20, 1.0) for i in range(20)]
    found = bootstrap([run], block=1, draws=20000, seed=11)
    assert found.skill == pytest.approx(0.525)
    assert found.low == pytest.approx(0.419, abs=0.01)
    assert found.high == pytest.approx(0.631, abs=0.01)


def test_draws_whose_base_scores_perfectly_are_left_out_and_counted():
    found = bootstrap([[(0.1, 0.0), A]], block=1, draws=1000, seed=2)
    assert found.skill == pytest.approx(1 - 0.36 / 0.5)
    # Only a draw of the first question twice has no skill: about a quarter.
    assert 150 < found.dropped < 350
    assert found.low is not None
    assert found.high == pytest.approx(0.48)
    assert bootstrap([], block=6, draws=10, seed=0) == Interval(0, 0, None, None, None, 0)
    assert bootstrap([[], [A]], block=6, draws=10, seed=0).blocks == 1  # an empty run adds none
    with pytest.raises(ValueError, match="at least one draw"):
        bootstrap([[A]], block=6, draws=0, seed=0)


def test_the_verdict():
    def interval(blocks: int, skill: float, low: float | None) -> Interval:
        return Interval(30, blocks, skill, low, 0.9, 0)

    assert _verdict(interval(5, 0.3, 0.01), block=6, seed=4) == "met (seed 4)"
    # A positive skill whose interval reaches zero does not meet the bar.
    assert _verdict(interval(5, 0.3, -0.01), block=6, seed=4) == "not met (seed 4)"
    assert _verdict(interval(5, 0.3, 0.0), block=6, seed=4) == "not met (seed 4)"
    assert _verdict(interval(4, 0.3, 0.2), block=6, seed=4) == (
        "cannot be judged: 4 block(s), fewer than the 5 the bar needs"
    )
    assert _verdict(interval(5, 0.3, 0.2), block=3, seed=4) == (
        "not judged: the bar is read with blocks of 6, this report used 3"
    )
    assert _verdict(interval(5, 0.3, None), block=6, seed=4) == "cannot be judged (no interval)"


def test_the_report_names_each_run_and_judges_only_the_4h_headline():
    runs = [
        RunScores("r1", "2027-02-01T00:00:00+00:00", {"h4": [_score(1, *A)], "h24": []}),
        RunScores(
            "r2",
            "2027-02-01T00:00:00+00:00",
            {"h4": [_score(2, *B)], "h24": None},
            left_out=3,
        ),
    ]
    assert describe_pool(runs, draws=400, seed=7) == [
        "pooled over 2 run(s), the validation segment of each run's pinned split; circular "
        "blocks of 6 consecutive question(s) within each run, 400 draws, seed 7",
        "  run r1 (split pinned 2027-02-01T00:00:00+00:00): h4 1 question(s), h24 0 question(s)",
        "  run r2 (split pinned 2027-02-01T00:00:00+00:00): h4 1 question(s), h24 n/a (no train "
        "base rate); 3 left out at the model cutoff",
        "h4 headline: n 2 in 2 block(s); skill -0.240, 90% interval [-0.240, -0.240]",
        "h4 up vs down given a move: n 0 in 0 block(s); skill n/a, 90% interval [n/a, n/a] "
        "(reported only)",
        "h24 headline: n 0 in 0 block(s); skill n/a, 90% interval [n/a, n/a] (reported only: its "
        "returns overlap from question to question)",
        "h24 up vs down given a move: n 0 in 0 block(s); skill n/a, 90% interval [n/a, n/a] "
        "(reported only)",
        "plan section 5 bar (h4 headline skill, lower end of the 90% interval above 0, blocks "
        "of 6, at least 5 blocks): cannot be judged: 2 block(s), fewer than the 5 the bar needs",
    ]


def test_the_bar_needs_five_blocks_of_six():
    thirty = [RunScores("r", "t", {"h4": [_score(i, *A) for i in range(30)], "h24": []})]
    assert describe_pool(thirty, draws=100, seed=2)[-1].endswith(": met (seed 2)")
    twenty_four = [RunScores("r", "t", {"h4": [_score(i, *A) for i in range(24)], "h24": []})]
    assert describe_pool(twenty_four, draws=100)[-1].endswith(
        ": cannot be judged: 4 block(s), fewer than the 5 the bar needs"
    )
    assert describe_pool(thirty, block=3, draws=100)[-1].endswith(
        ": not judged: the bar is read with blocks of 6, this report used 3"
    )


# -- the fixture: two runs in one store, probed ---------------------------------------


def _probe_argv(store: Path, variant_file: Path, probe_file: Path, run_id: str) -> list[str]:
    argv = replay_argv(store, variant_file, "--probe", str(probe_file), "--repeats", "1")
    argv[argv.index(RUN_ID)] = run_id
    return argv


@pytest.fixture
def two_runs(tmp_path: Path, monkeypatch) -> tuple[Path, Path, Path]:
    store = write_gate_store(tmp_path / "paper_trading.db")
    write_gate_store(store, run_id=OTHER_RUN)
    variant_file = write_variant(tmp_path, "echo")
    probe_file = write_probe(tmp_path)
    monkeypatch.setattr(cli, "_sleep", lambda _seconds: None)
    for run_id, model in (
        (RUN_ID, Forecaster()),
        (OTHER_RUN, Forecaster(forecast_text({"up": 0.2, "down": 0.5, "flat": 0.3}))),
    ):
        monkeypatch.setattr(cli, "_build_model", lambda _variant, model=model: model)
        for segment in ("train", "validation"):
            argv = _probe_argv(store, variant_file, probe_file, run_id)
            assert cli.main([*argv, "--segment", segment]) == 0
    return store, variant_file, probe_file


def _pool(store: Path, *extra: str, variant: str = "echo") -> list[str]:
    return [
        "pool",
        "--db",
        str(store),
        "--replay-db",
        str(store.parent / "replay.sqlite"),
        "--variant",
        variant,
        "--probe",
        "direction-t",
        *extra,
    ]


def test_one_run_pooled_is_that_runs_headline(two_runs):
    store, variant_file, _ = two_runs
    with Database(store, migrate=False) as db:
        questions = load_decisions(db, RUN_ID).questions
    split = build_split(questions, interval="4h", step_ms=STEP_MS)
    card = score_run(questions, [], step_ms=STEP_MS, costs=CostModel(), split=split)
    with ReplayStore(store.parent / "replay.sqlite") as replay:
        [(_, answers)] = replay.probe_answers(load_variant(variant_file).sha, RUN_ID)
    eligible = {q.input_id for q in questions}
    [only] = headline_scores(card, answers, eligible, "h4", SegmentName.VALIDATION)
    # Slot 6, an up: the same 0.26 against 0.5 the run's own report prints.
    assert (only.input_id, only.brier, only.base_brier, only.stand_in) == (
        input_id(VALIDATION[0]),
        pytest.approx(0.26),
        pytest.approx(0.5),
        False,
    )
    # Up given a move is .6 / .7 against a train base that only went up.
    assert only.binary == (pytest.approx((1 - 0.6 / 0.7) ** 2), pytest.approx(0.0))
    assert headline_scores(card, answers, eligible, "h24", SegmentName.VALIDATION) == []


# -- the command ---------------------------------------------------------------------


def test_the_pool_command_pools_two_runs(two_runs, capsys):
    store, _, _ = two_runs
    capsys.readouterr()
    argv = _pool(store, "--run-id", RUN_ID, "--run-id", OTHER_RUN, "--draws", "400", "--seed", "7")
    assert cli.main(argv) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("direction probe 'direction-t', variant echo (")
    assert out[2].startswith(f"  run {RUN_ID} (split pinned ")
    assert out[2].endswith("): h4 1 question(s), h24 0 question(s)")
    assert out[3].startswith(f"  run {OTHER_RUN} (split pinned ")
    assert "h4 headline: n 2 in 2 block(s); skill -0.240, 90% interval [-0.240, -0.240]" in out
    assert out[-1].endswith(": cannot be judged: 2 block(s), fewer than the 5 the bar needs")


def test_the_pool_command_writes_nothing(two_runs, capsys):
    store, _, _ = two_runs
    replay_db = store.parent / "replay.sqlite"
    before = replay_db.read_bytes()
    assert cli.main(_pool(store, "--run-id", RUN_ID)) == 0
    assert replay_db.read_bytes() == before
    conn = sqlite3.connect(replay_db)
    try:
        assert conn.execute("SELECT count(*) FROM ledger").fetchone() == (0,)
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (("--run-id", RUN_ID, "--run-id", RUN_ID), f"--run-id names {RUN_ID} more than once"),
        (("--run-id", RUN_ID, "--block", "0"), "--block must be at least 1, got 0"),
        (("--run-id", RUN_ID, "--draws", "0"), "--draws must be at least 1, got 0"),
        (("--run-id", "paper-NONE"), "run 'paper-NONE' not found"),
    ],
)
def test_the_pool_command_refuses_by_name(two_runs, capsys, extra, message):
    store, _, _ = two_runs
    capsys.readouterr()
    assert cli.main(_pool(store, *extra)) == 1
    assert message in capsys.readouterr().err


def test_a_run_without_a_4h_base_rate_is_refused_not_left_out(two_runs, capsys, monkeypatch):
    store, _, _ = two_runs
    real = cli.headline_scores

    def none_for_the_other_run(card, answers, eligible, key, segment):
        if key == "h4" and all(a.input_id.startswith(OTHER_RUN) for a in answers[0]):
            return None
        return real(card, answers, eligible, key, segment)

    monkeypatch.setattr(cli, "headline_scores", none_for_the_other_run)
    capsys.readouterr()
    assert cli.main(_pool(store, "--run-id", RUN_ID, "--run-id", OTHER_RUN)) == 1
    assert (
        f"run {OTHER_RUN!r} has no h4 train base rate (no train question has an outcome at that "
        "horizon)"
    ) in capsys.readouterr().err
    assert cli.main(_pool(store, "--run-id", RUN_ID)) == 0


def test_a_run_without_a_pinned_split_or_the_probe_is_refused(two_runs, tmp_path, capsys):
    store, variant_file, _ = two_runs
    third = "paper-GATE3"
    write_gate_store(store, run_id=third)
    capsys.readouterr()
    assert cli.main(_pool(store, "--run-id", third)) == 1
    assert f"run {third!r} has no split pinned in" in capsys.readouterr().err
    # Probing the third run with another probe pins its split; the named one is still missing.
    other = tmp_path / "other"
    other.mkdir()
    zeta = write_probe(other, "zeta", instructions="Zeta.")
    assert cli.main([*_probe_argv(store, variant_file, zeta, third), "--limit", "1"]) == 0
    capsys.readouterr()
    assert cli.main(_pool(store, "--run-id", third)) == 1
    assert (
        f"variant 'echo' was not asked the probe 'direction-t' on run {third!r}"
    ) in capsys.readouterr().err


def test_a_variant_without_a_cutoff_is_refused_unless_told(two_runs, tmp_path, capsys, monkeypatch):
    store, _, probe_file = two_runs
    no_cutoff = tmp_path / "nocut"
    no_cutoff.mkdir()
    variant_file = write_variant(no_cutoff, "blind", cutoff=None)
    monkeypatch.setattr(cli, "_build_model", lambda _variant: Forecaster())
    for segment in ("train", "validation"):
        argv = _probe_argv(store, variant_file, probe_file, RUN_ID)
        assert cli.main([*argv, "--segment", segment]) == 0
    capsys.readouterr()
    assert cli.main(_pool(store, "--run-id", RUN_ID, variant="blind")) == 1
    assert "'blind' records no model_cutoff" in capsys.readouterr().err
    assert cli.main(_pool(store, "--run-id", RUN_ID, "--include-pre-cutoff", variant="blind")) == 0


def test_the_pool_leaves_out_the_questions_on_or_before_the_cutoff(
    two_runs, tmp_path, capsys, monkeypatch
):
    store, _, probe_file = two_runs
    late = tmp_path / "late"
    late.mkdir()
    # 2027-01-16: every question of the fixture (slots 0-9, the 15th and 16th)
    # is decided on or before it; the report counts the validation ones, slots 6-7.
    variant_file = write_variant(late, "late", cutoff="2027-01-16")
    monkeypatch.setattr(cli, "_build_model", lambda _variant: Forecaster())
    for segment in ("train", "validation"):
        argv = _probe_argv(store, variant_file, probe_file, RUN_ID)
        assert cli.main([*argv, "--segment", segment]) == 0
    capsys.readouterr()
    assert cli.main(_pool(store, "--run-id", RUN_ID, variant="late")) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[2].endswith("): h4 0 question(s), h24 0 question(s); 2 left out at the model cutoff")
    assert cli.main(_pool(store, "--run-id", RUN_ID, "--include-pre-cutoff", variant="late")) == 0
    assert (
        capsys.readouterr().out.splitlines()[2].endswith("): h4 1 question(s), h24 0 question(s)")
    )
