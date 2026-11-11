"""Whether one more section of text moves the decision (discovery plan §4.7, experiment B).

The paper trader was shown a research section for six runs and the
scorecard could not say whether it read it: the rule behind the section
barely changed side, so "the model agreed with the rule" and "the model
would have done that anyway" looked the same (``score``'s ``research
effect`` lines). This module reads the other experiment, the one a replay
can run and a live run cannot: the SAME recorded questions put to variants
that differ only in their ``extra_context`` (none, a neutral text of the
same length, the section saying long, the section saying short), and the
answers compared question by question.

Three things are read, as pure functions of records; the command opens the
stores and prints:

- **Exposure** needs no model: a variant's extra context is in the message
  or it is not. The report says how long each variant's is and how it
  opens, and names everything else the two variants differ in, because a
  shift between two variants is the extra context's only when nothing else
  differs.
- **Uptake**: how many answers mention the section, as a case-insensitive
  search of the stored answer text for the phrases given (:data:`MENTION`
  by default). The same count on the other variant is what it is read
  against: a variant never shown the section should mention it nowhere,
  and one that does says the phrases are too loose. The phrases name the
  section, not the word
  "research" alone: the shipped system prompt tells every variant that the
  "research debate" is absent, and an answer saying so mentions no section.
- **Decision effect**: on each question both variants answered, whether the
  model's reading moved. The reading is the scorecard's own
  (:func:`~.score.score_run`): the side the model asked for at the margin
  it asked for, the side held for a maintain, nothing for a fail-closed
  round; as a signed multiple of equity, so a larger long counts as a move
  toward long as much as a flat turned long does. The executed reading
  (what the gate let through) and the confidence are read beside it.

The definitions, once:

- **A pair** is one question answered by both variants on the same repeat.
  Repeats are exchangeable, so pairing repeat 0 with repeat 0 is a
  convention, not a match; the per-repeat blocks show how much the count
  moves between repeats.
- **Toward long / toward short / unchanged** is the sign of the variant's
  exposure less the other's on a pair; a difference within
  :data:`TOLERANCE` is unchanged, so two means of the same answers taken
  over different numbers of repeats do not read as a move. The p-value is
  the scorecard's exact two-sided sign test on the two moved counts.
  Neighbouring 4h questions share a market regime, so the pairs are not
  independent draws and the p-value is too small by an unknown amount; it
  orders results, it is not a bar.
- **Per question** each variant's readable repeats are averaged first, so
  a question counts once however many times it was asked. The interval of
  the mean shift is the pool's circular block bootstrap
  (:func:`~.pool.circular_draw`): each run redrawn on its own circle in
  blocks of consecutive questions, which is what the sign test's p-value
  lacks. Each run's own count of questions is printed under the pooled
  figures: a run both variants answered on no question in common adds
  nothing to them, and nothing else would say so.
- **How far apart two answers are** is read three times with one
  yardstick (the share of pairs whose side differs, and the mean absolute
  exposure difference): between two repeats of the variant, between two
  repeats of the other, and between the two variants on the same repeat.
  The first two are read on the questions the per-question shift is read
  on, so that a question one variant alone answered cannot move the spread
  the shift is held against. A shift between variants that is no larger
  than the spread between two repeats of one variant is noise.
- **The verdict** (the bar, decided 2026-11-11, before any answer was
  bought) is read off the per-question mean shift, and says the extra
  context moves the decision only when both hold: the shift's interval
  does not hold 0, and the shift is larger in size than the spread between
  two repeats of EITHER variant (its mean absolute exposure difference).
  The second is a bar on size, not on evidence: a shift smaller than what
  one variant does between two askings of one question is not worth
  wiring a section for, however many questions agree on its sign.
  Otherwise the verdict is "no measurable effect", naming the first of
  the two that failed. It is not judged at all when the two variants
  do not differ in their extra context alone (the sentence would credit the
  extra context with a shift a model could have made, or with noise); when no question has
  a readable claim from both variants; when a variant has no question with
  a readable claim on two of its repeats to take a spread from; with a
  block size other than the pool's (:data:`~.pool.BLOCK`: a block of one
  question would draw neighbours as if they were independent); or on
  fewer blocks than the pool reads an interval with
  (:data:`~.pool.MIN_BLOCKS`: an interval drawn from a few blocks
  collapses onto the point).

No later price is read, so a variant's ``model_cutoff`` plays no part. The
holdout is never read: the rows come through the scorecard's own lock,
which keeps one set of rows for every report on a run.

What this cannot show: these are one-completion answers to a message that
is the context alone, with the section after the position block (an extra
context follows the whole recorded context), not before it where the
daemon renders it. The shipped section is dated to the context's own
close, the freshest the daemon's could be, so as far as freshness goes a
shift is an upper bound on what a section up to two research bars old
would do. The shipped system
prompt tells the model that "the research debate" is not in the message
before the section is shown to it, so "no measurable effect" may be that
sentence discounting the section. A shift says the model is sensitive to
the text. It does not say the section works as wired into the paper
trader's prompt.
"""

from __future__ import annotations

import random
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from itertools import combinations
from statistics import fmean
from typing import Final

from .compare import Table
from .pool import BLOCK, LEVEL, MIN_BLOCKS, block_count, circular_draw, quantile
from .score import (
    FAIL_CLOSED,
    Question,
    Scorecard,
    Scored,
    ScoreError,
    has_research_section,
    rate,
    sign_test,
)
from .upstream import TargetSide, from_epoch_ms
from .variant import Variant

__all__ = [
    "EFFECT",
    "MENTION",
    "NO_EFFECT",
    "TOLERANCE",
    "UNJUDGED",
    "Arm",
    "ArmRun",
    "Effect",
    "Reading",
    "already_carried",
    "arm_run",
    "describe_effect",
    "differences",
    "mentions",
    "read_rows",
    "shift_interval",
    "verdict",
]

# How a verdict opens (module docstring); what follows says which way, which
# half of the bar failed, or why nothing is judged.
EFFECT: Final = "the extra context moves the decision"
NO_EFFECT: Final = "no measurable effect"
UNJUDGED: Final = "cannot be judged"

# What an answer is searched for to count as mentioning the research section:
# the section's own title, the phrase an answer is likely to use for the rule
# it reports, the name the section gives its source, and that rule's family
# id. An answer that leans on the section under other words is missed, so
# the count is a lower bound.
MENTION: Final = ("research signal", "research rule", "research radar", "btc-4h-maker")

# Exposures closer than this are the same exposure. They are margins in
# whole percent times a leverage, so real differences are far above it and
# what falls under it is float rounding (a mean of three equal answers
# against one of them).
TOLERANCE: Final = 1e-9

_SIDES: Final = (TargetSide.LONG, TargetSide.SHORT, TargetSide.FLAT)


def mentions(text: str, phrases: Iterable[str]) -> bool:
    """Whether ``text`` holds any of ``phrases``, whatever the case."""
    folded = text.casefold()
    return any(phrase.casefold() in folded for phrase in phrases)


def differences(a: Variant, b: Variant) -> list[str]:
    """The parts of what reaches the model that ``a`` and ``b`` differ in, by name.

    Read off :attr:`~.variant.Variant.identity`, the mapping the sha is a
    digest of, so a part added to a variant's identity is a part this names.
    """
    mine, theirs = a.identity, b.identity
    return [
        key.replace("_", " ") for key in {**mine, **theirs} if mine.get(key) != theirs.get(key)
    ]


def already_carried(run_id: str, questions: Sequence[Question]) -> str | None:
    """A note when ``run_id``'s recorded contexts already held a research section, else ``None``.

    ``questions`` are the ones the report reads (train and validation). An
    extra context put to such a question is a second section beside the
    recorded one, which is not the experiment the report is written for (one
    section against none): said, not refused, since the report reads any
    extra context.
    """
    carried = sum(1 for question in questions if has_research_section(question))
    if not carried:
        return None
    return (
        f"note: {carried} of {len(questions)} question(s) of {run_id} were recorded with a "
        "research section already in the context; an extra context is one more section beside it"
    )


@dataclass(frozen=True)
class Reading:
    """One stored answer as the scorecard reads it, and whether its text mentions the phrases.

    ``side`` and ``exposure`` are the model reading (``None`` together on a
    fail-closed round, which claims nothing); ``executed_exposure`` is what
    the gate left the book at. Exposures are signed multiples of equity.
    """

    input_id: str
    at_ms: int
    side: TargetSide | None
    exposure: float | None
    executed_exposure: float
    confidence: float | None
    risk_action: str
    mentioned: bool

    def __post_init__(self) -> None:
        if (self.side is None) != (self.exposure is None):
            raise ScoreError(
                f"{self.input_id}: a reading claims a side at an exposure, or neither (got "
                f"side {self.side!r}, exposure {self.exposure!r})"
            )


def read_rows(
    rows: Iterable[Scored], responses: Mapping[str, str], phrases: Sequence[str]
) -> tuple[dict[str, Reading], int]:
    """One repeat's scored rows as readings by question, and how many the provider refused.

    ``responses`` holds the stored answer text of every answered row, by
    ``input_id``. A refused question has a row and no answer: it is counted
    and pairs with nothing.
    """
    found: dict[str, Reading] = {}
    refused = 0
    for row in rows:
        if row.answer is None:
            refused += 1
            continue
        question = row.question
        found[question.input_id] = Reading(
            input_id=question.input_id,
            at_ms=question.at_ms,
            side=row.ai_side,
            exposure=row.ai_exposure,
            executed_exposure=row.executed_exposure,
            confidence=row.answer.confidence,
            risk_action=row.answer.risk_action.value,
            mentioned=mentions(responses[question.input_id], phrases),
        )
    return found, refused


@dataclass(frozen=True)
class ArmRun:
    """One variant's answers on one run: its readings by repeat, then by question.

    ``refused`` counts the questions read that the provider refused;
    ``unread`` the stored answers and recorded refusals the report does not
    read, because their questions are outside the run's train and
    validation segments: the holdout.
    """

    run_id: str
    repeats: Mapping[int, Mapping[str, Reading]]
    refused: int = 0
    unread: int = 0


def arm_run(
    run_id: str,
    cards: Mapping[int, Scorecard],
    responses: Mapping[int, Mapping[str, str]],
    phrases: Sequence[str],
    *,
    refusals: int = 0,
) -> ArmRun:
    """One variant's answers on one run, from its card per repeat and what it said per repeat.

    ``responses`` holds every stored answer's text and ``refusals`` counts
    every refusal recorded for the variant on the run; the ones no card has
    a row for are what the lock leaves unread, and are counted.
    """
    repeats = {}
    refused = 0
    for repeat, card in cards.items():
        found, lost = read_rows(card.rows, responses.get(repeat, {}), phrases)
        refused += lost
        if found:
            # A repeat with nothing read (asked the holdout only, or refused
            # throughout) is not a repeat the report has: counting it would
            # print "2 repeat(s)" over one repeat's answers.
            repeats[repeat] = found
    recorded = sum(len(texts) for texts in responses.values()) + refusals
    read = sum(len(found) for found in repeats.values()) + refused
    return ArmRun(run_id, repeats, refused, recorded - read)


@dataclass(frozen=True)
class Arm:
    """One variant and its answers on every run compared, the runs in the order given.

    ``phrases`` is what the answers were searched for (the readings'
    ``mentioned``), kept beside them so a count is never printed under
    phrases it was not searched with.
    """

    variant: Variant
    runs: Sequence[ArmRun]
    phrases: Sequence[str]

    def __post_init__(self) -> None:
        # A bare string is a sequence of its characters, and a search for
        # single letters matches every answer.
        phrases = () if isinstance(self.phrases, str) else tuple(self.phrases)
        if not phrases or not all(isinstance(phrase, str) and phrase.strip() for phrase in phrases):
            raise ScoreError(
                f"the phrases an answer is searched for are non-blank texts, got {self.phrases!r}"
            )
        object.__setattr__(self, "phrases", phrases)

    @property
    def name(self) -> str:
        return self.variant.name

    @property
    def repeats(self) -> list[int]:
        return sorted({repeat for run in self.runs for repeat in run.repeats})

    def readings(self) -> list[Reading]:
        return [
            reading
            for run in self.runs
            for by_question in run.repeats.values()
            for reading in by_question.values()
        ]


@dataclass(frozen=True)
class Effect:
    """The report: ``preamble`` (who is compared, and what differs), then ``body``, and one CSV row per pair."""

    preamble: list[str]
    body: list[str]
    table: Table


_Pair = tuple[Reading, Reading]


def _paired(mine: Mapping[str, Reading], theirs: Mapping[str, Reading]) -> list[_Pair]:
    """The questions both hold a reading for, in time order."""
    common = sorted(set(mine) & set(theirs), key=lambda input_id: mine[input_id].at_ms)
    return [(mine[input_id], theirs[input_id]) for input_id in common]


def _exposures(pairs: Iterable[_Pair]) -> list[tuple[float, float]]:
    """The two exposures of each pair in which both answers claim a side."""
    return [
        (a.exposure, b.exposure)
        for a, b in pairs
        if a.exposure is not None and b.exposure is not None
    ]


def _moves(shifts: Sequence[float]) -> str:
    """``toward long N, toward short N, unchanged N (sign test p ...)`` over the shifts."""
    up = sum(1 for shift in shifts if shift > TOLERANCE)
    down = sum(1 for shift in shifts if shift < -TOLERANCE)
    return (
        f"toward long {up}, toward short {down}, unchanged {len(shifts) - up - down} "
        f"(sign test p {sign_test(up, down):.3f})"
    )


def _signed(value: float) -> str:
    """``value`` to three places with its sign; what rounds to zero prints as ``+0.000``."""
    return f"{round(value, 3) + 0.0:+.3f}"


def shift_interval(
    runs: Sequence[Sequence[float]], *, block: int, draws: int, seed: int
) -> tuple[float, float] | None:
    """The :data:`~.pool.LEVEL` interval of the mean shift over ``runs``; ``None`` with no shift.

    ``runs`` holds each run's per-question shifts in time order. Each run
    is redrawn on its own circle (:func:`~.pool.circular_draw`), every draw
    the size of the runs together, and the interval is read off the drawn
    means as the pool reads its own.
    """
    if draws < 1:
        raise ValueError(f"at least one draw, got {draws}")
    runs = [run for run in runs if run]
    if not runs:
        return None
    rng = random.Random(seed)
    means = sorted(
        fmean(shift for run in runs for shift in circular_draw(run, block, rng))
        for _ in range(draws)
    )
    tail = (1 - LEVEL) / 2
    return quantile(means, tail), quantile(means, 1 - tail)


def _question_means(run: ArmRun) -> dict[str, float]:
    """Per question, in time order, the mean exposure of its readable repeats."""
    exposures: dict[str, list[float]] = {}
    decided: dict[str, int] = {}
    for by_question in run.repeats.values():
        for reading in by_question.values():
            if reading.exposure is not None:
                exposures.setdefault(reading.input_id, []).append(reading.exposure)
                decided[reading.input_id] = reading.at_ms
    return {
        input_id: fmean(exposures[input_id]) for input_id in sorted(decided, key=decided.__getitem__)
    }


def _context_line(variant: Variant) -> str:
    text = variant.extra_context
    if text is None:
        return f"extra context of {variant.name}: none"
    lines = text.splitlines()
    return (
        f"extra context of {variant.name}: {len(text)} character(s) in {len(lines)} line(s), "
        f"opening {lines[0][:60]!r}"
    )


def _answers_line(arm: Arm) -> str:
    readings = arm.readings()
    sides = Counter(reading.side for reading in readings)
    said = sum(1 for reading in readings if reading.mentioned)
    searched = " or ".join(repr(phrase) for phrase in arm.phrases)
    unread = sum(run.unread for run in arm.runs)
    return (
        f"  {arm.name}: {len(readings)} answer(s) in {len(arm.repeats)} repeat(s): "
        + ", ".join(f"{side.value} {sides[side]}" for side in _SIDES)
        + f", fail-closed {sides[None]}; {sum(run.refused for run in arm.runs)} refused by the "
        "provider; "
        f"mentioning {searched}: {rate(said, len(readings))}"
        + (
            f"; {unread} more answer(s) or refusal(s) recorded outside train and validation, "
            "not read"
            if unread
            else ""
        )
    )


def _repeat_lines(
    repeat: int, pairs: Sequence[_Pair], name: str, other: str, absent: Sequence[str]
) -> list[str]:
    """One repeat's block; ``absent`` names the runs it could not be paired on, under the title."""
    exposures = _exposures(pairs)
    lines = [
        f"-- repeat {repeat}: {len(pairs)} question(s) answered by both, {len(exposures)} with a "
        "readable claim from both --",
        *absent,
    ]
    if exposures:
        shifts = [mine - theirs for mine, theirs in exposures]
        lines.append(f"  model reading: {_moves(shifts)}; mean shift {_signed(fmean(shifts))}")
        table = Counter(
            (theirs.side, mine.side)
            for mine, theirs in pairs
            if mine.side is not None and theirs.side is not None
        )
        lines.append(
            f"  sides, {other} -> {name}: "
            + ", ".join(
                f"{before.value} -> {after.value} {table[before, after]}"
                for before in _SIDES
                for after in _SIDES
                if table[before, after]
            )
        )
    if pairs:
        executed = [mine.executed_exposure - theirs.executed_exposure for mine, theirs in pairs]
        lines.append(
            f"  executed reading: {_moves(executed)}; mean shift {_signed(fmean(executed))}"
        )
    stated = [
        (mine.confidence, theirs.confidence)
        for mine, theirs in pairs
        if mine.confidence is not None and theirs.confidence is not None
    ]
    if stated:
        lines.append(
            f"  confidence where both state one: n {len(stated)}, mean "
            f"{fmean(mine for mine, _ in stated):.3f} against "
            f"{fmean(theirs for _, theirs in stated):.3f}"
        )
    return lines


@dataclass(frozen=True)
class _Shift:
    """The per-question mean shift, its interval, and the blocks it was drawn in."""

    shift: float
    low: float
    high: float
    blocks: int


def _question_lines(
    treatment: Arm, control: Arm, *, block: int, draws: int, seed: int
) -> tuple[list[str], _Shift | None, list[frozenset[str]]]:
    """The per-question block, the shift the verdict reads, and the questions it is read on.

    The shift is ``None`` with no question read by both; the questions are
    per run, and are what a spread the shift is held against is read on too.
    """
    lines = ["-- per question, repeats averaged --"]
    runs: list[list[tuple[float, float]]] = []
    questions = []
    for mine, theirs in zip(treatment.runs, control.runs, strict=True):
        ours, others = _question_means(mine), _question_means(theirs)
        common = [input_id for input_id in ours if input_id in others]
        questions.append(frozenset(common))
        runs.append([(ours[input_id], others[input_id]) for input_id in common])
    means = [pair for run in runs for pair in run]
    shifts = [[mine - theirs for mine, theirs in run] for run in runs]
    # A run both variants answered, on no question in common, adds nothing
    # to the pooled figures below: its own count is where that shows.
    by_run = "  by run: " + ", ".join(
        f"{mine.run_id} {len(run)}" for mine, run in zip(treatment.runs, runs, strict=True)
    )
    found = shift_interval(shifts, block=block, draws=draws, seed=seed)
    if found is None:
        return [*lines, "  no question has a readable claim from both", by_run], None, questions
    flat = [shift for run in shifts for shift in run]
    read = _Shift(fmean(flat), *found, block_count(shifts, block))
    lines.append(
        f"  {len(means)} question(s) read by both: mean requested exposure "
        f"{_signed(fmean(mine for mine, _ in means))} against "
        f"{_signed(fmean(theirs for _, theirs in means))}, shift {_signed(read.shift)}"
    )
    lines.append(
        f"  {LEVEL:.0%} interval of the shift [{_signed(read.low)}, {_signed(read.high)}]: "
        f"{read.blocks} circular block(s) of {block} question(s) within each run, {draws} "
        f"draws, seed {seed}"
    )
    lines.append(f"  {_moves(flat)}")
    return [*lines, by_run], read, questions


def _gap(pairs: Sequence[_Pair]) -> float | None:
    """The mean absolute exposure difference over the pairs both read; ``None`` with none."""
    exposures = _exposures(pairs)
    return fmean(abs(mine - theirs) for mine, theirs in exposures) if exposures else None


def _apart(label: str, pairs: Sequence[_Pair]) -> str:
    """How far apart the two answers of each pair are, over the pairs both read."""
    gap = _gap(pairs)
    if gap is None:
        return f"  {label}: no pair with a readable claim from both"
    readable = len(_exposures(pairs))
    differ = sum(
        1 for a, b in pairs if a.side is not None and b.side is not None and a.side is not b.side
    )
    return (
        f"  {label}: side differs in {differ} of {readable} pair(s) "
        f"({differ / readable:.1%}), mean absolute exposure difference {gap:.3f}"
    )


def _repeat_pairs(arm: Arm, questions: Sequence[frozenset[str]]) -> list[_Pair]:
    """Every pair of answers one variant gave one question on two of its repeats.

    Only on ``questions`` (per run, the ones the shift is read on): a spread
    taken on questions the other variant never read would move the bar
    without moving the shift it is held against.
    """
    return [
        pair
        for run, within in zip(arm.runs, questions, strict=True)
        for first, second in combinations(sorted(run.repeats), 2)
        for pair in _paired(run.repeats[first], run.repeats[second])
        if pair[0].input_id in within
    ]


def _between_repeats(arm: Arm, questions: Sequence[frozenset[str]]) -> str:
    label = f"two repeats of {arm.name}"
    if len(arm.repeats) < 2:
        return f"  {label}: fewer than two repeats hold an answer, so there is nothing to compare"
    return _apart(label, _repeat_pairs(arm, questions))


def verdict(
    shift: float,
    low: float,
    high: float,
    *,
    spreads: Sequence[tuple[str, float | None]],
    block: int,
    blocks: int,
) -> str:
    """Whether a per-question mean ``shift`` clears the bar (module docstring), as a sentence.

    ``low`` and ``high`` bound its interval, drawn in ``blocks`` blocks of
    ``block`` questions; ``spreads`` is each variant's name and its mean
    absolute exposure difference between two repeats (``None`` where it has
    no such pair): both variants', or the size half of the bar reads one.
    """
    if not spreads:
        raise ValueError("a verdict is read against each variant's spread; none was given")
    for name, spread in spreads:
        if spread is None:
            return (
                f"{UNJUDGED}: {name} has no question with a readable claim on two repeats to "
                "take a spread from"
            )
    if block != BLOCK:
        return f"{UNJUDGED}: the bar is read with blocks of {BLOCK} question(s), not {block}"
    if blocks < MIN_BLOCKS:
        return f"{UNJUDGED}: {blocks} block(s), fewer than the {MIN_BLOCKS} an interval is read with"
    if low <= TOLERANCE and high >= -TOLERANCE:
        return f"{NO_EFFECT}: the interval of the shift holds 0"
    widest = max(spread for _, spread in spreads if spread is not None)
    if abs(shift) <= widest + TOLERANCE:
        return (
            f"{NO_EFFECT}: the shift is no larger than the spread between two repeats of one "
            f"variant ({widest:.3f})"
        )
    return f"{EFFECT} toward {'long' if shift > 0 else 'short'}"


# One answer's cells in the CSV: each column's name and how it is read off a
# reading. A fail-closed round is filed under the scorecard's own word for it.
_COLUMNS: Final[tuple[tuple[str, Callable[[Reading], object]], ...]] = (
    ("side", lambda r: FAIL_CLOSED if r.side is None else r.side.value),
    ("exposure", lambda r: "" if r.exposure is None else r.exposure),
    ("executed_exposure", lambda r: r.executed_exposure),
    ("confidence", lambda r: "" if r.confidence is None else r.confidence),
    ("risk_action", lambda r: r.risk_action),
    ("mentioned", lambda r: int(r.mentioned)),
)


def _cells(reading: Reading) -> list[object]:
    return [read(reading) for _, read in _COLUMNS]


def describe_effect(treatment: Arm, control: Arm, *, block: int, draws: int, seed: int) -> Effect:
    """``treatment``'s answers against ``control``'s on the same questions (module docstring).

    Both arms hold the same runs in the same order and were searched for
    the same phrases; anything else is refused, since it would pair one
    run's questions with another's, or read one arm's mentions against a
    floor counted another way.
    """
    name, other = treatment.name, control.name
    covered = [[run.run_id for run in arm.runs] for arm in (treatment, control)]
    if covered[0] != covered[1]:
        raise ScoreError(
            f"{name} holds runs {covered[0]} and {other} holds {covered[1]}: the two are "
            "compared run by run, in one order"
        )
    if list(treatment.phrases) != list(control.phrases):
        raise ScoreError(
            f"{name} was searched for {list(treatment.phrases)} and {other} for "
            f"{list(control.phrases)}: one arm's mentions are read against the other's only "
            "under the same phrases"
        )
    differ = differences(treatment.variant, control.variant)
    # ``differences`` spells the identity's ``extra_context`` key this way.
    if not differ:
        differ_line = "the two differ in nothing that reaches the model"
    elif differ == ["extra context"]:
        differ_line = "the two differ in: extra context"
    else:
        differ_line = (
            f"the two differ in: {', '.join(differ)}; a shift between them is not the extra "
            "context's alone"
        )
    preamble = [
        f"decision effect: {name} against {other}",
        treatment.variant.describe(),
        f"against {control.variant.describe()}",
        differ_line,
        _context_line(treatment.variant),
        _context_line(control.variant),
    ]
    lines = [
        "-- answers (train and validation; the holdout is never read) --",
        _answers_line(treatment),
        _answers_line(control),
    ]
    header = [
        "run_id",
        "input_id",
        "repeat",
        "at",
        *(f"variant_{column}" for column, _ in _COLUMNS),
        *(f"against_{column}" for column, _ in _COLUMNS),
    ]
    rows: list[list[object]] = []
    same_repeat: list[_Pair] = []
    for repeat in sorted({*treatment.repeats, *control.repeats}):
        pairs: list[_Pair] = []
        absent: list[str] = []
        for mine, theirs in zip(treatment.runs, control.runs, strict=True):
            lacking = [
                arm_name
                for arm_name, run in ((name, mine), (other, theirs))
                if repeat not in run.repeats
            ]
            if lacking:
                # Said per run: a repeat with nothing read on some run (not
                # asked there, refused throughout, or asked the holdout only)
                # is paired on fewer questions than its neighbours.
                absent.append(
                    f"  {mine.run_id}: no answer read on repeat {repeat} from "
                    f"{' or '.join(lacking)}, so nothing is paired there"
                )
                continue
            found = _paired(mine.repeats[repeat], theirs.repeats[repeat])
            pairs.extend(found)
            rows.extend(
                [
                    mine.run_id,
                    a.input_id,
                    repeat,
                    from_epoch_ms(a.at_ms).isoformat(),
                    *_cells(a),
                    *_cells(b),
                ]
                for a, b in found
            )
        lines.extend(_repeat_lines(repeat, pairs, name, other, absent))
        same_repeat.extend(pairs)
    per_question, read, questions = _question_lines(
        treatment, control, block=block, draws=draws, seed=seed
    )
    lines.extend(per_question)
    lines.extend(
        [
            "-- how far apart two answers to one question are (readable claims on the questions "
            "read by both; exposure as a multiple of equity) --",
            _between_repeats(treatment, questions),
            _between_repeats(control, questions),
            _apart(f"{name} against {other}, same repeat", same_repeat),
        ]
    )
    if differ != ["extra context"]:
        # The bar's sentence is about the extra context: said of variants
        # that differ in a model as well, it would credit the wrong thing.
        judged = f"{UNJUDGED}: " + (
            f"the variants differ in more than their extra context ({', '.join(differ)})"
            if differ
            else "the variants differ in nothing that reaches the model"
        )
    elif read is None:
        judged = f"{UNJUDGED}: no question has a readable claim from both"
    else:
        judged = verdict(
            read.shift,
            read.low,
            read.high,
            spreads=[
                (arm.name, _gap(_repeat_pairs(arm, questions))) for arm in (treatment, control)
            ],
            block=block,
            blocks=read.blocks,
        )
    lines.append(f"verdict: {judged}")
    return Effect(preamble, lines, (header, rows))
