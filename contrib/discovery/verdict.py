"""The final verdict of one arm (plan §4.5): a snapshot taken before any promotion, judged by a pure function.

An arm's search files trials as it goes, and each one is gated at the rule
count of ITS moment: the first trial has to clear ``sharpe_base``, the
tenth ``sharpe_base + k·ln 10``. The research loop never promotes, so what
the search "found" is not the set of trials that ever passed — it is the
set that still passes once the budget is spent and the count is final
(plan §1 #8). Success is read there, and there only; the round at which a
trial first passed is kept as a diagnostic, not as a result.

The judgement is taken as a SNAPSHOT, and from the snapshot alone, for a
reason the ledger forces. The research package's gate lists "already
promoted" as a blocker — a trial sees its holdout once — so asking the
ledger again after the selected trial is promoted would call it
ineligible, and a benchmark re-run would then disagree with the one that
chose it. So :func:`snapshot_arm` reads the ledger ONCE, before any
promotion (and refuses to run after one), keeps every input the gate
reads (each trial's figures, the final rule count, the penalty), and
:func:`final_verdict` re-applies the research package's own gate to that
record. Nothing is re-asked; ``tests/test_verdict.py`` holds that the
verdict is the ledger's before promotion and unchanged after it.

The gate is :func:`~contrib.autoresearch.ledger.promotion_verdict` itself,
applied to a trial rebuilt as MEASURED — not a copy of its five rules. The
"already promoted" blocker is thereby structurally absent, which is the one
difference the plan asks for; the other four (ruined, no trades, no gain,
below threshold) are exactly the ledger's.

How a search ENDED is read off the loop's report and the hypothesist's
own flag, and kept apart: a spent budget, a candidate pool walked to its
end (plan §4.3's exhausted protocol), an operator's interrupt, and a seam
that failed are four different facts about a run. The report alone tells
the interrupt apart (``search()`` names it) but not the exhausted pool
from the dead key — both arrive as the one error family — and reading
them as one would count a dead key as a pool walked to its end. Two
sources because ``search()`` keeps only the error's sentence; a report
that carried the stop's kind would make the flag redundant (an upstream
follow-up, not this package's to make).
"""

from __future__ import annotations

from dataclasses import dataclass

from .records import Reader, dumps
from .upstream import (
    INTERRUPTED,
    CostModel,
    Experiment,
    Ledger,
    Penalty,
    SearchReport,
    SegmentMetrics,
    Split,
    StrategySpec,
    Trial,
    TrialStatus,
    VocabEnum,
    parse_spec,
    promotion_verdict,
    spec_hash,
    spec_to_document,
)

__all__ = [
    "ArmSnapshot",
    "ArmVerdict",
    "Ended",
    "TrialRecord",
    "VerdictError",
    "classify_ending",
    "final_verdict",
    "snapshot_arm",
]


class VerdictError(RuntimeError):
    """The snapshot cannot be taken, read or built, and the sentence says why.

    A ``RuntimeError`` as the ledger's own refusals are, because the
    refusals that matter most are about the STATE of a run — a promotion
    made before the snapshot, a report for another experiment, a record on
    disk a later build cannot read. The records' own gates raise it too, so
    a caller has one class to catch for "this is not a usable snapshot"
    whether the record came from a file or was built in code.
    """


_read = Reader(VerdictError, "snapshot")


class Ended(VocabEnum, noun="search ending"):
    """Why the arm's search stopped. ``budget_spent`` is the only ending that spent it all."""

    BUDGET_SPENT = "budget_spent"
    EXHAUSTED = "exhausted"
    INTERRUPTED = "interrupted"
    SEAM_FAILED = "seam_failed"


def classify_ending(report: SearchReport, *, exhausted: bool) -> Ended:
    """Read the ending off the loop's report and the hypothesist's own flag.

    ``search()`` knows two early stops — a ``HypothesistError`` and an
    interrupt — and records the first as the error's sentence. A candidate
    pool walked to its end raises that same error family (plan §4.3: the
    research package is not changed to know about pools), so the report
    alone cannot tell "exhausted" from "the seam failed"; the hypothesist
    says which, and ``exhausted`` is its flag. A flag raised against a
    report that spent its whole budget is a contradiction — the pool ran
    out, yet every round answered — and is refused rather than read; so is
    a report with no stop sentence that did not spend its budget, which
    ``search()`` never returns.
    """
    if report.stopped is None:
        if report.spent != report.max_trials:
            raise VerdictError(
                f"{report.experiment_id}'s report names no stop and spent {report.spent} of "
                f"{report.max_trials} round(s); a search that stopped short says why"
            )
        if exhausted:
            raise VerdictError(
                f"the hypothesist says its candidates ran out, but {report.experiment_id}'s "
                f"search spent all {report.max_trials} round(s); a pool that ran out stops a run"
            )
        return Ended.BUDGET_SPENT
    if report.stopped == INTERRUPTED:
        return Ended.INTERRUPTED
    return Ended.EXHAUSTED if exhausted else Ended.SEAM_FAILED


# -- the snapshot -------------------------------------------------------------------


@dataclass(frozen=True)
class TrialRecord:
    """One trial as the snapshot keeps it: the rule and its two measured windows, nothing else.

    No status and no holdout: the first is what the snapshot exists to not
    re-ask, the second is what the gate never reads (plan §1 #8). The
    rule's hash is derived from the spec and written beside it for the
    reader of the file; a record read back whose written hash is not its
    spec's is refused.
    """

    trial_id: int
    spec: StrategySpec
    train: SegmentMetrics
    validation: SegmentMetrics

    def __post_init__(self) -> None:
        _read.whole(self.trial_id, "trial.trial_id", minimum=0)
        if not isinstance(self.spec, StrategySpec):
            raise VerdictError(f"trial #{self.trial_id}: the record's spec is not a StrategySpec")
        for name in ("train", "validation"):
            if not isinstance(getattr(self, name), SegmentMetrics):
                raise VerdictError(f"trial #{self.trial_id}: {name} is not a SegmentMetrics")

    @property
    def spec_hash(self) -> str:
        return spec_hash(self.spec)

    def as_measured(self, experiment_id: str) -> Trial:
        """The trial as the gate reads it: measured, never promoted, whatever the ledger says now."""
        return Trial(
            trial_id=self.trial_id,
            experiment_id=experiment_id,
            family=self.spec.family.value,
            spec=self.spec,
            spec_hash=self.spec_hash,
            train=self.train,
            validation=self.validation,
            holdout=None,
            status=TrialStatus.MEASURED,
            created_at="",
            promoted_at=None,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "trial_id": self.trial_id,
            "spec": spec_to_document(self.spec),
            "spec_hash": self.spec_hash,
            "train": self.train.to_dict(),
            "validation": self.validation.to_dict(),
        }

    @classmethod
    def from_dict(cls, payload: object) -> TrialRecord:
        body = _read.exact(payload, ("trial_id", "spec", "spec_hash", "train", "validation"), "trial")
        try:
            record = cls(
                trial_id=body["trial_id"],
                spec=parse_spec(body["spec"]),
                train=SegmentMetrics.from_dict(body["train"]),
                validation=SegmentMetrics.from_dict(body["validation"]),
            )
        except ValueError as exc:
            raise VerdictError(f"trial: {exc}") from exc
        if body["spec_hash"] != record.spec_hash:
            raise VerdictError(
                f"trial #{record.trial_id}: the record's spec_hash {body['spec_hash']!r} is not "
                f"the hash of its spec ({record.spec_hash!r})"
            )
        return record


@dataclass(frozen=True)
class ArmSnapshot:
    """Everything the final verdict reads, taken from one arm's ledger before any promotion.

    ``distinct_rules`` is the ledger's ``rules_tried`` for the coin — the
    ``n`` of the gate — and ``answers`` is how many rounds the search
    spent, refusals and duplicates included (plan §4.3 reports both, and
    does not mix them). ``first_eligible_round`` is the diagnostic: the
    earliest round whose answer passed the gate AT THAT MOMENT, which says
    nothing about whether it still does.
    """

    experiment: Experiment
    ended: Ended
    answers: int
    distinct_rules: int
    first_eligible_round: int | None
    trials: tuple[TrialRecord, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.experiment, Experiment):
            raise VerdictError("snapshot.experiment: expected an Experiment")
        object.__setattr__(self, "ended", _read.enum(Ended, self.ended, "snapshot.ended"))
        _read.whole(self.answers, "snapshot.answers", minimum=0)
        _read.whole(self.distinct_rules, "snapshot.distinct_rules", minimum=0)
        if self.first_eligible_round is not None:
            _read.whole(self.first_eligible_round, "snapshot.first_eligible_round", minimum=1)
        trials = tuple(_read.sequence(self.trials, "snapshot.trials"))
        if any(not isinstance(record, TrialRecord) for record in trials):
            raise VerdictError("snapshot.trials: expected TrialRecords")
        hashes = {record.spec_hash for record in trials}
        if len(hashes) != len(trials):
            raise VerdictError("a snapshot lists each rule once; two records share a spec_hash")
        # The count the gate divides by is at least the rules this record
        # holds: a ledger with other experiments on the coin may count more
        # (each raised the bar for every trial), never fewer.
        if self.distinct_rules < len(hashes):
            raise VerdictError(
                f"distinct_rules is {self.distinct_rules}, below the {len(hashes)} distinct "
                f"rule(s) the snapshot itself holds"
            )
        # The record is judged from alone, so the facts the ledger would have
        # held are held here: one row per trial id, no trial without the answer
        # it came from, a passing round only among the rounds spent, and every
        # figure for the window the gate reads it as.
        ids = [record.trial_id for record in trials]
        if len(set(ids)) != len(ids):
            raise VerdictError("a snapshot lists each trial once; two records share a trial_id")
        if len(trials) > self.answers:
            raise VerdictError(
                f"snapshot.trials: {len(trials)} trial(s) from {self.answers} answer(s); every "
                f"trial came from one answer"
            )
        if self.first_eligible_round is not None:
            if not trials:
                raise VerdictError(
                    "snapshot.first_eligible_round: a round passed the gate, but no trial is held"
                )
            if self.first_eligible_round > self.answers:
                raise VerdictError(
                    f"snapshot.first_eligible_round: round {self.first_eligible_round} of "
                    f"{self.answers} answer(s)"
                )
        split = self.experiment.split
        for record in trials:
            for name, segment in (("train", split.train), ("validation", split.validation)):
                filed = getattr(record, name).segment
                if filed != segment:
                    raise VerdictError(
                        f"trial #{record.trial_id}: its {name} figures are for {filed}, not the "
                        f"experiment's {segment}"
                    )
        object.__setattr__(self, "trials", trials)

    def to_dict(self) -> dict[str, object]:
        return {
            "experiment": _experiment_document(self.experiment),
            "ended": self.ended.value,
            "answers": self.answers,
            "distinct_rules": self.distinct_rules,
            "first_eligible_round": self.first_eligible_round,
            "trials": [record.to_dict() for record in self.trials],
        }

    @classmethod
    def from_dict(cls, payload: object) -> ArmSnapshot:
        body = _read.exact(
            payload,
            ("experiment", "ended", "answers", "distinct_rules", "first_eligible_round", "trials"),
            "snapshot",
        )
        return cls(
            experiment=_experiment_from_document(body["experiment"]),
            ended=body["ended"],
            answers=body["answers"],
            distinct_rules=body["distinct_rules"],
            first_eligible_round=body["first_eligible_round"],
            trials=tuple(
                TrialRecord.from_dict(record)
                for record in _read.sequence(body["trials"], "snapshot.trials")
            ),
        )

    def dumps(self) -> str:
        return dumps(self.to_dict())

    @classmethod
    def loads(cls, text: str) -> ArmSnapshot:
        return cls.from_dict(_read.loads(text))


def snapshot_arm(
    ledger: Ledger, experiment: Experiment, report: SearchReport, *, exhausted: bool
) -> ArmSnapshot:
    """Read one arm's ledger once, after its search and before any promotion.

    Refused if any trial of the experiment is already promoted: the
    snapshot is the record of the run's own choice, and a promotion made
    before it means something else chose. One search per arm: the report
    has to account for every answer the ledger filed, since the diagnostic
    round and the ending are read off the report while the counts are read
    off the ledger, and a report of a second invocation would number its
    rounds from one again.
    """
    ledger.require_stored(experiment)
    if report.experiment_id != experiment.experiment_id:
        raise VerdictError(
            f"the report is for {report.experiment_id}, the experiment is "
            f"{experiment.experiment_id}"
        )
    answers = sum(ledger.proposal_counts(experiment.experiment_id).values())
    if report.spent != answers:
        raise VerdictError(
            f"the report spent {report.spent} round(s) but {experiment.experiment_id}'s ledger "
            f"holds {answers} answer(s); an arm is one search, and its snapshot reads that "
            f"search's report"
        )
    trials = ledger.trials(experiment.experiment_id)
    promoted = [trial.trial_id for trial in trials if trial.status is TrialStatus.PROMOTED]
    if promoted:
        raise VerdictError(
            f"trial(s) #{', #'.join(map(str, promoted))} of {experiment.experiment_id} are "
            f"already promoted; the snapshot is taken before any promotion, and nothing is "
            f"re-asked after"
        )
    first = min(
        (
            round_.number
            for round_ in report.rounds
            if round_.measurement is not None and round_.measurement.verdict.eligible
        ),
        default=None,
    )
    return ArmSnapshot(
        experiment=experiment,
        ended=classify_ending(report, exhausted=exhausted),
        answers=answers,
        distinct_rules=ledger.rules_tried(experiment.coin),
        first_eligible_round=first,
        trials=tuple(
            TrialRecord(
                trial_id=trial.trial_id,
                spec=trial.spec,
                train=trial.train,
                validation=trial.validation,
            )
            for trial in trials
        ),
    )


# -- the verdict ------------------------------------------------------------------------


@dataclass(frozen=True)
class ArmVerdict:
    """What one arm's search is worth, read off its snapshot.

    ``final_eligible`` lists every trial the gate passes at the final rule
    count, in the order the selection rule ranks them (validation net
    Sharpe descending, then trial id ascending), so the selected trial is
    its first entry and a reader can see what came second. ``threshold``
    is ``None`` only when no rule was tried on the coin at all: the
    research package's ``threshold()`` is for a count of at least one, and
    is not asked.
    """

    ended: Ended
    answers: int
    distinct_rules: int
    threshold: float | None
    final_eligible: tuple[int, ...]
    first_eligible_round: int | None

    def __post_init__(self) -> None:
        # The correlations the docstring states, checked — a verdict built by
        # hand (plan PR 4's report) must not be able to say what this one
        # cannot (the rule ``Round`` and ``Trial`` follow upstream).
        object.__setattr__(self, "ended", _read.enum(Ended, self.ended, "verdict.ended"))
        _read.whole(self.answers, "verdict.answers", minimum=0)
        _read.whole(self.distinct_rules, "verdict.distinct_rules", minimum=0)
        if (self.threshold is None) != (self.distinct_rules == 0):
            raise VerdictError(
                f"verdict.threshold: {self.threshold!r} with {self.distinct_rules} rule(s) tried; "
                f"the threshold is None exactly when no rule was tried"
            )
        if self.threshold is not None:
            _read.number(self.threshold, "verdict.threshold")
        eligible = tuple(
            _read.whole(trial_id, f"verdict.final_eligible[{index}]", minimum=0)
            for index, trial_id in enumerate(
                _read.sequence(self.final_eligible, "verdict.final_eligible")
            )
        )
        # Bounded by the answers, as the snapshot's trials are: every trial
        # came from one answer, and the count of rules on the coin may be
        # larger than this arm's answers.
        if len(set(eligible)) != len(eligible) or len(eligible) > min(
            self.distinct_rules, self.answers
        ):
            raise VerdictError(
                f"verdict.final_eligible: {eligible!r} lists a trial twice or more trials than "
                f"the {self.distinct_rules} rule(s) tried or the {self.answers} answer(s) spent"
            )
        object.__setattr__(self, "final_eligible", eligible)
        if self.first_eligible_round is not None:
            _read.whole(self.first_eligible_round, "verdict.first_eligible_round", minimum=1)
            if self.first_eligible_round > self.answers:
                raise VerdictError(
                    f"verdict.first_eligible_round: round {self.first_eligible_round} of "
                    f"{self.answers} answer(s)"
                )

    @property
    def selected(self) -> int | None:
        """The trial the arm chose: the first of ``final_eligible``, or none."""
        return self.final_eligible[0] if self.final_eligible else None

    @property
    def found(self) -> bool:
        """A non-empty ``final_eligible``: success on a conditional world, a false call on a null one."""
        return bool(self.final_eligible)

    def to_dict(self) -> dict[str, object]:
        return {
            "ended": self.ended.value,
            "answers": self.answers,
            "distinct_rules": self.distinct_rules,
            "threshold": self.threshold,
            "final_eligible": list(self.final_eligible),
            "selected": self.selected,
            "first_eligible_round": self.first_eligible_round,
        }


def final_verdict(snapshot: ArmSnapshot) -> ArmVerdict:
    """The research package's gate, re-applied to every trial at the FINAL rule count.

    Pure: the same snapshot gives the same verdict, before or after the
    ledger promoted anything. The selection is the plan's fixed order and
    nothing else — not the diagnostic round, not the order the trials were
    filed in.
    """
    experiment = snapshot.experiment
    count = snapshot.distinct_rules
    passing = [
        record
        for record in snapshot.trials
        if promotion_verdict(
            experiment, record.as_measured(experiment.experiment_id), count
        ).eligible
    ]
    ranked = sorted(passing, key=lambda record: (-record.validation.net.sharpe, record.trial_id))
    return ArmVerdict(
        ended=snapshot.ended,
        answers=snapshot.answers,
        distinct_rules=count,
        threshold=experiment.penalty.threshold(count) if count else None,
        final_eligible=tuple(record.trial_id for record in ranked),
        first_eligible_round=snapshot.first_eligible_round,
    )


# -- the experiment record ----------------------------------------------------------------
#
# The ledger has no ``to_dict`` for an experiment (its row is written column
# by column), so the snapshot writes one here: every field, through the
# records the fields already have, so the experiment comes back EQUAL and a
# round trip is testable by equality. An upstream ``Experiment.to_dict``
# would retire this (a follow-up candidate, not this package's to make).

_EXPERIMENT_KEYS = (
    "experiment_id",
    "coin",
    "costs",
    "split",
    "indicator_lookback",
    "penalty",
    "notes",
    "created_at",
)


def _experiment_document(experiment: Experiment) -> dict[str, object]:
    return {
        "experiment_id": experiment.experiment_id,
        "coin": experiment.coin,
        "costs": experiment.costs.to_dict(),
        "split": experiment.split.to_dict(),
        "indicator_lookback": experiment.indicator_lookback,
        "penalty": experiment.penalty.to_dict(),
        "notes": experiment.notes,
        "created_at": experiment.created_at,
    }


def _experiment_from_document(payload: object) -> Experiment:
    body = _read.exact(payload, _EXPERIMENT_KEYS, "snapshot.experiment")
    # The text fields first, by this reader: ``Experiment`` canonicalises the
    # coin with ``str`` methods and keeps ``created_at`` unchecked, so a
    # number there would escape the ``ValueError`` lane below as an
    # ``AttributeError``, or be filed silently.
    text = {name: _read.text(body[name], f"snapshot.experiment.{name}") for name in ("experiment_id", "coin")}
    strings = {
        name: _read.string(body[name], f"snapshot.experiment.{name}")
        for name in ("notes", "created_at")
    }
    try:
        return Experiment(
            costs=CostModel.from_dict(
                dict(_read.mapping(body["costs"], "snapshot.experiment.costs"))
            ),
            split=Split.from_dict(dict(_read.mapping(body["split"], "snapshot.experiment.split"))),
            indicator_lookback=body["indicator_lookback"],
            penalty=Penalty.from_dict(body["penalty"]),
            **text,
            **strings,
        )
    except ValueError as exc:
        raise VerdictError(f"snapshot.experiment: {exc}") from exc
