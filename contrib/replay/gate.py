"""The controls, and the mother plan's §5 bar written as a program (replay plan PR 3a).

Two things, both pure functions of records:

- **Four controls**, each walked through the same :func:`~.account.simulate`
  as the candidate, so every figure compared is a figure of the same
  account under the same fill model (plan §3.3). ``buy_hold`` goes long at
  the run's margin cap at the first question and holds; ``flat`` never
  trades; ``research_bias`` takes the radar's side at the cap whenever the
  side changes (a question with no bias holds); ``paper`` is the incumbent
  — the paper trader's own recorded answers, every approved target an
  order was created for. The first three are the scorecard's baselines
  (:mod:`.score`), re-stated as paths; the fourth is the "current variant"
  of §5.
- **The bar** (:func:`judge`): a candidate wins on the validation segment
  when, at once, (1) its net Sharpe is above every control's, (2) above
  ``Penalty.threshold(n)`` for ``n`` variants tried in the store, (3) its
  model reading beats the paper trader's question by question (McNemar's
  exact p below :data:`ALPHA`, the candidate ahead) over at least
  :data:`POOLED_MIN_QUESTIONS` paired validation questions pooled across
  runs (decided 2026-09-24: validation pools, the holdout stays per run),
  and (4) its fail-closed rate is not above the paper trader's. All four,
  or it is not eligible for a look at the holdout. Each check prints its
  inputs beside its verdict, so a reader can re-derive it.

The Sharpe here is the scorecard's own definition over the ACCOUNT's
per-decision returns (:mod:`.account`), annualised by the bars in a year
as a one-bar horizon is; pooled across runs by concatenating each run's
validation returns in run order. Checks (3) and (4) read a scorecard's
validation rows through :class:`Readings`, one per run.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from .account import Decision, Path, Target
from .paper_store import InputFacts
from .score import HORIZONS, Answer, Question, Scorecard, paired_hits, rate, segment_of, sharpe
from .upstream import Penalty, SegmentName, Split, TargetSide

__all__ = [
    "ALPHA",
    "CONTROLS",
    "POOLED_MIN_QUESTIONS",
    "Check",
    "Readings",
    "Verdict",
    "answer_targets",
    "control_targets",
    "decisions",
    "describe_paths",
    "judge",
    "pooled_sharpe",
]

# The controls, in report order; ``paper`` is the incumbent of plan §5.
CONTROLS: Final = ("buy_hold", "flat", "research_bias", "paper")
# Plan §5 (3): the paired comparison needs this many validation questions
# pooled across runs, and this significance.
POOLED_MIN_QUESTIONS: Final = 100
ALPHA: Final = 0.05
# The horizon the paired hits are read at: one bar, the scorecard's first.
_PAIRED_HORIZON: Final = HORIZONS[0]
# The research ledger's own promote penalty, at its defaults.
_PENALTY: Final = Penalty()

# What each question asks of the account, in the question's order.
Targets = Sequence[Target | None]


def _cap(question: Question) -> Decimal:
    return Decimal(str(question.max_margin_pct))


def answer_targets(
    questions: Sequence[Question], answers: Mapping[str, Answer]
) -> list[Target | None]:
    """The account's ask per question, from a set of answers: the approved target an order was created for.

    A maintain, a rejection, a fail-closed round, an unanswered question and
    an approved target inside the deadband all ask nothing (``None``); a
    guardrail block that created an order is a flat target (its approved
    margin is pinned at zero).
    """
    asks: list[Target | None] = []
    for question in questions:
        answer = answers.get(question.input_id)
        if answer is None or not answer.order_created:
            asks.append(None)
            continue
        assert answer.approved_margin_pct is not None and answer.target_side is not None
        if answer.approved_margin_pct == 0:
            asks.append(Target(TargetSide.FLAT, Decimal(0)))
        else:
            asks.append(Target(answer.target_side, Decimal(str(answer.approved_margin_pct))))
    return asks


def control_targets(name: str, questions: Sequence[Question]) -> list[Target | None]:
    """The asks of one of the three price controls (``paper`` takes :func:`answer_targets`)."""
    if name == "flat":
        return [None] * len(questions)
    asks: list[Target | None] = []
    last: Target | None = None
    for question in questions:
        if name == "buy_hold":
            wanted: Target | None = Target(TargetSide.LONG, _cap(question))
        elif name == "research_bias":
            bias = question.research_bias
            if bias is None:
                asks.append(None)
                continue
            wanted = (
                Target(TargetSide.FLAT, Decimal(0))
                if bias is TargetSide.FLAT
                else Target(bias, _cap(question))
            )
        else:
            raise ValueError(f"no control named {name!r}; the controls are {', '.join(CONTROLS)}")
        # Re-asked only when the ask changes: the account re-sizes a held
        # target to equity only through a new ask, as the paper trader
        # does through its deadband.
        asks.append(None if wanted == last else wanted)
        last = wanted
    return asks


def decisions(
    questions: Sequence[Question],
    inputs: Mapping[str, InputFacts],
    targets: Targets,
    *,
    split: Split | None,
    step_ms: int,
    holdout: bool = False,
) -> list[Decision]:
    """The account's input: each question with its exact mark and leverage and its ask.

    The holdout's questions are left out unless ``holdout`` says to walk
    them (the lock the scorecard keeps); the account then stops at the
    validation segment's last question.
    """
    if len(targets) != len(questions):
        raise ValueError(f"{len(targets)} asks for {len(questions)} questions")
    walked: list[Decision] = []
    for question, target in zip(questions, targets, strict=True):
        segment = segment_of(split, question.at_ms, step_ms)
        if segment is SegmentName.HOLDOUT and not holdout:
            continue
        facts = inputs[question.input_id]
        walked.append(
            Decision(
                input_id=question.input_id,
                at_ms=question.at_ms,
                segment=segment,
                mark=facts.mark,
                leverage=facts.leverage,
                target=target,
            )
        )
    return walked


def pooled_sharpe(
    paths: Iterable[Path],
    segment: SegmentName | None,
    bars_per_year: float,
    *,
    only: Collection[str] | None = None,
) -> tuple[float, int]:
    """``(sharpe, n)`` over the paths' returns in ``segment``, concatenated in path order.

    ``only`` names the questions whose returns count (the bar's cutoff
    scope, plan §6); every path is filtered by the same set.
    """
    values = [r for path in paths for r in path.returns(segment, only=only)]
    return sharpe(values, bars_per_year), len(values)


def describe_paths(
    name: str, paths: Mapping[str, Path], *, bars_per_year: float, holdout: bool
) -> list[str]:
    """One driver's paths: each run's two lines, then the pooled Sharpe per segment."""
    lines = [f"-- {name} --"]
    for run_id, path in paths.items():
        lines.append(f"  run {run_id}:")
        lines.extend(f"  {line}" for line in path.describe())
    segments = [SegmentName.TRAIN, SegmentName.VALIDATION]
    if holdout:
        segments.append(SegmentName.HOLDOUT)
    pooled = []
    for segment in segments:
        value, n = pooled_sharpe(paths.values(), segment, bars_per_year)
        pooled.append(f"{segment.value} sharpe {value:.2f} (n {n})")
    lines.append("  pooled: " + "; ".join(pooled))
    return lines


@dataclass(frozen=True)
class Readings:
    """What checks (3) and (4) read off one run's scorecard: its validation rows, reduced.

    ``hits`` is the model's reading at the paired horizon by question (an
    unanswered or fail-closed row reads ``None``); ``fail_closed`` and
    ``answered`` count the validation rows that carried an answer.
    """

    hits: Mapping[str, bool | None]
    fail_closed: int
    answered: int

    @classmethod
    def of(cls, card: Scorecard) -> Readings:
        rows = [row for row in card.rows if row.segment is SegmentName.VALIDATION]
        return cls(
            hits={row.question.input_id: row.outcomes[_PAIRED_HORIZON].ai_hit for row in rows},
            fail_closed=sum(1 for row in rows if row.fail_closed),
            answered=sum(1 for row in rows if row.answer is not None),
        )


def _paired(
    mine: Sequence[Readings], theirs: Sequence[Readings]
) -> tuple[list[bool | None], list[bool | None]]:
    """The two readings on the validation questions both hold, run by run, in question order."""
    a: list[bool | None] = []
    b: list[bool | None] = []
    for ours, other in zip(mine, theirs, strict=True):
        for input_id in sorted(set(ours.hits) & set(other.hits)):
            a.append(ours.hits[input_id])
            b.append(other.hits[input_id])
    return a, b


@dataclass(frozen=True)
class Check:
    """One of the four: what it compared, and whether it passed."""

    number: int
    name: str
    passed: bool
    detail: str

    def describe(self) -> str:
        return (
            f"  ({self.number}) {self.name}: {self.detail} -> {'pass' if self.passed else 'FAIL'}"
        )


@dataclass(frozen=True)
class Verdict:
    """The four checks on one candidate, and the one word they add up to."""

    candidate: str
    checks: tuple[Check, ...]

    @property
    def eligible(self) -> bool:
        return all(check.passed for check in self.checks)

    def describe(self) -> list[str]:
        lines = [f"-- plan section 5 bar: {self.candidate} on the validation segment --"]
        lines.extend(check.describe() for check in self.checks)
        lines.append(
            f"  verdict: {self.candidate} "
            + (
                "passes all four -> eligible for --holdout (one look, recorded in the ledger)"
                if self.eligible
                else "does not pass; the holdout stays locked"
            )
        )
        return lines


def judge(
    *,
    candidate: str,
    candidate_paths: Sequence[Path],
    control_paths: Mapping[str, Sequence[Path]],
    trials: int,
    candidate_readings: Sequence[Readings],
    paper_readings: Sequence[Readings],
    bars_per_year: float,
    eligible: Collection[str] | None = None,
) -> Verdict:
    """The four checks of plan §5 on the validation segment (module docstring).

    ``candidate_readings`` and ``paper_readings`` are the two sides' cards
    reduced by :class:`Readings`, one per run in the same run order;
    ``trials`` is how many variants the store has tried (a repeat each), at
    least one. ``eligible`` names the questions past the model cutoff (plan
    §6): the Sharpe of the candidate AND of every control is pooled over
    those alone, so checks (1) and (2) leave out what (3) and (4) leave out.
    """

    def pooled(paths: Sequence[Path]) -> tuple[float, int]:
        return pooled_sharpe(paths, SegmentName.VALIDATION, bars_per_year, only=eligible)

    mine, n_mine = pooled(candidate_paths)
    theirs = {name: pooled(paths) for name, paths in control_paths.items()}
    one = Check(
        1,
        "net sharpe above every control",
        all(mine > value for value, _ in theirs.values()),
        f"{candidate} {mine:.2f} (n {n_mine}) vs "
        + ", ".join(f"{name} {value:.2f} (n {n})" for name, (value, n) in theirs.items()),
    )
    bar = _PENALTY.threshold(trials)
    two = Check(
        2,
        "above the promote threshold",
        mine > bar,
        f"{mine:.2f} vs {bar:.2f} ({_PENALTY.sharpe_base:g} + {_PENALTY.k:g} x ln {trials}; "
        f"{trials} variant(s) tried in the store)",
    )
    paired = paired_hits(*_paired(candidate_readings, paper_readings))
    three = Check(
        3,
        "paired hits beat paper",
        paired.n >= POOLED_MIN_QUESTIONS
        and paired.a_only > paired.b_only
        and paired.p_value < ALPHA,
        f"n {paired.n} (needs >= {POOLED_MIN_QUESTIONS}), {candidate} only {paired.a_only}, "
        f"paper only {paired.b_only}, McNemar p {paired.p_value:.3f} (needs < {ALPHA})",
    )
    failed = sum(r.fail_closed for r in candidate_readings)
    answered = sum(r.answered for r in candidate_readings)
    paper_failed = sum(r.fail_closed for r in paper_readings)
    paper_answered = sum(r.answered for r in paper_readings)
    comparable = answered > 0 and paper_answered > 0
    four = Check(
        4,
        "fail-closed rate not above paper",
        comparable and failed / answered <= paper_failed / paper_answered,
        f"{candidate} {rate(failed, answered)} vs paper {rate(paper_failed, paper_answered)}"
        + ("" if comparable else "; no answered validation question to read a rate from"),
    )
    return Verdict(candidate, (one, two, three, four))
