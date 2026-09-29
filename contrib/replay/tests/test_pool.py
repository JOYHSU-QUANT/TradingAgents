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

Each run's forecaster gives every question the same answer, so the mean of
its train forecasts, the model's own prior, is that answer too: against it
each run scores exactly 0, as any fixed forecast does (plan section 5,
revised 2026-09-29). The own-prior cases that need a forecast that varies
build a run of their own (:func:`_varied`).
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
from contrib.replay.probe_score import QuestionScore, ensemble, headline_scores, own_prior
from contrib.replay.replay_store import ReplayStore
from contrib.replay.score import build_split, score_run
from contrib.replay.upstream import CostModel, Database, SegmentName
from contrib.replay.variant import load_variant

from .papers import (
    RUN_ID,
    TRAIN,
    VALIDATION,
    input_id,
    replay_argv,
    write_gate_store,
    write_variant,
)
from .test_probe import H4, Forecaster, forecast_text, write_probe

OTHER_RUN = "paper-GATE2"
STEP_MS = 4 * 3_600_000
A, B = (0.26, 0.5), (0.98, 0.5)  # an up forecast at .6 and one at .2, against the base rate


def _score(
    i: int,
    brier: float,
    base: float,
    own: tuple[float, float] | None = None,
    own_binary: tuple[float, float] | None = None,
) -> QuestionScore:
    return QuestionScore(f"q{i}", i, brier, base, False, None, own, own_binary)


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
    def interval(blocks: int, low: float | None) -> Interval:
        return Interval(30, blocks, 0.3, low, 0.9, 0)

    ok = interval(5, 0.01)
    assert _verdict([("a", ok), ("b", ok)], block=6, seed=4) == "met (seed 4)"
    # Every judged interval must clear 0; one that reaches it is named.
    assert _verdict([("a", ok), ("b", interval(5, 0.0))], block=6, seed=4) == (
        "not met: b lower end +0.000 (seed 4)"
    )
    assert _verdict([("a", interval(5, -0.01)), ("b", interval(5, -0.02))], block=6, seed=4) == (
        "not met: a lower end -0.010, b lower end -0.020 (seed 4)"
    )
    # One that fails says "not met" even when another cannot be read, named beside it.
    assert _verdict([("a", interval(5, -0.01)), ("b", interval(4, 0.2))], block=6, seed=4) == (
        "not met: a lower end -0.010; b has 4 block(s), fewer than the 5 the bar needs (seed 4)"
    )
    # Nothing fails and something cannot be read: the bar cannot be judged.
    assert _verdict([("a", ok), ("b", interval(4, 0.2))], block=6, seed=4) == (
        "cannot be judged: b has 4 block(s), fewer than the 5 the bar needs"
    )
    assert _verdict([("a", interval(5, None)), ("b", interval(0, None))], block=6, seed=4) == (
        "cannot be judged: a has no interval; b has 0 block(s), fewer than the 5 the bar needs"
    )
    assert _verdict([("a", ok), ("b", ok)], block=3, seed=4) == (
        "not judged: the bar is read with blocks of 6, this report used 3"
    )


def test_the_report_names_each_run_and_judges_the_4h_skills_against_the_own_prior():
    runs = [
        RunScores(
            "r1", "2027-02-01T00:00:00+00:00", {"h4": [_score(1, *A, own=B)], "h24": []}
        ),
        RunScores(
            "r2",
            "2027-02-01T00:00:00+00:00",
            {"h4": [_score(2, *B, own=B, own_binary=A)], "h24": None},
            left_out=3,
        ),
    ]
    none = "n 0 in 0 block(s); skill n/a, 90% interval [n/a, n/a]"
    overlap = " (reported only: its returns overlap from question to question)"
    assert describe_pool(runs, draws=400, seed=7) == [
        "pooled over 2 run(s), the validation segment of each run's pinned split; circular "
        "blocks of 6 consecutive question(s) within each run, 400 draws, seed 7",
        "  run r1 (split pinned 2027-02-01T00:00:00+00:00): h4 1 question(s), h24 0 question(s)",
        "  run r2 (split pinned 2027-02-01T00:00:00+00:00): h4 1 question(s), h24 n/a (no train "
        "base rate); 3 left out at the model cutoff",
        "h4 headline against the train base rate: n 2 in 2 block(s); skill -0.240, 90% interval "
        "[-0.240, -0.240] (reported only)",
        # Two B pairs: 1 - 1.96 / 1.0.
        "h4 headline against the model's own train prior: n 2 in 2 block(s); skill -0.960, 90% "
        "interval [-0.960, -0.960]",
        f"h4 up vs down given a move, against the train base rate: {none} (reported only)",
        "h4 up vs down given a move, against the model's own train prior: n 1 in 1 block(s); "
        "skill +0.480, 90% interval [+0.480, +0.480]",
        f"h24 headline against the train base rate: {none}{overlap}",
        f"h24 headline against the model's own train prior: {none}{overlap}",
        f"h24 up vs down given a move, against the train base rate: {none}{overlap}",
        f"h24 up vs down given a move, against the model's own train prior: {none}{overlap}",
        "plan section 5 bar (revised 2026-09-29: the h4 headline skill and the h4 up-vs-down "
        "skill, each against the model's own train prior, the lower end of each 90% interval "
        "above 0, blocks of 6, at least 5 blocks each): cannot be judged: the headline has 2 "
        "block(s), fewer than the 5 the bar needs; up vs down has 1 block(s), fewer than the 5 "
        "the bar needs",
    ]


def _run_of(n: int, *, base: tuple[float, float], own: tuple[float, float], moved: int) -> RunScores:
    """``n`` questions scored ``base`` against the base rate and ``own`` against the prior;
    the first ``moved`` of them moved and score ``own`` up against down too."""
    scores = [
        _score(i, *base, own=own, own_binary=own if i < moved else None) for i in range(n)
    ]
    return RunScores("r", "t", {"h4": scores, "h24": []})


def test_the_bar_needs_five_blocks_of_six_for_each_skill():
    assert describe_pool([_run_of(30, base=A, own=A, moved=30)], draws=100, seed=2)[-1].endswith(
        ": met (seed 2)"
    )
    assert describe_pool([_run_of(24, base=A, own=A, moved=24)], draws=100)[-1].endswith(
        ": cannot be judged: the headline has 4 block(s), fewer than the 5 the bar needs; up vs "
        "down has 4 block(s), fewer than the 5 the bar needs"
    )
    # Up against down counts only the questions that moved.
    assert describe_pool([_run_of(30, base=A, own=A, moved=24)], draws=100)[-1].endswith(
        ": cannot be judged: up vs down has 4 block(s), fewer than the 5 the bar needs"
    )
    assert describe_pool(
        [_run_of(30, base=A, own=A, moved=30)], block=3, draws=100
    )[-1].endswith(": not judged: the bar is read with blocks of 6, this report used 3")


def test_beating_the_base_rate_without_beating_the_own_prior_is_not_met():
    # The first acceptance run's shape: better than the base rate on every
    # question (A), no better than the model's own fixed forecast (B).
    report = describe_pool([_run_of(30, base=A, own=B, moved=30)], draws=100, seed=2)
    assert report[2].startswith(
        "h4 headline against the train base rate: n 30 in 5 block(s); skill +0.480"
    )
    assert report[-1].endswith(
        ": not met: the headline lower end -0.960, up vs down lower end -0.960 (seed 2)"
    )
    # Beating the prior on the headline alone is not enough either.
    mixed = RunScores(
        "r",
        "t",
        {"h4": [_score(i, *A, own=A, own_binary=B) for i in range(30)], "h24": []},
    )
    assert describe_pool([mixed], draws=100, seed=2)[-1].endswith(
        ": not met: up vs down lower end -0.960 (seed 2)"
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
    # Every train answer is the same, so the prior is that answer: no skill against it.
    assert only.own == (pytest.approx(0.26), pytest.approx(0.26))
    assert only.own_binary == (pytest.approx((1 / 7) ** 2), pytest.approx((1 / 7) ** 2))
    assert headline_scores(card, answers, eligible, "h24", SegmentName.VALIDATION) == []


# -- the model's own prior, on a forecast that varies ------------------------------

# Train slots 1, 3 and 5 answer h4 up .2 / down .2 / flat .6; the others,
# and the validation slots, answer the default up .6 / down .1 / flat .3. The
# prior is the mean over the six train slots: up .4 / down .15 / flat .45.
LOW = {"up": 0.2, "down": 0.2, "flat": 0.6}
PRIOR = {"up": 0.4, "down": 0.15, "flat": 0.45}


def _varied(tmp_path: Path, monkeypatch, *, text_for: dict[int, str] | None = None):
    """A run probed on its train and validation segments.

    Returns ``(card, answers, eligible, store)``: the scorecard, the probe's
    answers, every question's input id, and the paper store's path.
    """
    store = write_gate_store(tmp_path / "paper_trading.db")
    variant_file = write_variant(tmp_path, "echo")
    probe_file = write_probe(tmp_path)
    monkeypatch.setattr(cli, "_sleep", lambda _seconds: None)
    answers_for = {slot: forecast_text(LOW) for slot in TRAIN[1::2]} | (text_for or {})
    model = Forecaster(text_for=answers_for)
    monkeypatch.setattr(cli, "_build_model", lambda _variant: model)
    for segment in ("train", "validation"):
        argv = _probe_argv(store, variant_file, probe_file, RUN_ID)
        assert cli.main([*argv, "--segment", segment]) == 0
    with Database(store, migrate=False) as db:
        questions = load_decisions(db, RUN_ID).questions
    split = build_split(questions, interval="4h", step_ms=STEP_MS)
    card = score_run(questions, [], step_ms=STEP_MS, costs=CostModel(), split=split)
    with ReplayStore(store.parent / "replay.sqlite") as replay:
        [(_, answers)] = replay.probe_answers(load_variant(variant_file).sha, RUN_ID)
    return card, answers, {q.input_id for q in questions}, store


def test_the_own_prior_is_the_mean_of_the_eligible_train_forecasts(tmp_path, monkeypatch):
    card, answers, eligible, _ = _varied(tmp_path, monkeypatch)
    merged = ensemble(answers)
    assert own_prior(card, merged, eligible, "h4") == pytest.approx(PRIOR)
    # Only the eligible train questions count: without the three low ones, the
    # prior is the default answer. The validation answers never count.
    high = eligible - {input_id(slot) for slot in TRAIN[1::2]}
    assert own_prior(card, merged, high, "h4") == pytest.approx(H4)
    assert own_prior(card, merged, {input_id(slot) for slot in VALIDATION}, "h4") is None


def test_each_question_is_scored_against_the_own_prior(tmp_path, monkeypatch):
    card, answers, eligible, _ = _varied(tmp_path, monkeypatch)
    [only] = headline_scores(card, answers, eligible, "h4", SegmentName.VALIDATION)
    # Slot 6 went up: the forecast costs 0.26; the prior (.4-1)² + .15² + .45²
    # = 0.585. Up given a move: 6/7 against the prior's .4 / .55 = 8/11.
    assert only.own == (pytest.approx(0.26), pytest.approx(0.585))
    assert only.own_binary == (pytest.approx((1 / 7) ** 2), pytest.approx((3 / 11) ** 2))
    # The base-rate pair is untouched by the prior.
    assert (only.brier, only.base_brier) == (pytest.approx(0.26), pytest.approx(0.5))
    # Only a question that moved is scored up against down: train slots 1, 3
    # and 5 stayed flat.
    train = headline_scores(card, answers, eligible, "h4", SegmentName.TRAIN)
    assert [s.own_binary is None for s in train] == [False, True, False, True, False, True]


def test_the_pool_prints_the_skill_against_the_own_prior(tmp_path, monkeypatch, capsys):
    _, _, _, store = _varied(tmp_path, monkeypatch)
    capsys.readouterr()
    assert cli.main(_pool(store, "--run-id", RUN_ID, "--draws", "50")) == 0
    out = capsys.readouterr().out.splitlines()
    # 1 - 0.26 / 0.585 = 5/9; up against down 1 - (1/7)² / (3/11)² = 1 - 121/441.
    assert (
        "h4 headline against the model's own train prior: n 1 in 1 block(s); skill +0.556, 90% "
        "interval [+0.556, +0.556]"
    ) in out
    assert (
        "h4 up vs down given a move, against the model's own train prior: n 1 in 1 block(s); "
        "skill +0.726, 90% interval [+0.726, +0.726]"
    ) in out


def test_a_stand_in_is_scored_as_the_own_prior(tmp_path, monkeypatch):
    card, answers, eligible, _ = _varied(
        tmp_path, monkeypatch, text_for={VALIDATION[0]: "no idea"}
    )
    [only] = headline_scores(card, answers, eligible, "h4", SegmentName.VALIDATION)
    assert only.stand_in
    assert (only.brier, only.base_brier) == (pytest.approx(0.5), pytest.approx(0.5))
    assert only.own == (pytest.approx(0.585), pytest.approx(0.585))
    assert only.own_binary == (pytest.approx((3 / 11) ** 2), pytest.approx((3 / 11) ** 2))


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
    assert (
        "h4 headline against the train base rate: n 2 in 2 block(s); skill -0.240, 90% interval "
        "[-0.240, -0.240] (reported only)"
    ) in out
    assert out[-1].endswith(
        ": cannot be judged: the headline has 2 block(s), fewer than the 5 the bar needs; up vs "
        "down has 2 block(s), fewer than the 5 the bar needs"
    )


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


def test_a_run_without_a_train_forecast_is_refused_not_left_out(two_runs, capsys):
    store, variant_file, probe_file = two_runs
    third = "paper-GATE3"
    write_gate_store(store, run_id=third)
    # Probed on its validation segment only: no train answer, so no prior.
    argv = _probe_argv(store, variant_file, probe_file, third)
    assert cli.main([*argv, "--segment", "validation"]) == 0
    capsys.readouterr()
    assert cli.main(_pool(store, "--run-id", RUN_ID, "--run-id", third)) == 1
    assert (
        f"run {third!r} has no valid h4 train forecast from 'echo' for the probe 'direction-t', "
        "so the model's own prior is unknown"
    ) in capsys.readouterr().err


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
