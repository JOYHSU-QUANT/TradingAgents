"""The landing (plan PR 2's acceptance): the store holds the world, the world measures as the manifest says, the run directory.

``landed_world`` (``conftest``) lands the example world once through the
research package's backfill and verifies it once; the tests here read
that store and that measurement. The refusal branches are reached on
small worlds or with the measured index stubbed, so the suite pays the
feature frame over the full world once, not once per refusal.
"""

from __future__ import annotations

import shutil
from dataclasses import replace

import pytest

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
from contrib.discovery.manifest import Bars, Shares, WorldKind
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


def test_a_walk_that_does_not_reach_the_world_s_edge_is_refused(monkeypatch):
    class Daylight(SyntheticHistory):
        def get_candles(self, coin, interval, lookback, *, end):
            if interval == "1d":
                return []
            return super().get_candles(coin, interval, lookback, end=end)

    monkeypatch.setattr(landing, "SyntheticHistory", Daylight)
    with (
        ResearchStore() as store,
        pytest.raises(WorldError, match="1d candles: the walk stopped because the venue"),
    ):
        land(generate(small_manifest(count=30)), store)


# -- the world measures as the manifest says ----------------------------------------------------


def test_the_landing_measures_what_an_experiment_would(landed_world, example_world):
    _path, measured = landed_world
    manifest = example_manifest()
    bars = manifest.world.bars
    assert measured.world_id == manifest.world.id
    assert measured.bars == bars.warmup_bars + bars.count
    assert measured.daily_bars == len(example_world.daily)
    assert measured.settlements == len(example_world.funding)
    assert measured.funding_holes == 0
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


def test_a_warm_up_that_ends_before_every_feature_has_a_value_is_refused(landed_world, monkeypatch):
    path, measured = landed_world
    too_late = measured.decision_start_index + 100
    monkeypatch.setattr(landing, "first_measurable_index", lambda frame: too_late)
    with ResearchStore(path) as store, pytest.raises(WorldError) as refused:
        verify(store, example_manifest())
    assert f"first has a value at bar {too_late}" in str(refused.value)
    assert f"raise world.bars.warmup_bars to at least {too_late}" in str(refused.value)


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
    assert default_root() == default_db_path().parent / "discovery"


def test_a_landing_is_staged_and_only_a_verified_one_reaches_the_final_path(tmp_path, monkeypatch):
    manifest = small_manifest(count=30)
    history = generate(manifest)
    # Nothing has to be measured for the staging to be tested; the frame
    # over a thirty-bar world would refuse, so the check is stubbed.
    monkeypatch.setattr(landing, "verify", lambda store, m: None)
    target, fetched, _landing = land_world(tmp_path, manifest, history)
    assert target == world_dir(tmp_path, manifest)
    assert (target / MANIFEST_FILENAME).read_bytes() == manifest.dumps().encode("utf-8")
    with ResearchStore(target / ARTIFACT_FILENAME) as store:
        assert store.count_candles(manifest.world.id, "4h") == 30
    assert [p.name for p in tmp_path.iterdir()] == [target.name]  # no staging left behind
    # Landed once: the same manifest again is refused, and the directory stays.
    with pytest.raises(WorldError, match="already holds this manifest's world"):
        land_world(tmp_path, manifest, history)
    # A history that is not the manifest's world is refused before anything is written.
    with pytest.raises(WorldError, match="a run directory holds one world"):
        land_world(tmp_path, small_manifest(count=31), history)
    assert [p.name for p in tmp_path.iterdir()] == [target.name]


def test_a_landing_that_is_refused_leaves_nothing_behind(tmp_path, monkeypatch):
    manifest = small_manifest(count=30)
    history = generate(manifest)

    def refuse(store, m):
        raise WorldError("the world is not fit to measure on")

    monkeypatch.setattr(landing, "verify", refuse)
    with pytest.raises(WorldError, match="not fit to measure on"):
        land_world(tmp_path / "bench", manifest, history)
    assert list((tmp_path / "bench").iterdir()) == []
    # The same when the manifest cannot be written after a good landing.
    monkeypatch.setattr(landing, "verify", lambda store, m: None)
    monkeypatch.setattr(landing, "MANIFEST_FILENAME", "nowhere/manifest.json")
    with pytest.raises(OSError):
        land_world(tmp_path / "bench", manifest, history)
    assert list((tmp_path / "bench").iterdir()) == []
