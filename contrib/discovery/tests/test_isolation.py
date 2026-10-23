"""The three isolation facts the design stands on, pinned (plan §6 PR 1, acceptance a–c).

Each is a fact about the research package this package relies on and does
not control — that the oracle's read path writes nothing, that two arms
over one coin share no ledger state, that a prompt carries the opaque id
and nothing else of the manifest — so each is held HERE, where a change
upstream that quietly broke it would be read as a benchmark result.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from contrib.autoresearch.dsl import parse_spec, spec_to_document
from contrib.autoresearch.hypothesis import build_system_prompt, build_user_prompt, search
from contrib.autoresearch.ledger import Ledger, Penalty
from contrib.autoresearch.research import measure
from contrib.autoresearch.store import ResearchStore
from contrib.discovery.manifest import WorldKind
from contrib.discovery.oracle import evaluate_unfiled

from .conftest import BUY, COIN, ScriptedHypothesist, arm_store, example_manifest, open_arm


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# -- (a) the oracle path files nothing ----------------------------------------------------


def test_the_oracle_path_leaves_the_arm_s_store_byte_identical(history, tmp_path):
    path = tmp_path / "arm.sqlite"
    with arm_store(history, path) as store:
        experiment = open_arm(Ledger(store))
    before = _digest(path)

    with ResearchStore(path) as store:
        ledger = Ledger(store)
        result = evaluate_unfiled(ledger, experiment, parse_spec(BUY), holdout=True)
        assert [r.segment.name.value for r in result.results] == ["train", "validation", "holdout"]
        assert ledger.rules_tried(COIN) == 0
        assert ledger.trials(experiment.experiment_id) == []
        assert "no rule has been tried on this coin yet" in build_user_prompt(ledger, experiment)
    assert _digest(path) == before

    # The control: the verb the oracle must NOT use does change the file.
    with ResearchStore(path) as store:
        measure(Ledger(store), experiment, parse_spec(BUY))
    assert _digest(path) != before


# -- (b) one coin, one sqlite per arm, no shared state --------------------------------------


def test_two_arms_on_one_world_share_the_coin_name_and_nothing_else(history, tmp_path):
    world_before = _digest(history)
    with arm_store(history, tmp_path / "random.sqlite") as store:
        random_arm = Ledger(store)
        experiment = open_arm(random_arm, penalty=Penalty(sharpe_base=1.0, k=0.25))
        measure(random_arm, experiment, parse_spec(BUY))
        assert random_arm.rules_tried(COIN) == 1
        assert random_arm.penalty_pin(COIN) == Penalty(sharpe_base=1.0, k=0.25)

    with arm_store(history, tmp_path / "grid.sqlite") as store:
        grid_arm = Ledger(store)
        # Nothing the first arm did is state here: no rule counted, no
        # holdout pinned, no penalty pinned — a different one is accepted,
        # which the first arm's store would refuse.
        assert grid_arm.rules_tried(COIN) == 0
        assert grid_arm.holdout_pin(COIN) is None
        assert grid_arm.penalty_pin(COIN) is None
        experiment = open_arm(grid_arm, penalty=Penalty(sharpe_base=0.5, k=0.0))
        assert "no rule has been tried on this coin yet" in build_user_prompt(grid_arm, experiment)
    # And the world artifact both copied from is untouched.
    assert _digest(history) == world_before


# -- (c) the prompts carry the id and nothing else of the manifest ----------------------------


def test_nothing_a_model_is_shown_names_the_world_s_kind_or_its_rule(history, tmp_path):
    manifest = example_manifest(kind=WorldKind.CONDITIONAL_V1)
    assert manifest.world.id == COIN
    with arm_store(history, tmp_path / "llm.sqlite") as store:
        ledger = Ledger(store)
        experiment = open_arm(ledger, coin=manifest.world.id)
        # A round that filed a trial and one that was refused, so the prompt
        # has every section it can have.
        hypothesist = ScriptedHypothesist([json.dumps(BUY), "not a rule"])
        search(ledger, experiment, hypothesist, model="scripted", max_trials=2)
        shown = build_system_prompt() + "\n" + build_user_prompt(ledger, experiment)

    assert manifest.world.id in shown  # the one word of the manifest a prompt carries
    # ``COND`` exact: the language's prose says "condition", and that is not
    # the leak this test is about.
    assert "COND" not in shown
    lowered = shown.lower()
    for word in ("null", "planted", *(kind.value for kind in WorldKind)):
        assert word not in lowered, word
    planted = json.dumps(spec_to_document(manifest.world.planted.spec), sort_keys=True)
    assert planted not in shown
