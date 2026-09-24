"""The direction probe pooled across runs, with a block-bootstrap interval (replay plan PR 2.2).

One run's validation segment holds a dozen questions or so, too few to say
whether a Brier skill score is "clearly above 0". Plan §5 (decided
2026-09-24, before any run) sets the bar on the pooled figure instead:

    the 4h headline Brier skill score over the validation questions of
    every run, each run cut by the split pinned for it in the replay
    store, with the lower end of a 90% block-bootstrap interval above 0,
    read with blocks of six questions and only when the runs make at
    least five blocks. 24h is reported, not judged (its returns overlap
    from question to question); up against down given a move is reported
    beside it.

The definitions, once (the choices marked were decided 2026-09-24):

- **Each run is scored on its own terms.** Its questions are the headline
  forecasts of :func:`~.probe_score.headline_scores`, against that run's own
  flat band and train base rate, so pooling adds up per-question Brier
  scores that were each measured the way a single run's report measures
  them; pooled over one run, the figure is that run's headline figure. A
  run with no 4h train base rate is refused by the command rather than
  left out quietly (decided): which runs count is the operator's call.
- **The pooled skill** is ``1 - sum(Brier) / sum(Brier of the base rate)``
  over every pooled question: questions weigh equally, whichever run they
  came from (decided).
- **The interval** is a circular block bootstrap run within each run
  (decided): a run of ``n`` validation questions, in time order, is
  redrawn as ``ceil(n / block)`` blocks of ``block`` consecutive
  questions, each starting at a random question and wrapping from the
  run's last question to its first, cut back to ``n``. Every run keeps its
  own size in every draw, and no block crosses runs. Neighbouring 4h
  questions share a market regime, so resampling them one by one would
  pretend they are independent and draw too narrow an interval; the
  circle means every window of ``block`` questions can be drawn and no
  short leftover block is drawn as often as a full one. The 5th and 95th
  percentiles of the drawn skills (linear interpolation between order
  statistics) bound the 90% interval; a draw whose base-rate Brier sums
  to 0 has no skill and is left out, and counted. The draws come from
  ``random.Random(seed)``, so the same inputs print the same interval.
- **The bar is read** only with the default block size (:data:`BLOCK`)
  and when the runs make at least :data:`MIN_BLOCKS` blocks between them
  (``sum(ceil(n / block))``, decided): with one block every draw is the
  same sample and the interval collapses onto the point. Otherwise the
  line says why it is not judged. The seed is printed on the line.
"""

from __future__ import annotations

import math
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

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

# ``(model, base)`` squared errors of one question.
Pair = tuple[float, float]


@dataclass(frozen=True)
class RunScores:
    """One run's validation questions, scored, per probe key.

    ``scores[key]`` is ``None`` when the run has no train base rate at that
    horizon. ``left_out`` counts the run's validation questions decided on
    or before the model cutoff, which were left out of it.
    """

    run_id: str
    pinned_at: str
    scores: Mapping[str, Sequence[QuestionScore] | None]
    left_out: int = 0


def pooled_skill(pairs: Sequence[Pair]) -> float | None:
    """``1 - sum(model) / sum(base)``; ``None`` with no pairs or a base that sums to 0."""
    base = math.fsum(b for _, b in pairs)
    if not pairs or base == 0:
        return None
    return 1 - math.fsum(m for m, _ in pairs) / base


def block_count(runs: Sequence[Sequence[Pair]], size: int) -> int:
    """How many blocks of ``size`` the runs are redrawn in: ``sum(ceil(n / size))``."""
    if size < 1:
        raise ValueError(f"a block holds at least one question, got {size}")
    return sum(math.ceil(len(run) / size) for run in runs)


def circular_draw(run: Sequence[Pair], size: int, rng: random.Random) -> list[Pair]:
    """One redraw of a run: ``ceil(n / size)`` wrapping blocks from random starts, cut to ``n``."""
    n = len(run)
    drawn: list[Pair] = []
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
    dropped: int  # draws with no skill (their base-rate Brier summed to 0)


def _quantile(ordered: Sequence[float], q: float) -> float:
    """The ``q`` quantile of sorted values, interpolated linearly between order statistics."""
    position = q * (len(ordered) - 1)
    below = math.floor(position)
    above = min(below + 1, len(ordered) - 1)
    return ordered[below] + (ordered[above] - ordered[below]) * (position - below)


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
    if draws < 1:
        raise ValueError(f"at least one draw, got {draws}")
    runs = [run for run in runs if run]
    blocks = block_count(runs, block)
    pooled = [pair for run in runs for pair in run]
    skill = pooled_skill(pooled)
    if skill is None:
        return Interval(len(pooled), blocks, None, None, None, 0)
    rng = random.Random(seed)
    drawn: list[float] = []
    dropped = 0
    for _ in range(draws):
        sample = [pair for run in runs for pair in circular_draw(run, block, rng)]
        value = pooled_skill(sample)
        if value is None:
            dropped += 1
        else:
            drawn.append(value)
    if not drawn:
        return Interval(len(pooled), blocks, skill, None, None, dropped)
    drawn.sort()
    tail = (1 - level) / 2
    return Interval(
        len(pooled), blocks, skill, _quantile(drawn, tail), _quantile(drawn, 1 - tail), dropped
    )


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


def _verdict(judged: Interval, *, block: int, seed: int) -> str:
    if block != BLOCK:
        return f"not judged: the bar is read with blocks of {BLOCK}, this report used {block}"
    if judged.blocks < MIN_BLOCKS:
        return (
            f"cannot be judged: {judged.blocks} block(s), fewer than the {MIN_BLOCKS} the bar needs"
        )
    if judged.low is None:
        return "cannot be judged (no interval)"
    return ("met" if judged.low > 0 else "not met") + f" (seed {seed})"


def describe_pool(
    runs: Sequence[RunScores], *, block: int = BLOCK, draws: int = DRAWS, seed: int = 0
) -> list[str]:
    """The pooled report: each run's share, then each horizon's skill and interval, then the bar."""
    lines = [
        f"pooled over {len(runs)} run(s), the validation segment of each run's pinned split; "
        f"circular blocks of {block} consecutive question(s) within each run, {draws} draws, "
        f"seed {seed}"
    ]
    by_key: dict[str, list[list[Pair]]] = {key: [] for key in PROBE_KEYS}
    binary_by_key: dict[str, list[list[Pair]]] = {key: [] for key in PROBE_KEYS}
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
                + (f" ({stand_ins} standing in as the base rate)" if stand_ins else "")
            )
            ordered = sorted(scored, key=lambda s: s.at_ms)
            by_key[key].append([(s.brier, s.base_brier) for s in ordered])
            binary_by_key[key].append([s.binary for s in ordered if s.binary is not None])
        cutoff = f"; {run.left_out} left out at the model cutoff" if run.left_out else ""
        lines.append(
            f"  run {run.run_id} (split pinned {run.pinned_at}): " + ", ".join(shares) + cutoff
        )
    judged: Interval | None = None
    for key in PROBE_KEYS:
        found = bootstrap(by_key[key], block=block, draws=draws, seed=seed)
        if key == JUDGED_KEY:
            judged = found
            note = ""
        else:
            note = " (reported only: its returns overlap from question to question)"
        lines.append(f"{key} headline: {_interval_text(found, draws)}{note}")
        moved = bootstrap(binary_by_key[key], block=block, draws=draws, seed=seed)
        lines.append(
            f"{key} up vs down given a move: {_interval_text(moved, draws)} (reported only)"
        )
    assert judged is not None
    lines.append(
        f"plan section 5 bar ({JUDGED_KEY} headline skill, lower end of the {LEVEL:.0%} interval "
        f"above 0, blocks of {BLOCK}, at least {MIN_BLOCKS} blocks): "
        + _verdict(judged, block=block, seed=seed)
    )
    return lines
