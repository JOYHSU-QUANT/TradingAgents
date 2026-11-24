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
a rule's training figures, at most as many times a round, and does no
reasoning about them. What it is matched to the tool arm on is that number
and nothing else: it chooses among the pool's rules, where the model
writes any rule of the language, has two other look-ups, and reads the
validation feedback. So the tool arm doing better than it is evidence of
more than the sight of training figures, and the tool arm doing no better
says little, the pool being a short list to pick from; what this arm does
beyond Random is what that sight buys a picker with no judgement. Every
rule it draws leaves the pool, proposed or not, and the pool runs out as
the other arms' does.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from .manifest import Candidate, SearchSpace
from .research_tools import TrainView, backtest_train
from .upstream import HypothesistError, SegmentMetrics, SegmentName

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

    A round is this class's to count, whatever arm it is: :meth:`propose`
    takes the round's rules, asks :meth:`_choose` for the answer, and only
    then counts them — so an arm is a way of choosing, and none can forget
    to move the pool on or to end it.
    """

    # How many of the pool's rules one answer takes.
    _a_round = 1
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

    def _choose(self, numbers: tuple[int, ...]) -> str:
        """The answer of a round that took ``numbers`` from the pool: here, the one rule taken."""
        (number,) = numbers
        return self.pool.candidates[number].answer()

    def propose(self, system: str, user: str) -> str:
        del system, user  # unread by design
        if self.drawn == len(self.order):
            # The exhausted protocol, kept in this one place: the flag
            # first, then the seam error (the verdict reads the flag off an
            # arm whose search the error stopped).
            self.exhausted = True
            raise CandidatesExhausted(
                f"{self.label}: every one of the pool's {self.pool.size} candidate(s) has been "
                f"{self._taken}"
            )
        # The round's rules, or the ones left if fewer. Chosen among before
        # anything is counted: a round that cannot be answered (a rule the
        # store cannot measure ends the search) leaves the arm as it found it.
        numbers = self.order[self.drawn : self.drawn + self._a_round]
        answer = self._choose(numbers)
        self.drawn += len(numbers)
        self.proposed += 1
        return answer


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

    0. the rule traded and was not ruined — the higher net Sharpe first,
       a rule that lost in training included: it has a figure to compare;
    1. it made no trade in training — a Sharpe of 0 that says nothing of
       the rule, so it is proposed only when nothing drawn beside it
       traded. (It may yet trade in validation, which is the window the
       promote gate reads; the tier is about what training shows, not
       about what the gate will say.)
    2. it was ruined — last whatever its ratios, since no ratio of a
       ruined run is read (the research package puts a ruined trial last
       for the same reason).

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

    def __post_init__(self) -> None:
        # A record built by hand must not say less than the arm's own do:
        # a number of a pool, and figures of the window the field names.
        if isinstance(self.number, bool) or not isinstance(self.number, int) or self.number < 0:
            raise ValueError(f"a scored rule is numbered from 0 in its pool, got {self.number!r}")
        if not isinstance(self.train, SegmentMetrics):
            raise ValueError(f"a scored rule carries its training figures, got {self.train!r}")
        # ``getattr``: figures built by hand may carry no window at all.
        if getattr(self.train.segment, "name", None) is not SegmentName.TRAIN:
            raise ValueError(
                f"a rule is scored on the training window, and these figures are for "
                f"{self.train.segment}"
            )


@dataclass(frozen=True)
class PickRound:
    """What one round drew, in the order drawn, each rule scored on the training window."""

    scored: tuple[Scored, ...]

    def __post_init__(self) -> None:
        scored = tuple(self.scored)
        if not scored:
            raise ValueError("a round scores at least one rule")
        if any(not isinstance(one, Scored) for one in scored):
            raise ValueError("a round holds Scored rules")
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
    and the search seed, as the Random arm's is, on a stream of its own, so
    the two arms' orders are drawn independently (decided 2026-11-24: one
    shared order would tie the two arms' luck together) — and without
    replacement: a round takes the next ``backtests`` numbers of it, and
    all of them leave the pool for good, the ones not proposed too
    (decided the same day: the arm then sees the most rules a walk of the
    pool can, which makes it the strongest control of its kind). A last
    round finding fewer left scores those; one finding none raises
    :class:`CandidatesExhausted`, as the other pool arms do — after
    :func:`rounds_to_draw` answers.

    ``view`` is the training window of the arm's own store
    (:class:`~contrib.discovery.research_tools.TrainView`): nothing after
    it is held there, so nothing after it can reach a pick. What the store
    cannot answer (``EvaluationError``) is left to propagate, as it is
    from the search's own measurement of the same rule.

    ``rounds`` is what each round drew and scored, so the arm's use of the
    training window can be counted: never more than ``backtests`` scored
    rules a round, which is the tool arm's own limit. Read-only, as
    ``backtests`` is: they describe a search that has been made.
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
        self._a_round = backtests
        self._rounds: list[PickRound] = []

    @property
    def backtests(self) -> int:
        """How many rules a round scores, the last round's fewer if fewer are left."""
        return self._a_round

    @property
    def rounds(self) -> tuple[PickRound, ...]:
        """Every round answered so far, in order."""
        return tuple(self._rounds)

    def _choose(self, numbers: tuple[int, ...]) -> str:
        round_ = PickRound(
            tuple(
                Scored(number, backtest_train(self.view, self.pool.candidates[number].spec))
                for number in numbers
            )
        )
        answer = self.pool.candidates[round_.picked].answer()
        self._rounds.append(round_)
        return answer
