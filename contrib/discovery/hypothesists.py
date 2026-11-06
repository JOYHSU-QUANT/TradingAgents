"""The Random and Grid arms (plan §4.3): two hypothesists over one finite candidate pool.

Both arms answer the research package's own ``search()`` through its
``Hypothesist`` port, so an arm is one run of the loop the model arm runs
(plan §1 #2) and the research package is not changed to know about pools.
What they are NOT is the point: neither reads the prompt it is handed — the
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
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from .manifest import Candidate, SearchSpace
from .upstream import HypothesistError

__all__ = [
    "CandidatesExhausted",
    "GridHypothesist",
    "Pool",
    "PoolHypothesist",
    "RandomHypothesist",
    "enumerate_pool",
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
    """A :class:`~contrib.autoresearch.ports.Hypothesist` that proposes a pool in a fixed order.

    ``order`` is the candidates' numbers in proposal order, every number
    once; ``proposed`` counts what has been answered; ``exhausted`` is set
    the moment the pool runs out, and stays set. The prompts are read by
    neither arm (the module docstring).
    """

    def __init__(self, pool: Pool, order: tuple[int, ...], label: str) -> None:
        if sorted(order) != list(range(pool.size)):
            raise ValueError(f"{label}: the order lists each of the {pool.size} candidate(s) once")
        self.pool = pool
        self.order = order
        self.label = label
        self.proposed = 0
        self.exhausted = False

    def propose(self, system: str, user: str) -> str:
        del system, user  # unread by design
        if self.proposed == len(self.order):
            self.exhausted = True
            raise CandidatesExhausted(
                f"{self.label}: every one of the pool's {self.pool.size} candidate(s) has been "
                f"proposed"
            )
        candidate = self.pool.candidates[self.order[self.proposed]]
        self.proposed += 1
        return candidate.answer()


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
