"""The direction probe, scored (replay plan PR 2.1): Brier, log loss, skill over the base rate.

A pure function of records, as :mod:`.compare` is: the CLI opens the
stores and prints, and everything between is here. The definitions, once:

- **What happened** at a horizon is one of the probe's three classes, read
  from the scorecard's own later mark and flat band (:mod:`.score`), so the
  probe and the decision are marked against the same prices: ``flat`` when
  the absolute return stayed strictly under the band (the median absolute
  return of the run's train and validation questions at that horizon), or
  was exactly zero; otherwise ``up`` or ``down`` by its sign. A question
  with no later mark, or at a horizon with no band, has no outcome and is
  counted, not scored.
- **The Brier score** of one forecast is the sum over the three classes of
  ``(p - y)²``, ``y`` being 1 for the class that happened and 0 for the
  others (0 is perfect, 2 is certain and wrong); a segment's figure is the
  mean. **The log loss** is ``-ln p`` of the class that happened, with ``p``
  floored at :data:`LOG_LOSS_FLOOR` so one confident miss is costly rather
  than infinite.
- **The base rate** is the reference every forecast is held against: the
  share of ``up`` / ``down`` / ``flat`` among the TRAIN questions with an
  outcome (answered or not, eligible or not: it is a fact about the prices,
  not about the model), given as the same fixed answer to every question.
  **The Brier skill score** is ``1 - Brier / Brier of the base rate`` on the
  same questions: 0 or below says the model's probabilities carry no
  direction the base rate does not.
- **The headline** (decided 2026-09-24) scores ONE forecast per question:
  the mean, class by class, of its valid repeats (:func:`ensemble`). A
  question whose repeats answered only ``invalid_probe`` is scored as the
  base rate (it adds no skill and keeps its place in ``n``), and the skill
  without those stand-ins is printed beside it; a question refused on every
  repeat is counted, not scored.
- **Up against down given a move** (decided 2026-09-24) takes the questions
  that moved (``up`` or ``down``) and scores ``up / (up + down)`` of each
  forecast with the binary Brier score ``(q - y)²``, against the share of
  ``up`` among the train moves. It does not depend on where the flat band
  sits, which the model is only told in words.
- **Temperature scaling** (one parameter, fitted by minimising the log loss
  of the train headline forecasts, searched over
  :data:`TEMPERATURE_BOUNDS`) is reported on the validation segment (and the
  holdout when it is read) only, where it was not fitted.
- **The reliability table** files each headline forecast under the
  probability of its most likely class (ties go to the class listed first in
  :data:`~.probe.CLASSES`), ten buckets, and says how often that class
  happened; the expected calibration error is the question-weighted mean
  gap between the two.

Each repeat is then scored on its own, ``invalid_probe`` left out (plan
§3-10), and the skill scores summarised across repeats by their median and
range: how stable the headline is, not a second headline.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final

from .probe import CLASSES, INVALID_PROBE, PROBE_KEYS, REFUSED, Probe, ProbeAnswer
from .score import Scorecard
from .upstream import SegmentName

__all__ = [
    "LOG_LOSS_FLOOR",
    "TEMPERATURE_BOUNDS",
    "Binary",
    "Figures",
    "ReliabilityBucket",
    "base_rates",
    "binary_figures",
    "brier",
    "describe_probe",
    "ensemble",
    "figures",
    "fit_temperature",
    "log_loss",
    "move_base_rate",
    "outcome_class",
    "reliability",
    "tempered",
    "up_given_move",
]

# The least probability the log loss reads a forecast as giving the class
# that happened: -ln(0.001) is about 6.9, the most one forecast can cost.
LOG_LOSS_FLOOR: Final = 1e-3

# The temperatures the fit searches, lowest (sharpest) to highest (flattest).
TEMPERATURE_BOUNDS: Final = (0.05, 20.0)

_RELIABILITY_BUCKETS: Final = 10
_SEARCH_STEPS: Final = 100
_GOLDEN: Final = (math.sqrt(5) - 1) / 2

# A forecast and the class that happened.
Pair = tuple[Mapping[str, float], str]


def outcome_class(ret: float | None, band: float | None) -> str | None:
    """The class that happened, or ``None`` with no later mark or no band (module docstring)."""
    if ret is None or band is None:
        return None
    if abs(ret) < band or ret == 0:
        return "flat"
    return "up" if ret > 0 else "down"


def brier(forecast: Mapping[str, float], outcome: str) -> float:
    return math.fsum((forecast[name] - (name == outcome)) ** 2 for name in CLASSES)


def log_loss(forecast: Mapping[str, float], outcome: str) -> float:
    return -math.log(max(forecast[outcome], LOG_LOSS_FLOOR))


def tempered(forecast: Mapping[str, float], temperature: float) -> dict[str, float]:
    """``forecast`` at ``temperature``: each log-probability (floored) divided by it, renormalised."""
    logits = {name: math.log(max(forecast[name], LOG_LOSS_FLOOR)) / temperature for name in CLASSES}
    top = max(logits.values())
    weights = {name: math.exp(value - top) for name, value in logits.items()}
    total = math.fsum(weights.values())
    return {name: weight / total for name, weight in weights.items()}


def fit_temperature(pairs: Sequence[Pair]) -> float | None:
    """The temperature within :data:`TEMPERATURE_BOUNDS` that minimises the mean log loss.

    A golden-section search over the inverse temperature ``b``, minimising
    the exact mean of ``logsumexp(b * l) - b * l[outcome]``, ``l`` being the
    floored log-probabilities :func:`tempered` divides: that is convex in
    ``b``. The reported log loss floors the TEMPERED probability too, and
    that would flatten the search into a plateau once a sharpened forecast
    puts the floor under a class that happens (a forecast of .7 / .1 / .2
    over three ups and three flats was fitted at the search's edge).
    ``None`` with no pairs.
    """
    if not pairs:
        return None
    logged = [
        ([math.log(max(f[name], LOG_LOSS_FLOOR)) for name in CLASSES], CLASSES.index(o))
        for f, o in pairs
    ]

    def loss(inverse: float) -> float:
        total = 0.0
        for logs, happened in logged:
            scaled = [inverse * value for value in logs]
            top = max(scaled)
            total += top + math.log(math.fsum(math.exp(v - top) for v in scaled)) - scaled[happened]
        return total / len(logged)

    low, high = 1 / TEMPERATURE_BOUNDS[1], 1 / TEMPERATURE_BOUNDS[0]
    for _ in range(_SEARCH_STEPS):
        left = high - _GOLDEN * (high - low)
        right = low + _GOLDEN * (high - low)
        if loss(left) <= loss(right):
            high = right
        else:
            low = left
    return 2 / (low + high)


@dataclass(frozen=True)
class Figures:
    """One set of forecasts against the base rate. The scores are ``None`` with no pairs."""

    n: int
    brier: float | None
    base_brier: float | None
    log_loss: float | None
    base_log_loss: float | None

    @property
    def skill(self) -> float | None:
        """The Brier skill score; ``None`` when the base rate is unknown or scores perfectly."""
        if self.brier is None or not self.base_brier:
            return None
        return 1 - self.brier / self.base_brier


def figures(pairs: Sequence[Pair], base: Mapping[str, float] | None) -> Figures:
    """The forecasts' mean Brier score and log loss, and the base rate's on the same outcomes."""
    if not pairs:
        return Figures(0, None, None, None, None)

    def mean(values: Iterable[float]) -> float:
        return math.fsum(values) / len(pairs)

    return Figures(
        n=len(pairs),
        brier=mean(brier(f, o) for f, o in pairs),
        base_brier=None if base is None else mean(brier(base, o) for _, o in pairs),
        log_loss=mean(log_loss(f, o) for f, o in pairs),
        base_log_loss=None if base is None else mean(log_loss(base, o) for _, o in pairs),
    )


def base_rates(card: Scorecard, bars: int) -> tuple[dict[str, float], int] | None:
    """``(share of each class, questions counted)`` over the train rows with an outcome."""
    seen = [
        outcome
        for row in card.rows
        if row.segment is SegmentName.TRAIN
        for outcome in [outcome_class(row.outcomes[bars].ret, card.flat_bands[bars])]
        if outcome is not None
    ]
    if not seen:
        return None
    return {name: seen.count(name) / len(seen) for name in CLASSES}, len(seen)


@dataclass(frozen=True)
class ReliabilityBucket:
    low: float  # the bucket is [low, low + 0.1), the top one closed
    n: int
    predicted: float  # the mean probability of the most likely class
    happened: float  # how often that class happened


def reliability(pairs: Sequence[Pair]) -> tuple[list[ReliabilityBucket], float | None]:
    """The non-empty buckets, lowest first, and the expected calibration error (``None``: no pairs)."""
    filed: dict[int, list[tuple[float, bool]]] = {}
    for forecast, outcome in pairs:
        top = max(CLASSES, key=lambda name: forecast[name])  # the first of equals wins
        p = forecast[top]
        index = min(int(p * _RELIABILITY_BUCKETS), _RELIABILITY_BUCKETS - 1)
        filed.setdefault(index, []).append((p, top == outcome))
    buckets = [
        ReliabilityBucket(
            low=index / _RELIABILITY_BUCKETS,
            n=len(entries),
            predicted=math.fsum(p for p, _ in entries) / len(entries),
            happened=sum(hit for _, hit in entries) / len(entries),
        )
        for index, entries in sorted(filed.items())
    ]
    if not pairs:
        return buckets, None
    ece = math.fsum(b.n * abs(b.happened - b.predicted) for b in buckets) / len(pairs)
    return buckets, ece


# -- one forecast per question, and up against down -------------------------------


def ensemble(answers: Mapping[int, Sequence[ProbeAnswer]]) -> dict[str, ProbeAnswer]:
    """Each question's repeats as one answer: the mean of its valid forecasts, class by class.

    A question with no valid forecast is ``invalid_probe`` if any repeat
    answered with one, and ``refused`` if the provider refused every repeat.
    """
    by_input: dict[str, list[ProbeAnswer]] = {}
    for given in answers.values():
        for answer in given:
            by_input.setdefault(answer.input_id, []).append(answer)
    merged: dict[str, ProbeAnswer] = {}
    for input_id, seen in by_input.items():
        forecasts = [a.forecast for a in seen if a.forecast is not None]
        if forecasts:
            mean = {
                key: {
                    name: math.fsum(f[key][name] for f in forecasts) / len(forecasts)
                    for name in CLASSES
                }
                for key in PROBE_KEYS
            }
            merged[input_id] = ProbeAnswer(input_id, mean, None)
        elif any(a.invalid_reason == INVALID_PROBE for a in seen):
            merged[input_id] = ProbeAnswer(input_id, None, INVALID_PROBE)
        else:
            merged[input_id] = ProbeAnswer(input_id, None, REFUSED)
    return merged


def up_given_move(forecast: Mapping[str, float]) -> float:
    """The forecast's probability of ``up`` should the price move: ``up / (up + down)``, else 0.5."""
    moving = forecast["up"] + forecast["down"]
    return 0.5 if moving == 0 else forecast["up"] / moving


def move_base_rate(card: Scorecard, bars: int) -> tuple[float, int] | None:
    """``(share of up among the train moves, moves counted)``, ``None`` with no train move."""
    found = base_rates(card, bars)
    if found is None:
        return None
    shares, n = found
    moving = shares["up"] + shares["down"]
    if moving == 0:
        return None
    return shares["up"] / moving, round(n * moving)


@dataclass(frozen=True)
class Binary:
    """Up against down on the questions that moved; the scores are ``None`` with none."""

    n: int
    brier: float | None
    base_brier: float | None

    @property
    def skill(self) -> float | None:
        if self.brier is None or not self.base_brier:
            return None
        return 1 - self.brier / self.base_brier


def binary_figures(pairs: Sequence[Pair], base_up: float | None) -> Binary:
    """The binary Brier score ``(q - y)²`` of :func:`up_given_move` on the pairs that moved."""
    moved = [(up_given_move(f), o == "up") for f, o in pairs if o != "flat"]
    if not moved:
        return Binary(0, None, None)
    n = len(moved)
    return Binary(
        n=n,
        brier=math.fsum((q - y) ** 2 for q, y in moved) / n,
        base_brier=None if base_up is None else math.fsum((base_up - y) ** 2 for _, y in moved) / n,
    )


# -- the report ------------------------------------------------------------------


@dataclass
class _Tally:
    """One set of answers, one segment, one horizon: the pairs scored, and what was not.

    ``stand_ins`` holds the outcome of every question answered only with
    ``invalid_probe``, for the headline to score as the base rate.
    """

    pairs: list[Pair] = field(default_factory=list)
    stand_ins: list[str] = field(default_factory=list)
    invalid: int = 0
    refused: int = 0
    no_outcome: int = 0


def _tallies(
    card: Scorecard,
    answers: Iterable[ProbeAnswer],
    eligible: Collection[str],
    key: str,
) -> dict[SegmentName | None, _Tally]:
    """Per segment the card keeps, the answers at one horizon, eligible questions only."""
    bars = PROBE_KEYS[key]
    by_input = {a.input_id: a for a in answers}
    tallies: dict[SegmentName | None, _Tally] = {}
    for row in card.rows:
        me = row.question.input_id
        answer = by_input.get(me)
        if answer is None or me not in eligible:
            continue
        tally = tallies.setdefault(row.segment, _Tally())
        outcome = outcome_class(row.outcomes[bars].ret, card.flat_bands[bars])
        if answer.invalid_reason == REFUSED:
            tally.refused += 1
        elif answer.invalid_reason == INVALID_PROBE:
            tally.invalid += 1
            if outcome is not None:
                tally.stand_ins.append(outcome)
        elif outcome is None:
            tally.no_outcome += 1
        else:
            assert answer.forecast is not None
            tally.pairs.append((answer.forecast[key], outcome))
    return tallies


def _num(value: float | None, form: str) -> str:
    return "n/a" if value is None else form.format(value)


def _label(segment: SegmentName | None) -> str:
    return "unsplit" if segment is None else segment.value


def _segment_order(segment: SegmentName | None) -> int:
    return -1 if segment is None else list(SegmentName).index(segment)


def _order(item: tuple[tuple[str, SegmentName | None], object]) -> tuple[int, int]:
    """Horizon first (in :data:`PROBE_KEYS` order), then segment (train, validation, holdout)."""
    key, segment = item[0]
    return list(PROBE_KEYS).index(key), _segment_order(segment)


def _reliability_line(label: str, pairs: Sequence[Pair]) -> str:
    buckets, ece = reliability(pairs)
    cells = "; ".join(
        f"{b.low:.1f}-{b.low + 0.1:.1f} n {b.n} predicted {b.predicted:.1%} happened "
        f"{b.happened:.1%}"
        for b in buckets
    )
    return f"  {label}: " + (cells or "no forecast scored") + f"; ECE {_num(ece, '{:.3f}')}"


def describe_probe(
    *,
    card: Scorecard,
    probe: Probe,
    answers: Mapping[int, Sequence[ProbeAnswer]],
    eligible: Collection[str],
) -> list[str]:
    """The probe's section of ``score --replay-db``: the headline, then each repeat on its own.

    ``card`` is the whole run scored with no answers, every question it
    keeps (so the base rate sees every train question); ``eligible`` is the
    questions the model cutoff lets in (:func:`~.compare.cutoff_scope`).
    """
    lines = [
        f"== direction {probe.describe()}: up / down / flat, asked on its own ==",
        "flat band (median |return| of the train and validation questions): "
        + ", ".join(
            f"{card.horizon_label(bars)} {_num(card.flat_bands[bars], '{:.3%}')}"
            for bars in PROBE_KEYS.values()
        ),
    ]
    bases: dict[str, dict[str, float] | None] = {}
    move_bases: dict[str, float | None] = {}
    moves: list[str] = []
    for key, bars in PROBE_KEYS.items():
        label = card.horizon_label(bars)
        found = base_rates(card, bars)
        bases[key] = None if found is None else found[0]
        lines.append(
            f"base rate, train, {label}: "
            + (
                "n/a (no train question has an outcome)"
                if found is None
                else " / ".join(f"{name} {found[0][name]:.1%}" for name in CLASSES)
                + f" (n {found[1]})"
            )
        )
        moved = move_base_rate(card, bars)
        move_bases[key] = None if moved is None else moved[0]
        moves.append(f"{label} n/a" if moved is None else f"{label} {moved[0]:.1%} (n {moved[1]})")
    lines.append("base rate of up among the moves, train: " + ", ".join(moves))

    lines.append(
        "-- headline: each question's repeats averaged into one forecast; a question answered "
        "only with invalid_probe is scored as the base rate --"
    )
    headline = list(ensemble(answers).values())
    reliable: dict[tuple[str, SegmentName | None], list[Pair]] = {}
    for key, bars in PROBE_KEYS.items():
        label = card.horizon_label(bars)
        base = bases[key]
        tallies = _tallies(card, headline, eligible, key)
        for segment, tally in sorted(tallies.items(), key=lambda kv: _segment_order(kv[0])):
            stand_ins = [] if base is None else [(base, o) for o in tally.stand_ins]
            scored = figures([*tally.pairs, *stand_ins], base)
            own = figures(tally.pairs, base)
            reliable[(key, segment)] = tally.pairs
            lines.append(
                f"  {label} {_label(segment)}: n {scored.n} scored ({tally.invalid} answered only "
                f"invalid_probe, {tally.refused} refused, {tally.no_outcome} without an outcome); "
                f"Brier {_num(scored.brier, '{:.3f}')} vs base "
                f"{_num(scored.base_brier, '{:.3f}')}, skill {_num(scored.skill, '{:+.3f}')}; "
                f"log loss {_num(scored.log_loss, '{:.3f}')} vs base "
                f"{_num(scored.base_log_loss, '{:.3f}')}; without the base-rate stand-ins: n "
                f"{own.n}, skill {_num(own.skill, '{:+.3f}')}"
            )
            binary = binary_figures([*tally.pairs, *stand_ins], move_bases[key])
            lines.append(
                f"  {label} {_label(segment)}, up vs down given a move: n {binary.n}, Brier "
                f"{_num(binary.brier, '{:.3f}')} vs base {_num(binary.base_brier, '{:.3f}')}, "
                f"skill {_num(binary.skill, '{:+.3f}')}"
            )
        fitted = fit_temperature(tallies.get(SegmentName.TRAIN, _Tally()).pairs)
        for segment in (SegmentName.VALIDATION, SegmentName.HOLDOUT):
            held_out = tallies.get(segment, _Tally())
            if not held_out.pairs:
                continue
            if fitted is None:
                lines.append(
                    f"  {label} {segment.value}, temperature-scaled: n/a (no train forecast to fit "
                    "it on)"
                )
                continue
            stand_ins = [] if base is None else [(base, o) for o in held_out.stand_ins]
            scaled = figures(
                [*((tempered(f, fitted), o) for f, o in held_out.pairs), *stand_ins], base
            )
            lines.append(
                f"  {label} {segment.value}, temperature {fitted:.2f} fitted on the train "
                f"headline: Brier {_num(scaled.brier, '{:.3f}')}, skill "
                f"{_num(scaled.skill, '{:+.3f}')}; log loss {_num(scaled.log_loss, '{:.3f}')}"
            )
    lines.append(
        "-- headline reliability (the most likely class: its mean probability, and how often it "
        "happened) --"
    )
    for (key, segment), pairs in sorted(reliable.items(), key=_order):
        label = f"{card.horizon_label(PROBE_KEYS[key])} {_label(segment)}"
        lines.append(_reliability_line(label, pairs))

    lines.append(
        "-- each repeat on its own, invalid_probe left out (how stable the headline is) --"
    )
    skills: dict[tuple[str, SegmentName | None], list[float]] = {}
    for repeat, given in sorted(answers.items()):
        lines.append(f"-- probe repeat {repeat} --")
        for key, bars in PROBE_KEYS.items():
            label = card.horizon_label(bars)
            tallies = _tallies(card, given, eligible, key)
            for segment, tally in sorted(tallies.items(), key=lambda kv: _segment_order(kv[0])):
                scored = figures(tally.pairs, bases[key])
                values = skills.setdefault((key, segment), [])
                if scored.skill is not None:
                    values.append(scored.skill)
                lines.append(
                    f"  {label} {_label(segment)}: n {scored.n} scored ({tally.invalid} "
                    f"invalid_probe, {tally.refused} refused, {tally.no_outcome} without an "
                    f"outcome); Brier {_num(scored.brier, '{:.3f}')} vs base "
                    f"{_num(scored.base_brier, '{:.3f}')}, skill {_num(scored.skill, '{:+.3f}')}; "
                    f"log loss {_num(scored.log_loss, '{:.3f}')} vs base "
                    f"{_num(scored.base_log_loss, '{:.3f}')}"
                )
    lines.append(f"-- probe across {len(answers)} repeat(s): Brier skill score, median (range) --")
    # Every horizon and segment that had an answer gets a line: one where no
    # repeat has a skill says so, rather than leaving the summary quietly
    # shorter than the repeats above it.
    for (key, segment), values in sorted(skills.items(), key=_order):
        lines.append(
            f"  {card.horizon_label(PROBE_KEYS[key])} {_label(segment)}: "
            + (
                f"{statistics.median(values):+.3f} ({min(values):+.3f} to {max(values):+.3f})"
                if values
                else "n/a (no repeat has a skill score)"
            )
        )
    return lines
