"""The oracle (plan §3 step 3, §4.4): the planted rule and the whole pool scored on an arm's store, filed nowhere.

The oracle is the planted rule measured under exactly the conditions the
arms search under — same store, same split, same costs — so that "the loop
did not find it" can be told apart from "it was not there to find" (plan
§3 step 3). It cannot go through :func:`~contrib.autoresearch.research.measure`:
that verb files what it scores as a trial, the next prompt would show the
model the answer before it searched, and the coin's rule count — the ``n``
of the promote threshold — would be one higher than the search earned
(plan §1 #4). It goes through the pure function ``calibrate`` uses instead,
:func:`~contrib.autoresearch.evaluator.evaluate_split`, which writes nothing
(plan §1 #5). ``tests/test_isolation.py`` holds that: the arm's store is
byte-identical around a call here.

**Room over the gate.** The oracle "has room" when the planted rule, as a
trial measured at the END of the budget — the gate at ``threshold(max_trials)``,
not ``threshold(1)`` (plan §4.4) — passes the research package's own gate
with its validation net Sharpe at least ``oracle_margin`` over that
threshold (:class:`PlantedFigures`). On development seeds that is what
sizes ``beta``; on test seeds it is recorded and the world is kept either
way (plan §4.2).

**The pool, whole.** The Grid arm's full-pool reference (plan §4.3): every
candidate's validation net Sharpe, read off one frame, with no budget
spent and nothing filed (:class:`PoolReference`). It is the reference
distribution of the pool on this world — what the best candidate of the
pool scores, and how many would clear the gate at the pool's own count —
and not an upper bound on the model arm, which may propose outside the
pool.

``holdout`` is the research package's own lock, restated: the caller names
the window at the call site. WHEN the oracle may name it is the benchmark's
protocol, not these functions' — train and validation figures may be read
before the arms search (to size the planted effect, on development seeds
only), the holdout figure only after every arm has searched and chosen
(plan §4.4), and :mod:`.bench` is where that order is kept: it writes
``oracle.json`` after the arms, and the ``oracle`` command previews the
two measured windows on development seeds alone.

The read mirrors the research package's private ``research._frame`` (the
bundle bound, the clean-history scan, the frame) and, for the holdout, the
tail check ``promote`` makes on the holdout-bound frame; it is kept in step
by hand because that helper is private. Publishing it upstream would make
:func:`frame_for` two lines (a follow-up candidate, not this package's to
make).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .manifest import Budget, Planted
from .records import Reader, RecordError, dumps
from .upstream import (
    Experiment,
    FeatureFrame,
    Ledger,
    SegmentMetrics,
    SegmentName,
    SplitResult,
    StrategySpec,
    Verdict,
    evaluate_split,
    load_bundle,
    require_clean_history,
    require_measurable_tail,
)
from .verdict import gate_at

__all__ = [
    "ORACLE_VERSION",
    "Oracle",
    "OracleError",
    "PlantedFigures",
    "PoolReference",
    "evaluate_unfiled",
    "frame_for",
    "planted_figures",
    "pool_reference",
]

# Bumped when a key of ``oracle.json`` is added, removed or re-read.
ORACLE_VERSION = 1


class OracleError(RecordError):
    """An oracle record cannot be read or built, and the sentence says why (as ``VerdictError``)."""


_read = Reader(OracleError, "oracle")


# -- the read path ------------------------------------------------------------------------


def frame_for(ledger: Ledger, experiment: Experiment, *, holdout: bool) -> FeatureFrame:
    """The frame a measurement of ``experiment`` runs on, read no further than the lock allows.

    The same bundle bound (``Split.loadable_until``) the research package's
    own measurement reads under, so a figure computed here is over exactly
    the bars a trial's would be; with ``holdout`` named, the bound moves to
    the holdout's end and the tail is checked as ``promote`` checks it. The
    experiment has to be the stored one: the split it carries is what makes
    an oracle figure comparable with the arm's trials.
    """
    ledger.require_stored(experiment)
    bundle = load_bundle(
        ledger.store,
        coin=experiment.coin,
        interval=experiment.split.interval,
        until_ms=experiment.split.loadable_until(holdout=holdout),
    )
    require_clean_history(bundle, experiment.split.interval)
    frame = FeatureFrame(bundle, indicator_lookback=experiment.indicator_lookback)
    if holdout:
        require_measurable_tail(frame)
    return frame


def evaluate_unfiled(
    ledger: Ledger,
    experiment: Experiment,
    spec: StrategySpec,
    *,
    holdout: bool,
    frame: FeatureFrame | None = None,
) -> SplitResult:
    """``spec`` scored on ``experiment``'s windows (the holdout only when named), filed nowhere.

    The full result, trades included, is returned: the scorer (plan §4.6)
    reads the planted side's positions off it, and
    ``SegmentMetrics.from_result`` gives the figures. ``frame`` is a frame
    :func:`frame_for` already read for this experiment, under at least the
    lock ``holdout`` names — a bar's features never read past it, so a
    holdout-bound frame scores the two earlier windows exactly as a
    narrower one would, and a run that reads several figures pays the
    indicator walk once.
    """
    if frame is None:
        frame = frame_for(ledger, experiment, holdout=holdout)
    return evaluate_split(spec, frame, experiment.split, experiment.costs, holdout=holdout)


# -- the planted rule's room over the gate -------------------------------------------------


@dataclass(frozen=True)
class PlantedFigures:
    """The planted rule measured as a trial at the end of the budget (the module docstring).

    ``threshold`` is the gate at ``budget`` rules tried and ``blockers``
    what the research package's whole gate said there (the sentences, as
    ``Verdict`` holds them: trades, gain, not ruined, over the threshold —
    empty when it passed); ``eligible``, ``margin`` (the validation net
    Sharpe's distance over the threshold) and ``passed`` (eligible with the
    margin at least ``oracle_margin``) are derived, written for the reader
    and checked on the read. ``holdout`` is ``None`` until the arms have
    searched. Each figure is held to the window it is filed under.
    """

    train: SegmentMetrics
    validation: SegmentMetrics
    holdout: SegmentMetrics | None
    budget: int
    threshold: float
    oracle_margin: float
    blockers: tuple[str, ...]

    def __post_init__(self) -> None:
        for name, window in (
            ("train", SegmentName.TRAIN),
            ("validation", SegmentName.VALIDATION),
            ("holdout", SegmentName.HOLDOUT),
        ):
            figure = getattr(self, name)
            if figure is None and name == "holdout":
                continue
            if not isinstance(figure, SegmentMetrics):
                raise OracleError(f"planted.{name}: expected a SegmentMetrics")
            if figure.segment.name is not window:
                raise OracleError(
                    f"planted.{name}: a figure for {figure.segment.name.value}, filed as {name}"
                )
        _read.whole(self.budget, "planted.budget", minimum=1)
        _read.number(self.threshold, "planted.threshold")
        _read.amount(self.oracle_margin, "planted.oracle_margin")
        object.__setattr__(
            self,
            "blockers",
            tuple(
                _read.text(sentence, f"planted.blockers[{index}]")
                for index, sentence in enumerate(_read.sequence(self.blockers, "planted.blockers"))
            ),
        )

    @property
    def eligible(self) -> bool:
        return not self.blockers

    @property
    def margin(self) -> float:
        return self.validation.net.sharpe - self.threshold

    @property
    def passed(self) -> bool:
        return self.eligible and self.margin >= self.oracle_margin

    def describe(self, *, holdout: bool = True) -> list[str]:
        """The figures as lines; ``holdout`` False keeps the holdout figure off the console."""
        lines = [
            f"planted rule: train net sharpe {self.train.net.sharpe:.4f}, validation "
            f"{self.validation.net.sharpe:.4f} against threshold({self.budget}) = "
            f"{self.threshold:.4f}: margin {self.margin:+.4f} "
            f"(oracle_margin {self.oracle_margin:g}), "
            + ("eligible" if self.eligible else "not eligible")
            + f" at the end of the budget; the oracle {'has' if self.passed else 'has NO'} room"
        ]
        if self.holdout is not None and holdout:
            lines.append(f"planted rule: holdout net sharpe {self.holdout.net.sharpe:.4f}")
        return lines

    def to_dict(self) -> dict[str, object]:
        return {
            "train": self.train.to_dict(),
            "validation": self.validation.to_dict(),
            "holdout": None if self.holdout is None else self.holdout.to_dict(),
            "budget": self.budget,
            "threshold": self.threshold,
            "oracle_margin": self.oracle_margin,
            "blockers": list(self.blockers),
            "eligible": self.eligible,
            "margin": self.margin,
            "passed": self.passed,
        }

    @classmethod
    def from_dict(cls, payload: object) -> PlantedFigures:
        body = _read.exact(
            payload,
            (
                "train",
                "validation",
                "holdout",
                "budget",
                "threshold",
                "oracle_margin",
                "blockers",
                "eligible",
                "margin",
                "passed",
            ),
            "planted",
        )
        try:
            figures = cls(
                train=SegmentMetrics.from_dict(body["train"]),
                validation=SegmentMetrics.from_dict(body["validation"]),
                holdout=(
                    None if body["holdout"] is None else SegmentMetrics.from_dict(body["holdout"])
                ),
                budget=body["budget"],
                threshold=body["threshold"],
                oracle_margin=body["oracle_margin"],
                blockers=body["blockers"],
            )
        except ValueError as exc:
            raise OracleError(f"planted: {exc}") from exc
        # Written for the reader of the file; checked against what they are
        # derived from, so a record edited by hand cannot say the oracle
        # passed when its figures say otherwise.
        written = (body["eligible"], body["margin"], body["passed"])
        derived = (figures.eligible, figures.margin, figures.passed)
        if written != derived:
            raise OracleError(
                f"planted: the record says eligible, margin, passed = {written!r}; its figures "
                f"give {derived!r}"
            )
        return figures


def planted_figures(
    ledger: Ledger,
    experiment: Experiment,
    planted: Planted,
    budget: Budget,
    *,
    holdout: bool,
    frame: FeatureFrame | None = None,
) -> PlantedFigures:
    """The planted rule scored on ``experiment`` and gated at the end of ``budget`` (plan §4.4)."""
    result = evaluate_unfiled(ledger, experiment, planted.spec, holdout=holdout, frame=frame)
    train = SegmentMetrics.from_result(result.train)
    validation = SegmentMetrics.from_result(result.validation)
    # The gate as the final verdict applies it, at a count — here the whole
    # budget, the most the arm's own count can reach; its threshold is the
    # one it judged by.
    gate = gate_at(experiment, planted.spec, train, validation, budget.max_trials)
    return PlantedFigures(
        train=train,
        validation=validation,
        holdout=None if result.holdout is None else SegmentMetrics.from_result(result.holdout),
        budget=budget.max_trials,
        threshold=gate.threshold,
        oracle_margin=budget.oracle_margin,
        blockers=gate.blockers,
    )


# -- the pool, whole ----------------------------------------------------------------------------


@dataclass(frozen=True)
class PoolReference:
    """Every candidate's validation net Sharpe, in pool order, and the gate at the pool's count.

    ``best`` is the number of the highest (the first, on a tie); ``clearing``
    how many candidates pass the research package's whole gate at
    ``threshold(size)`` — what a search that tried the whole pool would
    have to choose from.
    """

    validation_sharpes: tuple[float, ...]
    threshold: float
    clearing: int

    def __post_init__(self) -> None:
        sharpes = tuple(
            _read.number(value, f"pool.validation_sharpes[{index}]")
            for index, value in enumerate(
                _read.sequence(self.validation_sharpes, "pool.validation_sharpes")
            )
        )
        if not sharpes:
            raise OracleError("pool.validation_sharpes: a pool has at least one candidate")
        object.__setattr__(self, "validation_sharpes", sharpes)
        _read.number(self.threshold, "pool.threshold")
        # A candidate clears the gate only with its Sharpe at the threshold or
        # over it, so the count is bounded by the sharpes held beside it.
        over = sum(sharpe >= self.threshold for sharpe in sharpes)
        if _read.whole(self.clearing, "pool.clearing", minimum=0) > over:
            raise OracleError(
                f"pool.clearing: {self.clearing} clear the gate, but only {over} of "
                f"{len(sharpes)} validation sharpe(s) reach threshold {self.threshold}"
            )

    @property
    def size(self) -> int:
        return len(self.validation_sharpes)

    @property
    def best(self) -> int:
        return max(range(self.size), key=lambda i: (self.validation_sharpes[i], -i))

    def describe(self) -> str:
        return (
            f"full pool: {self.size} candidate(s); best validation net sharpe "
            f"{self.validation_sharpes[self.best]:.4f} (candidate #{self.best}) against "
            f"threshold({self.size}) = {self.threshold:.4f}; {self.clearing} clear the gate there"
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "size": self.size,
            "validation_sharpes": list(self.validation_sharpes),
            "best": self.best,
            "threshold": self.threshold,
            "clearing": self.clearing,
        }

    @classmethod
    def from_dict(cls, payload: object) -> PoolReference:
        body = _read.exact(
            payload, ("size", "validation_sharpes", "best", "threshold", "clearing"), "pool"
        )
        reference = cls(
            validation_sharpes=body["validation_sharpes"],
            threshold=body["threshold"],
            clearing=body["clearing"],
        )
        if body["size"] != reference.size or body["best"] != reference.best:
            raise OracleError(
                f"pool: the record says size {body['size']!r} and best {body['best']!r}; its "
                f"figures give {reference.size} and {reference.best}"
            )
        return reference


def pool_reference(
    ledger: Ledger,
    experiment: Experiment,
    specs: Sequence[StrategySpec],
    *,
    frame: FeatureFrame | None = None,
) -> PoolReference:
    """The whole pool scored on ``experiment``'s two measured windows, on one frame, nothing filed."""
    if frame is None:
        frame = frame_for(ledger, experiment, holdout=False)
    count = len(specs)
    sharpes: list[float] = []
    gates: list[Verdict] = []
    for index, spec in enumerate(specs):
        result = evaluate_split(spec, frame, experiment.split, experiment.costs, holdout=False)
        train = SegmentMetrics.from_result(result.train)
        validation = SegmentMetrics.from_result(result.validation)
        sharpes.append(validation.net.sharpe)
        gates.append(gate_at(experiment, spec, train, validation, count, trial_id=index))
    if not gates:
        raise OracleError("pool: a pool has at least one candidate")
    return PoolReference(
        validation_sharpes=tuple(sharpes),
        threshold=gates[0].threshold,
        clearing=sum(gate.eligible for gate in gates),
    )


# -- the record ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Oracle:
    """``oracle.json``: the planted rule's figures (``None`` on a null world) and the pool's."""

    world_id: str
    planted: PlantedFigures | None
    pool: PoolReference

    def __post_init__(self) -> None:
        _read.text(self.world_id, "world_id")
        if self.planted is not None and not isinstance(self.planted, PlantedFigures):
            raise OracleError("planted: expected PlantedFigures or None")
        if not isinstance(self.pool, PoolReference):
            raise OracleError("pool: expected a PoolReference")

    def describe(self, *, holdout: bool = True) -> list[str]:
        lines = [] if self.planted is None else self.planted.describe(holdout=holdout)
        return [*lines, self.pool.describe()]

    def to_dict(self) -> dict[str, object]:
        return {
            "oracle_version": ORACLE_VERSION,
            "world_id": self.world_id,
            "planted": None if self.planted is None else self.planted.to_dict(),
            "pool": self.pool.to_dict(),
        }

    @classmethod
    def from_dict(cls, payload: object) -> Oracle:
        _read.version(payload, "oracle_version", ORACLE_VERSION)
        body = _read.exact(payload, ("oracle_version", "world_id", "planted", "pool"), "oracle")
        return cls(
            world_id=body["world_id"],
            planted=None if body["planted"] is None else PlantedFigures.from_dict(body["planted"]),
            pool=PoolReference.from_dict(body["pool"]),
        )

    def dumps(self) -> str:
        return dumps(self.to_dict())

    @classmethod
    def loads(cls, text: str) -> Oracle:
        return cls.from_dict(_read.loads(text))
