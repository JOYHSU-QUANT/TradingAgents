"""How close each arm's chosen rule came to the planted one, and the table over worlds (plan §3 step 6, §4.6).

A final verdict says an arm found SOMETHING; on a conditional world the
question left is whether what it found is what was planted. Three answers
are reported apart and never blended (plan §4.6):

- **Structural distance** (reported, never judged on): did it write a
  similar rule? :func:`structural_distance`, 0 for the same conditions and
  1 for nothing in common. A clause under ``filters`` and the same clause
  under ``entry.<side>`` are one condition here (plan §1 #10), and the
  exits are not read: how long a rule holds shows in the positions.
- **Direction agreement** (the main reading): did it trade similarly? On
  the holdout, bar by bar, over the bars at least one of the two holds a
  position (:func:`direction_agreement`) — only those bars, because two
  rules flat most of the time would otherwise agree by doing nothing.
- **Holdout performance**: was it useful out of sample? The selected
  rule's net Sharpe beside the planted rule's.

A selected rule counts as RECOVERED at direction agreement of at least
``scoring.recovery.theta_behavior`` and, when the manifest requires it, a
positive holdout net Sharpe on a holdout it did not ruin (no ratio of a
ruined run is read, which is the research package's own rule). An arm
that selected nothing and an agreement that is undefined (neither rule
held anything in the holdout) are not recoveries and stay in the
denominator; an arm whose seam failed did not finish its search, and is
counted apart, as the calibration counts it.

**Positions are expanded from the holdout's trades** (:func:`positions`),
not from the research package's ``replay_sides``: that reports the side
after each close's decision, one bar ahead of the fill, simulates no ruin
and no flatten at the window's end, so comparing it with a trade would be
off by one and disagree at both edges. A trade holds exactly the bars
``Trade.bars_held`` counts from its ``entry_index``: an ordinary exit fills
at the exit bar's open and that bar is not held, a flatten at the window's
end and a ruin fill at its close and it is, and a reversal's exit bar
belongs to the position that opened on it.

**Nothing new is measured by the scorer's say-so.** The records already
hold every holdout figure — the promotion wrote the selected rule's, the
oracle the planted rule's — so the holdout re-read here to get the TRADES
has to reproduce those figures exactly or the score is refused: a store
that does not reproduce its own record is not the store the record was
written from. The read is the oracle's (nothing is filed), on the Grid
arm's store, whose experiment is the one every arm's conditions equal.

``scores.json`` is a pure function of the records and the store, so
scoring a world twice writes the same bytes; ``score`` may be re-run. The
report reads the records and the scores and no store, so a world's stores
may be pruned once it is scored. On a test seed ``score``'s console keeps
off everything read from the holdout — the Sharpe, the direction agreement
and whether the rule was recovered (plan §4.2) — as ``search``'s keeps its
figures off. The report seals the same figures on a test seed unless told
to ``--unseal`` them, which is the one deliberate reading of a test set.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from statistics import fmean, median
from typing import Final

from .bench import (
    VERDICTS_FILENAME,
    Arm,
    ArmResult,
    RowKey,
    Searched,
    effort_note,
    experiment_id,
    in_row_order,
    iter_searched,
    read_record,
    read_searched,
    row_head,
    row_key,
    seam_note,
    store_path,
)
from .manifest import Manifest, ManifestError, Planted, Recovery, SeedSet, WorldKind
from .oracle import evaluate_unfiled, frame_for
from .records import Reader, RecordError, dumps
from .upstream import (
    Condition,
    Experiment,
    FeatureRef,
    Ledger,
    Op,
    ResearchStore,
    SegmentMetrics,
    Side,
    StrategySpec,
    Trade,
    atomic_write_bytes,
    spec_hash,
)
from .verdict import Ended

__all__ = [
    "SCORES_FILENAME",
    "SCORES_VERSION",
    "SCORING_VERSION",
    "ArmScore",
    "HoldoutRead",
    "ReportEntry",
    "ReportRow",
    "ScoreError",
    "Scores",
    "direction_agreement",
    "positions",
    "read_scores",
    "report_table",
    "require_this_scorer",
    "score_records",
    "score_world",
    "structural_distance",
]

SCORES_FILENAME: Final = "scores.json"

# Bumped when a key of ``scores.json`` is added, removed or re-read.
SCORES_VERSION: Final = 1

# The definitions of this module, as a manifest's ``scoring.version`` names
# them: a manifest asking for another is refused rather than scored by these.
SCORING_VERSION: Final = "1"

# The agreement thresholds a development row reports recoveries at, so
# ``theta_behavior`` can be chosen from one run (plan §4.2, §8 #6): the
# manifest's value is part of the run's digest, so a threshold tried by
# editing it would be a new directory and a new search.
_THETA_SENSITIVITY: Final = (0.5, 0.6, 0.7, 0.8, 0.9)


class ScoreError(RecordError):
    """A score cannot be read, built or computed, and the sentence says why (as ``VerdictError``)."""


_read = Reader(ScoreError, "scores")


# -- the three readings (pure) ------------------------------------------------------------------


def positions(trades: Sequence[Trade], *, first: int, count: int) -> tuple[int, ...]:
    """The position held on each of ``count`` bars from bar ``first``: +1 long, -1 short, 0 flat.

    Bar indices are the frame's, as the trades carry them. A trade holds
    ``bars_held`` bars from its entry (the module docstring). Two trades
    claiming one bar, or a trade outside the window, are not what the
    evaluator writes, and are refused rather than resolved.
    """
    held = [0] * count
    for trade in trades:
        direction = 1 if trade.side is Side.LONG else -1
        for index in range(trade.entry_index, trade.entry_index + trade.bars_held):
            offset = index - first
            if not 0 <= offset < count:
                raise ScoreError(
                    f"a trade holds bar {index}, outside the window of {count} bar(s) from {first}"
                )
            if held[offset]:
                raise ScoreError(f"two trades hold bar {index}")
            held[offset] = direction
    return tuple(held)


def direction_agreement(
    selected: Sequence[int], planted: Sequence[int]
) -> tuple[float | None, int]:
    """The share of bars the two positions agree on, over the bars at least one holds a position.

    Returns the share and how many bars it is over. The bars where both
    are flat are left out ON PURPOSE (the module docstring); do not widen
    this to every bar. No bar held by either gives ``(None, 0)``: agreement
    is undefined there, not 1.
    """
    if len(selected) != len(planted):
        raise ScoreError(f"positions over {len(selected)} and {len(planted)} bars")
    compared = [
        (ours, theirs) for ours, theirs in zip(selected, planted, strict=True) if ours or theirs
    ]
    if not compared:
        return None, 0
    return sum(ours == theirs for ours, theirs in compared) / len(compared), len(compared)


_MIRRORED: Final = {Op.GT: Op.LT, Op.LT: Op.GT, Op.GE: Op.LE, Op.LE: Op.GE}

# A condition as this module compares it: left feature, comparison, right side.
_Clause = tuple[FeatureRef, Op, object]


def _ref_key(ref: FeatureRef) -> tuple[str, int, int]:
    return (ref.kind.value, ref.period or 0, ref.offset)


def _normal(condition: Condition) -> _Clause:
    """``condition`` with a feature-to-feature comparison read from one fixed side."""
    left, op, right = condition.left, condition.op, condition.right
    if isinstance(right, FeatureRef) and _ref_key(right) < _ref_key(left):
        return right, _MIRRORED[op], left
    return left, op, right


def _closeness(ours: object, theirs: object) -> float:
    """1 for one threshold, falling with the relative gap between two numbers, 0 for unlike ones."""
    if ours == theirs:
        return 1.0
    if not (isinstance(ours, float) and isinstance(theirs, float)):
        return 0.0
    return max(0.0, 1.0 - abs(ours - theirs) / max(abs(ours), abs(theirs)))


def _clause_similarity(ours: _Clause, theirs: _Clause) -> float:
    """0 for two features of different kinds; else the mean of same-period, same-op and threshold closeness."""
    our_ref, our_op, our_right = ours
    their_ref, their_op, their_right = theirs
    if our_ref.kind is not their_ref.kind:
        return 0.0
    same_period = (our_ref.period, our_ref.offset) == (their_ref.period, their_ref.offset)
    return (float(same_period) + float(our_op is their_op) + _closeness(our_right, their_right)) / 3


def _conditions(spec: StrategySpec, side: Side) -> list[_Clause]:
    """What must hold for ``spec`` to enter ``side``, ``filters`` and ``entry`` as one list."""
    if not spec.entries(side):
        return []
    # On the normal form, so one clause written from either side (or under
    # another parameter name) is one condition, as ``spec_hash`` reads it.
    return sorted({_normal(c) for c in (*spec.entries(side), *spec.filters)}, key=str)


def _side_similarity(selected: StrategySpec, planted: StrategySpec, side: Side) -> float:
    """Greedy best pairing of the two rules' conditions on ``side``, over the longer list.

    A condition one rule has and the other lacks counts against it, whichever
    rule it is on. Greedy rather than optimal: rules have a handful of
    clauses, and this is a figure reported and never judged on.
    """
    ours, theirs = _conditions(selected, side), _conditions(planted, side)
    if not ours or not theirs:
        return 0.0
    # Stable under ``reverse``, so equal similarities stay in (i, j) order.
    ranked = sorted(
        (
            (_clause_similarity(a, b), i, j)
            for i, a in enumerate(ours)
            for j, b in enumerate(theirs)
        ),
        key=lambda pair: pair[0],
        reverse=True,
    )
    used_ours: set[int] = set()
    used_theirs: set[int] = set()
    total = 0.0
    for similarity, i, j in ranked:
        if i in used_ours or j in used_theirs:
            continue
        used_ours.add(i)
        used_theirs.add(j)
        total += similarity
    return total / max(len(ours), len(theirs))


def structural_distance(selected: StrategySpec, planted: StrategySpec) -> float:
    """0 for the same entry conditions, 1 for nothing in common (the module docstring).

    Averaged over the sides either rule enters; a side only one of them
    enters counts as similarity 0 in the average.
    """
    sides = [side for side in Side if selected.entries(side) or planted.entries(side)]
    similarity = fmean(_side_similarity(selected, planted, side) for side in sides)
    return 1.0 - similarity


# -- the record -----------------------------------------------------------------------------------


def _optional_share(value: object, path: str) -> float | None:
    if value is None:
        return None
    share = _read.number(value, path)
    if not 0 <= share <= 1:
        raise ScoreError(f"{path}: expected a value between 0 and 1, got {share!r}")
    return share


@dataclass(frozen=True)
class ArmScore:
    """One arm's one search, scored. ``recovered`` is derived and written for the reader.

    ``recovery`` is the world's rule, ``None`` on a null world: nothing was
    planted, so there is no agreement to read and nothing to recover; the
    selected rule's holdout figure is still reported, as the false call's.
    """

    arm: Arm
    replicate: int
    ended: Ended
    selected: int | None
    structural_distance: float | None
    agreement: float | None
    bars_compared: int
    holdout_sharpe: float | None
    holdout_ruined: bool
    recovery: Recovery | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "arm", _read.enum(Arm, self.arm, "score.arm"))
        _read.whole(self.replicate, "score.replicate", minimum=0)
        if self.arm is Arm.GRID and self.replicate != 0:
            raise ScoreError(f"{self.experiment_id}: the grid arm is one deterministic search")
        object.__setattr__(self, "ended", _read.enum(Ended, self.ended, "score.ended"))
        if self.selected is not None:
            _read.whole(self.selected, "score.selected", minimum=0)
        object.__setattr__(
            self,
            "structural_distance",
            _optional_share(self.structural_distance, "score.structural_distance"),
        )
        object.__setattr__(self, "agreement", _optional_share(self.agreement, "score.agreement"))
        _read.whole(self.bars_compared, "score.bars_compared", minimum=0)
        if self.holdout_sharpe is not None:
            _read.number(self.holdout_sharpe, "score.holdout_sharpe")
        _read.flag(self.holdout_ruined, "score.holdout_ruined")
        if self.recovery is not None and not isinstance(self.recovery, Recovery):
            raise ScoreError("score.recovery: expected a Recovery or None")
        if self.holdout_ruined and self.selected is None:
            raise ScoreError(f"{self.experiment_id}: nothing was selected to be ruined")
        if (self.selected is None) != (self.holdout_sharpe is None):
            raise ScoreError(
                f"{self.experiment_id}: a holdout figure is reported if and only if a trial was "
                f"selected"
            )
        if self.recovery is None or self.selected is None:
            if (
                self.structural_distance is not None
                or self.agreement is not None
                or self.bars_compared
            ):
                raise ScoreError(
                    f"{self.experiment_id}: a rule is compared with the planted one only when one "
                    f"was selected on a world that plants one"
                )
        else:
            if self.structural_distance is None:
                raise ScoreError(f"{self.experiment_id}: a compared rule has a structural distance")
            if (self.agreement is None) != (self.bars_compared == 0):
                raise ScoreError(
                    f"{self.experiment_id}: agreement {self.agreement!r} over "
                    f"{self.bars_compared} bar(s); it is undefined exactly when no bar is compared"
                )
            if (
                self.agreement is not None
                and round(self.agreement * self.bars_compared) / self.bars_compared
                != self.agreement
            ):
                raise ScoreError(
                    f"{self.experiment_id}: agreement {self.agreement!r} is not a whole count of "
                    f"{self.bars_compared} bar(s)"
                )

    @property
    def experiment_id(self) -> str:
        return experiment_id(self.arm, self.replicate)

    @property
    def found(self) -> bool:
        """A rule was selected: a candidate recovery on a conditional world, a false call on a null one."""
        return self.selected is not None

    @property
    def readable_holdout(self) -> float | None:
        """The holdout net Sharpe, unless the run was ruined: no ratio of a ruined run is read."""
        return None if self.holdout_ruined else self.holdout_sharpe

    @property
    def undefined(self) -> bool:
        """A rule was compared and neither it nor the planted one held a position in the holdout."""
        return self.recovery is not None and self.found and self.agreement is None

    def recovered_at(self, theta: float) -> bool:
        """Recovered, had the agreement threshold been ``theta``: the world's holdout rule still applies."""
        if self.recovery is None:
            raise ScoreError(f"{self.experiment_id}: nothing is planted on this world to recover")
        if self.agreement is None:
            return False
        profitable = self.readable_holdout is not None and self.readable_holdout > 0
        return self.agreement >= theta and (
            profitable or not self.recovery.require_positive_holdout
        )

    @property
    def recovered(self) -> bool | None:
        """Whether the planted rule was recovered (the module docstring); ``None`` on a null world."""
        return None if self.recovery is None else self.recovered_at(self.recovery.theta_behavior)

    def describe(self, *, holdout: bool = True) -> str:
        """The score as a line; ``holdout`` False keeps everything read from the holdout off the console."""
        line = f"{self.experiment_id}: {self._outcome(holdout=holdout)}"
        if self.ended is Ended.SEAM_FAILED:
            line += "; its seam failed, so the report does not count it"
        return line

    def _outcome(self, *, holdout: bool) -> str:
        figure = ""
        if holdout and self.found:
            figure = f", holdout net sharpe {self.holdout_sharpe:.4f}"
            if self.holdout_ruined:
                figure += " (ruined)"
        if self.recovery is None:
            return f"false call, trial #{self.selected}{figure}" if self.found else "no false call"
        if not self.found:
            return "nothing selected; not recovered"
        distance = f"structural distance {self.structural_distance:.4f}"
        if not holdout:
            return f"trial #{self.selected}: {distance}; the rest is not shown on a test seed"
        agreement = (
            "undefined (neither rule held a position)"
            if self.agreement is None
            else f"{self.agreement:.4f} over {self.bars_compared} bar(s)"
        )
        return f"trial #{self.selected}: direction agreement {agreement}, {distance}{figure}; " + (
            "recovered" if self.recovered else "not recovered"
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "arm": self.arm.value,
            "replicate": self.replicate,
            "ended": self.ended.value,
            "selected": self.selected,
            "structural_distance": self.structural_distance,
            "agreement": self.agreement,
            "bars_compared": self.bars_compared,
            "holdout_sharpe": self.holdout_sharpe,
            "holdout_ruined": self.holdout_ruined,
            "recovered": self.recovered,
        }

    @classmethod
    def from_dict(cls, payload: object, recovery: Recovery | None) -> ArmScore:
        body = _read.exact(
            payload,
            (
                "arm",
                "replicate",
                "ended",
                "selected",
                "structural_distance",
                "agreement",
                "bars_compared",
                "holdout_sharpe",
                "holdout_ruined",
                "recovered",
            ),
            "score",
        )
        score = cls(
            arm=body["arm"],
            replicate=body["replicate"],
            ended=body["ended"],
            selected=body["selected"],
            structural_distance=body["structural_distance"],
            agreement=body["agreement"],
            bars_compared=body["bars_compared"],
            holdout_sharpe=body["holdout_sharpe"],
            holdout_ruined=body["holdout_ruined"],
            recovery=recovery,
        )
        if body["recovered"] != score.recovered:
            raise ScoreError(
                f"{score.experiment_id}: the record says recovered = {body['recovered']!r}; its "
                f"figures give {score.recovered!r}"
            )
        return score


@dataclass(frozen=True)
class Scores:
    """``scores.json``: every arm of one world, scored under the manifest's recovery rule.

    ``planted_holdout_sharpe`` is the planted rule's own holdout figure,
    for the reader to set each selected rule's beside, and
    ``planted_holdout_ruined`` says whether that ratio may be read at all;
    the figure and ``recovery`` are ``None`` together, on a null world.
    ``manifest_digest`` binds the
    record to its conditions, as the other two records' does.
    """

    world_id: str
    manifest_digest: str
    scoring_version: str
    planted_holdout_sharpe: float | None
    planted_holdout_ruined: bool
    recovery: Recovery | None
    results: tuple[ArmScore, ...]

    def __post_init__(self) -> None:
        _read.text(self.world_id, "world_id")
        _read.digest(self.manifest_digest, "manifest_digest")
        _read.text(self.scoring_version, "scoring_version")
        if self.planted_holdout_sharpe is not None:
            _read.number(self.planted_holdout_sharpe, "planted_holdout_sharpe")
        if self.recovery is not None and not isinstance(self.recovery, Recovery):
            raise ScoreError("recovery: expected a Recovery or None")
        if (self.planted_holdout_sharpe is None) != (self.recovery is None):
            raise ScoreError(
                "planted_holdout_sharpe and recovery are both present on a world that plants a "
                "rule and both null on one that does not"
            )
        _read.flag(self.planted_holdout_ruined, "planted_holdout_ruined")
        if self.planted_holdout_ruined and self.planted_holdout_sharpe is None:
            raise ScoreError("planted_holdout_ruined: nothing was planted to be ruined")
        results = tuple(_read.sequence(self.results, "results"))
        if not results or any(not isinstance(result, ArmScore) for result in results):
            raise ScoreError("results: at least one ArmScore")
        ids = [result.experiment_id for result in results]
        if len(set(ids)) != len(ids):
            raise ScoreError(f"results: each arm and replicate once, got {ids}")
        if any(result.recovery != self.recovery for result in results):
            raise ScoreError("results: every arm is scored under the world's one recovery rule")
        object.__setattr__(self, "results", results)

    @property
    def readable_planted(self) -> float | None:
        """The planted rule's holdout net Sharpe, unless its run was ruined."""
        return None if self.planted_holdout_ruined else self.planted_holdout_sharpe

    def describe(self, *, holdout: bool = True) -> list[str]:
        """The world's scores as lines; ``holdout`` False keeps every holdout figure off the console."""
        lines = (
            [
                f"planted rule: holdout net sharpe {self.planted_holdout_sharpe:.4f}"
                + (" (ruined)" if self.planted_holdout_ruined else "")
            ]
            if holdout and self.planted_holdout_sharpe is not None
            else []
        )
        return [*lines, *(result.describe(holdout=holdout) for result in self.results)]

    def to_dict(self) -> dict[str, object]:
        return {
            "scores_version": SCORES_VERSION,
            "world_id": self.world_id,
            "manifest_digest": self.manifest_digest,
            "scoring_version": self.scoring_version,
            "planted_holdout_sharpe": self.planted_holdout_sharpe,
            "planted_holdout_ruined": self.planted_holdout_ruined,
            "recovery": None if self.recovery is None else self.recovery.to_dict(),
            "results": [result.to_dict() for result in self.results],
        }

    @classmethod
    def from_dict(cls, payload: object) -> Scores:
        _read.version(payload, "scores_version", SCORES_VERSION)
        body = _read.exact(
            payload,
            (
                "scores_version",
                "world_id",
                "manifest_digest",
                "scoring_version",
                "planted_holdout_sharpe",
                "planted_holdout_ruined",
                "recovery",
                "results",
            ),
            "scores",
        )
        try:
            recovery = None if body["recovery"] is None else Recovery.from_dict(body["recovery"])
        except ManifestError as exc:
            raise ScoreError(str(exc)) from exc
        return cls(
            world_id=body["world_id"],
            manifest_digest=body["manifest_digest"],
            scoring_version=body["scoring_version"],
            planted_holdout_sharpe=body["planted_holdout_sharpe"],
            planted_holdout_ruined=body["planted_holdout_ruined"],
            recovery=recovery,
            results=tuple(
                ArmScore.from_dict(item, recovery)
                for item in _read.sequence(body["results"], "results")
            ),
        )

    def dumps(self) -> str:
        return dumps(self.to_dict())

    @classmethod
    def loads(cls, text: str) -> Scores:
        return cls.from_dict(_read.loads(text))


# -- scoring one world ------------------------------------------------------------------------------


@dataclass(frozen=True)
class HoldoutRead:
    """A rule read on the holdout: the figures its record must reproduce, and its positions."""

    metrics: SegmentMetrics
    positions: tuple[int, ...]


def _holdout_reader(
    ledger: Ledger, experiment: Experiment
) -> Callable[[StrategySpec], HoldoutRead]:
    """A reader of rules on ``experiment``'s holdout: one frame, one read per distinct rule, nothing filed."""
    frame = frame_for(ledger, experiment, holdout=True)
    first, stop = experiment.split.holdout.bar_range([bar.open_time for bar in frame.bundle.bars])
    cache: dict[str, HoldoutRead] = {}

    def read(spec: StrategySpec) -> HoldoutRead:
        key = spec_hash(spec)
        if key not in cache:
            result = evaluate_unfiled(ledger, experiment, spec, holdout=True, frame=frame).holdout
            assert result is not None  # holdout=True is what was asked for
            cache[key] = HoldoutRead(
                metrics=SegmentMetrics.from_result(result),
                positions=positions(result.trades, first=first, count=stop - first),
            )
        return cache[key]

    return read


def _rule_to_compare(searched: Searched) -> Planted | None:
    """The planted rule when the store is needed: some arm selected a rule to compare with it."""
    planted = searched.manifest.world.planted
    if planted is None or not any(
        r.verdict.selected is not None for r in searched.verdicts.results
    ):
        return None
    return planted


def _no_reader(spec: StrategySpec) -> HoldoutRead:
    raise ScoreError(
        "an arm selected a rule on a world that plants one, so the holdout has to be read, and "
        "no reader was given"
    )


def _world_rule(searched: Searched) -> tuple[Recovery | None, SegmentMetrics | None]:
    """The world's recovery rule and the planted rule's holdout figure; both ``None`` on a null world."""
    figures = searched.oracle.planted
    if searched.manifest.world.planted is None or figures is None or figures.holdout is None:
        return None, None
    return searched.manifest.scoring.recovery, figures.holdout


def _planted_pair(figure: SegmentMetrics | None) -> tuple[float | None, bool]:
    """The two fields of ``Scores`` the planted rule's holdout figure gives."""
    return (None, False) if figure is None else (figure.net.sharpe, figure.ruined)


def _selected_spec(result: ArmResult) -> StrategySpec | None:
    """The rule the arm selected, as its snapshot holds it."""
    selected = result.verdict.selected
    if selected is None:
        return None
    return next(t.spec for t in result.snapshot.trials if t.trial_id == selected)


def _arm_score(
    result: ArmResult, searched: Searched, *, agreement: float | None, bars: int
) -> ArmScore:
    """An arm's score as its records give it; only the agreement has to be read off the store.

    The one definition, so ``read_scores`` holds a written score to exactly
    what ``score_records`` would write.
    """
    planted = searched.manifest.world.planted
    spec = _selected_spec(result)
    holdout = result.holdout
    return ArmScore(
        arm=result.arm,
        replicate=result.replicate,
        ended=result.snapshot.ended,
        selected=result.verdict.selected,
        structural_distance=(
            None if spec is None or planted is None else structural_distance(spec, planted.spec)
        ),
        agreement=agreement,
        bars_compared=bars,
        holdout_sharpe=None if holdout is None else holdout.net.sharpe,
        holdout_ruined=holdout is not None and holdout.ruined,
        recovery=_world_rule(searched)[0],
    )


def score_records(
    searched: Searched, *, read: Callable[[StrategySpec], HoldoutRead] = _no_reader
) -> Scores:
    """Every arm of ``searched`` scored; ``read`` reads a rule's holdout, when one has to be read.

    Each read has to reproduce the figure already on record for that rule
    (the module docstring).
    """
    manifest, verdicts = searched.manifest, searched.verdicts
    recovery, planted_figure = _world_rule(searched)
    planted_sharpe, planted_ruined = _planted_pair(planted_figure)
    rule = _rule_to_compare(searched)
    baseline: HoldoutRead | None = None
    if rule is not None:
        baseline = read(rule.spec)
        if baseline.metrics != planted_figure:
            raise ScoreError(
                f"{verdicts.world_id}: the planted rule's holdout read from the store is not the "
                f"figure in the oracle's record; the store is not the one the record was written "
                f"from"
            )
    results: list[ArmScore] = []
    for result in verdicts.results:
        agreement: float | None = None
        bars = 0
        spec = _selected_spec(result)
        if spec is not None and baseline is not None:
            chosen = read(spec)
            if chosen.metrics != result.holdout:
                raise ScoreError(
                    f"{result.experiment_id}: trial #{result.verdict.selected}'s holdout read from "
                    f"the store is not the figure its promotion recorded; the store is not the one "
                    f"the record was written from"
                )
            agreement, bars = direction_agreement(chosen.positions, baseline.positions)
        results.append(_arm_score(result, searched, agreement=agreement, bars=bars))
    return Scores(
        world_id=verdicts.world_id,
        manifest_digest=verdicts.manifest_digest,
        scoring_version=manifest.scoring.version,
        planted_holdout_sharpe=planted_sharpe,
        planted_holdout_ruined=planted_ruined,
        recovery=recovery,
        results=tuple(results),
    )


def require_this_scorer(manifest: Manifest) -> None:
    """Refuse a manifest that names a scorer this build does not have."""
    if manifest.scoring.version != SCORING_VERSION:
        raise ScoreError(
            f"scoring.version: this build scores by version {SCORING_VERSION}, the manifest asks "
            f"for {manifest.scoring.version!r}"
        )


def score_world(run_dir: Path, *, on_line: Callable[[str], None] | None = None) -> Scores:
    """Score a searched run directory and write ``scores.json`` (the module docstring).

    ``on_line`` is told the world's scores; on a test seed, without
    anything read from the holdout (plan §4.2).
    """
    searched = read_searched(run_dir)
    if searched is None:
        raise ScoreError(f"{run_dir} has not been searched; run `search` first")
    require_this_scorer(searched.manifest)
    if _rule_to_compare(searched) is None:
        scores = score_records(searched)
    else:
        path = store_path(run_dir, Arm.GRID, 0)
        if not path.is_file():
            raise ScoreError(f"{run_dir} holds no {path.name} for the grid arm; scoring reads it")
        with ResearchStore(path) as store:
            scores = score_records(
                searched,
                read=_holdout_reader(Ledger(store), searched.verdicts.grid.snapshot.experiment),
            )
    atomic_write_bytes(run_dir / SCORES_FILENAME, scores.dumps().encode("utf-8"))
    if on_line is not None:
        for line in scores.describe(holdout=searched.manifest.world.seed_set is SeedSet.DEV):
            on_line(line)
    return scores


# -- the table over worlds --------------------------------------------------------------------------


def read_scores(run_dir: Path, searched: Searched) -> Scores:
    """``scores.json`` of a searched directory, held to the records and the manifest beside it."""
    path = run_dir / SCORES_FILENAME
    if not path.is_file():
        raise ScoreError(f"{run_dir} is searched but holds no {SCORES_FILENAME}; run `score` on it")
    scores = read_record(path, Scores.loads)
    manifest, verdicts = searched.manifest, searched.verdicts
    if (scores.world_id, scores.manifest_digest) != (verdicts.world_id, verdicts.manifest_digest):
        raise ScoreError(
            f"{path} is for {scores.world_id} under manifest {scores.manifest_digest}; the "
            f"records beside it are for {verdicts.world_id} under {verdicts.manifest_digest}"
        )
    require_this_scorer(manifest)
    recovery, planted_figure = _world_rule(searched)
    if (
        scores.scoring_version != manifest.scoring.version
        or scores.recovery != recovery
        or (scores.planted_holdout_sharpe, scores.planted_holdout_ruined)
        != _planted_pair(planted_figure)
    ):
        raise ScoreError(
            f"{path}: its scoring version, recovery rule or planted holdout figure is not the one "
            f"the manifest and oracle record beside it give"
        )
    by_arm = {(result.arm, result.replicate): result for result in verdicts.results}
    if {(score.arm, score.replicate) for score in scores.results} != set(by_arm):
        raise ScoreError(f"{path} does not score the arms {sorted(by_arm, key=str)}")
    for score in scores.results:
        result = by_arm[(score.arm, score.replicate)]
        # Everything but the agreement and the bars it was taken over is a
        # function of the records; those two need a store, which the table
        # does not open.
        if score != _arm_score(
            result, searched, agreement=score.agreement, bars=score.bars_compared
        ):
            raise ScoreError(
                f"{path}: {score.experiment_id} is not scored as {VERDICTS_FILENAME} records it"
            )
    return scores


_SEALED: Final = "the figures read from the holdout are sealed on a test seed; --unseal prints them"


def _left_out(ruined: int) -> str:
    return f" ({ruined} ruined run(s) left out)" if ruined else ""


def _ratio(count: int, of: int) -> str:
    return f"{count}/{of} = {'n/a' if of == 0 else f'{count / of:.1%}'}"


def _mean(values: Sequence[float]) -> str:
    return "n/a" if not values else f"{fmean(values):.4f}"


@dataclass(frozen=True)
class ReportEntry:
    """One finished search on one world, as the table reads it; a failed seam is not one.

    ``readable_planted`` is ``Scores.readable_planted``: ``None`` on a null
    world and where the planted rule's own holdout run was ruined.
    """

    world_id: str
    score: ArmScore
    first_eligible_round: int | None
    oracle_passed: bool
    readable_planted: float | None
    effort: tuple[int, int]

    def __post_init__(self) -> None:
        _read.text(self.world_id, "entry.world_id")
        if not isinstance(self.score, ArmScore):
            raise ScoreError("entry.score: expected an ArmScore")
        if self.score.ended is Ended.SEAM_FAILED:
            raise ScoreError(
                f"{self.score.experiment_id}: an arm whose seam failed is counted apart, not "
                f"entered"
            )
        if self.first_eligible_round is not None:
            _read.whole(self.first_eligible_round, "entry.first_eligible_round", minimum=1)
        _read.flag(self.oracle_passed, "entry.oracle_passed")
        answers, rules = self.effort
        _read.whole(answers, "entry.answers", minimum=0)
        _read.whole(rules, "entry.distinct_rules", minimum=0)
        if self.readable_planted is not None:
            _read.number(self.readable_planted, "entry.readable_planted")
        if self.score.recovery is None and (
            self.oracle_passed or self.readable_planted is not None
        ):
            raise ScoreError(
                "entry: a null world plants nothing, so it has no oracle pass or planted figure"
            )


@dataclass(frozen=True)
class ReportRow:
    """One arm on one set of conditions across a root: the plan §3 table, row by row.

    Keyed as the calibration's rows are, so one rate is never taken over
    two sets of conditions. The headline counts SEARCHES, as the
    calibration does (every world of a row has the same replicates), and
    says over how many worlds; the oracle's pass is a world's and is counted
    per world, and where a world has several replicates a line says how
    they agreed, which is what tells a hard world from an unlucky search
    (plan §4.2). ``failed`` counts arms whose seam failed, set apart as the
    calibration sets them apart. A search that selected nothing, and an
    agreement that is undefined, stay in the denominator (plan §4.6); so
    does a run that was ruined, whose ratio is not read: the holdout means
    are over the pairs where neither side was ruined.

    On a test seed everything read from the holdout is SEALED unless asked
    for (:meth:`describe`), so reading the test set is a deliberate act
    (plan §4.2) and not a side effect of looking at a half-scored root.
    """

    kind: WorldKind
    null_definition_version: str
    seed_set: SeedSet
    conditions: str
    arm: Arm
    entries: tuple[ReportEntry, ...]
    failed: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", _read.enum(WorldKind, self.kind, "row.kind"))
        _read.text(self.null_definition_version, "row.null_definition_version")
        object.__setattr__(self, "seed_set", _read.enum(SeedSet, self.seed_set, "row.seed_set"))
        _read.digest(self.conditions, "row.conditions")
        object.__setattr__(self, "arm", _read.enum(Arm, self.arm, "row.arm"))
        _read.whole(self.failed, "row.failed", minimum=0)
        entries = tuple(_read.sequence(self.entries, "row.entries"))
        if any(not isinstance(entry, ReportEntry) for entry in entries):
            raise ScoreError("row.entries: expected ReportEntries")
        if not entries and not self.failed:
            raise ScoreError("row: a row tallies at least one result")
        strangers = [e.score.experiment_id for e in entries if e.score.arm is not self.arm]
        if strangers:
            raise ScoreError(f"row: {strangers} are not of this row's arm, {self.arm.value}")
        if entries and (self.kind is WorldKind.NULL_V1) != all(
            entry.score.recovery is None for entry in entries
        ):
            raise ScoreError(
                f"row.kind: a {self.kind.value} row holds scores of the other kind of world"
            )
        object.__setattr__(self, "entries", entries)

    @property
    def searches(self) -> int:
        return len(self.entries)

    def _by_world(self) -> dict[str, list[ReportEntry]]:
        grouped: dict[str, list[ReportEntry]] = {}
        for entry in self.entries:
            grouped.setdefault(entry.world_id, []).append(entry)
        return grouped

    @property
    def worlds(self) -> int:
        return len(self._by_world())

    @property
    def found(self) -> int:
        return sum(entry.score.found for entry in self.entries)

    @property
    def recovered(self) -> int:
        return sum(entry.score.recovered is True for entry in self.entries)

    @property
    def oracle_worlds(self) -> int:
        """The worlds the oracle had room on: a fact about the world, counted once."""
        return sum(group[0].oracle_passed for group in self._by_world().values())

    @property
    def searches_with_room(self) -> int:
        return sum(entry.oracle_passed for entry in self.entries)

    @property
    def recovered_with_room(self) -> int:
        return sum(entry.score.recovered is True and entry.oracle_passed for entry in self.entries)

    def _first_eligible(self) -> str:
        rounds = [
            e.first_eligible_round for e in self.entries if e.first_eligible_round is not None
        ]
        return "n/a" if not rounds else f"{median(rounds):g}"

    def _replicate_spread(self, hit: Callable[[ArmScore], bool], what: str) -> list[str]:
        """How each world's replicates agreed; no line where every world has one."""
        if self.searches == self.worlds:
            return []
        hits = [[hit(entry.score) for entry in group] for group in self._by_world().values()]
        every = sum(all(world) for world in hits)
        none = sum(not any(world) for world in hits)
        return [
            f"  worlds by replicates {what}: all {every}, some {len(hits) - every - none}, "
            f"none {none}"
        ]

    def describe(self, *, unseal: bool = False) -> list[str]:
        """The row as lines: the headline first, then the diagnostics; see the class docstring on sealing.

        The last line says what the row's searches actually spent, and is
        no holdout figure, so a sealed row prints it too.
        """
        effort = [f"  {effort_note([e.effort for e in self.entries])}"] if self.entries else []
        return [*self._lines(unseal=unseal), *effort]

    def _lines(self, *, unseal: bool) -> list[str]:
        head = row_head(
            (self.kind, self.null_definition_version, self.seed_set, self.conditions, self.arm)
        )
        tail = seam_note(self.failed)
        over = f"{self.searches} search(es) over {self.worlds} world(s)"
        first = f"first eligible round median {self._first_eligible()} (a diagnostic)"
        sealed = self.seed_set is SeedSet.TEST and not unseal
        if self.kind is WorldKind.NULL_V1:
            calls = [
                e.score.readable_holdout
                for e in self.entries
                if e.score.readable_holdout is not None
            ]
            figure = (
                _SEALED
                if sealed
                else f"holdout net sharpe of the false calls, mean {_mean(calls)}"
                f"{_left_out(self.found - len(calls))}"
            )
            return [
                f"{head}false calls {_ratio(self.found, self.searches)} ({over}){tail}",
                *self._replicate_spread(lambda s: s.found, "with a false call"),
                f"  {first}; {figure}",
            ]
        room = f"the oracle had room on {_ratio(self.oracle_worlds, self.worlds)} of the worlds"
        if sealed:
            return [
                f"{head}{over}; {room}; a rule selected in "
                f"{_ratio(self.found, self.searches)}{tail}",
                f"  {first}; {_SEALED}",
            ]
        scores = [entry.score for entry in self.entries]
        agreements = [s.agreement for s in scores if s.agreement is not None]
        distances = [s.structural_distance for s in scores if s.structural_distance is not None]
        pairs = [
            (e.score.readable_holdout, e.readable_planted)
            for e in self.entries
            if e.score.readable_holdout is not None and e.readable_planted is not None
        ]
        lines = [
            f"{head}recovered {_ratio(self.recovered, self.searches)} ({over}); {room}; recovered "
            f"where it had room {_ratio(self.recovered_with_room, self.searches_with_room)}{tail}",
            *self._replicate_spread(lambda s: s.recovered is True, "recovered"),
            f"  nothing selected {self.searches - self.found}, agreement undefined "
            f"{sum(s.undefined for s in scores)}; direction agreement median "
            f"{'n/a' if not agreements else f'{median(agreements):.4f}'}, structural distance "
            f"mean {_mean(distances)}",
            f"  holdout net sharpe over {len(pairs)} pair(s): selected mean "
            f"{_mean([ours for ours, _ in pairs])}, the planted rule mean "
            f"{_mean([theirs for _, theirs in pairs])}{_left_out(self.found - len(pairs))}; "
            f"{first}",
        ]
        if self.seed_set is SeedSet.DEV:
            at_theta = ", ".join(
                f"{theta:g}: {sum(s.recovered_at(theta) for s in scores)}"
                for theta in _THETA_SENSITIVITY
            )
            lines.append(f"  recovered at theta (development seeds): {at_theta}")
        return lines


def report_table(run_dirs: Iterable[Path]) -> list[ReportRow]:
    """Every searched and scored world tallied per row key; a searched one unscored is refused by name.

    What is read and refused is :func:`~contrib.discovery.bench.read_searched`'s
    and :func:`read_scores`'s, and no store is opened.
    """
    rows: dict[RowKey, list[ReportEntry]] = {}
    failed: Counter[RowKey] = Counter()
    for run_dir, searched in iter_searched(run_dirs):
        scores = read_scores(run_dir, searched)
        oracle = searched.oracle
        by_arm = {(r.arm, r.replicate): r for r in searched.verdicts.results}
        for score in scores.results:
            key = row_key(searched.manifest, score.arm)
            entries = rows.setdefault(key, [])
            if score.ended is Ended.SEAM_FAILED:
                failed[key] += 1
                continue
            entries.append(
                ReportEntry(
                    world_id=scores.world_id,
                    score=score,
                    first_eligible_round=by_arm[
                        (score.arm, score.replicate)
                    ].verdict.first_eligible_round,
                    oracle_passed=oracle.planted is not None and oracle.planted.passed,
                    readable_planted=scores.readable_planted,
                    effort=(
                        by_arm[(score.arm, score.replicate)].verdict.answers,
                        by_arm[(score.arm, score.replicate)].verdict.distinct_rules,
                    ),
                )
            )
    return [
        ReportRow(*key, entries=tuple(rows[key]), failed=failed[key]) for key in in_row_order(rows)
    ]
