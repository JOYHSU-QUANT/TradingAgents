"""The manifest round-trips exactly, refuses what the benchmark cannot run, and leaks nothing through its id."""

from __future__ import annotations

import inspect
import json
import re
from dataclasses import fields, replace

import pytest

from contrib.autoresearch.dsl import MAX_HOLD_BARS, MarketRegime, parse_spec
from contrib.autoresearch.store import canonical_coin
from contrib.discovery.landing import conditions_digest, manifest_digest
from contrib.discovery.manifest import (
    MANIFEST_VERSION,
    POOL_SIZING,
    Arms,
    Candidate,
    Manifest,
    ManifestError,
    SeedSet,
    TrainPickArm,
    WorldKind,
    _arm_keys,
    opaque_world_id,
)
from contrib.discovery.records import Reader

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
    assert conditional["world"]["planted"]["spec"]["exit"] == {"max_bars": 6}
    # The two worlds share nothing a prompt could see: the same seed gives
    # the same id whatever is planted.
    assert null["world"]["id"] == conditional["world"]["id"]


def test_the_identity_text_is_the_canonical_text_with_the_two_fill_ins_set_aside():
    manifest = example_manifest()
    # Unfilled, the identity IS the file; filled, it is still the unfilled file.
    assert manifest.identity() == manifest.dumps()
    filled = replace(
        manifest,
        search_space=replace(manifest.search_space, pool_size=16),
        artifact_sha256="0" * 64,
    )
    assert Manifest.loads(filled.dumps()) == filled
    assert filled.identity() == manifest.dumps()
    read = Manifest.loads(filled.identity())
    assert read.search_space.pool_size is None and read.artifact_sha256 is None
    # Any other field is part of the identity.
    assert replace(manifest, indicator_lookback=201).identity() != manifest.identity()


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


# -- the tool arm (tools plan T4) -------------------------------------------------------------------

TOOLS = {
    "provider": "openai",
    "model": "gpt",
    "temperature": 0.0,
    "replicates": 2,
    "world_subset": ["SYN-EB3BC030"],
    "lookups": 5,
    "backtests": 2,
    "pack_version": 1,
}


def _with_tools(**changes):
    """The example manifest's record naming the tool arm in the model arm's place."""
    return _edited(**{"arms.llm": None, "arms.llm_tools": {**TOOLS, **changes}})


def test_a_manifest_may_name_the_tool_arm_in_the_model_arm_s_place():
    manifest = Manifest.from_dict(_with_tools())
    tools = manifest.arms.llm_tools
    assert manifest.arms.llm is None
    assert (tools.replicates, tools.lookups, tools.backtests, tools.pack_version) == (2, 5, 2, 1)
    assert tools.world_subset == ("SYN-EB3BC030",)
    assert Manifest.loads(manifest.dumps()) == manifest
    assert manifest.to_dict()["arms"]["llm_tools"] == TOOLS
    # The limits and the pack are conditions of the run: another of either
    # is another directory, and another row of a table.
    for changed in ({"lookups": 6}, {"backtests": 1}, {"pack_version": 2}):
        other = Manifest.from_dict(_with_tools(**changed))
        assert other.identity() != manifest.identity()
        assert other.conditions() != manifest.conditions()
    # No backtest at all is a limit the arm may have, and so is every call a backtest.
    assert Manifest.from_dict(_with_tools(backtests=0)).arms.llm_tools.backtests == 0
    assert Manifest.from_dict(_with_tools(backtests=5)).arms.llm_tools.backtests == 5


@pytest.mark.parametrize(
    ("changes", "fragment"),
    [
        ({"lookups": 0}, "arms.llm_tools.lookups"),
        ({"lookups": True}, "arms.llm_tools.lookups"),
        ({"lookups": 5.0}, "arms.llm_tools.lookups"),
        ({"backtests": -1}, "arms.llm_tools.backtests"),
        ({"backtests": 6}, "arms.llm_tools.backtests: 6 of 5 call"),
        ({"pack_version": 0}, "arms.llm_tools.pack_version"),
        ({"pack_version": "1"}, "arms.llm_tools.pack_version"),
        ({"replicates": 0}, "arms.llm_tools.replicates"),
        ({"provider": ""}, "arms.llm_tools.provider"),
        ({"temperature": -1}, "arms.llm_tools.temperature"),
        ({"world_subset": ["BTC"]}, "arms.llm_tools.world_subset"),
        (
            {"world_subset": ["SYN-00000000"]},
            "arms.llm_tools.world_subset: SYN-EB3BC030 is not in it",
        ),
        ({"extra": 1}, "arms.llm_tools: expected exactly the keys"),
    ],
)
def test_a_tool_arm_the_benchmark_cannot_run_is_refused_naming_the_path(changes, fragment):
    with pytest.raises(ManifestError, match=fragment):
        Manifest.from_dict(_with_tools(**changes))


def test_a_manifest_names_one_model_arm_at_most():
    record = _with_tools()
    record["arms"]["llm"] = example_manifest().to_dict()["arms"]["llm"]
    with pytest.raises(ManifestError, match="names one model arm, llm or llm_tools"):
        Manifest.from_dict(record)


def test_the_arms_hold_each_arm_to_its_own_record_and_write_no_arm_a_shape_has_no_place_for():
    arms = example_manifest().arms
    tools = Manifest.from_dict(_with_tools()).arms.llm_tools
    # The model arm's record is not the tool arm's, though it has its five fields.
    with pytest.raises(ManifestError, match="arms.llm_tools: expected an LlmToolsArm or None"):
        replace(arms, llm=None, llm_tools=arms.llm)
    with pytest.raises(ManifestError, match="arms.llm: expected an LlmArm or None"):
        replace(arms, llm=tools)
    # Written in the shape of a version before the tool arm, an arm that is
    # there is refused — not left out without a word.
    with_tools = replace(arms, llm=None, llm_tools=tools)
    assert sorted(with_tools.to_dict(version=4)) == ["llm", "llm_tools", "random"]
    with pytest.raises(ManifestError, match="before version 4 have no tool arm to write"):
        with_tools.to_dict(version=3)
    assert sorted(arms.to_dict(version=3)) == ["llm", "random"]
    # The same of the arm version 5 added, at either version before it.
    picking = replace(arms, train_pick=TrainPickArm(search_seeds=(0,), backtests=2))
    assert sorted(picking.to_dict(version=5)) == ["llm", "llm_tools", "random", "train_pick"]
    for version in (3, 4):
        with pytest.raises(
            ManifestError, match="arms.train_pick: .* before version 5 have no train-pick arm"
        ):
            picking.to_dict(version=version)
    assert sorted(arms.to_dict(version=4)) == ["llm", "llm_tools", "random"]
    with pytest.raises(ManifestError, match="arms.train_pick: expected a TrainPickArm or None"):
        replace(arms, train_pick=arms.random)


# -- the pick by training score (tools plan T5) -----------------------------------------------------

# Seeds not in ascending order: a replicate is numbered by its seed and
# the arm runs them as listed, so the order written is the order kept.
PICK = {"search_seeds": [3, 0], "backtests": 2}


def _with_pick(**changes):
    """The example manifest's record naming the arm that picks by training score."""
    return _edited(**{"arms.train_pick": {**PICK, **changes}})


def test_a_manifest_may_name_the_arm_that_picks_by_training_score():
    manifest = Manifest.from_dict(_with_pick())
    pick = manifest.arms.train_pick
    assert (pick.search_seeds, pick.backtests) == ((3, 0), 2)
    assert Manifest.loads(manifest.dumps()) == manifest
    assert manifest.to_dict()["arms"]["train_pick"] == PICK
    # It stands beside a model arm or without one, and is a condition of
    # the run either way: its seeds and the number it scores a round are in
    # the identity, so another of either is another directory and another row.
    assert manifest.arms.llm is not None
    alone = Manifest.from_dict(_edited(**{"arms.llm": None, "arms.train_pick": PICK})).arms
    assert (alone.llm, alone.llm_tools, alone.train_pick) == (None, None, pick)
    unnamed = example_manifest()
    assert unnamed.arms.train_pick is None
    for changed in ({}, {"search_seeds": [0]}, {"backtests": 3}):
        other = Manifest.from_dict(_with_pick(**changed))
        assert (other == manifest) == (not changed)
        against = manifest if changed else unnamed
        assert other.identity() != against.identity()
        assert other.conditions() != against.conditions()
    # One rule a round is a pick among one: allowed, and the Random arm on another stream.
    assert Manifest.from_dict(_with_pick(backtests=1)).arms.train_pick.backtests == 1


@pytest.mark.parametrize(
    ("changes", "fragment"),
    [
        ({"search_seeds": []}, "arms.train_pick.search_seeds"),
        ({"search_seeds": [1, 1]}, "arms.train_pick.search_seeds: each value once"),
        ({"search_seeds": [-1]}, r"arms.train_pick.search_seeds\[0\]"),
        ({"search_seeds": [2**63]}, "a search seed is at most"),
        ({"backtests": 0}, "arms.train_pick.backtests"),
        ({"backtests": True}, "arms.train_pick.backtests"),
        ({"backtests": 2.0}, "arms.train_pick.backtests"),
        ({"extra": 1}, "arms.train_pick: expected exactly the keys"),
    ],
)
def test_a_pick_arm_the_benchmark_cannot_run_is_refused_naming_the_path(changes, fragment):
    with pytest.raises(ManifestError, match=fragment):
        Manifest.from_dict(_with_pick(**changes))


def test_beside_the_tool_arm_the_pick_scores_as_many_rules_a_round_as_that_arm_may():
    # The pick is that arm's control (tools plan §3.6): one manifest naming
    # both with two numbers would set a model allowed two scored backtests
    # a round beside a pick that had three.
    def both(backtests: int) -> dict:
        record = _with_tools()
        record["arms"]["train_pick"] = {**PICK, "backtests": backtests}
        return record

    manifest = Manifest.from_dict(both(TOOLS["backtests"]))
    assert manifest.arms.train_pick.backtests == manifest.arms.llm_tools.backtests == 2
    for other in (1, 3):
        with pytest.raises(
            ManifestError,
            match=rf"arms.train_pick.backtests: {other} a round, beside a tool arm allowed 2",
        ):
            Manifest.from_dict(both(other))
    # Beside a tool arm allowed one, it is still the two numbers that are
    # told: the sentence below is for a tool arm allowed none, and no other.
    one = _with_tools(backtests=1)
    one["arms"]["train_pick"] = {**PICK, "backtests": 2}
    with pytest.raises(
        ManifestError,
        match=r"arms.train_pick.backtests: 2 a round, beside a tool arm allowed 1",
    ):
        Manifest.from_dict(one)
    # A tool arm allowed no scored backtest has no number the pick could
    # match (it scores at least one), and the refusal says that, not two numbers.
    unmatched = _with_tools(backtests=0)
    unmatched["arms"]["train_pick"] = {**PICK, "backtests": 1}
    with pytest.raises(
        ManifestError, match=r"arms.train_pick: the tool arm beside it is allowed no scored backtest"
    ):
        Manifest.from_dict(unmatched)
    # The blind model arm has no such number, and holds the pick to none.
    assert Manifest.from_dict(_with_pick(backtests=3)).arms.llm is not None


def test_every_arm_a_manifest_names_has_a_key_at_this_build_s_version():
    # An arm added to ``Arms`` and not to the table of the versions
    # that write it would be left out of every manifest written, with
    # no refusal: the keys written at this version are the arms there are.
    assert set(_arm_keys(MANIFEST_VERSION)) == {field.name for field in fields(Arms)}
    assert _arm_keys(3) == ("random", "llm")
    assert _arm_keys(4) == ("random", "llm", "llm_tools")


# -- the versions before an arm was added, still read -----------------------------------------------


def _version_3(manifest: Manifest) -> dict:
    """``manifest``'s record as a version-3 build wrote it: that version, and no later arm's key."""
    record = _version_4(manifest)
    record["manifest_version"] = 3
    del record["arms"]["llm_tools"]
    return record


def _version_4(manifest: Manifest) -> dict:
    """``manifest``'s record as a version-4 build wrote it: that version, and no ``train_pick`` key."""
    record = manifest.to_dict()
    record["manifest_version"] = 4
    del record["arms"]["train_pick"]
    return record


def test_a_version_3_manifest_is_read_and_written_back_as_version_3():
    current = example_manifest()
    record = _version_3(current)
    old = Manifest.from_dict(record)
    assert (old.manifest_version, old.arms.llm_tools) == (3, None)
    # Key for key what was read: no key of a later version is written in.
    assert old.to_dict() == record
    assert Manifest.loads(old.dumps()) == old and old.dumps() == Manifest.loads(old.dumps()).dumps()
    # The version is the one difference from the same manifest written now,
    # and it is a difference: the two name two directories.
    assert old != current and replace(old, manifest_version=MANIFEST_VERSION) == current
    assert old.identity() != current.identity()
    # ... and two rows of a table: the version is in the conditions too.
    assert old.conditions() != current.conditions()
    # Each version is read by its own key set, neither by the other's.
    with pytest.raises(ManifestError, match="arms: expected exactly the keys"):
        Manifest.from_dict({**record, "arms": {**record["arms"], "llm_tools": None}})
    # And a version with no tool arm cannot be made to name one.
    with pytest.raises(ManifestError, match="a version-3 manifest names no tool arm"):
        replace(Manifest.from_dict(_with_tools()), manifest_version=3)
    for version in (2, 6, True, 4.0):
        with pytest.raises(ManifestError, match="this build reads versions 3, 4, 5, got"):
            replace(current, manifest_version=version)


def test_a_version_4_manifest_is_read_and_written_back_as_version_4():
    # Version 4 as version 3 above, one arm later: it has the tool arm's
    # key and not the pick's, and names a tool arm and no pick.
    current = Manifest.from_dict(_with_tools())
    record = _version_4(current)
    old = Manifest.from_dict(record)
    assert (old.manifest_version, old.arms.train_pick) == (4, None)
    assert old.arms.llm_tools == current.arms.llm_tools
    assert old.to_dict() == record
    assert Manifest.loads(old.dumps()) == old and old.dumps() == Manifest.loads(old.dumps()).dumps()
    assert old != current and replace(old, manifest_version=MANIFEST_VERSION) == current
    assert old.identity() != current.identity()
    assert old.conditions() != current.conditions()
    with pytest.raises(ManifestError, match="arms: expected exactly the keys"):
        Manifest.from_dict({**record, "arms": {**record["arms"], "train_pick": None}})
    with pytest.raises(ManifestError, match="arms: expected exactly the keys"):
        Manifest.from_dict(_version_3(example_manifest()) | {"manifest_version": 4})
    for version in (3, 4):
        with pytest.raises(
            ManifestError, match=f"a version-{version} manifest names no train-pick arm"
        ):
            replace(Manifest.from_dict(_with_pick()), manifest_version=version)


@pytest.mark.parametrize(
    ("record", "identity", "conditions"),
    [
        (lambda: example_manifest().to_dict(), "1790869d", "df4b254d"),
        (lambda: example_manifest(kind=WorldKind.NULL_V1).to_dict(), "e433a673", "1a57ec5c"),
        (lambda: _with_tools(), "9fdaa1c0", "b614c29c"),
    ],
    ids=["conditional-v1", "null-v1", "tool arm"],
)
def test_a_version_4_manifest_names_the_directory_it_named_before_the_pick(
    record, identity, conditions
):
    # The digests the build BEFORE the pick by training score gave the
    # example manifest and the one naming the tool arm (develop at
    # 9480e1b5, measured 2026-11-24, where version 4 was the build's own).
    # As for version 3 below: they move if the example manifest is edited,
    # and for no other reason.
    old = Manifest.from_dict(_version_4(Manifest.from_dict(record())))
    assert (manifest_digest(old), conditions_digest(old)) == (identity, conditions)


def test_a_record_s_version_is_one_the_build_reads_and_is_read_by_type():
    # The reader two-version records share: a version is an ``int`` among
    # those read, and ``True`` and ``4.0`` — equal to 1 and to 4 — are not.
    read = Reader(ManifestError, "manifest")
    assert read.one_version_of({"v": 3, "later": 1}, "v", (3, 4)) == 3
    assert read.one_version_of({"v": 4}, "v", (3, 4)) == 4
    for payload, versions in (
        ({"v": 4.0}, (3, 4)),
        ({"v": True}, (1, 2)),
        ({"v": "3"}, (3, 4)),
        ({"v": 5}, (3, 4)),
        ({}, (3, 4)),
    ):
        with pytest.raises(ManifestError, match="v: this build reads versions"):
            read.one_version_of(payload, "v", versions)
    with pytest.raises(ManifestError, match="manifest: expected an object"):
        read.one_version_of([3], "v", (3, 4))


@pytest.mark.parametrize(
    ("kind", "identity", "conditions"),
    [
        (WorldKind.CONDITIONAL_V1, "c533fb4c", "3d22a4f5"),
        (WorldKind.NULL_V1, "aeeace77", "b5b7801d"),
    ],
    ids=lambda value: getattr(value, "value", value),
)
def test_a_version_3_manifest_names_the_directory_it_named_before_the_tool_arm(
    kind, identity, conditions
):
    # The two digests the build BEFORE the tool arm gave the example
    # manifest (develop at f2039c6b, measured 2026-11-19, where version 3
    # was the only one). A run directory is named by the first and its
    # records carry it, and a table's rows are keyed by the second: the
    # worlds searched under version 3 are read only while both stand. They
    # move if the example manifest is edited, and for no other reason.
    old = Manifest.from_dict(_version_3(example_manifest(kind=kind)))
    assert (manifest_digest(old), conditions_digest(old)) == (identity, conditions)


@pytest.mark.parametrize(
    ("changes", "fragment"),
    [
        ({"manifest_version": 1}, "manifest_version"),
        ({"world.kind": "null-v1"}, "null-v1 world plants nothing"),
        ({"world.planted": None}, "conditional-v1 world carries a planted rule"),
        (
            {"world.kind": "conditional-v2", "world.planted": None},
            "conditional-v2 world carries a planted rule",
        ),
        ({"world.id": "SYN-NULL0001"}, "world.id"),
        ({"world.id": "syn-eb3bc030"}, "world.id"),
        # Test seed 7's id on a dev-seed-0 manifest: well-formed, and another
        # world's. The invariant is the record's, not the author's.
        ({"world.id": "SYN-4778249A"}, "not the id of dev seed 0"),
        ({"world.seed_set": "test"}, "not the id of test seed 0"),
        ({"world.world_seed": 1}, "not the id of dev seed 1"),
        ({"world.origin_ms": 1}, "not a UTC midnight"),
        # Zero is a midnight and not a venue stamp (the candle type refuses it).
        ({"world.origin_ms": 0}, "world.origin_ms"),
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
        ({"world.planted.beta": 0}, "world.planted.beta"),
        ({"indicator_lookback": 10}, "indicator_lookback"),
        ({"costs.taker_fee_rate": -1}, "CostModel.taker_fee_rate"),
        ({"penalty.k": -1}, "Penalty.k"),
        ({"search_space.pool_size": 0}, "search_space.pool_size"),
        ({"search_space.pool_size": 5}, "5 is filled in, and this build enumerates 16"),
        (
            {"search_space.threshold_grid.realized_vol_20": [0.002, "high"]},
            "realized_vol_20 is a number, so its grid is numbers",
        ),
        ({"manifest_version": True}, "manifest_version"),
        ({"manifest_version": 1.0}, "manifest_version"),
        ({"manifest_version": 6, "later": 1}, "this build reads versions 3, 4, 5, got 6"),
        ({"manifest_version": 2}, "this build reads versions 3, 4, 5, got 2"),
        ({"arms.llm_tools": _DROP}, "arms: expected exactly the keys"),
        ({"arms.train_pick": _DROP}, "arms: expected exactly the keys"),
        ({"artifact_sha256": "abc"}, "artifact_sha256: a SHA-256 is 64"),
        ({"artifact_sha256": "A" * 64}, "artifact_sha256: a SHA-256 is 64"),
        ({"artifact_sha256": 7}, "artifact_sha256: a SHA-256 is 64"),
        ({"artifact_sha256": _DROP}, "manifest: expected exactly the keys"),
        ({"search_space.threshold_grid.realized_vol_20": [0.002, ""]}, "realized_vol_20"),
        ({"arms.random.search_seeds": []}, "arms.random.search_seeds"),
        ({"arms.random.search_seeds": [0, 0]}, "arms.random.search_seeds"),
        ({"arms.random.search_seeds": [2**63]}, "a search seed is at most"),
        ({"arms.llm.world_subset": ["BTC"]}, "arms.llm.world_subset"),
        ({"arms.llm.replicates": 0}, "arms.llm.replicates"),
        ({"arms.llm.world_subset": ["SYN-00000000"]}, "SYN-EB3BC030 is not in it"),
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


def test_a_repeated_key_is_refused_rather_than_resolved_to_the_last():
    text = example_manifest().dumps()
    budget = '"budget": {\n    "max_trials": 10,\n    "oracle_margin": 0.25\n  },'
    assert budget in text
    doubled = text.replace(budget, budget + ' "budget": {"max_trials": 1, "oracle_margin": 0},')
    with pytest.raises(ManifestError, match="'budget' appears twice"):
        Manifest.loads(doubled)


def test_the_grid_is_immutable_and_read_by_feature():
    space = example_manifest().search_space
    assert space.grid("realized_vol_20") == (0.002, 0.004)
    assert isinstance(space.threshold_grid, tuple)
    # Built from pairs as well as from a mapping, and held to the same rules.
    assert replace(space, threshold_grid=space.threshold_grid) == space
    doubled = (*space.threshold_grid, ("realized_vol_20", (0.5,)))
    with pytest.raises(ManifestError, match="listed twice"):
        replace(space, threshold_grid=doubled)
    with pytest.raises(ManifestError, match=r"a \(feature, values\) pair"):
        replace(space, threshold_grid=(("realized_vol_20",),))
    # Enumerated and filled in: the size is the count this build enumerates.
    filled = Manifest.from_dict(_edited(**{"search_space.pool_size": 16}))
    assert filled.search_space.pool_size == 16 == len(filled.search_space.candidates)


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
    # Upper-case hex after the prefix: the alphabet that cannot spell "NULL"
    # or "COND", which is the whole of the no-word guarantee.
    assert re.fullmatch(r"SYN-[0-9A-F]{8}", world_id)


def test_a_seed_that_is_not_a_whole_number_is_refused():
    with pytest.raises(ManifestError, match="world_seed"):
        opaque_world_id(SeedSet.DEV, -1)


def test_a_candidate_built_by_hand_keeps_the_rule_the_grids_are_read_under():
    # A string on a numeric feature would be a feature-to-feature comparison
    # no grid writes; a number on the regime a comparison the language refuses.
    candidate = example_manifest().search_space.candidates[0]
    with pytest.raises(ManifestError, match="the threshold on funding_zscore_14 is a number"):
        replace(candidate, entry="realized_vol_20")
    with pytest.raises(ManifestError, match="the threshold on realized_vol_20 is a number"):
        replace(candidate, gate="high")
    label = next(iter(MarketRegime)).value
    spec = parse_spec(
        {
            "family": "regime_filter",
            "entry": {"long": [{"left": "funding_zscore_14", "op": "<", "right": -1.0}]},
            "filters": [{"left": "regime", "op": "==", "right": label}],
            "exit": {"max_bars": 6},
            "sizing": POOL_SIZING,
        }
    )
    assert Candidate(template=0, entry=-1.0, gate=label, spec=spec).gate == label
    with pytest.raises(ManifestError, match="the threshold on regime is a label"):
        Candidate(template=0, entry=-1.0, gate=1.0, spec=spec)
