"""One world run to its verdicts (plan §3, PR 3): the run directory, the records, the protocol's order.

One real run, once per session: the example world (landed once by the
suite's ``landed_world`` fixture) under a two-answer budget with one
Random replicate and the Grid arm, so the arms' reports, the two records,
the filled manifest and the arms' own stores can all be read off one
search. The protocol's refusals and the interrupt are cheap and run on
fresh copies with the slow parts patched out where the test is not about
them.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

from contrib.autoresearch.ledger import Ledger, TrialStatus
from contrib.autoresearch.ports import HypothesistError
from contrib.autoresearch.store import ResearchStore
from contrib.discovery import bench
from contrib.discovery.bench import (
    ARMS_DIRNAME,
    ORACLE_FILENAME,
    VERDICTS_FILENAME,
    Arm,
    ArmResult,
    BenchError,
    CalibrationRow,
    Verdicts,
    calibration_table,
    experiment_id,
    preview_oracle,
    read_manifest,
    require_artifact,
    run_world,
    store_path,
)
from contrib.discovery.landing import ARTIFACT_FILENAME, MANIFEST_FILENAME, split_of, world_dir
from contrib.discovery.manifest import ManifestError, SeedSet, WorldKind
from contrib.discovery.oracle import Oracle, OracleError
from contrib.discovery.verdict import Ended, VerdictError, final_verdict

from .conftest import COIN, bench_manifest, example_manifest, land_run

LANDED = sorted([MANIFEST_FILENAME, ARTIFACT_FILENAME])


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _listing(run: Path) -> list[str]:
    return sorted(p.name for p in run.iterdir())


@pytest.fixture
def fresh_run(landed_world, tmp_path):
    """A landed run directory nothing has searched, for the tests that stop a run short."""
    artifact, _landing = landed_world
    return land_run(tmp_path, artifact, bench_manifest())


# -- the run ----------------------------------------------------------------------------------


def test_a_searched_run_directory_holds_the_arms_the_records_and_the_filled_manifest(searched):
    run, verdicts, oracle, lines, before = searched
    assert _listing(run) == sorted([*LANDED, ARMS_DIRNAME, ORACLE_FILENAME, VERDICTS_FILENAME])
    for arm, replicate in ((Arm.RANDOM, 0), (Arm.GRID, 0)):
        assert store_path(run, arm, replicate).is_file()
    assert _listing(run / ARMS_DIRNAME) == ["grid", "random"]
    # The artifact was copied, never written; the records read back equal;
    # the landed manifest (``pool_size`` filled in by the landing) is
    # untouched, and the directory it names is still this one.
    assert _digest(run / ARTIFACT_FILENAME) == before
    assert Verdicts.loads((run / VERDICTS_FILENAME).read_text(encoding="utf-8")) == verdicts
    assert Oracle.loads((run / ORACLE_FILENAME).read_text(encoding="utf-8")) == oracle
    filled = read_manifest(run)
    assert filled.search_space.pool_size == 16 == verdicts.pool_size
    assert replace(filled.search_space, pool_size=None) == bench_manifest().search_space
    assert (run / MANIFEST_FILENAME).read_bytes() == filled.dumps().encode("utf-8")
    assert world_dir(run.parent, filled) == run
    assert "pool: 16 candidate(s) (16 enumerated)" in lines
    # A development seed: the holdout figures are on the console.
    assert any("holdout net sharpe" in line for line in lines)


def test_every_arm_is_one_search_under_the_one_set_of_conditions(searched):
    run, verdicts, _oracle, lines, _before = searched
    manifest = read_manifest(run)
    assert [(r.arm, r.replicate) for r in verdicts.results] == [(Arm.RANDOM, 0), (Arm.GRID, 0)]
    for result in verdicts.results:
        assert result.snapshot.ended is Ended.BUDGET_SPENT
        assert result.snapshot.answers == 2 == result.verdict.answers
        assert result.snapshot.distinct_rules == 2
        assert result.verdict.threshold == manifest.penalty.threshold(2)
        assert result.verdict == final_verdict(result.snapshot)
        experiment = result.snapshot.experiment
        assert experiment.experiment_id == result.experiment_id
        assert result.experiment_id == experiment_id(result.arm, result.replicate)
        assert experiment.coin == COIN == manifest.world.id
        assert experiment.split == split_of(manifest.world, manifest.split)
        assert experiment.costs == manifest.costs
        assert experiment.penalty == manifest.penalty
        assert experiment.indicator_lookback == manifest.indicator_lookback
        assert f"2 of 2 round(s) spent on {result.experiment_id}: 2 measured" in "\n".join(lines)
        assert result.describe() in lines
        # The arm's own store is the record: its one experiment, its two
        # trials, and the selected trial promoted there — and only it.
        with ResearchStore(store_path(run, result.arm, result.replicate)) as store:
            ledger = Ledger(store)
            assert ledger.rules_tried(COIN) == 2
            trials = ledger.trials(result.experiment_id)
            assert len(trials) == 2
            promoted = [t for t in trials if t.status is TrialStatus.PROMOTED]
            if result.verdict.selected is None:
                assert promoted == [] and result.holdout is None
            else:
                assert [t.trial_id for t in promoted] == [result.verdict.selected]
                assert promoted[0].holdout == result.holdout


def test_the_oracle_is_read_after_the_arms_with_the_holdout_and_the_whole_pool(searched):
    run, verdicts, oracle, lines, _before = searched
    manifest = read_manifest(run)
    assert oracle.world_id == COIN
    assert oracle.planted is not None  # a conditional world
    assert oracle.planted.holdout is not None
    assert oracle.planted.budget == 2
    assert oracle.planted.threshold == manifest.penalty.threshold(2)
    assert oracle.planted.oracle_margin == 0.25
    assert oracle.planted.margin == oracle.planted.validation.net.sharpe - oracle.planted.threshold
    assert oracle.pool.size == 16 == len(oracle.pool.validation_sharpes)
    assert oracle.pool.threshold == manifest.penalty.threshold(16)
    assert 0 <= oracle.pool.best < 16
    assert 0 <= oracle.pool.clearing <= 16
    for line in oracle.describe():
        assert line in lines
    # Printed after every arm's report: the order the protocol fixes.
    assert lines.index(oracle.describe()[0]) > lines.index(verdicts.results[-1].describe())
    # The oracle's read filed nothing on the grid arm's store.
    with ResearchStore(store_path(run, Arm.GRID, 0)) as store:
        assert Ledger(store).rules_tried(COIN) == 2


def test_a_world_is_searched_once(searched):
    run, *_ = searched
    with pytest.raises(BenchError, match="searched before"):
        run_world(run)


def test_the_calibration_table_tallies_the_searched_worlds_by_kind_and_arm(searched, tmp_path):
    run, *_ = searched
    unsearched = tmp_path / "not-a-run"
    unsearched.mkdir()
    rows = calibration_table([unsearched, run])
    assert [(r.kind, r.null_definition_version, r.arm, r.searches) for r in rows] == [
        (WorldKind.CONDITIONAL_V1, "v1", Arm.RANDOM, 1),
        (WorldKind.CONDITIONAL_V1, "v1", Arm.GRID, 1),
    ]
    for row in rows:
        assert row.found in (0, 1) and row.rate == row.found and row.failed == 0
        assert row.describe().startswith(
            f"conditional-v1 (v1) {row.arm.value}: {row.found} non-empty"
        )
        assert "seam failure" not in row.describe()
    assert calibration_table([unsearched]) == []
    # The row's own gates: a tally of nothing, or more found than searched.
    with pytest.raises(BenchError, match="at least one result"):
        CalibrationRow(WorldKind.NULL_V1, "v1", Arm.GRID, searches=0, found=0, failed=0)
    with pytest.raises(BenchError, match="2 found in 1 search"):
        CalibrationRow(WorldKind.NULL_V1, "v1", Arm.GRID, searches=1, found=2, failed=0)
    dead = CalibrationRow(WorldKind.NULL_V1, "v1", Arm.GRID, searches=0, found=0, failed=1)
    assert dead.rate is None
    assert (
        dead.describe()
        == "null-v1 (v1) grid: 0 false calls in 0 search(es) = n/a; 1 seam failure(s) not counted"
    )


# -- the records --------------------------------------------------------------------------------


def test_a_result_whose_verdict_is_not_its_snapshot_s_is_refused(searched):
    _run, verdicts, *_ = searched
    result = verdicts.results[0]
    document = result.to_dict()
    document["verdict"]["answers"] = 3
    with pytest.raises(BenchError, match="not the verdict of its snapshot"):
        ArmResult.from_dict(document)
    # And a selection the order did not make: the verdict record's own gate.
    document = result.to_dict()
    document["verdict"]["selected"] = 100
    with pytest.raises(VerdictError, match=r"verdict\.selected: 100 is not the first"):
        ArmResult.from_dict(document)


def test_a_holdout_figure_goes_with_a_selection_and_only_with_one(searched):
    _run, verdicts, *_ = searched
    chosen = [r for r in verdicts.results if r.holdout is not None]
    empty = [r for r in verdicts.results if r.holdout is None]
    if chosen:
        with pytest.raises(BenchError, match="if and only if a trial was selected"):
            replace(chosen[0], holdout=None)
    if empty and chosen:
        with pytest.raises(BenchError, match="if and only if a trial was selected"):
            replace(empty[0], holdout=chosen[0].holdout)
    if not chosen:
        pytest.skip("no arm selected a trial on this seed; nothing to take away")


def test_the_verdicts_record_holds_every_arm_to_one_set_of_conditions(searched):
    _run, verdicts, *_ = searched
    first, second = verdicts.results
    experiment = second.snapshot.experiment
    other = replace(experiment, indicator_lookback=experiment.indicator_lookback + 1)
    with pytest.raises(BenchError, match="other conditions"):
        Verdicts(
            world_id=verdicts.world_id,
            pool_size=verdicts.pool_size,
            results=(first, replace(second, snapshot=replace(second.snapshot, experiment=other))),
        )
    with pytest.raises(BenchError, match="each arm and replicate once"):
        Verdicts(world_id=verdicts.world_id, pool_size=16, results=(first, first))
    with pytest.raises(BenchError, match="verdicts_version"):
        Verdicts.from_dict({**verdicts.to_dict(), "verdicts_version": 2})
    # No arm of a pool can have tried more rules than the pool holds, and
    # the grid arm is one deterministic search.
    with pytest.raises(BenchError, match="tried 2 distinct rule\\(s\\) from a pool of 1"):
        Verdicts(world_id=verdicts.world_id, pool_size=1, results=(first, second))
    assert second.arm is Arm.GRID
    with pytest.raises(BenchError, match="grid-3: the grid arm is one deterministic search"):
        replace(second, replicate=3)


# -- the protocol's refusals ---------------------------------------------------------------------


def test_the_oracle_preview_reads_two_windows_on_a_throw_away_copy(fresh_run):
    before = _digest(fresh_run / ARTIFACT_FILENAME)
    figures = preview_oracle(fresh_run)
    assert figures.holdout is None
    assert figures.budget == 2
    assert _listing(fresh_run) == LANDED
    assert _digest(fresh_run / ARTIFACT_FILENAME) == before
    assert figures.describe()[0].startswith("planted rule: train net sharpe")


@pytest.mark.parametrize(
    ("changes", "fragment"),
    [
        ({"seed_set": SeedSet.TEST}, "test seed: the oracle's figures are read before a search"),
        ({"kind": WorldKind.NULL_V1}, "null-v1 world: nothing is planted"),
    ],
)
def test_the_oracle_preview_is_for_planted_development_worlds_only(
    landed_world, tmp_path, changes, fragment
):
    # Refused before the store is read: no copy is made.
    artifact, _landing = landed_world
    run = land_run(tmp_path, artifact, bench_manifest(**changes))
    with pytest.raises(BenchError, match=fragment):
        preview_oracle(run)
    assert _listing(run) == LANDED


def test_a_run_directory_without_the_manifest_or_the_artifact_is_not_a_run(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    with pytest.raises(ManifestError, match="cannot read the manifest at"):
        read_manifest(run)
    (run / MANIFEST_FILENAME).write_bytes(bench_manifest().dumps().encode("utf-8"))
    assert read_manifest(run) == bench_manifest()
    with pytest.raises(BenchError, match=f"holds no {ARTIFACT_FILENAME}"):
        require_artifact(run)
    with pytest.raises(BenchError, match=f"holds no {ARTIFACT_FILENAME}"):
        run_world(run)
    assert _listing(run) == [MANIFEST_FILENAME]


def test_a_filled_pool_size_that_is_not_this_build_s_is_refused_where_the_manifest_is_read():
    manifest = bench_manifest()
    with pytest.raises(ManifestError, match="5 is filled in, and this build enumerates 16"):
        replace(manifest, search_space=replace(manifest.search_space, pool_size=5))
    filled = replace(manifest, search_space=replace(manifest.search_space, pool_size=16))
    assert filled.search_space.candidates == manifest.search_space.candidates


def test_an_interrupted_arm_ends_the_run_and_leaves_the_directory_as_landed(
    fresh_run, monkeypatch, landed_world
):
    _artifact, landing = landed_world
    # The landing is the slow part and not the point here.
    monkeypatch.setattr(bench, "verify", lambda store, manifest: landing)

    def interrupted(self, system, user):
        raise KeyboardInterrupt

    monkeypatch.setattr(bench.RandomHypothesist, "propose", interrupted)
    with pytest.raises(KeyboardInterrupt):
        run_world(fresh_run)
    assert _listing(fresh_run) == LANDED


def test_a_seam_failure_is_recorded_as_one_and_the_next_arm_still_runs(
    fresh_run, monkeypatch, landed_world, searched
):
    _artifact, landing = landed_world
    _run, _verdicts, oracle, *_ = searched
    # The landing and the oracle are the slow parts and not the point here:
    # the oracle's two readers answer with the session's real figures.
    monkeypatch.setattr(bench, "verify", lambda store, manifest: landing)
    monkeypatch.setattr(bench, "frame_for", lambda ledger, experiment, *, holdout: None)
    monkeypatch.setattr(bench, "planted_figures", lambda *args, **kwargs: oracle.planted)
    monkeypatch.setattr(bench, "pool_reference", lambda *args, **kwargs: oracle.pool)

    def dead(self, system, user):
        raise HypothesistError("the seam failed")

    monkeypatch.setattr(bench.RandomHypothesist, "propose", dead)
    verdicts, _oracle = run_world(fresh_run)
    random_, grid = verdicts.results
    assert random_.snapshot.ended is Ended.SEAM_FAILED
    assert random_.snapshot.answers == 0 and random_.verdict.selected is None
    assert grid.snapshot.ended is Ended.BUDGET_SPENT and grid.snapshot.answers == 2
    # The dead arm is not a search the calibration counts: it is named
    # beside the row, out of the rate's denominator.
    rows = calibration_table([fresh_run])
    assert [(r.arm, r.searches, r.failed) for r in rows] == [(Arm.RANDOM, 0, 1), (Arm.GRID, 1, 0)]
    assert rows[0].rate is None and "1 seam failure(s) not counted" in rows[0].describe()


# -- the oracle record's own gates ------------------------------------------------------------------


def test_the_oracle_record_refuses_derived_values_that_are_not_its_figures(searched):
    _run, _verdicts, oracle, *_ = searched
    document = oracle.to_dict()
    assert Oracle.from_dict(document) == oracle
    # The planted rule's room, written for the reader and checked on the
    # read: ``eligible`` is the gate's blockers, ``passed`` and ``margin``
    # follow, and none of the three can be written apart from its source.
    assert oracle.planted is not None
    assert oracle.planted.eligible == (not oracle.planted.blockers)
    for key, value in (
        ("passed", not oracle.planted.passed),
        ("margin", oracle.planted.margin + 1.0),
        ("eligible", not oracle.planted.eligible),
    ):
        tampered = oracle.to_dict()
        tampered["planted"][key] = value
        with pytest.raises(OracleError, match="planted: the record says eligible, margin, passed"):
            Oracle.from_dict(tampered)
    # A figure filed under another window is refused by its own name.
    swapped = oracle.to_dict()
    swapped["planted"]["train"], swapped["planted"]["validation"] = (
        swapped["planted"]["validation"],
        swapped["planted"]["train"],
    )
    with pytest.raises(OracleError, match="planted.train: a figure for validation"):
        Oracle.from_dict(swapped)
    # And the pool cannot claim more clearing the gate than reach the threshold.
    tampered = oracle.to_dict()
    tampered["pool"]["clearing"] = 16
    tampered["pool"]["threshold"] = max(tampered["pool"]["validation_sharpes"]) + 1.0
    with pytest.raises(OracleError, match="pool.clearing: 16 clear the gate, but only 0"):
        Oracle.from_dict(tampered)
    # The pool's size and best, likewise.
    tampered = oracle.to_dict()
    tampered["pool"]["best"] = (tampered["pool"]["best"] + 1) % 16
    with pytest.raises(OracleError, match="pool: the record says size 16 and best"):
        Oracle.from_dict(tampered)
    tampered = oracle.to_dict()
    tampered["pool"]["size"] = 15
    with pytest.raises(OracleError, match="pool: the record says size 15"):
        Oracle.from_dict(tampered)
    # Another build's record is refused by its version, before its keys.
    with pytest.raises(OracleError, match="oracle_version: this build reads version 1, got 2"):
        Oracle.from_dict({**document, "oracle_version": 2, "later": 1})


def test_a_holdout_figure_for_another_window_is_refused(searched):
    _run, verdicts, *_ = searched
    chosen = [r for r in verdicts.results if r.holdout is not None]
    if not chosen:
        pytest.skip("no arm selected a trial on this seed; no holdout figure to misfile")
    result = chosen[0]
    assert result.holdout is not None
    misfiled = replace(result.holdout, segment=result.snapshot.experiment.split.validation)
    with pytest.raises(BenchError, match="the holdout figure is for"):
        replace(result, holdout=misfiled)


def test_the_calibration_table_refuses_verdicts_filed_under_another_world(
    searched, landed_world, tmp_path
):
    run, *_ = searched
    artifact, _landing = landed_world
    other = land_run(tmp_path, artifact, bench_manifest(world_seed=1))
    (other / VERDICTS_FILENAME).write_bytes((run / VERDICTS_FILENAME).read_bytes())
    with pytest.raises(BenchError, match="the manifest beside it for"):
        calibration_table([other])
    # And another pool: the manifest beside the verdicts writes the pool the
    # arms drew from, or the tally is of something else.
    manifest = bench_manifest()
    wider = replace(
        manifest.search_space,
        threshold_grid={
            "funding_zscore_14": (-2.0, -1.0, 1.0, 2.0),
            "realized_vol_20": (0.002, 0.004, 0.006),
        },
        pool_size=None,
    )
    other_pool = tmp_path / "other-pool"
    other_pool.mkdir()
    (other_pool / MANIFEST_FILENAME).write_bytes(
        replace(manifest, search_space=wider).dumps().encode("utf-8")
    )
    (other_pool / VERDICTS_FILENAME).write_bytes((run / VERDICTS_FILENAME).read_bytes())
    with pytest.raises(
        BenchError, match="drew from a pool of 16, the manifest beside it writes one of 24"
    ):
        calibration_table([other_pool])


def test_the_calibration_table_reads_a_world_whose_artifact_was_pruned(searched, tmp_path):
    run, *_ = searched
    pruned = tmp_path / run.name
    pruned.mkdir()
    for name in (MANIFEST_FILENAME, VERDICTS_FILENAME):
        (pruned / name).write_bytes((run / name).read_bytes())
    assert [r.searches for r in calibration_table([pruned])] == [1, 1]
    # The verbs that measure still want the artifact.
    with pytest.raises(BenchError, match=f"holds no {ARTIFACT_FILENAME}"):
        preview_oracle(pruned)


# -- the manifest a search runs under -------------------------------------------------------------


def test_a_manifest_naming_a_model_arm_is_refused_before_anything_is_searched(
    landed_world, tmp_path
):
    artifact, _landing = landed_world
    with_model = bench_manifest()
    with_model = replace(with_model, arms=replace(with_model.arms, llm=example_manifest().arms.llm))
    run = land_run(tmp_path, artifact, with_model)
    with pytest.raises(BenchError, match="names a model arm"):
        run_world(run)
    assert _listing(run) == LANDED


def test_a_manifest_edited_after_the_landing_is_not_the_directory_s_own(fresh_run):
    # The directory is named by the manifest's digest; a manifest with
    # another budget names another directory, and both verbs refuse it.
    manifest = read_manifest(fresh_run)
    edited = replace(manifest, budget=replace(manifest.budget, max_trials=3))
    (fresh_run / MANIFEST_FILENAME).write_bytes(edited.dumps().encode("utf-8"))
    for verb in (run_world, preview_oracle):
        with pytest.raises(BenchError, match="not the manifest this directory was landed for"):
            verb(fresh_run)
    assert _listing(fresh_run) == LANDED


def test_the_holdout_figures_can_be_kept_off_the_console(searched):
    # ``search`` prints them on development seeds only (plan §4.2); the
    # records hold them either way.
    _run, verdicts, oracle, *_ = searched
    chosen = [r for r in verdicts.results if r.holdout is not None]
    if not chosen:
        pytest.skip("no arm selected a trial on this seed; no holdout figure to hide")
    shown, hidden = chosen[0].describe(), chosen[0].describe(holdout=False)
    assert "holdout net sharpe" in shown and "holdout net sharpe" not in hidden
    assert shown.startswith(hidden)
    assert oracle.planted is not None and oracle.planted.holdout is not None
    assert any("holdout" in line for line in oracle.describe())
    assert not any("holdout" in line for line in oracle.describe(holdout=False))
