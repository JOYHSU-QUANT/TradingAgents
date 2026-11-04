"""The landing (plan PR 2's acceptance): the store holds the world, the world measures as the manifest says, the run directory.

``landed_world`` (``conftest``) lands the example world once through the
research package's backfill and verifies it once; the tests here read
that store and that measurement. The refusal branches are reached on
small worlds, on corrupted copies of the store that are refused before
the frame is built, or with the measured index stubbed — except the tail
check, which is the one gate that has to walk every column to speak, and
pays for it once.
"""

from __future__ import annotations

import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from contrib.autoresearch.evaluator import EvaluationError
from contrib.autoresearch.fetch import FUNDING_SERIES, StopReason
from contrib.autoresearch.ledger import Ledger
from contrib.autoresearch.store import ResearchStore, default_db_path
from contrib.autoresearch.upstream import interval_to_ms
from contrib.discovery import landing
from contrib.discovery.landing import (
    ARTIFACT_FILENAME,
    MANIFEST_FILENAME,
    default_root,
    experiment_for,
    land,
    land_world,
    manifest_digest,
    split_of,
    verify,
    world_dir,
)
from contrib.discovery.manifest import Bars, SearchSpace, Shares, WorldKind
from contrib.discovery.worlds import SyntheticHistory, WorldError, generate

from .conftest import STEP_MS, example_manifest, small_manifest

# -- the store holds the world ------------------------------------------------------------------


def test_the_store_holds_exactly_the_generated_series(landed_world, example_world):
    path, _landing = landed_world
    coin = example_world.world.id
    with ResearchStore(path) as store:
        assert list(store.iter_candles(coin, "4h")) == list(example_world.bars)
        assert list(store.iter_candles(coin, "1d")) == list(example_world.daily)
        assert list(store.iter_funding(coin)) == list(example_world.funding)
        # The walk recorded where it reached, as it does for the venue.
        for series, stopped in (
            ("4h", StopReason.REACHED_SINCE),
            ("1d", StopReason.REACHED_SINCE),
            (FUNDING_SERIES, StopReason.REACHED_END),
        ):
            state = store.series_state(coin=coin, series=series)
            assert state is not None and state["stopped"] == stopped.name


def test_the_walks_land_every_row_once():
    with ResearchStore() as store:
        fetched = land(generate(small_manifest(count=30)), store)
    assert [f.rows_added for f in fetched] == [30, 5, 30 * 4 + 1]


def test_a_walk_that_does_not_reach_the_world_s_edge_or_loses_rows_is_refused(monkeypatch):
    class Daylight(SyntheticHistory):
        def get_candles(self, coin, interval, lookback, *, end):
            if interval == "1d":
                return []
            return super().get_candles(coin, interval, lookback, end=end)

    class Short(SyntheticHistory):
        def get_candles(self, coin, interval, lookback, *, end):
            served = super().get_candles(coin, interval, lookback, end=end)
            return served[:-1] if interval == "1d" else served

    history = generate(small_manifest(count=30))
    monkeypatch.setattr(landing, "SyntheticHistory", Daylight)
    with (
        ResearchStore() as store,
        pytest.raises(WorldError, match="1d candles: the walk stopped because the venue"),
    ):
        land(history, store)
    # A walk that ends where it meant to but dropped a row is not the world either.
    monkeypatch.setattr(landing, "SyntheticHistory", Short)
    with ResearchStore() as store, pytest.raises(WorldError, match="holds 4 row\\(s\\) after"):
        land(history, store)


# -- the world measures as the manifest says ----------------------------------------------------


def test_the_landing_measures_what_an_experiment_would(landed_world, example_world):
    _path, measured = landed_world
    manifest = example_manifest()
    bars = manifest.world.bars
    assert measured.world_id == manifest.world.id
    assert measured.bars == bars.warmup_bars + bars.count
    assert measured.daily_bars == len(example_world.daily)
    assert measured.settlements == len(example_world.funding)
    # Every feature of the vocabulary has a value before the decision bars
    # begin, with room to spare (the warm-up was sized for that).
    assert measured.first_measurable_index <= measured.decision_start_index == bars.warmup_bars
    assert (
        measured.first_measurable_ms
        == example_world.bars[measured.first_measurable_index].open_time
    )
    # The three windows tile exactly the decision bars, by the shares.
    assert measured.split == split_of(manifest.world, manifest.split)
    assert measured.segment_bars == (240, 80, 80)
    assert measured.split.train.start_ms == example_world.bars[bars.warmup_bars].open_time
    assert measured.split.holdout.end_ms == example_world.bars[-1].open_time + STEP_MS
    lines = measured.describe()
    assert lines[0].startswith(f"world {manifest.world.id}: {measured.bars} 4h bars")
    assert "every feature has a value from bar" in lines[1]
    assert lines[-1].startswith("split (4h): holdout:") and lines[-1].endswith("(80 bars)")


def test_the_split_is_the_shares_over_the_decision_bars_alone():
    world = small_manifest(warmup=10, count=100).world
    cut = split_of(world, Shares())
    start = world.origin_ms + 10 * STEP_MS
    assert (cut.train.start_ms, cut.holdout.end_ms) == (start, start + 100 * STEP_MS)
    assert [(s.end_ms - s.start_ms) // STEP_MS for s in cut.ordered] == [60, 20, 20]
    assert cut.interval == "4h"


def test_an_arm_s_experiment_binds_the_manifest_s_conditions(landed_world, tmp_path):
    path, measured = landed_world
    manifest = example_manifest()
    shutil.copy(path, tmp_path / "arm.sqlite")
    with ResearchStore(tmp_path / "arm.sqlite") as store:
        ledger = Ledger(store)
        experiment = ledger.create_experiment(experiment_for(manifest, "arm"))
        assert experiment.coin == manifest.world.id
        assert experiment.split == measured.split
        assert experiment.costs == manifest.costs
        assert experiment.penalty == manifest.penalty
        assert experiment.indicator_lookback == manifest.indicator_lookback
        # The first experiment on the arm's coin pinned the manifest's holdout.
        assert ledger.holdout_pin(manifest.world.id) == measured.split.holdout.start_ms
        assert ledger.penalty_pin(manifest.world.id) == manifest.penalty


# -- refusals -----------------------------------------------------------------------------------


def _damaged(landed: Path, tmp_path: Path, sql: str, *params) -> Path:
    """A copy of the landed store with one statement run against it."""
    copy = tmp_path / "damaged.sqlite"
    shutil.copy(landed, copy)
    with ResearchStore(copy) as store:
        store.conn.execute(sql, params)
    return copy


def test_a_store_whose_history_is_not_a_grid_is_refused(landed_world, example_world, tmp_path):
    path, _measured = landed_world
    coin = example_world.world.id
    manifest = example_manifest()
    # A missing bar is refused by the count before any scan; a MISSHAPEN
    # one (right count, wrong close) is the research package's own scan
    # refusing, before any frame.
    missing_bar = _damaged(
        path,
        tmp_path,
        "DELETE FROM candles WHERE coin = ? AND interval = '4h' AND open_time = ?",
        coin,
        example_world.bars[700].open_time,
    )
    with ResearchStore(missing_bar) as store, pytest.raises(WorldError, match="not that world"):
        verify(store, manifest)
    misshapen = _damaged(
        path,
        tmp_path,
        "UPDATE candles SET close_time = open_time + 1000"
        " WHERE coin = ? AND interval = '4h' AND open_time = ?",
        coin,
        example_world.bars[700].open_time,
    )
    with ResearchStore(misshapen) as store, pytest.raises(EvaluationError, match="not a grid"):
        verify(store, manifest)
    # A missing settlement: the research package counts it; this world has
    # none to count, so it is refused here.
    missing_settlement = _damaged(
        path,
        tmp_path,
        "DELETE FROM funding WHERE coin = ? AND time = ?",
        coin,
        example_world.funding[3000].time,
    )
    with ResearchStore(missing_settlement) as store, pytest.raises(WorldError, match="1 hole"):
        verify(store, manifest)


def test_a_store_whose_tail_lacks_a_feature_is_refused(
    landed_world, example_world, tmp_path, monkeypatch
):
    # The last two daily bars gone: the daily features go stale on the final
    # bars, which is the holdout, and ``require_measurable_tail`` says so.
    # (The index is stubbed so the one column walk paid here is the tail's.)
    path, measured = landed_world
    stale = _damaged(
        path,
        tmp_path,
        "DELETE FROM candles WHERE coin = ? AND interval = '1d' AND open_time >= ?",
        example_world.world.id,
        example_world.daily[-2].open_time,
    )
    monkeypatch.setattr(
        landing, "first_measurable_index", lambda frame: measured.first_measurable_index
    )
    with ResearchStore(stale) as store, pytest.raises(EvaluationError, match="1d bars"):
        verify(store, example_manifest())


def test_the_warm_up_must_end_at_or_after_the_bar_every_feature_first_has_a_value(
    landed_world, monkeypatch
):
    path, measured = landed_world
    start = measured.decision_start_index
    monkeypatch.setattr(landing, "require_measurable_tail", lambda frame: None)
    # Exactly at the first decision bar is a legal world; one bar later is not.
    monkeypatch.setattr(landing, "first_measurable_index", lambda frame: start)
    with ResearchStore(path) as store:
        assert verify(store, example_manifest()).first_measurable_index == start
    monkeypatch.setattr(landing, "first_measurable_index", lambda frame: start + 1)
    with ResearchStore(path) as store, pytest.raises(WorldError) as refused:
        verify(store, example_manifest())
    assert f"first has a value at bar {start + 1}" in str(refused.value)
    assert f"raise world.bars.warmup_bars to at least {start + 1}" in str(refused.value)


def test_a_store_that_is_not_the_manifest_s_world_is_refused(landed_world):
    path, _measured = landed_world
    manifest = example_manifest()
    bars = manifest.world.bars
    longer = replace(
        manifest, world=replace(manifest.world, bars=Bars("4h", bars.count + 1, bars.warmup_bars))
    )
    moved = replace(
        manifest,
        world=replace(manifest.world, origin_ms=manifest.world.origin_ms + interval_to_ms("1d")),
    )
    with ResearchStore(path) as store:
        for other in (longer, moved):
            with pytest.raises(WorldError, match="not that world's artifact"):
                verify(store, other)


# -- the run directory ----------------------------------------------------------------------------


def test_the_run_directory_is_named_by_the_id_and_the_manifest_s_digest(tmp_path):
    manifest = example_manifest()
    twin = example_manifest(kind=WorldKind.NULL_V1)
    here, there = world_dir(tmp_path, manifest), world_dir(tmp_path, twin)
    assert here.parent == there.parent == tmp_path
    assert here.name == f"{manifest.world.id}-{manifest_digest(manifest)}"
    assert here.name.startswith(twin.world.id) and there.name.startswith(twin.world.id)
    assert here != there  # the null twin shares the id, not the directory
    assert len(manifest_digest(manifest)) == 8
    # The pool count a later step fills in does not move the digest.
    space = manifest.search_space
    filled = replace(
        manifest,
        search_space=SearchSpace(
            templates=space.templates,
            threshold_grid=dict(space.threshold_grid),
            candidate_order=space.candidate_order,
            pool_size=16,
        ),
    )
    assert manifest_digest(filled) == manifest_digest(manifest)
    assert default_root() == default_db_path().parent / "discovery"


def test_a_landing_is_staged_and_only_a_verified_one_reaches_the_final_path(tmp_path, monkeypatch):
    history = generate(small_manifest(count=30))
    manifest = history.manifest
    # Nothing has to be measured for the staging to be tested; the frame
    # over a thirty-bar world would refuse, so the check is stubbed.
    monkeypatch.setattr(landing, "verify", lambda store, m: None)
    target, fetched, _landing = land_world(tmp_path, history)
    assert target == world_dir(tmp_path, manifest)
    assert (target / MANIFEST_FILENAME).read_bytes() == manifest.dumps().encode("utf-8")
    with ResearchStore(target / ARTIFACT_FILENAME) as store:
        assert store.count_candles(manifest.world.id, "4h") == 30
    assert sorted(p.name for p in target.iterdir()) == sorted(
        [ARTIFACT_FILENAME, MANIFEST_FILENAME]
    )
    assert [p.name for p in tmp_path.iterdir()] == [target.name]  # no staging left behind
    # Landed once: the same manifest again is refused, and the directory stays.
    with pytest.raises(WorldError, match="already holds this manifest's world"):
        land_world(tmp_path, history)
    assert [p.name for p in tmp_path.iterdir()] == [target.name]


def test_a_landing_that_is_refused_leaves_nothing_behind(tmp_path, monkeypatch):
    history = generate(small_manifest(count=30))

    def refuse(store, m):
        raise WorldError("the world is not fit to measure on")

    monkeypatch.setattr(landing, "verify", refuse)
    with pytest.raises(WorldError, match="not fit to measure on"):
        land_world(tmp_path / "bench", history)
    assert list((tmp_path / "bench").iterdir()) == []
    # The same when the manifest cannot be written after a good landing.
    monkeypatch.setattr(landing, "verify", lambda store, m: None)
    monkeypatch.setattr(landing, "MANIFEST_FILENAME", "nowhere/manifest.json")
    with pytest.raises(OSError):
        land_world(tmp_path / "bench", history)
    assert list((tmp_path / "bench").iterdir()) == []
