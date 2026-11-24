"""The pick by training score, run by the bench (tools plan §4, T5): searched after Grid, recorded with the others, read back.

The arm itself — what it draws, scores and proposes, round after round —
is held in ``test_hypothesists.py`` on the hand-written history. Here it
is one arm of a world: the example world landed under a manifest that
names it and searched ONCE (``picked``): a run costs a landing check and
the oracle's frame whatever its arms do, and each arm a frame a round and
one more where a trial is selected.

That one run is chosen for the case the records had none of before this
arm: a pool of three rules and a budget of three, two rules drawn a
round. Random and Grid answer three times and are never asked a fourth;
the pick draws two, then the one left, and is asked a third time — so it
ends with its pool out after TWO answers, a number that is neither the
pool's size nor the budget.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from contrib.discovery import bench
from contrib.discovery.bench import (
    ARMS_DIRNAME,
    VERDICTS_FILENAME,
    Arm,
    ArmResult,
    BenchError,
    Verdicts,
    _train_view,
    calibration_table,
    read_manifest,
    read_searched,
    run_world,
    store_path,
)
from contrib.discovery.hypothesists import (
    CandidatesExhausted,
    TrainPickHypothesist,
    enumerate_pool,
    rounds_to_draw,
)
from contrib.discovery.landing import MANIFEST_FILENAME, world_dir
from contrib.discovery.manifest import Budget, Manifest, TrainPickArm
from contrib.discovery.modeluse import MODEL_USE_FILENAME
from contrib.discovery.scoring import report_table, score_world
from contrib.discovery.tooluse import TOOL_USE_FILENAME
from contrib.discovery.verdict import Ended, final_verdict

from .conftest import bench_manifest, example_manifest, land_run
from .test_bench import _copy_records, _stray

# Not its own position in the list: a replicate of this arm is numbered
# by its seed, as the Random arm's is.
SEED = 3
BACKTESTS = 2
POOL = 3
BUDGET = 3
# Two a round from three rules: two, then the one left.
ROUNDS = rounds_to_draw(POOL, BACKTESTS)
assert ROUNDS == 2


def _pick_manifest() -> Manifest:
    """The bench's manifest (one Random seed, no model) over three rules, naming the pick beside them."""
    base = bench_manifest()
    space = replace(
        base.search_space,
        templates=base.search_space.templates[:1],
        threshold_grid={"funding_zscore_14": (-2.0, -1.0, 1.0), "realized_vol_20": (0.002,)},
    )
    return replace(
        base,
        budget=Budget(max_trials=BUDGET, oracle_margin=0.25),
        search_space=space,
        arms=replace(
            base.arms, train_pick=TrainPickArm(search_seeds=(SEED,), backtests=BACKTESTS)
        ),
    )


@pytest.fixture(scope="module")
def picked(landed_world, tmp_path_factory):
    """The example world searched once with the pick among its arms: the run, the records, what was printed."""
    artifact, _landing = landed_world
    run = land_run(tmp_path_factory.mktemp("pick"), artifact, _pick_manifest())
    lines: list[str] = []
    verdicts, oracle = run_world(run, on_line=lines.append)
    return run, verdicts, oracle, lines


def _result(verdicts: Verdicts, arm: Arm, replicate: int) -> ArmResult:
    return next(r for r in verdicts.results if (r.arm, r.replicate) == (arm, replicate))


def _with(verdicts: Verdicts, changed: ArmResult) -> Verdicts:
    """``verdicts`` with the result of ``changed``'s arm and replicate replaced by it."""
    return replace(
        verdicts,
        results=tuple(
            changed if (r.arm, r.replicate) == (changed.arm, changed.replicate) else r
            for r in verdicts.results
        ),
    )


def test_the_pick_is_searched_after_grid_in_a_store_of_its_own_and_recorded_with_the_others(picked):
    run, verdicts, _oracle, lines = picked
    manifest = read_manifest(run)
    assert verdicts.pool_size == POOL
    assert [(r.arm, r.replicate) for r in verdicts.results] == [
        (Arm.RANDOM, 0),
        (Arm.GRID, 0),
        (Arm.TRAIN_PICK, SEED),
    ]
    assert sorted(p.name for p in (run / ARMS_DIRNAME).iterdir()) == ["grid", "random", "train_pick"]
    # Its store and nothing beside it: the arm starts no server, keeps no
    # trace, and leaves no record of its own.
    assert [p.name for p in (run / ARMS_DIRNAME / "train_pick").iterdir()] == [
        f"replicate-{SEED}.sqlite"
    ]
    assert store_path(run, Arm.TRAIN_PICK, SEED).is_file()
    assert not (run / TOOL_USE_FILENAME).exists() and not (run / MODEL_USE_FILENAME).exists()
    # Random and Grid spend the budget on the three rules and are not
    # asked again; the pick's pool is out after two answers.
    for arm in (Arm.RANDOM, Arm.GRID):
        snapshot = _result(verdicts, arm, 0).snapshot
        assert (snapshot.ended, snapshot.answers) == (Ended.BUDGET_SPENT, BUDGET)
    pick = _result(verdicts, Arm.TRAIN_PICK, SEED)
    assert pick.experiment_id == f"train_pick-{SEED}"
    assert (pick.snapshot.ended, pick.snapshot.answers) == (Ended.EXHAUSTED, ROUNDS)
    assert pick.snapshot.distinct_rules == ROUNDS == len(pick.snapshot.trials)
    said = "\n".join(lines)
    assert f"{ROUNDS} of {BUDGET} round(s) spent on train_pick-{SEED}: {ROUNDS} measured" in said
    assert (
        f"train_pick seed {SEED}: every one of the pool's {POOL} candidate(s) has been drawn" in said
    )
    assert pick.describe() in lines
    # One search under the conditions every arm has, of rules of the pool.
    assert bench._conditions(pick.snapshot.experiment) == bench._conditions(
        verdicts.grid.snapshot.experiment
    )
    assert {trial.spec_hash for trial in pick.snapshot.trials} < {
        candidate.spec_hash for candidate in manifest.search_space.candidates
    }
    # And the directory reads back as what was written: a pool out in
    # fewer answers than it holds rules is this arm's, and is read.
    searched = read_searched(run)
    assert searched is not None and searched.verdicts == verdicts
    assert (searched.tool_use, searched.model_use) == (None, None)


def test_the_pick_is_run_after_grid_and_before_the_arm_that_costs_money(monkeypatch):
    # Read off the list a run walks, with nothing searched: a manifest
    # naming the pick beside a model arm runs every arm that asks no
    # provider first, so a world whose pool arms cannot run has spent
    # nothing. A replicate of the pick is numbered by its seed, in the
    # order the manifest lists them.
    base = example_manifest()
    assert base.arms.llm is not None and base.arms.random.search_seeds == (0, 1)
    manifest = replace(
        base,
        arms=replace(base.arms, train_pick=TrainPickArm(search_seeds=(5, 2), backtests=BACKTESTS)),
    )

    def build(provider, name, base_url=None, **kwargs):
        return bench.ChatHypothesist(model=object(), label=f"{provider}/{name}")

    monkeypatch.setattr(bench.ChatHypothesist, "build", build)
    assert [(arm, replicate) for arm, replicate, _opened in bench._arms(manifest)] == [
        (Arm.RANDOM, 0),
        (Arm.RANDOM, 1),
        (Arm.GRID, 0),
        (Arm.TRAIN_PICK, 5),
        (Arm.TRAIN_PICK, 2),
        (Arm.LLM, 0),
    ]


def test_the_pick_s_trials_are_the_rules_that_score_best_on_its_own_training_window(picked):
    run, verdicts, *_ = picked
    manifest = read_manifest(run)
    pool = enumerate_pool(manifest.search_space)
    pick = _result(verdicts, Arm.TRAIN_PICK, SEED)
    # The search over again, from the arm's own store and its seed (T5 a):
    # the same rules drawn, the same ones proposed, the pool out as it was.
    again = TrainPickHypothesist(
        pool,
        _train_view(store_path(run, Arm.TRAIN_PICK, SEED), pick.snapshot.experiment),
        world_id=manifest.world.id,
        search_seed=SEED,
        backtests=manifest.arms.train_pick.backtests,
    )
    for _ in range(ROUNDS):
        again.propose("ignored", "ignored")
    with pytest.raises(CandidatesExhausted):
        again.propose("ignored", "ignored")
    assert [len(round_.scored) for round_ in again.rounds] == [2, 1]
    trials = sorted(pick.snapshot.trials, key=lambda trial: trial.trial_id)
    assert [trial.spec_hash for trial in trials] == [
        pool.candidates[round_.picked].spec_hash for round_ in again.rounds
    ]
    # The figures each was picked on are its trial's own for the training
    # window: the arm scored what the loop then measured.
    for trial, round_ in zip(trials, again.rounds, strict=True):
        assert trial.train == round_.best.train


def test_the_tables_give_the_pick_a_row_of_its_own_between_grid_and_a_model(picked):
    run, _verdicts, _oracle, _lines = picked
    rows = calibration_table([run])
    assert [row.arm for row in rows] == [Arm.RANDOM, Arm.GRID, Arm.TRAIN_PICK]
    pick = rows[-1]
    # A pool drawn dry is a search that finished, not a seam that failed.
    assert (pick.searches, pick.failed, pick.effort) == (1, 0, ((ROUNDS, ROUNDS),))
    assert " train_pick: " in pick.describe()
    # Its row says how many rules each answer was picked among: the two
    # distinct rules it counts are the ones proposed, of three it scored.
    looked = "; each answer the best of up to 2 rule(s) scored on the training window"
    assert pick.scored == BACKTESTS and pick.describe().endswith(
        f"answers median 2 (min 2), distinct rules median 2 (min 2){looked}"
    )
    for row in rows[:-1]:
        assert row.scored is None and "scored on the training window" not in row.describe()
    # The number goes with that arm's row and no other.
    with pytest.raises(BenchError, match=r"row\.scored: 2 on a random row"):
        replace(rows[0], scored=2)
    for missing in (None, 0, True):
        with pytest.raises(BenchError, match=r"row\.scored: expected a whole number"):
            replace(pick, scored=missing)
    # Scored and reported as any arm is; it asked no model, and its row
    # says nothing of tokens.
    scores = score_world(run)
    assert [(s.arm, s.replicate) for s in scores.results] == [
        (Arm.RANDOM, 0),
        (Arm.GRID, 0),
        (Arm.TRAIN_PICK, SEED),
    ]
    report = {row.arm: row for row in report_table([run])}
    assert list(report) == [Arm.RANDOM, Arm.GRID, Arm.TRAIN_PICK]
    assert (report[Arm.TRAIN_PICK].model_uses, report[Arm.TRAIN_PICK].model) == ((), None)
    assert not any("tokens" in line for line in report[Arm.TRAIN_PICK].describe())
    # The report's row says the same of what was looked at, on its effort line.
    assert report[Arm.TRAIN_PICK].scored == BACKTESTS
    assert [line for line in report[Arm.TRAIN_PICK].describe() if looked in line] == [
        f"  answers median 2 (min 2), distinct rules median 2 (min 2){looked}"
    ]
    for arm in (Arm.RANDOM, Arm.GRID):
        assert report[arm].scored is None
        assert not any("scored on the training window" in line for line in report[arm].describe())
    with pytest.raises(BenchError, match=r"row\.scored: 2 on a grid row"):
        replace(report[Arm.GRID], scored=2)
    # A table's rows come in the enum's order, and the pool's bounds hold
    # the arms that propose from it.
    assert list(Arm) == [Arm.RANDOM, Arm.GRID, Arm.TRAIN_PICK, Arm.LLM, Arm.LLM_TOOLS]
    assert [arm for arm in Arm if arm.walks_pool] == [Arm.RANDOM, Arm.GRID, Arm.TRAIN_PICK]
    # ... and of those, the two that take one rule an answer are held to
    # running out after as many answers as the pool holds rules.
    assert [arm for arm in Arm if arm.one_rule_an_answer] == [Arm.RANDOM, Arm.GRID]


def test_records_and_a_manifest_that_disagree_about_the_pick_are_refused(picked, tmp_path):
    run, verdicts, oracle, _lines = picked
    manifest = read_manifest(run)
    # A replicate the manifest names and the records do not hold.
    short = replace(verdicts, results=verdicts.results[:-1])
    with pytest.raises(BenchError, match=r"holds the arms .* the manifest beside it names"):
        read_searched(_stray(tmp_path, run, short, oracle))
    # The records of a run with the pick, beside a manifest that names none.
    without = replace(manifest, arms=replace(manifest.arms, train_pick=None))
    bare = world_dir(tmp_path, without)
    bare.mkdir()
    (bare / MANIFEST_FILENAME).write_bytes(without.dumps().encode("utf-8"))
    _copy_records(run, bare)
    with pytest.raises(
        BenchError, match=rf"holds the arms .*train_pick-{SEED}.* the manifest beside"
    ):
        read_searched(bare)
    # And held to that manifest by hand, past the check above, the pick's
    # result is refused for what it is, not passed over unheld.
    with pytest.raises(
        BenchError,
        match=rf"train_pick-{SEED} is a pick by training score, and the manifest beside it names none",
    ):
        bench._hold_to_manifest(bare / VERDICTS_FILENAME, verdicts, without)
    # A rule of the pick's that is no rule of the pool: it proposes from
    # the pool and nowhere else, as Random and Grid do.
    pick = _result(verdicts, Arm.TRAIN_PICK, SEED)
    first, *rest = pick.snapshot.trials
    foreign = replace(first, spec=replace(first.spec, max_bars=first.spec.max_bars + 1))
    outside = replace(pick, snapshot=replace(pick.snapshot, trials=(foreign, *rest)))
    with pytest.raises(
        BenchError, match=rf"train_pick-{SEED} filed trial\(s\) .* not rules of the pool"
    ):
        read_searched(_stray(tmp_path, run, _with(verdicts, outside), oracle))


def test_the_pick_s_pool_is_out_in_the_rounds_its_draws_make_and_no_other_number(picked, tmp_path):
    # Three rules two a round are out in two answers. A record of the pick
    # running dry after one is not that search's — and is the manifest's
    # to refuse, where the number drawn a round is written: the record
    # itself holds only that the answers do not outnumber the pool.
    run, verdicts, oracle, _lines = picked
    pick = _result(verdicts, Arm.TRAIN_PICK, SEED)
    # Round 1 as the first round to pass the gate: a snapshot may say so
    # of a trial that no longer passes at the final count, and has to say
    # a round if one still does — so this holds whatever the trial scored.
    after_one = replace(
        pick.snapshot,
        answers=1,
        distinct_rules=1,
        first_eligible_round=1,
        trials=pick.snapshot.trials[:1],
    )
    # A result carries a holdout figure exactly when its snapshot selects a trial.
    assert oracle.planted is not None
    holdout = (
        None
        if final_verdict(after_one).selected is None
        else (pick.holdout or oracle.planted.holdout)
    )
    early = replace(pick, snapshot=after_one, holdout=holdout)
    with pytest.raises(
        BenchError,
        match=(
            rf"train_pick-{SEED} walked its pool to the end in 1 answer\(s\); drawing 2 a round "
            r"from a pool of 3 ends it in 2"
        ),
    ):
        read_searched(_stray(tmp_path, run, _with(verdicts, early), oracle))
    # Nor does it answer more often than that, however its search ended: a
    # third answer inside the budget of three has no rule left to be of.
    third = replace(
        pick, snapshot=replace(pick.snapshot, ended=Ended.BUDGET_SPENT, answers=BUDGET)
    )
    with pytest.raises(
        BenchError,
        match=(
            rf"train_pick-{SEED} answered 3 time\(s\); drawing 2 a round from a pool of 3 gives "
            r"2 answer\(s\) at most"
        ),
    ):
        read_searched(_stray(tmp_path, run, _with(verdicts, third), oracle))
    over = replace(pick, snapshot=replace(pick.snapshot, answers=POOL + 1))
    with pytest.raises(BenchError, match=rf"train_pick-{SEED} answered 4 time\(s\) from a pool of 3"):
        _with(verdicts, over)
