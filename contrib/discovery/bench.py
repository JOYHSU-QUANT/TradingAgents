"""One world run to its verdicts (plan §3 steps 2–5, PR 3): the arms, the final verdicts, the oracle, on disk.

A landed run directory (``manifest.json`` and ``world.sqlite``, plan PR 2)
is taken through the protocol in the order the plan fixes, and the order
is the point of this module:

1. **The pool** is the manifest's (``SearchSpace.candidates``, written and
   checked where the manifest is read; ``generate`` filled ``pool_size``
   in when it landed the world, and the landed manifest is not written
   again). The manifest has to be the directory's own — the directory is
   named by its digest — and has to name no arm this build does not run.
2. **Every arm copies the artifact** into its own store and opens its own
   experiment from the manifest's conditions (:func:`~contrib.discovery.landing.experiment_for`):
   one coin, one sqlite per arm, the same split, costs, window and
   penalty for all (plan §1 #6; held by :class:`Verdicts`). The first copy
   is verified as the landing was: the artifact an arm measures on is the
   world the manifest describes.
3. **Each arm searches** through the research package's own ``search()``
   with its hypothesist and the manifest's budget, and is judged at once:
   the snapshot before any promotion, the pure verdict, and — when the
   verdict selected a trial — that one promotion, through the research
   package's own ``promote``, which is what reads the holdout for it
   (:class:`ArmResult`). An interrupted arm ends the run: a replicate is
   one search, not resumed (plan §9 (14)), and nothing is written.
4. **The oracle last** (plan §4.4): the planted rule's holdout figure and
   the whole pool's validation figures are read off the Grid arm's store
   after every arm has searched and chosen, and written to ``oracle.json``.

Everything is built in a staging directory inside the run directory and
moved into place at the end (the two records, then the arms' stores into
``arms/``), so a run that ends before the records are in leaves the
directory as the landing left it but for at most ``verdicts.json`` (if it
ends between the two moves), which the guard and calibrate refuse by
name; one that ends while the stores move in — a failed rename or an
interrupt — leaves the records and keeps the staging that holds the rest,
named in the refusal. ``arms/`` itself is made first, atomically, as the claim on
the directory: two searches started on one world cannot both pass the
guard. A run directory holding the claim alone is a search running or
one that did not finish (nothing is prescribed while one may run); one
holding the records is a world searched before: refused, since a search
is spent once — remove them by hand to search the world again.

The ``oracle`` command's preview (:func:`preview_oracle`) reads the planted
rule's two measured windows on a throw-away copy BEFORE any arm has
searched, which the protocol allows on development seeds only (plan §4.2,
§4.4): on a test seed the world is run, and the oracle's figures come out
of ``search`` with everyone else's.

:func:`calibration_table` is the null calibration's reading (plan §4.3,
PR 3's acceptance): over a root of run directories, how often each arm's
final verdict was non-empty, by the kind of world — a false call on a
null world, a candidate recovery on a conditional one (whether the
selected rule IS the planted one is the scorer's question, plan PR 4).
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from functools import cached_property
from pathlib import Path
from typing import TypeVar

from .hypothesists import GridHypothesist, PoolHypothesist, RandomHypothesist, enumerate_pool
from .landing import (
    ARTIFACT_FILENAME,
    MANIFEST_FILENAME,
    Landing,
    conditions_digest,
    discard_staging,
    experiment_for,
    manifest_digest,
    open_staging,
    verify,
    world_dir,
)
from .manifest import Manifest, SeedSet, WorldKind
from .oracle import Oracle, PlantedFigures, frame_for, planted_figures, pool_reference
from .records import Reader, RecordError, dumps
from .upstream import Experiment, Ledger, ResearchStore, SegmentMetrics, VocabEnum, promote, search
from .verdict import ArmSnapshot, ArmVerdict, Ended, final_verdict, snapshot_arm

__all__ = [
    "ARMS_DIRNAME",
    "ORACLE_FILENAME",
    "VERDICTS_FILENAME",
    "VERDICTS_VERSION",
    "Arm",
    "ArmResult",
    "BenchError",
    "CalibrationRow",
    "Verdicts",
    "calibration_table",
    "experiment_id",
    "own_manifest",
    "preview_oracle",
    "read_manifest",
    "require_artifact",
    "run_world",
    "store_path",
]

# What a searched run directory holds beside the landing (plan §2's layout).
ARMS_DIRNAME = "arms"
VERDICTS_FILENAME = "verdicts.json"
ORACLE_FILENAME = "oracle.json"

# Bumped when a key of ``verdicts.json`` is added, removed or re-read.
VERDICTS_VERSION = 1


class BenchError(RecordError):
    """The run cannot be made, read or continued as asked, and the sentence says why."""


_read = Reader(BenchError, "verdicts")
_R = TypeVar("_R")
logger = logging.getLogger(__name__)


class Arm(VocabEnum, noun="arm"):
    """The arms this runner runs. The model arm is plan PR 5's."""

    RANDOM = "random"
    GRID = "grid"


# -- the records ------------------------------------------------------------------------------


@dataclass(frozen=True)
class ArmResult:
    """One arm's one search: its snapshot, the selected trial's holdout, and the verdict read off it.

    ``replicate`` numbers the search: the search seed for the Random arm,
    0 for the Grid arm (deterministic, one search). ``holdout`` is the
    selected trial's figure from its promotion, present exactly when a
    trial was selected. The verdict is DERIVED — :func:`final_verdict` of
    the snapshot, as the package's other records derive what a reader
    could recompute — written beside the snapshot for the reader of the
    file and checked against it on the read, so the file can always be
    re-judged from the snapshot alone (plan §4.5).
    """

    arm: Arm
    replicate: int
    snapshot: ArmSnapshot
    holdout: SegmentMetrics | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "arm", _read.enum(Arm, self.arm, "result.arm"))
        _read.whole(self.replicate, "result.replicate", minimum=0)
        if self.arm is Arm.GRID and self.replicate != 0:
            raise BenchError(
                f"{self.experiment_id}: the grid arm is one deterministic search, replicate 0"
            )
        if not isinstance(self.snapshot, ArmSnapshot):
            raise BenchError("result.snapshot: expected an ArmSnapshot")
        if self.snapshot.ended is Ended.INTERRUPTED:
            # An interrupted arm ends the run and is never written (the
            # module docstring); a record that says it was is not this run's.
            raise BenchError(
                f"{self.experiment_id}: an interrupted search is not a result; the run that was "
                f"interrupted wrote nothing"
            )
        if self.snapshot.experiment.experiment_id != self.experiment_id:
            raise BenchError(
                f"{self.experiment_id}: the snapshot is of experiment "
                f"{self.snapshot.experiment.experiment_id}"
            )
        if (self.holdout is None) == (self.verdict.selected is not None):
            raise BenchError(
                f"{self.experiment_id}: a result carries a holdout figure if and only if a "
                f"trial was selected"
            )
        if self.holdout is not None:
            if not isinstance(self.holdout, SegmentMetrics):
                raise BenchError(f"{self.experiment_id}: holdout is not a SegmentMetrics")
            if self.holdout.segment != self.snapshot.experiment.split.holdout:
                raise BenchError(
                    f"{self.experiment_id}: the holdout figure is for {self.holdout.segment}, "
                    f"not the experiment's {self.snapshot.experiment.split.holdout}"
                )

    @cached_property
    def verdict(self) -> ArmVerdict:
        """The research package's gate at the final count: a pure function of the snapshot."""
        return final_verdict(self.snapshot)

    @property
    def experiment_id(self) -> str:
        return experiment_id(self.arm, self.replicate)

    def describe(self, *, holdout: bool = True) -> str:
        """The result as a line; ``holdout`` False keeps the holdout figure off the console."""
        verdict = self.verdict
        line = (
            f"{self.experiment_id}: ended {verdict.ended.value}, {verdict.answers} answer(s), "
            f"{verdict.distinct_rules} distinct rule(s)"
        )
        if verdict.threshold is not None:
            line += f", threshold {verdict.threshold:.4f}"
        if verdict.selected is None:
            return line + "; final eligible: none"
        assert self.holdout is not None  # the constructor's rule
        line += (
            f"; final eligible: {list(verdict.final_eligible)}, selected trial #{verdict.selected}"
        )
        if holdout:
            line += f" (holdout net sharpe {self.holdout.net.sharpe:.4f})"
        return line

    def to_dict(self) -> dict[str, object]:
        return {
            "arm": self.arm.value,
            "replicate": self.replicate,
            "snapshot": self.snapshot.to_dict(),
            "verdict": self.verdict.to_dict(),
            "holdout": None if self.holdout is None else self.holdout.to_dict(),
        }

    @classmethod
    def from_dict(cls, payload: object) -> ArmResult:
        body = _read.exact(
            payload, ("arm", "replicate", "snapshot", "verdict", "holdout"), "result"
        )
        try:
            holdout = None if body["holdout"] is None else SegmentMetrics.from_dict(body["holdout"])
        except ValueError as exc:
            raise BenchError(f"result.holdout: {exc}") from exc
        result = cls(
            arm=body["arm"],
            replicate=body["replicate"],
            snapshot=ArmSnapshot.from_dict(body["snapshot"]),
            holdout=holdout,
        )
        # Written for the reader of the file; checked against the snapshot
        # it is derived from, so a record cannot say what its snapshot does not.
        if ArmVerdict.from_dict(body["verdict"]) != result.verdict:
            raise BenchError(
                f"{result.experiment_id}: the record's verdict is not the verdict of its snapshot"
            )
        return result


def experiment_id(arm: Arm, replicate: int) -> str:
    return f"{arm.value}-{replicate}"


def store_path(run_dir: Path, arm: Arm, replicate: int) -> Path:
    """``<run>/arms/<arm>/replicate-<k>.sqlite`` (plan §2's layout)."""
    return run_dir / ARMS_DIRNAME / arm.value / f"replicate-{replicate}.sqlite"


@dataclass(frozen=True)
class Verdicts:
    """``verdicts.json``: every arm's result on one world, under the one set of conditions.

    The conditions are held here: every result's experiment is the same
    experiment but for its name and stamp (plan §4.3: three arms, one
    split, one cost model, one penalty; the budget is the manifest's and is
    not on the experiment), on the world's coin. ``pool_size`` is the pool
    the arms drew from — no arm of it can have filed more rules than that —
    and ``manifest_digest`` is the manifest the arms ran under
    (``landing.manifest_digest``: the run directory's own name), so the
    record is bound to its conditions and not only to a world id the null
    twin shares; the calibration holds both to the manifest beside it.
    """

    world_id: str
    pool_size: int
    manifest_digest: str
    results: tuple[ArmResult, ...]

    def __post_init__(self) -> None:
        _read.text(self.world_id, "world_id")
        _read.whole(self.pool_size, "pool_size", minimum=1)
        _read.digest(self.manifest_digest, "manifest_digest")
        results = tuple(_read.sequence(self.results, "results"))
        if not results or any(not isinstance(result, ArmResult) for result in results):
            raise BenchError("results: at least one ArmResult")
        ids = [result.experiment_id for result in results]
        if len(set(ids)) != len(ids):
            raise BenchError(f"results: each arm and replicate once, got {ids}")
        if experiment_id(Arm.GRID, 0) not in ids:
            # Every run has the one deterministic arm (``_arms`` appends it
            # unconditionally); the oracle is read on its store.
            raise BenchError(f"results: no grid arm among {ids}; every search runs it")
        first = _conditions(results[0].snapshot.experiment)
        for result in results:
            experiment = result.snapshot.experiment
            if experiment.coin != self.world_id:
                raise BenchError(
                    f"{result.experiment_id} ran on coin {experiment.coin}, not {self.world_id}"
                )
            if result.snapshot.distinct_rules > self.pool_size:
                raise BenchError(
                    f"{result.experiment_id} tried {result.snapshot.distinct_rules} distinct "
                    f"rule(s) from a pool of {self.pool_size}"
                )
            # Every arm searches in its own store, the first experiment on
            # its coin, so the coin's rule count is the arm's own trials —
            # a larger count would raise the gate over trials it never filed.
            if result.snapshot.distinct_rules != len(result.snapshot.trials):
                raise BenchError(
                    f"{result.experiment_id} counts {result.snapshot.distinct_rules} rule(s) "
                    f"tried and holds {len(result.snapshot.trials)} trial(s); each arm searches "
                    f"in its own store"
                )
            # Every arm here is a pool arm, proposing each candidate at most
            # once and running out exactly when it has proposed them all.
            answers = result.snapshot.answers
            if answers > self.pool_size:
                raise BenchError(
                    f"{result.experiment_id} answered {answers} time(s) from a pool of "
                    f"{self.pool_size}"
                )
            if result.snapshot.ended is Ended.EXHAUSTED and answers != self.pool_size:
                raise BenchError(
                    f"{result.experiment_id} walked its pool to the end in {answers} answer(s); "
                    f"the pool holds {self.pool_size}"
                )
            if _conditions(experiment) != first:
                raise BenchError(
                    f"{result.experiment_id} ran under other conditions than "
                    f"{results[0].experiment_id}; every arm of a world shares one split, cost "
                    f"model, window and penalty"
                )
        object.__setattr__(self, "results", results)

    def to_dict(self) -> dict[str, object]:
        return {
            "verdicts_version": VERDICTS_VERSION,
            "world_id": self.world_id,
            "pool_size": self.pool_size,
            "manifest_digest": self.manifest_digest,
            "results": [result.to_dict() for result in self.results],
        }

    @classmethod
    def from_dict(cls, payload: object) -> Verdicts:
        _read.version(payload, "verdicts_version", VERDICTS_VERSION)
        body = _read.exact(
            payload,
            ("verdicts_version", "world_id", "pool_size", "manifest_digest", "results"),
            "verdicts",
        )
        return cls(
            world_id=body["world_id"],
            pool_size=body["pool_size"],
            manifest_digest=body["manifest_digest"],
            results=tuple(
                ArmResult.from_dict(item) for item in _read.sequence(body["results"], "results")
            ),
        )

    def dumps(self) -> str:
        return dumps(self.to_dict())

    @classmethod
    def loads(cls, text: str) -> Verdicts:
        return cls.from_dict(_read.loads(text))


def _conditions(experiment: Experiment) -> Experiment:
    """The experiment with its name and stamp set aside: what every arm of a world shares."""
    return replace(experiment, experiment_id="arm", created_at="")


# -- the run directory ------------------------------------------------------------------------


def read_manifest(run_dir: Path) -> Manifest:
    """The landed manifest of ``run_dir``; a directory without one is not a run.

    The artifact is not demanded here: the calibration table reads the
    manifest and the verdicts of worlds whose artifacts may have been
    pruned; the verbs that measure ask for it (:func:`require_artifact`).
    """
    return Manifest.read(run_dir / MANIFEST_FILENAME)


def _record_text(path: Path) -> str:
    """A record file's text; one that cannot be read is refused by name."""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise BenchError(f"cannot read {path}: {exc}") from exc


def _read_record(path: Path, loads: Callable[[str], _R]) -> _R:
    """A record read from its file, every refusal — unreadable or malformed — naming the file.

    The records' own readers name the path inside the document, not the
    file; over a root of many run directories the file is what the
    operator needs.
    """
    text = _record_text(path)
    try:
        return loads(text)
    except RecordError as exc:
        raise BenchError(f"{path}: {exc}") from exc


def _give_back(claim: Path) -> None:
    """Remove the empty ``arms/`` claim of a run that did not finish; a leftover is named, not raised over."""
    try:
        claim.rmdir()
    except OSError:
        logger.warning(
            "could not remove the claim %s; the next search of this world will be refused until "
            "it is removed by hand",
            claim,
            exc_info=True,
        )


def _search_left(run_dir: Path) -> list[str]:
    """What a search has left in ``run_dir``: the claim and the two records, in that order."""
    return [n for n in (ARMS_DIRNAME, VERDICTS_FILENAME, ORACLE_FILENAME) if (run_dir / n).exists()]


def _unfinished(run_dir: Path, left: list[str]) -> str:
    """The sentence for a search that is running or did not finish: nothing prescribed while one may run."""
    return (
        f"{run_dir} holds {left}: a search of this world is running or did not finish; once no "
        f"search is running, remove them by hand to search the world again"
    )


def require_artifact(run_dir: Path) -> None:
    """Refuse a run directory an arm could not copy its world from."""
    if not (run_dir / ARTIFACT_FILENAME).is_file():
        raise BenchError(f"{run_dir} holds no {ARTIFACT_FILENAME}; generate the world first")


def own_manifest(run_dir: Path) -> Manifest:
    """The directory's own manifest: read, and named by the directory it sits in.

    The directory is named by the manifest's digest (``landing.world_dir``),
    so a ``manifest.json`` edited after the landing — a kind flipped, a
    penalty or budget changed — names another directory, and is refused
    rather than read as the conditions the directory describes. Every
    reader of a run directory comes through here, the calibration included:
    its rows are keyed by the kind and the null definition, two fields the
    world's id does not carry.
    """
    manifest = read_manifest(run_dir)
    expected = world_dir(run_dir.parent, manifest).name
    if run_dir.resolve().name != expected:
        raise BenchError(
            f"{run_dir / MANIFEST_FILENAME} is not the manifest this directory was landed for: "
            f"it names {expected}, the directory is {run_dir.resolve().name}"
        )
    return manifest


def landed_manifest(run_dir: Path) -> Manifest:
    """The manifest a measuring verb runs under: the directory's own, with its artifact beside it."""
    manifest = own_manifest(run_dir)
    require_artifact(run_dir)
    return manifest


def _copy_artifact(run_dir: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(run_dir / ARTIFACT_FILENAME, target)


# -- the oracle's preview --------------------------------------------------------------------------


def preview_oracle(run_dir: Path) -> PlantedFigures:
    """The planted rule's train and validation figures before any search, on a throw-away copy.

    Development seeds only: this is the reading that sizes ``beta`` (plan
    §4.1), and the protocol keeps it off test seeds, where nothing is
    tuned and the oracle's figures are written by ``search`` after the arms
    (plan §4.2, §4.4). The holdout is not read here on any seed.
    """
    manifest = landed_manifest(run_dir)
    world = manifest.world
    if world.planted is None:
        raise BenchError(
            f"{world.id} is a {world.kind.value} world: nothing is planted, so there is no "
            f"oracle to preview; run `search` for the pool's reference figures"
        )
    if world.seed_set is not SeedSet.DEV:
        raise BenchError(
            f"{world.id} is a {world.seed_set.value} seed: the oracle's figures are read before "
            f"a search on development seeds only, to size beta; on a test seed run `search`, "
            f"which writes them to {ORACLE_FILENAME} after the arms"
        )
    with tempfile.TemporaryDirectory(dir=run_dir, prefix=".oracle.partial-") as scratch:
        path = Path(scratch) / "oracle.sqlite"
        _copy_artifact(run_dir, path)
        with ResearchStore(path) as store:
            ledger = Ledger(store)
            experiment = ledger.create_experiment(experiment_for(manifest, "oracle"))
            return planted_figures(
                ledger, experiment, world.planted, manifest.budget, holdout=False
            )


# -- the run -----------------------------------------------------------------------------------


def _arms(manifest: Manifest) -> list[tuple[Arm, int, PoolHypothesist]]:
    """The arms to run, in order: the Random replicates as the manifest lists their seeds, then Grid."""
    pool = enumerate_pool(manifest.search_space)
    world_id = manifest.world.id
    arms: list[tuple[Arm, int, PoolHypothesist]] = [
        (Arm.RANDOM, seed, RandomHypothesist(pool, world_id=world_id, search_seed=seed))
        for seed in manifest.arms.random.search_seeds
    ]
    arms.append((Arm.GRID, 0, GridHypothesist(pool)))
    return arms


def run_world(
    run_dir: Path, *, on_line: Callable[[str], None] | None = None
) -> tuple[Verdicts, Oracle]:
    """Take a landed run directory through the arms, the verdicts and the oracle (the module docstring).

    ``on_line`` is told each arm's report as it completes, so a long run
    reports as it goes. Returns what was written to ``verdicts.json`` and
    ``oracle.json``.
    """
    say = on_line or (lambda line: None)
    # The directory's own manifest first, then "searched before", then the
    # artifact: a searched world whose artifact was pruned is told it was
    # searched, not sent back to `generate` (which would refuse the
    # directory and send the operator to delete the records).
    manifest = own_manifest(run_dir)
    left = _search_left(run_dir)
    if left == [ARMS_DIRNAME]:
        # The claim alone is what a search in progress leaves (it is made
        # first, the records moved in last): nothing is prescribed while one
        # may be running, as calibrate says of the same directory.
        raise BenchError(_unfinished(run_dir, left))
    if left:
        # A staging kept by a failed store move holds the stores the claim did
        # not receive; it is named so it is not left behind by a re-search.
        kept = sorted(p.name for p in run_dir.glob(".search.partial-*"))
        raise BenchError(
            f"{run_dir} holds {left + kept}: this world was searched before, and a search is "
            f"spent once — remove {ARMS_DIRNAME}/, {VERDICTS_FILENAME}, {ORACLE_FILENAME}"
            + (" and the kept staging" if kept else "")
            + " by hand to search it again"
        )
    require_artifact(run_dir)
    if manifest.arms.llm is not None:
        # The manifest is the run's specification, and a world is searched
        # once: a search that left out an arm the manifest names would be a
        # calibration that silently lacks it. The model arm is plan PR 5's;
        # `generate` refuses such a manifest too, so this is a directory
        # landed by another build.
        raise BenchError(
            f"{run_dir / MANIFEST_FILENAME} names a model arm (arms.llm), which this build does "
            f"not run; land the world again from a manifest with arms.llm null (editing this "
            f"one in place would change its digest, which names this directory)"
        )
    digest = manifest_digest(manifest)
    space = manifest.search_space
    pool_size = len(space.candidates)
    say(f"pool: {pool_size} candidate(s) ({space.enumerated} enumerated)")
    # Test seeds keep their holdout figures in the records and off the
    # console (plan §4.2: nothing is tuned on them).
    show_holdout = manifest.world.seed_set is SeedSet.DEV
    world_id = manifest.world.id
    # The claim: ``arms/`` is made first, atomically, so two searches of
    # one directory cannot both pass the guard above and leave one's
    # records beside the other's stores. It is filled from staging at the
    # end, and taken away again if the run ends before that.
    claim = run_dir / ARMS_DIRNAME
    try:
        claim.mkdir()
    except FileExistsError as exc:
        raise BenchError(
            f"{claim} exists: another search of this world is running or ran; a search is "
            f"spent once"
        ) from exc
    try:
        staging = open_staging(run_dir, ".search.partial-")
    except BaseException:
        # No staging, nothing run: the claim goes back, or the next search
        # would be told the world was searched.
        _give_back(claim)
        raise
    moved = False
    keep_staging = False
    try:
        results: list[ArmResult] = []
        landing: Landing | None = None
        for arm, replicate, hypothesist in _arms(manifest):
            path = store_path(staging, arm, replicate)
            _copy_artifact(run_dir, path)
            with ResearchStore(path) as store:
                ledger = Ledger(store)
                if landing is None:
                    # Once, on the first copy, before any experiment: the
                    # artifact an arm will measure on is the world the
                    # manifest describes.
                    landing = verify(store, manifest)
                experiment = ledger.create_experiment(
                    experiment_for(manifest, experiment_id(arm, replicate))
                )
                report = search(
                    ledger,
                    experiment,
                    hypothesist,
                    model=arm.value,
                    max_trials=manifest.budget.max_trials,
                )
                for line in report.describe():
                    say(line)
                snapshot = snapshot_arm(ledger, experiment, report, exhausted=hypothesist.exhausted)
                if snapshot.ended is Ended.INTERRUPTED:
                    # A replicate is one search and is not resumed (plan §9
                    # (14)): the run ends here, its staging goes with it, and
                    # the caller keeps the interrupt's own exit code.
                    raise KeyboardInterrupt
                selected = final_verdict(snapshot).selected
                holdout = (
                    None if selected is None else promote(ledger, experiment, selected).holdout
                )
                result = ArmResult(arm=arm, replicate=replicate, snapshot=snapshot, holdout=holdout)
                say(result.describe(holdout=show_holdout))
                results.append(result)
        verdicts = Verdicts(
            world_id=world_id,
            pool_size=pool_size,
            manifest_digest=digest,
            results=tuple(results),
        )
        # The oracle last (plan §4.4): read on the Grid arm's store — the
        # last result's — after every arm has searched and chosen. The
        # oracle path files nothing, so the store is the arm's record still.
        grid = results[-1].snapshot.experiment
        with ResearchStore(store_path(staging, Arm.GRID, 0)) as store:
            ledger = Ledger(store)
            # One frame for both readers: a bar's features never read past
            # it, so the holdout-bound frame measures the two earlier
            # windows exactly as a narrower one would (``promote`` re-measures
            # train and validation on it for that reason).
            frame = frame_for(ledger, grid, holdout=True)
            planted = (
                None
                if manifest.world.planted is None
                else planted_figures(
                    ledger, grid, manifest.world.planted, manifest.budget, holdout=True, frame=frame
                )
            )
            reference = pool_reference(
                ledger, grid, [candidate.spec for candidate in space.candidates], frame=frame
            )
        oracle = Oracle(world_id=world_id, manifest_digest=digest, planted=planted, pool=reference)
        for line in oracle.describe(holdout=show_holdout):
            say(line)
        # Written whole in staging, then moved: the two records first, then
        # each arm's store into the claim. A failure before the records are
        # both in leaves the directory as the landing left it (at most one
        # record, which calibrate and the guard refuse by name); a failure
        # while the stores move in leaves the records and the staging that
        # still holds the unmoved stores, named in the refusal so nothing
        # is lost. The landed manifest is not touched.
        (staging / VERDICTS_FILENAME).write_bytes(verdicts.dumps().encode("utf-8"))
        (staging / ORACLE_FILENAME).write_bytes(oracle.dumps().encode("utf-8"))
        for name in (VERDICTS_FILENAME, ORACLE_FILENAME):
            os.replace(staging / name, run_dir / name)
        moved = True
        done: list[str] = []
        # From here the staging holds stores the claim has not received, so
        # any end — a failed rename or an interrupt between two — keeps it.
        keep_staging = True
        try:
            for arm_dir in sorted((staging / ARMS_DIRNAME).iterdir()):
                arm_dir.rename(claim / arm_dir.name)
                done.append(arm_dir.name)
        except OSError as exc:
            raise BenchError(
                f"{run_dir}: the records are written, but moving the arms' stores into {claim} "
                f"failed after {done}; the rest are in {staging / ARMS_DIRNAME} — move them in by "
                f"hand, then remove {staging}"
            ) from exc
        keep_staging = False
    finally:
        # Emptied by the moves on success; the half-made run on any other
        # end, except the stores the claim did not receive.
        if not keep_staging:
            discard_staging(staging)
        if not moved:
            # The claim is given back only while it is still empty: a run
            # that failed moving its stores in has records beside them,
            # which the guard refuses and the operator removes by hand.
            _give_back(claim)
    return verdicts, oracle


# -- the calibration table -----------------------------------------------------------------------


@dataclass(frozen=True)
class CalibrationRow:
    """One arm on one kind of world across a root: how many searches, how many found something.

    On a null world a non-empty final verdict is a false call under that
    null definition (plan §4.1's wording); on a conditional world it is a
    candidate recovery, not yet a recovery (plan PR 4 scores that).

    A row is one set of conditions: ``conditions`` is the digest of the
    manifest with the seed set aside (``landing.conditions_digest``), so the
    worlds of one β tier, one penalty, one budget, one bar count and one
    seed set are tallied together and two calibrations under one root are
    two rows, each printed with its digest — never one rate over both.

    A search is a run that answered — spent its budget or walked its pool
    to the end; an arm whose seam failed never searched, is counted in
    ``failed`` and is not in the rate's denominator, so a dead arm cannot
    read as a clean search. Whatever a dead arm's verdict holds (a seam
    can fail after some trials were filed) is set aside with it, on
    purpose: a search cut short is not the search the budget bought, and
    its verdict is not comparable with the finished ones in the row.
    """

    kind: WorldKind
    null_definition_version: str
    seed_set: SeedSet
    conditions: str
    arm: Arm
    searches: int
    found: int
    failed: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", _read.enum(WorldKind, self.kind, "row.kind"))
        _read.text(self.null_definition_version, "row.null_definition_version")
        object.__setattr__(self, "seed_set", _read.enum(SeedSet, self.seed_set, "row.seed_set"))
        _read.digest(self.conditions, "row.conditions")
        object.__setattr__(self, "arm", _read.enum(Arm, self.arm, "row.arm"))
        _read.whole(self.searches, "row.searches", minimum=0)
        _read.whole(self.found, "row.found", minimum=0)
        _read.whole(self.failed, "row.failed", minimum=0)
        if self.found > self.searches:
            raise BenchError(f"row: {self.found} found in {self.searches} search(es)")
        if self.searches + self.failed == 0:
            raise BenchError("row: a row tallies at least one result")

    @property
    def rate(self) -> float | None:
        """The share of searches that found something; ``None`` when nothing searched."""
        return None if self.searches == 0 else self.found / self.searches

    def describe(self) -> str:
        what = "false calls" if self.kind is WorldKind.NULL_V1 else "non-empty verdicts"
        rate = "n/a" if self.rate is None else f"{self.rate:.1%}"
        line = (
            f"{self.kind.value} ({self.null_definition_version}) {self.seed_set.value} "
            f"[{self.conditions}] {self.arm.value}: "
            f"{self.found} {what} in {self.searches} search(es) = {rate}"
        )
        if self.failed:
            line += f"; {self.failed} seam failure(s) not counted"
        return line


def calibration_table(run_dirs: Iterable[Path]) -> list[CalibrationRow]:
    """Every searched run directory's verdicts, tallied by world kind and arm (the module docstring).

    A directory holding none of what a search leaves (``arms/``,
    ``verdicts.json``, ``oracle.json``) is a world not yet searched and is
    skipped; one holding some of it but not both records is a search in
    progress or one that did not finish, refused by name rather than
    dropped from the table without a word. A record that cannot be read,
    or that is not its manifest's — another world, pool, arm set, digest,
    set of conditions, budget, or rule outside the pool — is refused by
    name. Rows come in the enums' own order: kind, then the null
    definition, the seed set, the conditions, then arm.
    """
    tally: dict[tuple[WorldKind, str, SeedSet, str, Arm], list[int]] = {}
    for run_dir in run_dirs:
        left = _search_left(run_dir)
        if not left:
            continue
        if VERDICTS_FILENAME not in left or ORACLE_FILENAME not in left:
            # ``arms/`` alone is also what a search in progress leaves (the
            # claim is made first, the records moved in last), so the
            # sentence says so and prescribes nothing while one may be running.
            raise BenchError(_unfinished(run_dir, left))
        manifest = own_manifest(run_dir)
        if manifest.arms.llm is not None:
            # The same refusal ``search`` makes: a tally under a manifest that
            # names an arm this build never ran would be a calibration that
            # silently lacks it.
            raise BenchError(
                f"{run_dir / MANIFEST_FILENAME} names a model arm (arms.llm), which this build "
                f"does not run or tally"
            )
        verdicts = _read_record(run_dir / VERDICTS_FILENAME, Verdicts.loads)
        if verdicts.world_id != manifest.world.id:
            raise BenchError(
                f"{run_dir / VERDICTS_FILENAME} is for {verdicts.world_id}, the manifest beside "
                f"it for {manifest.world.id}"
            )
        pool_size = len(manifest.search_space.candidates)
        if verdicts.pool_size != pool_size:
            raise BenchError(
                f"{run_dir / VERDICTS_FILENAME} drew from a pool of {verdicts.pool_size}, the "
                f"manifest beside it writes one of {pool_size}"
            )
        # The arms the manifest names, no fewer and no others: a search
        # that left one out is refused before it runs (``run_world``), so a
        # record missing one was not written by this build's search.
        expected = {(Arm.RANDOM, seed) for seed in manifest.arms.random.search_seeds}
        expected.add((Arm.GRID, 0))
        held = {(result.arm, result.replicate) for result in verdicts.results}
        if held != expected:
            raise BenchError(
                f"{run_dir / VERDICTS_FILENAME} holds the arms "
                f"{sorted(experiment_id(a, r) for a, r in held)}, the manifest beside it names "
                f"{sorted(experiment_id(a, r) for a, r in expected)}"
            )
        # And the manifest itself: the null twin of a seed shares the id and
        # the pool, and differs by the digest alone.
        if verdicts.manifest_digest != manifest_digest(manifest):
            raise BenchError(
                f"{run_dir / VERDICTS_FILENAME} was written under manifest "
                f"{verdicts.manifest_digest}, the manifest beside it is {manifest_digest(manifest)}"
            )
        _hold_to_manifest(run_dir / VERDICTS_FILENAME, verdicts, manifest)
        # The oracle's record is written with the verdicts and read with
        # them, and both say which world and which manifest they were read
        # under.
        oracle = _read_record(run_dir / ORACLE_FILENAME, Oracle.loads)
        if (oracle.world_id, oracle.manifest_digest) != (
            verdicts.world_id,
            verdicts.manifest_digest,
        ):
            raise BenchError(
                f"{run_dir / ORACLE_FILENAME} is for {oracle.world_id} under manifest "
                f"{oracle.manifest_digest}; {VERDICTS_FILENAME} beside it is for "
                f"{verdicts.world_id} under {verdicts.manifest_digest}"
            )
        # And it describes this world: a planted figure exactly when the
        # world plants something, and the pool the manifest writes.
        if (oracle.planted is None) != (manifest.world.planted is None):
            raise BenchError(
                f"{run_dir / ORACLE_FILENAME} "
                + ("has no planted figure" if oracle.planted is None else "has a planted figure")
                + f" for a {manifest.world.kind.value} world"
            )
        if oracle.pool.size != pool_size:
            raise BenchError(
                f"{run_dir / ORACLE_FILENAME} scores a pool of {oracle.pool.size}, the manifest "
                f"beside it writes one of {pool_size}"
            )
        _hold_oracle_to_manifest(run_dir / ORACLE_FILENAME, oracle, manifest)
        conditions = conditions_digest(manifest)
        for result in verdicts.results:
            key = (
                manifest.world.kind,
                manifest.null_definition_version,
                manifest.world.seed_set,
                conditions,
                result.arm,
            )
            counts = tally.setdefault(key, [0, 0, 0])
            if result.snapshot.ended is Ended.SEAM_FAILED:
                counts[2] += 1
            else:
                counts[0] += 1
                counts[1] += result.verdict.found
    kinds = {kind: index for index, kind in enumerate(WorldKind)}
    seed_sets = {seed_set: index for index, seed_set in enumerate(SeedSet)}
    arms = {arm: index for index, arm in enumerate(Arm)}
    ordered = sorted(
        tally,
        key=lambda key: (kinds[key[0]], key[1], seed_sets[key[2]], key[3], arms[key[4]]),
    )
    return [
        CalibrationRow(
            kind=kind,
            null_definition_version=version,
            seed_set=seed_set,
            conditions=conditions,
            arm=arm,
            searches=tally[kind, version, seed_set, conditions, arm][0],
            found=tally[kind, version, seed_set, conditions, arm][1],
            failed=tally[kind, version, seed_set, conditions, arm][2],
        )
        for kind, version, seed_set, conditions, arm in ordered
    ]


def _hold_to_manifest(path: Path, verdicts: Verdicts, manifest: Manifest) -> None:
    """Refuse a verdicts record that was not searched under ``manifest``'s conditions.

    The arms share one set of conditions (``Verdicts`` holds that); here the
    set is held to the manifest's — a record whose experiments carry
    another penalty, split or cost model would be judged by them, since the
    verdict is re-derived from the snapshot. Each arm's answers are bounded
    by the manifest's budget and a spent budget is exactly it (the pool's
    half of those bounds is the record's own, held in ``Verdicts``), and
    every rule it filed is a rule of the pool.
    """
    first = verdicts.results[0].snapshot.experiment
    if _conditions(first) != _conditions(experiment_for(manifest, "arm")):
        raise BenchError(
            f"{path}: the arms ran under other conditions (split, costs, window or penalty) than "
            f"the manifest beside it sets"
        )
    budget = manifest.budget.max_trials
    pool = {candidate.spec_hash for candidate in manifest.search_space.candidates}
    for result in verdicts.results:
        snapshot = result.snapshot
        if snapshot.ended is Ended.BUDGET_SPENT and snapshot.answers != budget:
            raise BenchError(
                f"{path}: {result.experiment_id} spent its budget in {snapshot.answers} answer(s); "
                f"the manifest's budget is {budget}"
            )
        if snapshot.answers > budget:
            raise BenchError(
                f"{path}: {result.experiment_id} answered {snapshot.answers} time(s), over its "
                f"budget of {budget}"
            )
        outside = [trial.trial_id for trial in snapshot.trials if trial.spec_hash not in pool]
        if outside:
            raise BenchError(
                f"{path}: {result.experiment_id} filed trial(s) {outside} that are not rules of "
                f"the pool"
            )


def _hold_oracle_to_manifest(path: Path, oracle: Oracle, manifest: Manifest) -> None:
    """Refuse an oracle record whose gate is not the one the manifest sets.

    ``passed``, ``margin`` and ``clearing`` are read off the record's own
    budget, threshold and margin; those are held here to the manifest's
    budget and penalty, so the oracle pass rate is the manifest's question.
    """
    penalty = manifest.penalty
    budget = manifest.budget
    if oracle.pool.threshold != penalty.threshold(oracle.pool.size):
        raise BenchError(
            f"{path}: the pool is gated at {oracle.pool.threshold}, the manifest's penalty gives "
            f"threshold({oracle.pool.size}) = {penalty.threshold(oracle.pool.size)}"
        )
    planted = oracle.planted
    if planted is None:
        return
    # And measured on the manifest's windows, as every arm's trials are.
    split = experiment_for(manifest, "arm").split
    assert planted.holdout is not None  # the record's own rule
    windows = (planted.train.segment, planted.validation.segment, planted.holdout.segment)
    if windows != split.ordered:
        raise BenchError(
            f"{path}: the planted rule was measured on other windows than the manifest's split"
        )
    expected = (budget.max_trials, budget.oracle_margin, penalty.threshold(budget.max_trials))
    if (planted.budget, planted.oracle_margin, planted.threshold) != expected:
        raise BenchError(
            f"{path}: the planted rule is gated at budget {planted.budget}, oracle_margin "
            f"{planted.oracle_margin}, threshold {planted.threshold}; the manifest sets "
            f"{expected[0]}, {expected[1]}, {expected[2]}"
        )
