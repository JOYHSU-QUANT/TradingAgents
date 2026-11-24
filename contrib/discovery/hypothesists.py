"""The arms that propose from the candidate pool (plan §4.3; tools plan §3.6): Random, Grid, and the pick by training score.

Each arm answers the research package's own ``search()`` through its
``Hypothesist`` port, so an arm is one run of the loop the model arm runs
(plan §1 #2) and the research package is not changed to know about pools.
What they are NOT is the point: none reads the prompt it is handed — the
rules already tried and why they failed are shown to them and ignored —
so Random is the no-information baseline and Grid the fixed-order one,
and the model arm's advantage over them, if any, is what reading the
feedback buys.

**The pool** is the manifest's: :attr:`~contrib.discovery.manifest.SearchSpace.candidates`,
written and checked where the manifest is read (a manifest whose pool the
language refuses lands nothing), numbered by position in the manifest's
``candidate_order``, each rule once. :class:`Pool` is that tuple as an
arm takes it.

**Random** proposes the pool in a permutation drawn from the search seed
(and the world's id, so one seed is not one permutation on every world),
without replacement — a budget counts answers, and a repeat would be a
round spent on a rule the ledger already holds (plan §1 #9). **Grid**
proposes the pool in its numbered order. Both, when the pool is walked to
its end before the budget is, raise :class:`CandidatesExhausted` — a
``HypothesistError``, the one family ``search()`` stops on without
spending a round — and set their own ``exhausted`` flag, which is how the
verdict tells a pool that ran out from a seam that failed
(:func:`~contrib.discovery.verdict.classify_ending`).

**The pick by training score** (tools plan §3.6, T5) is the tool arm's
control, and the one arm here that reads anything of the world: each round
it draws a few rules from the pool, scores each on the TRAINING window
with the look-up the tool arm's model has
(:func:`~contrib.discovery.research_tools.backtest_train`, called as a
function: no server, no model), and proposes the one that scored best
there (:func:`train_rank`). It sees what a model with the tools may see of
a rule's training figures, as many times a round, and does no reasoning
about them — so what the tool arm does beyond it is the model's, and what
it does beyond Random is what the sight of training figures buys by
itself. Every rule it draws leaves the pool, proposed or not, and the pool
runs out as the other arms' does.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from .manifest import Candidate, SearchSpace
from .research_tools import TrainView, backtest_train
from .upstream import HypothesistError, SegmentMetrics

__all__ = [
    "CandidatesExhausted",
    "GridHypothesist",
    "PickRound",
    "Pool",
    "PoolHypothesist",
    "RandomHypothesist",
    "Scored",
    "TrainPickHypothesist",
    "enumerate_pool",
    "rounds_to_draw",
    "train_rank",
]


class CandidatesExhausted(HypothesistError):
    """The pool was proposed in full before the budget was spent (plan §4.3's exhausted protocol).

    A ``HypothesistError`` so ``search()`` stops without spending a round
    and returns its report; the hypothesist's ``exhausted`` flag is what
    says this was the pool and not the seam.
    """


@dataclass(frozen=True)
class Pool:
    """The candidates an arm proposes from, numbered by position, each rule once."""

    candidates: tuple[Candidate, ...]

    def __post_init__(self) -> None:
        # Defects of construction, not of a manifest: the manifest wrote the
        # pool and holds these already; a pool built by hand must not say less.
        candidates = tuple(self.candidates)
        if not candidates:
            raise ValueError("a pool holds at least one candidate")
        if any(not isinstance(candidate, Candidate) for candidate in candidates):
            raise ValueError("a pool holds Candidates")
        if len({candidate.spec_hash for candidate in candidates}) != len(candidates):
            raise ValueError("a pool lists each rule once")
        object.__setattr__(self, "candidates", candidates)

    @property
    def size(self) -> int:
        return len(self.candidates)


def enumerate_pool(space: SearchSpace) -> Pool:
    """The manifest's pool as an arm takes it."""
    return Pool(space.candidates)


# -- the arms ----------------------------------------------------------------------------------


class PoolHypothesist:
    """A :class:`~contrib.autoresearch.ports.Hypothesist` that takes a pool's rules in a fixed order.

    ``order`` is the candidates' numbers in the order they leave the pool,
    every number once; ``drawn`` counts those that have left it and
    ``proposed`` the answers given — one number for an arm that proposes
    every rule it takes, as this class does; ``exhausted`` is set the
    moment an arm is asked with nothing left in the pool, and stays set.
    The prompts are read by no arm (the module docstring).
    """

    # What the stop says became of every rule: an arm that takes more than
    # it proposes says another word.
    _taken = "proposed"

    def __init__(self, pool: Pool, order: tuple[int, ...], label: str) -> None:
        if sorted(order) != list(range(pool.size)):
            raise ValueError(f"{label}: the order lists each of the {pool.size} candidate(s) once")
        self.pool = pool
        self.order = order
        self.label = label
        self.drawn = 0
        self.proposed = 0
        self.exhausted = False

    def _next(self, count: int) -> tuple[int, ...]:
        """The next ``count`` numbers of the order, or the ones left if fewer; none left is the pool's end.

        The one place the exhausted protocol is kept: the flag first, then
        the seam error (the verdict reads the flag off an arm whose search
        the error stopped). Nothing is counted here — the caller counts
        what it took (``drawn``) once it has an answer to give.
        """
        if self.drawn == len(self.order):
            self.exhausted = True
            raise CandidatesExhausted(
                f"{self.label}: every one of the pool's {self.pool.size} candidate(s) has been "
                f"{self._taken}"
            )
        return self.order[self.drawn : self.drawn + count]

    def propose(self, system: str, user: str) -> str:
        del system, user  # unread by design
        (number,) = self._next(1)
        self.drawn += 1
        self.proposed += 1
        return self.pool.candidates[number].answer()


class GridHypothesist(PoolHypothesist):
    """The pool in its numbered order: the manifest's ``candidate_order``, nothing drawn."""

    def __init__(self, pool: Pool) -> None:
        super().__init__(pool, tuple(range(pool.size)), "grid")


class RandomHypothesist(PoolHypothesist):
    """The pool in a permutation drawn from the world's id and the search seed, without replacement.

    The world's id is in the stream so a search seed is not one permutation
    on every world; the kind of world is not (the id never carries it).
    """

    def __init__(self, pool: Pool, *, world_id: str, search_seed: int) -> None:
        order = list(range(pool.size))
        random.Random(f"{world_id}/random/{search_seed}").shuffle(order)
        super().__init__(pool, tuple(order), f"random seed {search_seed}")


# -- the pick by training score ------------------------------------------------------------------


def train_rank(metrics: SegmentMetrics) -> tuple[int, float]:
    """Where a rule's training figures put it among the rules of one round: the lowest is proposed.

    Decided 2026-11-24, before any result of the tool arm was read. Three
    tiers, and the net Sharpe read in the first alone:

    0. the rule traded and was not ruined — the higher net Sharpe first;
    1. it made no trade — a Sharpe of 0 that says nothing, and a rule the
       promote gate refuses whatever else it scores, so it is proposed
       only when nothing drawn beside it traded;
    2. it was ruined — last whatever its ratios, since no ratio of a
       ruined run is read (the research package ranks its trials so).

    Within the second and third tiers every rule ranks alike, and among
    equals the one drawn first is proposed (:attr:`PickRound.picked`).
    """
    if metrics.ruined:
        return (2, 0.0)
    if metrics.trades == 0:
        return (1, 0.0)
    return (0, -metrics.net.sharpe)


@dataclass(frozen=True)
class Scored:
    """One rule a round drew — its number in the pool — and its figures on the training window."""

    number: int
    train: SegmentMetrics


@dataclass(frozen=True)
class PickRound:
    """What one round drew, in the order drawn, each rule scored on the training window."""

    scored: tuple[Scored, ...]

    def __post_init__(self) -> None:
        scored = tuple(self.scored)
        if not scored or any(not isinstance(one, Scored) for one in scored):
            raise ValueError("a round scores at least one rule")
        if len({one.number for one in scored}) != len(scored):
            raise ValueError("a round draws each rule once")
        object.__setattr__(self, "scored", scored)

    @property
    def best(self) -> Scored:
        """The rule proposed, with its figures: the best by :func:`train_rank`, the first drawn among equals."""
        # ``min`` keeps the first of equal keys, and ``scored`` is in the order drawn.
        return min(self.scored, key=lambda one: train_rank(one.train))

    @property
    def picked(self) -> int:
        """The number of the rule proposed."""
        return self.best.number


def rounds_to_draw(pool_size: int, backtests: int) -> int:
    """How many answers empty a pool of ``pool_size`` rules taken ``backtests`` a round.

    Every round but the last takes ``backtests`` and the last what is
    left, so it is the ceiling of the quotient — and the most answers a
    pick by training score can give, whatever its budget.
    """
    return -(-pool_size // backtests)


class TrainPickHypothesist(PoolHypothesist):
    """Each round: draw ``backtests`` rules, score each on the training window, propose the best there.

    The pool is drawn in a permutation of its own — from the world's id
    and the search seed, as the Random arm's is, on another stream, so the
    two arms of one seed do not walk one order — and without replacement:
    a round takes the next ``backtests`` numbers of it, and all of them
    leave the pool, the ones not proposed too. A last round finding fewer
    left scores those; one finding none raises
    :class:`CandidatesExhausted`, as the other pool arms do — after
    :func:`rounds_to_draw` answers.

    ``view`` is the training window of the arm's own store
    (:class:`~contrib.discovery.research_tools.TrainView`): nothing after
    it is held there, so nothing after it can reach a pick. What the store
    cannot answer (``EvaluationError``) is left to propagate, as it is
    from the search's own measurement of the same rule.

    ``rounds`` keeps what each round drew and scored, so the arm's use of
    the training window can be counted: never more than ``backtests``
    scored rules a round, which is the tool arm's own limit.
    """

    # Not every rule this arm takes is proposed.
    _taken = "drawn"

    def __init__(
        self,
        pool: Pool,
        view: TrainView,
        *,
        world_id: str,
        search_seed: int,
        backtests: int,
    ) -> None:
        if not isinstance(view, TrainView):
            raise ValueError(f"the pick by training score reads a train view, got {view!r}")
        if isinstance(backtests, bool) or not isinstance(backtests, int) or backtests < 1:
            raise ValueError(
                f"the pick by training score scores at least one rule a round, got {backtests!r}"
            )
        order = list(range(pool.size))
        random.Random(f"{world_id}/train_pick/{search_seed}").shuffle(order)
        super().__init__(pool, tuple(order), f"train_pick seed {search_seed}")
        self.view = view
        self.backtests = backtests
        self.rounds: list[PickRound] = []

    def propose(self, system: str, user: str) -> str:
        del system, user  # unread by design
        # Scored before anything is counted: a rule the store cannot
        # measure ends the search, and leaves this arm as the round found it.
        round_ = PickRound(
            tuple(
                Scored(number, backtest_train(self.view, self.pool.candidates[number].spec))
                for number in self._next(self.backtests)
            )
        )
        self.rounds.append(round_)
        self.drawn += len(round_.scored)
        self.proposed += 1
        return self.pool.candidates[round_.picked].answer()
