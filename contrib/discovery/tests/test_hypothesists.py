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

from contrib.autoresearch.dsl import Family, Op, Side, load_spec, parse_spec, spec_hash
from contrib.autoresearch.hypothesis import search
from contrib.autoresearch.ledger import ProposalOutcome
from contrib.autoresearch.metrics import SegmentMetrics
from contrib.autoresearch.ports import HypothesistError
from contrib.discovery import hypothesists
from contrib.discovery.bench import BenchError, _train_view
from contrib.discovery.hypothesists import (
    CandidatesExhausted,
    GridHypothesist,
    PickRound,
    Pool,
    PoolHypothesist,
    RandomHypothesist,
    Scored,
    TrainPickHypothesist,
    enumerate_pool,
    rounds_to_draw,
    train_rank,
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
from contrib.discovery.research_tools import TrainView, ViewError, backtest_train
from contrib.discovery.tools_server import LaunchError
from contrib.discovery.verdict import Ended, classify_ending

from .conftest import BUY_12, COIN, arm_store, example_manifest, open_arm
from .test_research_tools import _rewrite

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
    # Which grids take labels is the vocabulary's call, held where the grid
    # is read: a string on a numeric feature (the parser would read it as a
    # feature's name) and a number on the categorical regime are both
    # refused there; a regime grid of labels is walked in the order listed.
    template = Template(Side.LONG, Clause("funding_zscore_14", Op.LT), GATE, 12)
    with pytest.raises(
        ManifestError,
        match=r"threshold_grid\['funding_zscore_14'\]: funding_zscore_14 is a number, so its grid",
    ):
        _space(template, funding_zscore_14=(-1.0, "trending"), realized_vol_20=(0.002,))
    regime = Template(Side.LONG, Clause("regime", Op.EQ), GATE, 12)
    pool = enumerate_pool(_space(regime, regime=("volatile", "trending"), realized_vol_20=(0.002,)))
    assert [c.entry for c in pool.candidates] == ["volatile", "trending"]
    with pytest.raises(
        ManifestError, match=r"threshold_grid\['regime'\]: regime is categorical, so its grid"
    ):
        _space(regime, regime=("trending", 1.0), realized_vol_20=(0.002,))


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


# -- the pick by training score (tools plan §4, T5 a–c) ---------------------------------------------
#
# Over the suite's hand-written history, through an experiment opened on
# it, as the look-ups' own tests are: the view is read the way the bench
# reads it for the arm (``bench._train_view``), and the pool is the example
# manifest's sixteen rules. On that history the pool holds rules that gain
# in training, rules that lose, and rules that never trade — every tier of
# the ranking but ruin, which is built by hand below.


@pytest.fixture(scope="module")
def view(opened_arm) -> TrainView:
    return _train_view(*opened_arm)


def _pick(pool, view, *, seed=0, backtests=2, world=COIN) -> TrainPickHypothesist:
    return TrainPickHypothesist(pool, view, world_id=world, search_seed=seed, backtests=backtests)


def _walked(arm: TrainPickHypothesist) -> list[str]:
    """Every answer the arm gives before its pool is out."""
    answers = []
    while True:
        try:
            answers.append(arm.propose("ignored", "ignored"))
        except CandidatesExhausted:
            return answers


@pytest.mark.parametrize("backtests", [1, 2, 3, 5, 16, 20])
def test_the_pick_scores_no_more_than_its_limit_a_round_and_draws_no_rule_twice(
    pool, view, monkeypatch, backtests
):
    scored: list[str] = []
    real = hypothesists.backtest_train

    def counted(read, spec):
        assert read is view
        scored.append(spec_hash(spec))
        return real(read, spec)

    monkeypatch.setattr(hypothesists, "backtest_train", counted)
    arm = _pick(pool, view, backtests=backtests)
    answers = _walked(arm)
    # (b) Never more than the limit: every round scores the limit, and a
    # last round finding fewer left scores those.
    full, left = divmod(pool.size, backtests)
    assert [len(round_.scored) for round_ in arm.rounds] == [backtests] * full + (
        [left] if left else []
    )
    assert len(answers) == arm.proposed == len(arm.rounds)
    # (c) Without replacement: what was drawn is the pool in the arm's
    # order, each rule once — the ones not proposed have left it too — and
    # one backtest was run for each rule drawn, none besides.
    drawn = [one.number for round_ in arm.rounds for one in round_.scored]
    assert drawn == list(arm.order)
    assert scored == [pool.candidates[number].spec_hash for number in drawn]
    assert arm.drawn == pool.size
    assert len(arm.rounds) == rounds_to_draw(pool.size, backtests)
    # An answer is its round's pick, and no rule is answered twice.
    assert answers == [pool.candidates[round_.picked].answer() for round_ in arm.rounds]
    assert len(set(answers)) == len(answers)
    # The pool out, the flag is set and stays; asking again scores nothing.
    assert arm.exhausted
    with pytest.raises(CandidatesExhausted, match="every one of the pool's 16 candidate"):
        arm.propose("s", "u")
    assert arm.exhausted and len(scored) == pool.size and arm.proposed == len(answers)


def test_one_world_and_seed_is_one_search_and_the_pick_s_stream_is_its_own(pool, view):
    # (a) The same seed on the same world draws, scores and proposes the same.
    first, again = _pick(pool, view), _pick(pool, view)
    assert first.order == again.order
    assert _walked(first) == _walked(again)
    assert first.rounds == again.rounds
    # Another seed and another world are other orders, and so is the Random
    # arm of the same seed: the two arms do not walk one permutation.
    others = (
        _pick(pool, view, seed=1),
        _pick(pool, view, world="SYN-00000000"),
        RandomHypothesist(pool, world_id=COIN, search_seed=0),
    )
    for other in others:
        assert sorted(other.order) == list(range(pool.size))
        assert other.order != first.order
    assert first.label == "train_pick seed 0"


def test_each_round_proposes_the_rule_that_scored_best_on_the_training_window(pool, view):
    arm = _pick(pool, view)
    _walked(arm)
    for round_ in arm.rounds:
        for one in round_.scored:
            # The look-up's own figures for the rule, on the view's window.
            assert one.train == backtest_train(view, pool.candidates[one.number].spec)
            assert one.train.segment == view.train
        best = min(train_rank(one.train) for one in round_.scored)
        assert round_.picked == next(
            one.number for one in round_.scored if train_rank(one.train) == best
        )
    summary = [
        [(one.train.trades > 0, round(one.train.net.sharpe, 3)) for one in round_.scored]
        for round_ in arm.rounds
    ]
    picks = [
        [one.number for one in round_.scored].index(round_.picked) for round_ in arm.rounds
    ]
    # Seed 0 on this history, pinned because each line is a case of the
    # ranking met on real figures: the higher net Sharpe of two rules that
    # traded, whichever was drawn first; a rule that LOST in training over
    # one that never traded (the fourth round); and of two that never
    # traded, the one drawn first (the last).
    assert summary == [
        [(True, -9.301), (True, 10.261)],
        [(True, 6.949), (True, -9.301)],
        [(True, 9.488), (True, -12.215)],
        [(True, -11.568), (False, 0.0)],
        [(True, 6.949), (True, -12.215)],
        [(False, 0.0), (True, 10.261)],
        [(True, -11.568), (True, 9.488)],
        [(False, 0.0), (False, 0.0)],
    ]
    assert picks == [1, 0, 0, 0, 0, 1, 1, 0]


def test_the_ranking_reads_ruin_first_then_whether_it_traded_then_the_net_sharpe(view):
    traded = backtest_train(view, parse_spec(BUY_12))
    assert traded.trades > 0 and not traded.ruined

    def scoring(sharpe: float, **changes) -> SegmentMetrics:
        return replace(traded, net=replace(traded.net, sharpe=sharpe), **changes)

    gained, lost = scoring(1.5), scoring(-4.0)
    idle, idle_high = scoring(0.0, trades=0), scoring(9.0, trades=0)
    ruined, ruined_high = scoring(-2.0, ruined=True), scoring(50.0, ruined=True)
    ruined_idle = scoring(0.0, trades=0, ruined=True)
    # The tiers, best first, and the net Sharpe read in the first alone.
    assert train_rank(scoring(1.6)) < train_rank(gained) < train_rank(lost)
    assert train_rank(lost) < train_rank(idle) == train_rank(idle_high)
    assert train_rank(idle) < train_rank(ruined)
    assert train_rank(ruined) == train_rank(ruined_high) == train_rank(ruined_idle)

    def picked(*figures: SegmentMetrics) -> int:
        return PickRound(tuple(Scored(10 + i, train) for i, train in enumerate(figures))).picked

    assert picked(lost, gained, idle, ruined_high) == 11
    assert picked(idle, lost) == 11  # a rule that traded, though it lost, over one that did not
    assert picked(ruined_high, idle) == 11  # no ratio of a ruined run is read
    assert picked(ruined, ruined_high) == 10  # ... not even to order two of them
    # Among equals, the one drawn first.
    assert picked(idle_high, idle) == picked(gained, scoring(1.5)) == 10
    assert picked(lost) == 10
    with pytest.raises(ValueError, match="a round scores at least one rule"):
        PickRound(())
    with pytest.raises(ValueError, match="a round scores at least one rule"):
        PickRound((gained,))
    with pytest.raises(ValueError, match="a round draws each rule once"):
        PickRound((Scored(3, gained), Scored(3, lost)))


def test_rewriting_everything_after_training_changes_no_pick(
    opened_arm, view, pool, history, tmp_path
):
    # The line the look-ups read behind (tools plan §2.2), held on what
    # this arm does with them: everything after the last training bar's
    # close replaced, and every round draws, scores and proposes as before.
    _path, experiment = opened_arm
    bars = view.frame.bundle.bars
    before = _pick(pool, view)
    answers = _walked(before)
    rewritten = tmp_path / "rewritten.sqlite"
    with arm_store(history, rewritten) as store:
        assert _rewrite(store, since_ms=bars[-1].close_time + 1) > 0
    after = _pick(pool, _train_view(rewritten, experiment))
    assert _walked(after) == answers
    assert after.rounds == before.rounds
    # The control: the same rewrite reaching back INSIDE training moves the
    # scores, so the equality above is not an arm that read no bars.
    control = tmp_path / "control.sqlite"
    with arm_store(history, control) as store:
        assert _rewrite(store, since_ms=bars[-60].close_time, series=("4h",)) > 0
    moved = _pick(pool, _train_view(control, experiment))
    _walked(moved)
    assert moved.rounds != before.rounds


def test_the_pick_through_the_real_loop_files_what_it_picked_and_ends_exhausted(arm, tmp_path):
    # Three rules, two a round, a budget of five: two rounds draw the pool
    # dry (2 + 1), the loop files the two picks, and the third round asks
    # and is told the pool is out — a stop that spends no round.
    template = Template(Side.LONG, Clause("funding_zscore_14", Op.LT), GATE, 12)
    pool = enumerate_pool(
        _space(template, funding_zscore_14=(-1.0, 0.0, 1.0), realized_vol_20=(0.002,))
    )
    assert pool.size == 3
    experiment = open_arm(arm)
    # Read as the bench reads it: beside the handle the search holds open.
    pick = _pick(pool, _train_view(tmp_path / "arm.sqlite", experiment))
    report = search(arm, experiment, pick, model="train_pick", max_trials=5)
    assert [len(round_.scored) for round_ in pick.rounds] == [2, 1]
    assert report.spent == 2 == pick.proposed
    assert report.stopped is not None
    assert "every one of the pool's 3 candidate(s) has been drawn" in report.stopped
    assert report.counts()[ProposalOutcome.MEASURED] == 2
    assert classify_ending(report, exhausted=pick.exhausted) is Ended.EXHAUSTED
    # What the loop filed is what each round picked, and no rule the arm
    # only scored: looking is not counted against the search.
    assert arm.rules_tried(COIN) == 2
    trials = sorted(arm.trials(experiment.experiment_id), key=lambda trial: trial.trial_id)
    assert [trial.spec_hash for trial in trials] == [
        pool.candidates[round_.picked].spec_hash for round_ in pick.rounds
    ]
    # And the figures a rule was picked on are the ones its trial carries
    # for the training window.
    for trial, round_ in zip(trials, pick.rounds, strict=True):
        assert trial.train == round_.best.train


def test_a_store_that_does_not_hold_the_split_s_training_window_is_a_named_refusal(opened_arm):
    # The view's own refusal, told as the bench tells a run that cannot be
    # made — not a traceback. Here: the three windows moved past the last
    # bar the store holds, so no bar of the training window is there.
    path, experiment = opened_arm
    split = experiment.split
    span = split.holdout.end_ms - split.train.start_ms
    moved = {
        name: replace(window, start_ms=window.start_ms + span, end_ms=window.end_ms + span)
        for name, window in (
            ("train", split.train),
            ("validation", split.validation),
            ("holdout", split.holdout),
        )
    }
    with pytest.raises(
        BenchError,
        match=(
            rf"^arm: .*arm\.sqlite does not hold the training window the split names for {COIN}: "
            r".*the frame's bars do not run from the window's first bar to its last"
        ),
    ) as refused:
        _train_view(path, replace(experiment, split=replace(split, **moved)))
    # The read is the server's, and the refusal the view's own under it.
    assert isinstance(refused.value.__cause__, LaunchError)
    assert isinstance(refused.value.__cause__.__cause__, ViewError)


def test_a_pick_that_cannot_be_built_is_refused(pool, view):
    for backtests in (0, -1, True, 2.0, "2"):
        with pytest.raises(ValueError, match="scores at least one rule a round"):
            _pick(pool, view, backtests=backtests)
    with pytest.raises(ValueError, match="reads a train view"):
        _pick(pool, view.frame)
