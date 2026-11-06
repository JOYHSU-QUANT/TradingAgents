"""The pool and the two arms over it (plan §4.3; PR 3's acceptance: Random without replacement, Grid in order, exhausted flagged).

The pool tests are on the manifest alone — enumeration is pure. The last
test runs an arm through the research package's real ``search()`` on the
hand-written history, because what the exhausted protocol promises is a
fact about that loop: a pool walked to its end stops the run without
spending a round, every candidate having been filed once.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from contrib.autoresearch.dsl import Family, Op, Side, load_spec, spec_hash
from contrib.autoresearch.hypothesis import search
from contrib.autoresearch.ledger import ProposalOutcome
from contrib.autoresearch.ports import HypothesistError
from contrib.discovery.hypothesists import (
    CandidatesExhausted,
    GridHypothesist,
    Pool,
    PoolHypothesist,
    RandomHypothesist,
    enumerate_pool,
)
from contrib.discovery.manifest import (
    POOL_SIZING,
    CandidateOrder,
    Clause,
    ManifestError,
    SearchSpace,
    Template,
    family_for,
)
from contrib.discovery.verdict import Ended, classify_ending

from .conftest import COIN, example_manifest, open_arm

GATE = Clause("realized_vol_20", Op.GT)


def _space(*templates: Template, **grids) -> SearchSpace:
    return SearchSpace(
        templates=templates,
        threshold_grid=grids,
        candidate_order=CandidateOrder.TEMPLATES_THEN_GRID,
    )


@pytest.fixture(scope="module")
def pool():
    return enumerate_pool(example_manifest().search_space)


# -- the pool ------------------------------------------------------------------------------


def test_the_pool_is_the_templates_crossed_with_their_grids_in_candidate_order(pool):
    space = example_manifest().search_space
    # Two templates, four entry thresholds, two gate thresholds: sixteen,
    # numbered template by template, entry ascending outside, gate inside.
    assert pool.size == space.enumerated == 16
    assert pool.candidates == space.candidates
    assert [(c.template, c.entry, c.gate) for c in pool.candidates[:5]] == [
        (0, -2.0, 0.002),
        (0, -2.0, 0.004),
        (0, -1.0, 0.002),
        (0, -1.0, 0.004),
        (0, 1.0, 0.002),
    ]
    assert [c.template for c in pool.candidates] == [0] * 8 + [1] * 8
    # Every candidate is the rule its template and thresholds say, written
    # the one canonical way: the gate under ``filters``, ``max_bars`` the
    # exit, the pool's sizing, the family its features earn.
    for candidate in pool.candidates:
        template = space.templates[candidate.template]
        spec = candidate.spec
        assert spec.entries(template.side)[0].left.name == template.entry.feature
        assert spec.entries(template.side)[0].op is template.entry.op
        assert spec.entries(template.side)[0].right == candidate.entry
        assert not spec.entries(Side.SHORT if template.side is Side.LONG else Side.LONG)
        assert [(c.left.name, c.op, c.right) for c in spec.filters] == [
            (template.gate.feature, template.gate.op, candidate.gate)
        ]
        assert spec.max_bars == template.max_bars and not spec.exit_long and not spec.exit_short
        assert spec.sizing.mode.value == POOL_SIZING["mode"]
        assert spec.sizing.fraction == POOL_SIZING["fraction"]
        assert spec.family is Family.FUNDING_FILTER


def test_the_grid_values_are_walked_ascending_whatever_order_they_are_listed_in():
    template = Template(Side.LONG, Clause("funding_zscore_14", Op.LT), GATE, 12)
    listed = enumerate_pool(
        _space(template, funding_zscore_14=(2.0, -2.0, 0.5), realized_vol_20=(0.004, 0.002))
    )
    assert [(c.entry, c.gate) for c in listed.candidates] == [
        (-2.0, 0.002),
        (-2.0, 0.004),
        (0.5, 0.002),
        (0.5, 0.004),
        (2.0, 0.002),
        (2.0, 0.004),
    ]


def test_two_spellings_of_one_rule_are_one_candidate(pool):
    space = example_manifest().search_space
    twice = replace(space, templates=space.templates + space.templates)
    again = enumerate_pool(twice)
    assert twice.enumerated == 32
    assert again.size == 16
    assert [c.spec_hash for c in again.candidates] == [c.spec_hash for c in pool.candidates]
    # And a pool built by hand must not say less than the manifest holds.
    with pytest.raises(ValueError, match="each rule once"):
        Pool(pool.candidates + pool.candidates[:1])
    with pytest.raises(ValueError, match="at least one candidate"):
        Pool(())
    with pytest.raises(ValueError, match="holds Candidates"):
        Pool(("not a candidate",))
    # A candidate is held to its fields; its hash is its spec's, not a field.
    first = pool.candidates[0]
    assert first.spec_hash == spec_hash(first.spec)
    with pytest.raises(ManifestError, match="candidate.template"):
        replace(first, template=-1)
    with pytest.raises(ManifestError, match="candidate.gate"):
        replace(first, gate=float("nan"))
    # The thresholds are the spec's: a candidate claiming another number
    # for its entry or its gate is refused.
    with pytest.raises(ManifestError, match="candidate.entry: 0.5 is not the spec's threshold"):
        replace(first, entry=0.5)
    with pytest.raises(ManifestError, match="candidate.gate: 0.5 is not the spec's threshold"):
        replace(first, gate=0.5)


def test_a_candidate_answers_with_the_text_the_parser_reads_back_as_its_rule(pool):
    for candidate in pool.candidates:
        assert spec_hash(load_spec(candidate.answer())) == candidate.spec_hash


def test_a_candidate_the_language_refuses_is_refused_by_the_template_that_wrote_it():
    # The regime is compared with a label, never ordered: the manifest's
    # clause accepts the operator, the parser does not, and the sentence
    # names the template and the parser's reason.
    template = Template(Side.LONG, Clause("regime", Op.GT), GATE, 12)
    with pytest.raises(
        ManifestError,
        match=r"search_space\.templates\[0\] with regime > 'trending'.*the regime is a label",
    ):
        enumerate_pool(_space(template, regime=("trending",), realized_vol_20=(0.002,)))


def test_a_label_is_a_regime_label_and_a_grid_of_labels_keeps_its_order():
    # A string on a numeric feature is refused where the grid is read (the
    # parser would read it as a feature's name); a regime grid of labels is
    # walked in the order listed, and one mixing a number in names no order.
    template = Template(Side.LONG, Clause("funding_zscore_14", Op.LT), GATE, 12)
    with pytest.raises(
        ManifestError,
        match=r"threshold_grid\['funding_zscore_14'\]: a grid value is a number, or a regime",
    ):
        _space(template, funding_zscore_14=(-1.0, "trending"), realized_vol_20=(0.002,))
    regime = Template(Side.LONG, Clause("regime", Op.EQ), GATE, 12)
    pool = enumerate_pool(_space(regime, regime=("volatile", "trending"), realized_vol_20=(0.002,)))
    assert [c.entry for c in pool.candidates] == ["volatile", "trending"]
    with pytest.raises(
        ManifestError, match=r"threshold_grid\['regime'\]: a grid is numbers or labels"
    ):
        enumerate_pool(_space(regime, regime=("trending", 1.0), realized_vol_20=(0.002,)))


@pytest.mark.parametrize(
    ("features", "family"),
    [
        (("funding_zscore_14", "realized_vol_20"), Family.FUNDING_FILTER),
        (("regime", "realized_vol_20"), Family.REGIME_FILTER),
        (("close", "sma_20"), Family.BREAKOUT),
        # Funding wins over the regime: the parser holds both claims, and a
        # rule reading both satisfies both.
        (("funding_rate", "regime"), Family.FUNDING_FILTER),
    ],
)
def test_the_family_label_is_the_one_the_features_earn(features, family):
    assert family_for(features) is family


# -- the arms -------------------------------------------------------------------------------


def test_grid_walks_the_pool_in_its_numbered_order(pool):
    grid = GridHypothesist(pool)
    assert grid.order == tuple(range(pool.size))
    answers = [grid.propose("ignored", "ignored") for _ in range(pool.size)]
    assert answers == [c.answer() for c in pool.candidates]
    assert grid.proposed == pool.size and not grid.exhausted


def test_random_draws_a_permutation_from_the_world_and_the_seed_without_replacement(pool):
    world = example_manifest().world.id
    same = RandomHypothesist(pool, world_id=world, search_seed=0)
    again = RandomHypothesist(pool, world_id=world, search_seed=0)
    other_seed = RandomHypothesist(pool, world_id=world, search_seed=1)
    other_world = RandomHypothesist(pool, world_id="SYN-00000000", search_seed=0)
    for arm in (same, other_seed, other_world):
        assert sorted(arm.order) == list(range(pool.size))
    assert same.order == again.order
    assert same.order != tuple(range(pool.size))
    assert same.order != other_seed.order
    assert same.order != other_world.order
    answers = [same.propose("s", "u") for _ in range(pool.size)]
    assert sorted(answers) == sorted(c.answer() for c in pool.candidates)
    assert len(set(answers)) == pool.size


def test_a_pool_walked_to_its_end_raises_the_seam_error_and_flags_itself(pool):
    arm = GridHypothesist(pool)
    for _ in range(pool.size):
        arm.propose("s", "u")
    assert not arm.exhausted
    with pytest.raises(CandidatesExhausted, match="every one of the pool's 16 candidate"):
        arm.propose("s", "u")
    assert arm.exhausted
    # The family ``search()`` stops on, and it stays raised — the flag is
    # not cleared by asking again.
    assert isinstance(CandidatesExhausted("x"), HypothesistError)
    with pytest.raises(CandidatesExhausted):
        arm.propose("s", "u")
    assert arm.exhausted


def test_an_order_that_is_not_a_permutation_of_the_pool_is_refused(pool):
    with pytest.raises(ValueError, match="lists each of the 16 candidate"):
        PoolHypothesist(pool, (0, 0, 1), "hand-built")


def test_an_arm_through_the_real_loop_files_each_candidate_once_and_ends_exhausted(arm):
    # A two-candidate pool and a budget of three: the loop files both,
    # then the third round asks and the pool says it is out. The report
    # names the stop, spent two rounds (the stop spends none), and the
    # verdict reads the ending as exhausted off the flag.
    template = Template(Side.LONG, Clause("close", Op.GT), Clause("sma_20", Op.GT), 12)
    pool = enumerate_pool(_space(template, close=(0.0, 1.0), sma_20=(0.0,)))
    assert pool.size == 2
    experiment = open_arm(arm)
    grid = GridHypothesist(pool)
    report = search(arm, experiment, grid, model="grid", max_trials=3)
    assert report.spent == 2
    assert report.stopped is not None and "every one of the pool's 2 candidate" in report.stopped
    assert report.counts()[ProposalOutcome.MEASURED] == 2
    assert report.counts()[ProposalOutcome.DUPLICATE] == 0
    assert arm.rules_tried(COIN) == 2
    assert classify_ending(report, exhausted=grid.exhausted) is Ended.EXHAUSTED
    assert sorted(t.spec_hash for t in arm.trials(experiment.experiment_id)) == sorted(
        c.spec_hash for c in pool.candidates
    )
