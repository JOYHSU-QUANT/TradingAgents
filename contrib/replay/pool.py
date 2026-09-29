"""The direction probe pooled across runs, with a block-bootstrap interval (replay plan PR 2.2).

One run's validation segment holds a dozen questions or so, too few to say
whether a Brier skill score is "clearly above 0". Plan §5 sets the bar on
the pooled figure instead (decided 2026-09-24, before any run; revised
2026-09-29, after the first acceptance run):

    the 4h headline Brier skill score and the 4h up-against-down skill
    score, each against the model's own prior, over the validation
    questions of every run, each run cut by the split pinned for it in
    the replay store, with the lower end of each one's 90%
    block-bootstrap interval above 0, read with blocks of six questions
    and only when the runs make at least five blocks. 24h is reported,
    not judged (its returns overlap from question to question); both
    skills against the train base rate are reported beside them.

The 2026-09-24 bar held the headline against the train base rate. The first
acceptance run met it (+0.065, interval [+0.042, +0.088]) with no direction
in it: the mean of the model's own train forecasts, given as one fixed answer
to every question, scored +0.076 against the same base rate, the model
scored -0.012 against that fixed answer, and its up-against-down skill was
below 0. A skill against the base rate credits a better fixed forecast as
much as a better forecast per question. Against the model's own prior
(:func:`~.probe_score.own_prior`), the model giving its usual answer to
every question scores exactly 0: only answers that move away from that
prior, question by question, and move the right way, score above it.

The definitions, once (the choices marked were decided 2026-09-24, or
2026-09-29 where they say so):

- **Each run is scored on its own terms.** Its questions are the headline
  forecasts of :func:`~.probe_score.headline_scores`, against that run's own
  flat band, train base rate and prior, so pooling adds up per-question
  Brier scores that were each measured the way a single run's report
  measures them; pooled over one run, each figure is that run's own
  figure. A run with no 4h train base rate, or fewer than
  :data:`~.probe_score.PRIOR_MIN_FORECASTS` valid 4h train forecasts to
  form its prior from (decided 2026-09-29), is refused by the command
  rather than left out quietly (decided): which runs count is the
  operator's call.
- **The pooled skill** is ``1 - sum(Brier) / sum(Brier of the reference)``
  over every pooled question, the reference being the train base rate or
  the model's own prior: questions weigh equally, whichever run they came
  from (decided). Each run's prior is its own, from its own train segment,
  so it never sees a validation question.
- **The interval** is a circular block bootstrap run within each run
  (decided): a run of ``n`` validation questions, in time order, is
  redrawn as ``ceil(n / block)`` blocks of ``block`` consecutive
  questions, each starting at a random question and wrapping from the
  run's last question to its first, cut back to ``n``. Every run keeps its
  own size in every draw, and no block crosses runs. Neighbouring 4h
  questions share a market regime, so resampling them one by one would
  pretend they are independent and draw too narrow an interval; the
  circle means every window of ``block`` questions can be drawn and no
  short leftover block is drawn as often as a full one. **Every line of a
  horizon is read off the same draws** (decided 2026-09-29): up against
  down takes the questions in each drawn sample that moved, so its blocks
  are the same blocks of consecutive questions, a day each, as the
  headline's. The 5th and 95th percentiles of a line's drawn skills
  (linear interpolation between order statistics) bound its 90% interval;
  a draw in which the line has no question, or whose reference Brier sums
  to 0, gives it no skill and is left out, and counted. The draws come
  from ``random.Random(seed)``, so the same inputs print the same
  intervals.
- **The bar is read** only with the default block size (:data:`BLOCK`)
  and when the runs make at least :data:`MIN_BLOCKS` blocks between them
  (``sum(ceil(n / block))``, decided): with one block every draw is the
  same sample and the interval collapses onto the point. Otherwise the
  verdict says why it is not judged. It says "not met" as soon as one
  judged skill's lower end is at or below 0, naming a skill with no
  interval beside it, and "cannot be judged" when a judged skill has no
  interval and none fails. The seed is printed with "met" and "not met".
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, TypeVar

from .probe import PROBE_KEYS
from .probe_score import QuestionScore

__all__ = [
    "BLOCK",
    "DRAWS",
    "JUDGED_KEY",
    "LEVEL",
    "MIN_BLOCKS",
    "Interval",
    "RunScores",
    "block_count",
    "bootstrap",
    "bootstrap_lines",
    "circular_draw",
    "describe_pool",
    "pooled_skill",
]

# Questions per block: six 4h questions are a day.
BLOCK: Final = 6

# The fewest blocks, over all runs, the bar is read with.
MIN_BLOCKS: Final = 5

# Bootstrap draws by default.
DRAWS: Final = 10_000

# The interval's coverage: the bar reads its lower end (plan §5).
LEVEL: Final = 0.90

# The horizon the bar is judged at (plan §5); the others are reported only.
JUDGED_KEY: Final = "h4"

# ``(model, reference)`` squared errors of one question.
Pair = tuple[float, float]

_T = TypeVar("_T")

# The report's lines at each horizon, in order: each line's name, and the
# pair it reads off a question (``None`` when the question has none there).
_LINES: Final[tuple[tuple[str, Callable[[QuestionScore], Pair | None]], ...]] = (
    ("headline against the train base rate", lambda s: (s.brier, s.base_brier)),
    ("headline against the model's own train prior", lambda s: s.own),
    ("up vs down given a move, against the train base rate", lambda s: s.binary),
    ("up vs down given a move, against the model's own train prior", lambda s: s.own_binary),
)

# The lines the bar judges at JUDGED_KEY, and the name the verdict gives each.
_JUDGED_LINES: Final = {
    "headline against the model's own train prior": "the headline",
    "up vs down given a move, against the model's own train prior": "up vs down",
}


@dataclass(frozen=True)
class RunScores:
    """One run's validation questions, scored, per probe key.

    ``scores[key]`` is ``None`` when the run has no train base rate at that
    horizon. No validation question is left out at the model cutoff: one
    that would be leaves every train question out with it, and a run with
    no prior is refused before it is pooled.
    """

    run_id: str
    pinned_at: str
    scores: Mapping[str, Sequence[QuestionScore] | None]


def pooled_skill(pairs: Sequence[Pair]) -> float | None:
    """``1 - sum(model) / sum(base)``; ``None`` with no pairs or a base that sums to 0."""
    base = math.fsum(b for _, b in pairs)
    if not pairs or base == 0:
        return None
    return 1 - math.fsum(m for m, _ in pairs) / base


def block_count(runs: Sequence[Sequence[object]], size: int) -> int:
    """How many blocks of ``size`` the runs are redrawn in: ``sum(ceil(n / size))``."""
    if size < 1:
        raise ValueError(f"a block holds at least one question, got {size}")
    return sum(math.ceil(len(run) / size) for run in runs)


def circular_draw(run: Sequence[_T], size: int, rng: random.Random) -> list[_T]:
    """One redraw of a run: ``ceil(n / size)`` wrapping blocks from random starts, cut to ``n``."""
    n = len(run)
    drawn: list[_T] = []
    for _ in range(math.ceil(n / size)):
        start = rng.randrange(n)
        drawn.extend(run[(start + step) % n] for step in range(size))
    return drawn[:n]


@dataclass(frozen=True)
class Interval:
    """A pooled skill, its interval, and what it was drawn from."""

    n: int
    blocks: int
    skill: float | None
    low: float | None
    high: float | None
    dropped: int  # draws with no skill (no question on the line, or a reference summing to 0)


def _quantile(ordered: Sequence[float], q: float) -> float:
    """The ``q`` quantile of sorted values, interpolated linearly between order statistics."""
    position = q * (len(ordered) - 1)
    below = math.floor(position)
    above = min(below + 1, len(ordered) - 1)
    return ordered[below] + (ordered[above] - ordered[below]) * (position - below)


def _pairs(items: Sequence[_T], line: Callable[[_T], Pair | None]) -> list[Pair]:
    return [pair for pair in map(line, items) if pair is not None]


def bootstrap_lines(
    runs: Sequence[Sequence[_T]],
    lines: Sequence[Callable[[_T], Pair | None]],
    *,
    block: int,
    draws: int,
    seed: int,
    level: float = LEVEL,
) -> list[Interval]:
    """Each line's pooled skill and ``level`` interval, every line read off the same draws.

    ``runs`` holds each run's questions in time order; each run is redrawn
    on its own circle, and each line reads its pairs off the drawn
    questions. An empty run adds nothing; the block count is the questions',
    whichever of them a line reads.
    """
    if draws < 1:
        raise ValueError(f"at least one draw, got {draws}")
    runs = [run for run in runs if run]
    blocks = block_count(runs, block)
    whole = [item for run in runs for item in run]
    found = [_pairs(whole, line) for line in lines]
    skills = [pooled_skill(pairs) for pairs in found]
    drawn: list[list[float]] = [[] for _ in lines]
    dropped = [0] * len(lines)
    if any(skill is not None for skill in skills):
        rng = random.Random(seed)
        for _ in range(draws):
            sample = [item for run in runs for item in circular_draw(run, block, rng)]
            for index, line in enumerate(lines):
                if skills[index] is None:
                    continue
                value = pooled_skill(_pairs(sample, line))
                if value is None:
                    dropped[index] += 1
                else:
                    drawn[index].append(value)
    tail = (1 - level) / 2
    intervals = []
    for pairs, skill, values, missed in zip(found, skills, drawn, dropped, strict=True):
        values.sort()
        low = _quantile(values, tail) if values else None
        high = _quantile(values, 1 - tail) if values else None
        intervals.append(Interval(len(pairs), blocks, skill, low, high, missed))
    return intervals


def bootstrap(
    runs: Sequence[Sequence[Pair]],
    *,
    block: int,
    draws: int,
    seed: int,
    level: float = LEVEL,
) -> Interval:
    """The pooled skill of ``runs`` and its ``level`` interval, each run redrawn on its circle.

    ``runs`` holds each run's pairs in time order; an empty run adds nothing.
    """
    [found] = bootstrap_lines(
        runs, [lambda pair: pair], block=block, draws=draws, seed=seed, level=level
    )
    return found


def _num(value: float | None, form: str) -> str:
    return "n/a" if value is None else form.format(value)


def _interval_text(found: Interval, draws: int) -> str:
    text = (
        f"n {found.n} in {found.blocks} block(s); skill {_num(found.skill, '{:+.3f}')}, "
        f"{LEVEL:.0%} interval [{_num(found.low, '{:+.3f}')}, {_num(found.high, '{:+.3f}')}]"
    )
    if found.dropped:
        text += f" ({found.dropped} of {draws} draws had no skill and were left out)"
    return text


def _verdict(
    judged: Sequence[tuple[str, Interval]], *, blocks: int, block: int, seed: int
) -> str:
    """Met when every judged interval's lower end is above 0, on enough blocks.

    One judged interval with its lower end at or below 0 is enough to say
    "not met", whatever the others can say; one with no interval is named
    beside it.
    """
    assert judged, "the bar judges at least one interval"
    if block != BLOCK:
        return f"not judged: the bar is read with blocks of {BLOCK}, this report used {block}"
    if blocks < MIN_BLOCKS:
        return f"cannot be judged: {blocks} block(s), fewer than the {MIN_BLOCKS} the bar needs"
    failing = [
        f"{name} lower end {found.low:+.3f}"
        for name, found in judged
        if found.low is not None and found.low <= 0
    ]
    unread = [f"{name} has no interval" for name, found in judged if found.low is None]
    if failing:
        also = "".join(f"; {reason}" for reason in unread)
        return "not met: " + ", ".join(failing) + also + f" (seed {seed})"
    if unread:
        return "cannot be judged: " + "; ".join(unread)
    return f"met (seed {seed})"


def describe_pool(
    runs: Sequence[RunScores], *, block: int = BLOCK, draws: int = DRAWS, seed: int = 0
) -> list[str]:
    """The pooled report: each run's share, then each horizon's four skills, then the bar."""
    lines = [
        f"pooled over {len(runs)} run(s), the validation segment of each run's pinned split; "
        f"circular blocks of {block} consecutive question(s) within each run, {draws} draws, "
        f"seed {seed}"
    ]
    # Per horizon: each run's questions, in time order.
    by_key: dict[str, list[list[QuestionScore]]] = {key: [] for key in PROBE_KEYS}
    for run in runs:
        shares = []
        for key in PROBE_KEYS:
            scored = run.scores.get(key)
            if scored is None:
                shares.append(f"{key} n/a (no train base rate)")
                continue
            stand_ins = sum(s.stand_in for s in scored)
            shares.append(
                f"{key} {len(scored)} question(s)"
                + (
                    f" ({stand_ins} standing in as the base rate, or as the prior against it)"
                    if stand_ins
                    else ""
                )
            )
            by_key[key].append(sorted(scored, key=lambda s: s.at_ms))
        lines.append(f"  run {run.run_id} (split pinned {run.pinned_at}): " + ", ".join(shares))
    judged: list[tuple[str, Interval]] = []
    judged_blocks = 0
    for key in PROBE_KEYS:
        intervals = bootstrap_lines(
            by_key[key], [read for _, read in _LINES], block=block, draws=draws, seed=seed
        )
        for (name, _), found in zip(_LINES, intervals, strict=True):
            if key != JUDGED_KEY:
                note = " (reported only: its returns overlap from question to question)"
            elif name in _JUDGED_LINES:
                judged.append((_JUDGED_LINES[name], found))
                judged_blocks = found.blocks
                note = ""
            else:
                note = " (reported only)"
            lines.append(f"{key} {name}: {_interval_text(found, draws)}{note}")
    lines.append(
        f"plan section 5 bar (revised 2026-09-29: the {JUDGED_KEY} headline skill and the "
        f"{JUDGED_KEY} up-vs-down skill, each against the model's own train prior, the lower end "
        f"of each {LEVEL:.0%} interval above 0, blocks of {BLOCK}, at least {MIN_BLOCKS} "
        "blocks): " + _verdict(judged, blocks=judged_blocks, block=block, seed=seed)
    )
    return lines
