"""The manifest round-trips exactly, refuses what the benchmark cannot run, and leaks nothing through its id."""

from __future__ import annotations

import inspect
import json

import pytest

from contrib.autoresearch.dsl import MAX_HOLD_BARS
from contrib.autoresearch.store import canonical_coin
from contrib.discovery.manifest import (
    MANIFEST_VERSION,
    Manifest,
    ManifestError,
    SeedSet,
    WorldKind,
    opaque_world_id,
)

from .conftest import example_manifest

# -- round trips ----------------------------------------------------------------


@pytest.mark.parametrize("kind", list(WorldKind), ids=lambda kind: kind.value)
def test_a_manifest_comes_back_equal_from_its_record_and_from_its_text(kind):
    manifest = example_manifest(kind=kind)
    assert Manifest.from_dict(manifest.to_dict()) == manifest
    assert Manifest.loads(manifest.dumps()) == manifest
    # The text is the record, and the record is plain JSON with the version on it.
    assert json.loads(manifest.dumps()) == manifest.to_dict()
    assert manifest.to_dict()["manifest_version"] == MANIFEST_VERSION


def test_a_null_world_writes_no_rule_and_a_conditional_one_writes_the_parsed_rule():
    null = example_manifest(kind=WorldKind.NULL_V1).to_dict()
    conditional = example_manifest().to_dict()
    assert null["world"]["planted"] is None
    assert conditional["world"]["planted"]["spec"]["exit"] == {"max_bars": 12}
    # The two worlds share nothing a prompt could see: the same seed gives
    # the same id whatever is planted.
    assert null["world"]["id"] == conditional["world"]["id"]


def test_a_manifest_without_a_model_arm_is_a_complete_run_of_the_other_two():
    record = example_manifest().to_dict()
    record["arms"]["llm"] = None
    manifest = Manifest.from_dict(record)
    assert manifest.arms.llm is None
    assert Manifest.from_dict(manifest.to_dict()) == manifest


# -- refusals ------------------------------------------------------------------------

_DROP = object()


def _edited(**changes):
    """The example manifest's record with dotted paths replaced (or dropped)."""
    record = example_manifest().to_dict()
    for dotted, value in changes.items():
        *parents, last = dotted.split(".")
        body = record
        for key in parents:
            body = body[key]
        if value is _DROP:
            del body[last]
        else:
            body[last] = value
    return record


@pytest.mark.parametrize(
    ("changes", "fragment"),
    [
        ({"manifest_version": 2}, "manifest_version"),
        ({"world.kind": "null-v1"}, "null-v1 world plants nothing"),
        ({"world.planted": None}, "conditional-v1 world carries a planted rule"),
        ({"world.id": "SYN-NULL0001"}, "world.id"),
        ({"world.id": "syn-eb3bc030"}, "world.id"),
        # Test seed 7's id on a dev-seed-0 manifest: well-formed, and another
        # world's. The invariant is the record's, not the author's.
        ({"world.id": "SYN-4778249A"}, "not the id of dev seed 0"),
        ({"world.seed_set": "test"}, "not the id of test seed 0"),
        ({"world.world_seed": 1}, "not the id of dev seed 1"),
        ({"world.origin_ms": 1}, "not a UTC midnight"),
        ({"world.funding.sigma": 0}, "world.funding.sigma"),
        ({"world.price.vol": 0}, "world.price.vol"),
        ({"world.bars.interval": "1h"}, "world.bars.interval"),
        ({"world.planted.difficulty": "hard"}, "world.planted.difficulty"),
        ({"split.train_share": 0.7, "split.validation_share": 0.3}, "room for a holdout"),
        ({"budget.max_trials": 0}, "budget.max_trials"),
        ({"budget.oracle_margin": -0.1}, "budget.oracle_margin"),
        ({"indicator_lookback": 0}, "indicator_lookback"),
        ({"scoring.recovery.theta_behavior": 1.5}, "theta_behavior"),
        ({"world.seed_set": "prod"}, "world.seed_set"),
        ({"world.bars.count": "400"}, "world.bars.count"),
        ({"search_space.threshold_grid.realized_vol_20": [0.002, ""]}, "realized_vol_20"),
        ({"arms.random.search_seeds": []}, "arms.random.search_seeds"),
        ({"arms.random.search_seeds": [0, 0]}, "arms.random.search_seeds"),
        ({"arms.llm.world_subset": ["BTC"]}, "arms.llm.world_subset"),
        ({"arms.llm.replicates": 0}, "arms.llm.replicates"),
        ({"search_space.candidate_order": ""}, "search_space.candidate_order"),
        ({"search_space.threshold_grid.realized_vol_20": [0.002, 0.002]}, "each value once"),
        ({"search_space.threshold_grid.realized_vol_20": []}, "at least one value"),
        ({"search_space.threshold_grid.rsi_14": [30]}, "a grid for every feature"),
        ({"search_space.threshold_grid.realized_vol_20": _DROP}, "a grid for every feature"),
        ({"search_space.templates": []}, "search_space.templates"),
        ({"null_definition_version": ""}, "null_definition_version"),
        ({"costs": _DROP}, "manifest: expected exactly the keys"),
        ({"extra": 1}, "manifest: expected exactly the keys"),
        ({"world.extra": 1}, "world: expected exactly the keys"),
    ],
)
def test_a_record_the_benchmark_cannot_run_is_refused_naming_the_path(changes, fragment):
    with pytest.raises(ManifestError, match=fragment):
        Manifest.from_dict(_edited(**changes))


def test_a_template_feature_outside_the_vocabulary_is_refused():
    record = _edited()
    record["search_space"]["templates"][0]["entry"] = ["momentum_9", ">"]
    with pytest.raises(ManifestError, match="momentum_9"):
        Manifest.from_dict(record)


def test_a_template_hold_over_the_language_s_bound_is_refused():
    record = _edited()
    record["search_space"]["templates"][0]["exit"]["max_bars"] = MAX_HOLD_BARS + 1
    with pytest.raises(ManifestError, match=f"{MAX_HOLD_BARS}-bar hold"):
        Manifest.from_dict(record)


def test_a_planted_rule_without_an_exit_is_refused():
    # Plan §1 #11: the evaluator closes only through an exit rule, max_bars or
    # a reversal, so a rule planted without one would hold forever.
    record = _edited()
    del record["world"]["planted"]["spec"]["exit"]
    with pytest.raises(ManifestError, match="needs an exit"):
        Manifest.from_dict(record)


def test_a_two_sided_planted_rule_needs_an_exit_on_both_sides():
    record = _edited()
    spec = record["world"]["planted"]["spec"]
    del spec["exit"]
    spec["entry"]["short"] = [{"left": "funding_zscore_14", "op": ">", "right": 1.0}]
    spec["exit"] = {"long": [{"left": "funding_zscore_14", "op": ">", "right": 0}]}
    with pytest.raises(ManifestError, match=r"every side it enters \(short has none\)"):
        Manifest.from_dict(record)


def test_a_manifest_is_hashable_as_a_frozen_value_should_be():
    manifest = example_manifest()
    assert hash(manifest) == hash(Manifest.from_dict(manifest.to_dict()))


def test_a_planted_rule_the_parser_refuses_is_refused_as_the_parser_says():
    record = _edited()
    record["world"]["planted"]["spec"]["entry"]["long"][0]["left"] = "momentum_9"
    with pytest.raises(ManifestError, match="world.planted.spec: .*momentum_9"):
        Manifest.from_dict(record)


def test_text_that_is_not_json_is_refused_by_name():
    with pytest.raises(ManifestError, match="not valid JSON"):
        Manifest.loads("{")


# -- the opaque id -----------------------------------------------------------------


def test_the_id_is_a_function_of_the_seed_set_and_the_seed_alone():
    assert opaque_world_id(SeedSet.DEV, 0) == opaque_world_id(SeedSet.DEV, 0)
    assert opaque_world_id(SeedSet.DEV, 0) != opaque_world_id(SeedSet.DEV, 1)
    assert opaque_world_id(SeedSet.DEV, 0) != opaque_world_id(SeedSet.TEST, 0)
    # By construction, not by discipline: the kind of world is not an
    # argument, so no id can carry it (plan §1 #7).
    assert "kind" not in inspect.signature(opaque_world_id).parameters


@pytest.mark.parametrize("seed", range(50))
def test_the_id_is_spelled_as_the_store_files_it_and_spells_no_word(seed):
    world_id = opaque_world_id(SeedSet.TEST, seed)
    assert canonical_coin(world_id) == world_id
    assert world_id.startswith("SYN-") and len(world_id) == 12
    assert "NULL" not in world_id and "COND" not in world_id


def test_a_seed_that_is_not_a_whole_number_is_refused():
    with pytest.raises(ManifestError, match="world_seed"):
        opaque_world_id(SeedSet.DEV, -1)
