"""One world run to its verdicts (plan §3 steps 2–5, PR 3): the arms, the final verdicts, the oracle, on disk.

A landed run directory (``manifest.json`` and ``world.sqlite``, plan PR 2)
is taken through the protocol in the order the plan fixes, and the order
is the point of this module:

1. **The pool** is the manifest's (``SearchSpace.candidates``, written and
   checked where the manifest is read; ``generate`` filled ``pool_size``
   in when it landed the world, and the landed manifest is not written
   again). The manifest has to be the directory's own — the directory is
   named by its digest.
2. **Every arm copies the artifact** — its SHA-256 checked against the
   manifest's before any copy — into its own store and opens its own
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

**The tool arm** (tools plan §3.5, T4) is a model arm with the
training-window look-ups at hand (:mod:`.toolarm`): a manifest names it in
the model arm's place, and it is searched last, as that arm is. Its
hypothesist is OPENED for each search and not built once, because it
stands on a server that reads that search's own store: the server is
started as a child on the experiment the arm stored, while the search
holds the store open, and its stderr and the search's trace are kept
beside the store under ``arms/llm_tools/``. What the arm found is in
``verdicts.json`` with every other arm's, in the same shape; what it spent
looking — calls a round, the model's mistakes, rounds answered without
tools, tokens — is read off each trace once and written to a record of its
own, ``tool_use.json`` (:mod:`.tooluse`), which a run with no tool arm
does not have.

**What the blind model arm spent** (issue #433) is counted by the same rule
and kept the same way, in ``model_use.json`` (:mod:`.modeluse`): a counter
is put around its client for each search, and the sums are written with the
verdicts. A directory searched before that record existed has none and is
read as it was.

**The two sides of that arm's seam fail differently** (decided
2026-11-19). The MODEL not answering is a failed seam like any arm's:
recorded, counted apart, and the run goes on. The look-ups' SERVER not
answering — it did not start, it died, a look-up timed out or could not be
answered — stops the run (:class:`KeptForInquiry`): nothing is recorded,
the staging is kept with the trace and the server's log in it — renamed
(``.search.kept-…``), the name being the mark every later reader goes by,
and holding a note saying why (``kept-for-inquiry.txt``) — and the claim
stays. The directory is then refused to the next search and to every
reader of a search's records, pointing at the note, until someone has
looked and removed both; the staging alone, by its name, is enough to
refuse it, so the claim removed and the evidence left is not a world to
search past. The rule for a provider that went away would have
the world re-run until its model happened not to ask what the server could
not answer. What else is a defect on this side stops the run the same way,
because what the search left is the evidence of it: a finished search
whose trace cannot be read back, or is not the search its ledger and its
manifest describe; an arm that cannot be opened once its server is up; a
trace or a log that cannot be written.

Everything is built in a staging directory inside the run directory and
moved into place at the end (the records, ``oracle.json`` last, then the
arms' stores into ``arms/``), so a run that ends before the records are in
leaves the directory as the landing left it but for at most the records
that go in before the oracle's (``verdicts.json``, and a model arm's
``tool_use.json`` or ``model_use.json``, if it ends between the moves), which the guard and
calibrate refuse by name; one that ends while the stores move in — a failed rename or an
interrupt — leaves the records and keeps the staging that holds the rest,
named in a failed rename's refusal and, after an interrupt, by the next
``search``. ``arms/`` itself is made first, atomically, as the claim on
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

The ``tools`` command's conditions (:func:`tools_experiment`) are held to
development seeds for the same reason: it puts a world's training window
in front of a person (tools plan §3.3).

:func:`calibration_table` is the null calibration's reading (plan §4.3,
PR 3's acceptance): over a root of run directories, how often each arm's
final verdict was non-empty, by the kind of world, its null definition,
its seed set and its conditions (:func:`landing.conditions_digest`) — a
false call on a null world, a candidate recovery on a conditional one
(whether the selected rule IS the planted one is the scorer's question,
plan PR 4).
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import traceback
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, replace
from functools import cached_property
from pathlib import Path
from statistics import median
from typing import TypeVar

from .hypothesists import GridHypothesist, PoolHypothesist, RandomHypothesist, enumerate_pool
from .landing import (
    ARTIFACT_FILENAME,
    MANIFEST_FILENAME,
    Landing,
    artifact_digest,
    conditions_digest,
    discard_staging,
    experiment_for,
    manifest_digest,
    open_staging,
    verify,
    world_dir,
)
from .manifest import LlmArm, LlmToolsArm, Manifest, SeedSet, WorldKind
from .modeluse import MODEL_USE_FILENAME, MeteredModel, ModelSearch, ModelUse
from .oracle import Oracle, PlantedFigures, frame_for, planted_figures, pool_reference
from .records import Reader, RecordError, dumps
from .toolarm import (
    PACK_VERSION,
    Limits,
    Pack,
    ToolArmError,
    ToolHypothesist,
    ToolServerError,
    require_client_sdk,
)
from .tools_server import ServerParams, tool_specs
from .tooluse import TOOL_USE_FILENAME, SearchUse, ToolUse, TracePack, read_trace, trace_use
from .upstream import (
    ChatHypothesist,
    Experiment,
    Ledger,
    ResearchStore,
    SegmentMetrics,
    VocabEnum,
    promote,
    search,
)
from .verdict import ArmSnapshot, ArmVerdict, Ended, final_verdict, snapshot_arm

__all__ = [
    "ARMS_DIRNAME",
    "INQUIRY_FILENAME",
    "ORACLE_FILENAME",
    "VERDICTS_FILENAME",
    "VERDICTS_VERSION",
    "Arm",
    "ArmResult",
    "BenchError",
    "CalibrationRow",
    "KeptForInquiry",
    "RowKey",
    "Searched",
    "Verdicts",
    "calibration_table",
    "effort_note",
    "experiment_id",
    "in_row_order",
    "iter_searched",
    "model_hypothesist",
    "own_manifest",
    "preview_oracle",
    "read_manifest",
    "read_record",
    "read_searched",
    "require_artifact",
    "row_head",
    "row_key",
    "run_world",
    "seam_note",
    "server_log_of",
    "store_path",
    "tool_arm",
    "tools_experiment",
    "trace_of",
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
    """The arms this runner runs: the two that walk the pool, and a model arm where one is named.

    The model arm answers from the two prompts alone; the tool arm is the
    same model with the training-window look-ups at hand (tools plan T4).
    """

    RANDOM = "random"
    GRID = "grid"
    LLM = "llm"
    LLM_TOOLS = "llm_tools"

    @property
    def walks_pool(self) -> bool:
        """Whether this arm proposes from the candidate pool; a model may answer outside it."""
        return self in (Arm.RANDOM, Arm.GRID)


# -- the records ------------------------------------------------------------------------------


@dataclass(frozen=True)
class ArmResult:
    """One arm's one search: its snapshot, the selected trial's holdout, and the verdict read off it.

    ``replicate`` numbers the search: the search seed for the Random arm,
    0 for the Grid arm (deterministic, one search), the replicate's index
    for the model arm. ``holdout`` is the
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


def trace_of(store: Path) -> Path:
    """``replicate-<k>.trace.jsonl`` beside a tool-arm replicate's store: that search's trace."""
    return store.with_suffix(".trace.jsonl")


def server_log_of(store: Path) -> Path:
    """``replicate-<k>.server.log`` beside it: what that search's look-ups server said on stderr."""
    return store.with_suffix(".server.log")


@dataclass(frozen=True)
class Verdicts:
    """``verdicts.json``: every arm's result on one world, under the one set of conditions.

    The conditions are held here: every result's experiment is the same
    experiment but for its name and stamp (plan §4.3: three arms, one
    split, one cost model, one penalty; the budget is the manifest's and is
    not on the experiment), on the world's coin. ``pool_size`` is the pool
    the pool arms drew from — none of them can have filed more rules than that —
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
            if result.arm.walks_pool and result.snapshot.distinct_rules > self.pool_size:
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
            # A pool arm proposes each candidate at most once and runs out
            # exactly when it has proposed them all; the model answers
            # outside the pool (plan §4.3) and has no pool to run out of.
            answers = result.snapshot.answers
            if not result.arm.walks_pool and result.snapshot.ended is Ended.EXHAUSTED:
                raise BenchError(f"{result.experiment_id}: a model arm has no pool to walk to its end")
            if result.arm.walks_pool and answers > self.pool_size:
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

    @property
    def grid(self) -> ArmResult:
        """The Grid arm's result: every run has one, and its store is what the oracle and the scorer read."""
        return next(result for result in self.results if result.arm is Arm.GRID)

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


def read_record(path: Path, loads: Callable[[str], _R]) -> _R:
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
    """What a search has left in ``run_dir``: the claim and the records, in the order they are made.

    ``tool_use.json`` is a tool arm's alone and ``model_use.json`` a blind
    model arm's, so on a world with neither arm this lists what it always
    did.
    """
    names = (
        ARMS_DIRNAME,
        VERDICTS_FILENAME,
        TOOL_USE_FILENAME,
        MODEL_USE_FILENAME,
        ORACLE_FILENAME,
    )
    return [name for name in names if (run_dir / name).exists()]


_STAGING_PREFIX = ".search.partial-"

# The note a search that stopped on its tools' side leaves in the staging
# it keeps: why it stopped, and that the staging is there to be read.
INQUIRY_FILENAME = "kept-for-inquiry.txt"


# What a staging is renamed to when a search that stopped on its tools'
# side keeps it. The NAME is the mark that it was kept on purpose: a rename
# writes no byte, and the disk that could not take a trace's next line is
# not to be relied on to take a note saying so.
_KEPT_PREFIX = ".search.kept-"


def _kept_staging(run_dir: Path) -> list[str]:
    """The search stagings in ``run_dir``: a running search's, one a failed store move kept, a kill's, or one kept for inquiry."""
    return sorted(
        path.name
        for prefix in (_STAGING_PREFIX, _KEPT_PREFIX)
        for path in run_dir.glob(f"{prefix}*")
    )


def _mark_kept(staging: Path) -> Path:
    """Rename ``staging`` so that its name says it is kept on purpose; where it is afterwards.

    A rename that fails leaves the staging where it was, under the name it
    had, and is logged: the note inside (:func:`_kept_for_inquiry`) is then
    the one mark, and is read as one.
    """
    kept = staging.with_name(_KEPT_PREFIX + staging.name.removeprefix(_STAGING_PREFIX))
    try:
        staging.rename(kept)
    except OSError as exc:
        # The failure's own sentence and no stack: what this is raised
        # while handling may be a provider's failure, and a logged stack
        # would print that one's sentence with it.
        logger.warning("could not rename the kept staging %s to %s: %s", staging, kept, exc)
        return staging
    return kept


def _holds_stores(staging: Path) -> bool:
    """Whether a search's staging still holds an arm's store; one that cannot be listed is kept."""
    try:
        return any((staging / ARMS_DIRNAME).iterdir())
    except FileNotFoundError:
        return False
    except OSError:
        return True


def _kept_on_purpose(run_dir: Path) -> list[str]:
    """The stagings in ``run_dir`` a search kept ON PURPOSE (:func:`_kept_for_inquiry`).

    Those named as kept (:func:`_mark_kept`) — and one still under a
    staging's ordinary name that holds the note, which is what a rename
    that failed leaves. Either mark alone is enough: the two are made by
    different means so that one disk's refusal does not lose both.
    """
    return [
        name
        for name in _kept_staging(run_dir)
        if name.startswith(_KEPT_PREFIX) or (run_dir / name / INQUIRY_FILENAME).is_file()
    ]


def _unread_evidence(run_dir: Path) -> str | None:
    """The refusal for a directory still holding a staging kept for inquiry, whatever else it holds.

    The claim beside such a staging is what makes the next search stop;
    with the claim removed and the staging left, the directory would read
    as never searched, and the evidence would be searched past after all.
    So the staging itself refuses the directory, to every reader of a
    search's records and to the next search (the two verbs that read the
    landing alone, the oracle's preview and the look-ups by hand, do not
    ask).
    """
    on_purpose = _kept_on_purpose(run_dir)
    if not on_purpose:
        return None
    # What to remove is said of the staging alone. Both callers ask only
    # when the directory holds something other than a bare claim (an empty
    # ``arms/`` beside the staging is :func:`_unfinished`'s to speak of), so
    # here the claim is already gone, or ``arms/`` holds a finished search's
    # stores: there is no claim to name, and nobody is told to delete stores.
    return (
        f"{run_dir} holds {on_purpose}, kept on purpose by a search that stopped on its tools' "
        f"side: read {INQUIRY_FILENAME} in it, where it holds one, and the tool arm's trace and "
        f"its server's log under {ARMS_DIRNAME}/{Arm.LLM_TOOLS.value}/ there. This directory is "
        f"neither searched nor read while that staging is in it; once the staging has been "
        f"looked into, remove the staging by hand"
    )


def _unfinished(run_dir: Path, left: list[str]) -> str:
    """The sentence for a search that is running or did not finish: nothing prescribed while one may run.

    The staging is named with the claim: a run killed outright leaves one
    holding whole store copies, which removing ``arms/`` alone would leave
    behind. One kept ON PURPOSE — by a search that stopped on its tools'
    side — is marked as that (:func:`_kept_on_purpose`: by its name, or by
    the note in it), and is said to be: "remove them by hand" alone would
    have the evidence removed unread.
    """
    kept = _kept_staging(run_dir)
    sentence = (
        f"{run_dir} holds {left + kept}: a search of this world is running or did not finish; "
        f"once no search is running, remove them by hand to search the world again"
    )
    on_purpose = _kept_on_purpose(run_dir)
    if on_purpose:
        sentence += (
            f" — but {on_purpose} was kept on purpose by a search that stopped on its tools' side: "
            f"read {INQUIRY_FILENAME} in it, where it holds one, and the tool arm's trace and "
            f"its server's log under {ARMS_DIRNAME}/{Arm.LLM_TOOLS.value}/ there, before removing "
            f"anything"
        )
    return sentence


class KeptForInquiry(BenchError):
    """A search stopped on its tool arm's side, and what it left is kept to be looked into.

    A refusal of its own class because it is the one a batch must not run
    past (the CLI ends on it with an exit code of its own): the world next
    in line would meet the same server.
    """


class _ToolsSideStop(Exception):
    """A run has to stop on its tool arm's side; the sentence says what failed.

    Raised where the failure is met and turned into the operator's refusal
    in one place (``run_world``), which is also where the staging is marked
    to be kept — so no line that stops a run this way can forget to.
    """


def _spent(
    trace: Path, result: ArmResult, told: TracePack | None, tools: LlmToolsArm
) -> tuple[TracePack, SearchUse]:
    """What a finished tool-arm search was told and what it spent, read off its trace.

    Held to the search's own result, to the arm the manifest names
    (``tools``), and to what an earlier replicate of the arm was ``told``:
    one manifest's tool arm is one model under one pack. A trace this
    build wrote and cannot read back, or one that is not the search the
    ledger holds or the arm the manifest names, is a defect on this side,
    and the trace is the evidence of it: the run stops as it does for a
    server that failed, the trace kept, and not thrown away with a search
    that was paid for.
    """
    try:
        pack, use = trace_use(read_trace(trace), result.replicate)
        _hold_pack_to_arm(trace.name, pack, tools)
        if told is not None and pack != told:
            raise BenchError(
                f"{result.experiment_id} was told {pack}, an earlier replicate {told}; one "
                f"manifest's tool arm is one model under one pack"
            )
        _hold_use_to_result(trace.name, use, result)
    except RecordError as exc:
        raise _ToolsSideStop(
            f"what {result.experiment_id} spent could not be read back from its trace — {exc}"
        ) from exc
    return pack, use


def _model_use(
    manifest: Manifest,
    verdicts: Verdicts,
    searches: Sequence[ModelSearch],
    say: Callable[[str], None],
) -> ModelUse | None:
    """The blind model arm's record for a world just searched, or ``None``: no such arm, or no record to trust.

    Held to the verdicts as a reader will hold it (:func:`_hold_model_use`).
    Counts that are not the searches' would be a defect on this side — and,
    unlike a tool arm's trace, there is nothing left of them to look into:
    the counter was all there was. So the run is not stopped over them,
    which would throw away searches that were paid for to protect a figure
    that is a floor; the record is left unwritten, the world reads as "not
    recorded", and the operator is told why.
    """
    llm = manifest.arms.llm
    if llm is None:
        return None
    try:
        use = ModelUse(
            world_id=verdicts.world_id,
            manifest_digest=verdicts.manifest_digest,
            model=f"{llm.provider}/{llm.model}",
            searches=tuple(searches),
        )
        _hold_model_use(MODEL_USE_FILENAME, use, llm, verdicts)
    except RecordError as exc:
        logger.warning("what arms.llm spent is not recorded: %s", exc)
        say(f"model use: not recorded — {exc}")
        return None
    return use


def _kept_for_inquiry(
    run_dir: Path, staging: Path, why: str, cause: BaseException | None
) -> KeptForInquiry:
    """The refusal a run that stopped on its tools' side ends with, written into the staging it keeps.

    The caller keeps the staging and the claim (``run_world``'s
    ``finally``); here the staging is first renamed as kept
    (:func:`_mark_kept`), which is the mark every later reader goes by.
    The sentence goes to the operator once; the note is what is still
    there when the directory is next looked at, and what the next
    command's refusal points to. Where the failure was an exception this
    process caught (``cause``), its traceback goes under the sentence: the
    sentence is a line, and a defect is found by its stack. The exception's
    own stack and no chained one: what it was raised while handling may be
    a provider's failure, whose sentence this package writes to no file (it
    may quote what the provider was sent). A note that cannot be written
    is logged and not raised over — the refusal is the one to report, and
    the staging is kept either way, and marked by its name. (Unmarked only
    where the rename and the note BOTH failed: kept still, and read by the
    next command as any staging a run left behind.)
    """
    staging = _mark_kept(staging)
    sentence = (
        f"{run_dir}: {why}. Nothing is recorded for this world: a failure on the tools' side is "
        f"looked into, not searched past. What the search left is kept in {staging} (what a "
        f"tool arm's search got to write — its server's own log, and its trace once the arm was "
        f"open — is under {ARMS_DIRNAME}/{Arm.LLM_TOOLS.value}/ there), and the claim "
        f"{ARMS_DIRNAME}/ stays: the next search of this world is refused until both are removed "
        f"by hand"
    )
    note = f"{sentence}\n"
    if cause is not None:
        note += "\n" + "".join(traceback.format_exception(cause, chain=False))
    try:
        (staging / INQUIRY_FILENAME).write_text(note, encoding="utf-8", errors="replace")
    except (OSError, ValueError) as exc:
        logger.warning("could not write %s into %s: %s", INQUIRY_FILENAME, staging, exc)
    return KeptForInquiry(sentence)


def require_artifact(run_dir: Path, manifest: Manifest) -> None:
    """Refuse a run directory whose artifact is absent, or is not the file ``manifest`` landed.

    The artifact's shape proves little: the null twin of a seed shares its
    conditional world's coin, origin, bar count, settlement grid and even
    its funding and wick draws, and differs by the price path alone, so
    its ``world.sqlite`` copied into the other's directory passes ``verify``.
    The digest the landing wrote does not.
    """
    path = run_dir / ARTIFACT_FILENAME
    if not path.is_file():
        raise BenchError(f"{run_dir} holds no {ARTIFACT_FILENAME}; generate the world first")
    held = artifact_digest(path)
    if held != manifest.artifact_sha256:
        raise BenchError(
            f"{path} is not the artifact this directory's manifest landed: its SHA-256 is {held}, "
            f"the manifest records {manifest.artifact_sha256}"
        )


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
    require_artifact(run_dir, manifest)
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


# -- the look-ups, by hand -------------------------------------------------------------------------


def tools_experiment(run_dir: Path) -> Experiment:
    """The conditions a landed world's training-window look-ups are served under, for a person at the keyboard.

    The experiment an arm of this world would open
    (:func:`~contrib.discovery.landing.experiment_for`), as a value:
    nothing is stored. The ``tools`` command reads the coin, the split, the
    costs and the indicator window off it and starts the server on those
    and nothing else of the manifest (tools plan §2.2, §3.3). Development
    seeds only: nothing is tuned on a test seed (plan §4.2), and a person
    reading a test world's training window is in a position to tune the
    tools' wording on it.
    """
    manifest = landed_manifest(run_dir)
    world = manifest.world
    if world.seed_set is not SeedSet.DEV:
        raise BenchError(
            f"{world.id} is a {world.seed_set.value} seed: a world's training window is looked "
            f"at by hand on development seeds only, since nothing is tuned on a test seed and "
            f"the tools' wording is tuned by looking"
        )
    return experiment_for(manifest, "tools")


# -- the run -----------------------------------------------------------------------------------


_MODEL_RETRIES = 6


def model_hypothesist(llm: LlmArm | LlmToolsArm) -> ChatHypothesist:
    """The model a model arm of the manifest names, built the way the research package builds it, or a named refusal.

    Nothing is asked of the model here, so this is also the check that a
    world can be searched at all before minutes of work or any money is
    spent: an unknown provider, and a key that is not set wherever the
    client checks one as it is built (OpenRouter and the other
    OpenAI-compatible providers do; Anthropic direct and Bedrock notice at
    the first call, and that search ends as a failed seam). A client that
    dropped the temperature is refused: the records would describe a
    condition the run did not have. Retries are raised past the client's
    default because a replicate is not resumed: a transient failure at
    round 28 would throw away the 27 calls before it.
    """
    path = "arms.llm_tools" if isinstance(llm, LlmToolsArm) else "arms.llm"
    try:
        hypothesist = ChatHypothesist.build(
            llm.provider, llm.model, temperature=llm.temperature, max_retries=_MODEL_RETRIES
        )
    except Exception as exc:  # noqa: BLE001 - the clients share no base class for this
        raise BenchError(f"{path}: {exc}") from exc
    if getattr(hypothesist.model, "temperature", llm.temperature) is None:
        raise BenchError(
            f"{path}: the client for {llm.provider}/{llm.model} dropped temperature "
            f"{llm.temperature}; the manifest would describe a run that did not use it"
        )
    return hypothesist


def tool_arm(tools: LlmToolsArm) -> tuple[ChatHypothesist, Limits]:
    """The tool arm's model and its limits, or a named refusal, before anything is started for it.

    What can be known to be wrong without a server: a pack version that
    is not this build's — the manifest would describe a run told other
    words than it was — the SDK the arm's client is built on, the model
    (:func:`model_hypothesist`), and a client that will not be bound with
    this build's tools. Asked
    where a world is landed and again where it is searched, as the model
    arm's check is, so neither a backfill nor the pool arms' minutes are
    spent on an arm that cannot run.
    """
    if tools.pack_version != PACK_VERSION:
        raise BenchError(
            f"arms.llm_tools.pack_version: this build tells a model about its tools at pack "
            f"version {PACK_VERSION}, the manifest asks for {tools.pack_version}"
        )
    try:
        require_client_sdk()
        limits = Limits(lookups=tools.lookups, backtests=tools.backtests)
    except ToolArmError as exc:
        raise BenchError(f"arms.llm_tools: {exc}") from exc
    model = model_hypothesist(tools)
    bind = getattr(model.model, "bind_tools", None)
    if not callable(bind):
        raise BenchError(f"arms.llm_tools: {model.label} takes no tools: its client has no bind_tools")
    try:
        # Bound once here with the pack this build pins, and the result let
        # go: a client can have the method and still refuse to be bound (the
        # base class every chat client inherits it from raises), and that is
        # learnt by asking. Nothing is sent to a provider by it.
        bind(list(Pack.of(tool_specs(), limits).tools))
    except Exception as exc:  # noqa: BLE001 - each client refuses in a class of its own
        raise BenchError(
            f"arms.llm_tools: {model.label} could not be given the tools: {exc}"
        ) from exc
    return model, limits


# How an arm's hypothesist is had for one search: handed the arm's own copy
# of the artifact and the experiment stored in it, it gives the hypothesist
# for as long as the search lasts.
_Asked = PoolHypothesist | ChatHypothesist | ToolHypothesist
_Opened = Callable[[Path, Experiment], AbstractContextManager[_Asked]]


def _arms(manifest: Manifest) -> list[tuple[Arm, int, _Opened]]:
    """The arms to run, in order: the Random replicates as listed, Grid, then a model arm's replicates.

    Each as the way its hypothesist is OPENED for its search, not as a
    hypothesist: the tool arm's is opened over a server that reads the
    arm's own copy of the artifact under the experiment stored in it
    (tools plan §1 #8), and neither exists until the loop makes it. The
    other arms' hypothesists hold nothing of a store and are built here.

    A model arm last: it is the only arm that costs money, so a world
    whose pool arms cannot run has spent nothing.
    """
    pool = enumerate_pool(manifest.search_space)
    world_id = manifest.world.id

    def held(hypothesist: PoolHypothesist | ChatHypothesist) -> _Opened:
        return lambda path, experiment: nullcontext(hypothesist)

    arms: list[tuple[Arm, int, _Opened]] = [
        (Arm.RANDOM, seed, held(RandomHypothesist(pool, world_id=world_id, search_seed=seed)))
        for seed in manifest.arms.random.search_seeds
    ]
    arms.append((Arm.GRID, 0, held(GridHypothesist(pool))))
    llm = manifest.arms.llm
    if llm is not None:
        # One adapter, built once: it holds a client and no state of a
        # search. What a search spent IS state of that search, so each is
        # asked through a copy of the adapter whose client is counted
        # (:mod:`.modeluse`); the client under every counter is the one.
        blind = model_hypothesist(llm)

        def metered(path: Path, experiment: Experiment) -> AbstractContextManager[_Asked]:
            return nullcontext(replace(blind, model=MeteredModel(blind.model)))

        arms += [(Arm.LLM, k, metered) for k in range(llm.replicates)]
    tools = manifest.arms.llm_tools
    if tools is not None:
        model, limits = tool_arm(tools)

        def with_tools(path: Path, experiment: Experiment) -> AbstractContextManager[_Asked]:
            # The server is started on the experiment the arm STORED — its
            # split is the one the arm's trials are measured under — and
            # its stderr and the search's trace are kept beside the store.
            return ToolHypothesist.open(
                model,
                ServerParams.for_experiment(path, experiment),
                trace=trace_of(path),
                server_log=server_log_of(path),
                limits=limits,
            )

        arms += [(Arm.LLM_TOOLS, k, with_tools) for k in range(tools.replicates)]
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
    if left != [ARMS_DIRNAME] and (unread := _unread_evidence(run_dir)) is not None:
        # With the claim beside it, the sentence below names both; without
        # it — the claim removed and the staging left — this one does.
        raise BenchError(unread)
    if left == [ARMS_DIRNAME]:
        # The claim alone is what a search in progress leaves (it is made
        # first, the records moved in last): nothing is prescribed while one
        # may be running, as calibrate says of the same directory.
        raise BenchError(_unfinished(run_dir, left))
    if left:
        # A staging kept by a failed store move holds the stores the claim did
        # not receive; it is named so it is not left behind by a re-search.
        kept = _kept_staging(run_dir)
        records = [VERDICTS_FILENAME, ORACLE_FILENAME]
        for spent in (MODEL_USE_FILENAME, TOOL_USE_FILENAME):
            if spent in left:
                records.insert(1, spent)
        raise BenchError(
            f"{run_dir} holds {left + kept}: this world was searched before, and a search is "
            f"spent once — remove {ARMS_DIRNAME}/, {', '.join(records)}"
            + (" and the kept staging" if kept else "")
            + " by hand to search it again"
        )
    require_artifact(run_dir, manifest)
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
        staging = open_staging(run_dir, _STAGING_PREFIX)
    except BaseException:
        # No staging, nothing run: the claim goes back, or the next search
        # would be told the world was searched.
        _give_back(claim)
        raise
    # Set when the run stopped on its tool arm's side (``_ToolsSideStop``):
    # what the search left is then kept for whoever looks into it, the
    # claim with it. A flag, where the rest of the ending is read off the
    # directory: it is set before the note is written, so a note that could
    # not be written does not have the evidence discarded.
    inquiry = False
    try:
        results: list[ArmResult] = []
        uses: list[SearchUse] = []
        blind_uses: list[ModelSearch] = []
        told: TracePack | None = None
        landing: Landing | None = None
        for arm, replicate, opened in _arms(manifest):
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
                name = experiment_id(arm, replicate)
                tools_failed: str | None = None
                try:
                    with opened(path, experiment) as hypothesist:
                        report = search(
                            ledger,
                            experiment,
                            hypothesist,
                            model=(
                                arm.value
                                if isinstance(hypothesist, PoolHypothesist)
                                else hypothesist.label
                            ),
                            max_trials=manifest.budget.max_trials,
                        )
                        if isinstance(hypothesist, ToolHypothesist):
                            tools_failed = hypothesist.tools_failed
                except ToolServerError as exc:
                    # ``search()`` keeps a seam's failure to itself, so this
                    # one came from the opening: the server did not come up.
                    raise _ToolsSideStop(f"the look-ups' server failed in {name} — {exc}") from exc
                except ToolArmError as exc:
                    # What can be refused without a server was, before any
                    # arm ran (``tool_arm``). What is left to be refused
                    # HERE is found once the child is started — a server
                    # that is not this build's, a client that will not be
                    # bound after all — and the child's log is where it
                    # says why: kept, with the rest. (The opening's other
                    # refusals — a trace that exists, limits that are none —
                    # cannot be met in a staging just made, from limits
                    # ``tool_arm`` built.)
                    raise _ToolsSideStop(f"{name} could not be opened — {exc}") from exc
                except OSError as exc:
                    if arm is not Arm.LLM_TOOLS:
                        raise
                    # The arm's own files: a trace that could not be written
                    # in the middle of a search that was being paid for, a
                    # log that could not be opened for its server. What was
                    # written is kept, with the rest.
                    raise _ToolsSideStop(
                        f"{name} could not open or write its trace or its server's log — {exc}"
                    ) from exc
                if tools_failed is not None:
                    # Not a seam failure to count apart and search past
                    # (decided 2026-11-19): that rule is for a provider that
                    # went away. A world re-run until its model happens not
                    # to ask what the server could not answer is a sample
                    # picked by the model's behaviour, and a defect on this
                    # side would be re-run over. The run stops, nothing is
                    # recorded, and what was written is kept to be read.
                    raise _ToolsSideStop(f"the look-ups' server failed in {name} — {tools_failed}")
                for line in report.describe():
                    say(line)
                snapshot = snapshot_arm(
                    ledger,
                    experiment,
                    report,
                    exhausted=isinstance(hypothesist, PoolHypothesist) and hypothesist.exhausted,
                )
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
                tools = manifest.arms.llm_tools
                if arm is Arm.LLM_TOOLS and tools is not None:
                    # Read once the search is over and its trace closed:
                    # what the search spent, kept where a table can read it
                    # after the trace is gone.
                    told, use = _spent(trace_of(path), result, told, tools)
                    uses.append(use)
                meter = getattr(hypothesist, "model", None)
                if arm is Arm.LLM and isinstance(meter, MeteredModel):
                    # What came back through this search's counter, read
                    # once its verdict is taken: nothing of the search
                    # depended on it.
                    blind_uses.append(
                        meter.spent(replicate, seam_failed=snapshot.ended is Ended.SEAM_FAILED)
                    )
        verdicts = Verdicts(
            world_id=world_id,
            pool_size=pool_size,
            manifest_digest=digest,
            results=tuple(results),
        )
        tool_use: ToolUse | None = None
        tools = manifest.arms.llm_tools
        if tools is not None and told is not None:
            try:
                tool_use = ToolUse(
                    world_id=world_id, manifest_digest=digest, pack=told, searches=tuple(uses)
                )
                _hold_use_to_manifest(TOOL_USE_FILENAME, tool_use, tools)
            except RecordError as exc:
                # Each search was held as it was read; a record of them all
                # that cannot be built is the same defect, found later.
                raise _ToolsSideStop(
                    f"what the tool arm's searches spent does not make a record — {exc}"
                ) from exc
        model_use = _model_use(manifest, verdicts, blind_uses, say)
        # The oracle last (plan §4.4): read on the Grid arm's store after
        # every arm has searched and chosen. The oracle path files nothing,
        # so the store is the arm's record still.
        grid = verdicts.grid.snapshot.experiment
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
        # Written whole in staging, then moved: the records first, then
        # each arm's store into the claim. A failure before the records are
        # all in leaves the directory as the landing left it (but for the
        # records already moved, which calibrate and the guard refuse by
        # name, the oracle's not being among them); a failure
        # while the stores move in leaves the records and the staging that
        # still holds the unmoved stores, named in the refusal so nothing
        # is lost. The landed manifest is not touched.
        # ``oracle.json`` goes in last, whatever else there is: it is what
        # the ``finally`` below reads "the records are in" off.
        written = {VERDICTS_FILENAME: verdicts.dumps()}
        if tool_use is not None:
            written[TOOL_USE_FILENAME] = tool_use.dumps()
        if model_use is not None:
            written[MODEL_USE_FILENAME] = model_use.dumps()
        written[ORACLE_FILENAME] = oracle.dumps()
        for name, text in written.items():
            (staging / name).write_bytes(text.encode("utf-8"))
        for name in written:
            os.replace(staging / name, run_dir / name)
        done: list[str] = []
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
    except _ToolsSideStop as stop:
        # The one place the staging is marked to be kept, whichever line
        # stopped the run: the refusal says it was, and has to be true.
        # The flag first, so that whatever happens while the staging is
        # renamed and its note written — an interrupt, a full disk — the
        # ``finally`` below leaves it alone.
        inquiry = True
        cause = stop.__cause__
        raise _kept_for_inquiry(run_dir, staging, str(stop), cause) from cause
    finally:
        # Decided from the directory, not from how far the code got: an
        # interrupt can land between a rename and the line after it. The
        # guard refused any record and the claim keeps other searches out,
        # so an ``oracle.json`` here is this run's. Once it is in, the claim
        # stays (the records sit beside it, refused by the guard until the
        # operator removes them) and a staging still holding stores the
        # claim did not receive is kept — a failed rename and an interrupt
        # alike; emptied by the moves, it goes. Before it is in, the
        # half-made run goes and the claim is given back — unless the run
        # stopped on its tool arm's side (``inquiry``), when neither is
        # touched here: the staging — by now renamed as kept — is the
        # evidence, and the claim is what makes the next search name it
        # instead of running past it.
        records_in = (run_dir / ORACLE_FILENAME).exists()
        if not inquiry:
            if not (records_in and _holds_stores(staging)):
                discard_staging(staging)
            if not records_in:
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
    manifest with ``world_seed`` (and the id derived from it) set aside
    (``landing.conditions_digest``), so the worlds of one β tier, one
    penalty, one budget, one bar count and one seed set are tallied together and two calibrations under one root are
    two rows, each printed with its digest — never one rate over both.

    A search is a run that answered — spent its budget or walked its pool
    to the end; an arm whose seam failed did not finish its search, is counted in
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
    effort: tuple[tuple[int, int], ...] = ()

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
        what = "non-empty verdicts" if self.kind.plants else "false calls"
        rate = "n/a" if self.rate is None else f"{self.rate:.1%}"
        head = row_head(
            (self.kind, self.null_definition_version, self.seed_set, self.conditions, self.arm)
        )
        return (
            f"{head}{self.found} {what} in {self.searches} search(es) = {rate}"
            f"{seam_note(self.failed)}"
        ) + (f"; {effort_note(self.effort)}" if self.effort else "")


@dataclass(frozen=True)
class Searched:
    """A searched run directory read and held together: its manifest and its records.

    ``tool_use`` is what the tool arm's searches spent (:mod:`.tooluse`):
    present exactly when the manifest names that arm. ``model_use`` is what
    the blind model arm's did (:mod:`.modeluse`): never present without
    that arm, and absent with it where the world was searched before that
    record existed.
    """

    manifest: Manifest
    verdicts: Verdicts
    oracle: Oracle
    tool_use: ToolUse | None = None
    model_use: ModelUse | None = None


def read_searched(run_dir: Path) -> Searched | None:
    """``run_dir``'s manifest and records, each held to the others; ``None`` if nothing searched it.

    A directory holding none of what a search leaves (``arms/``,
    ``verdicts.json``, a model arm's ``tool_use.json`` or ``model_use.json``,
    ``oracle.json``) is
    a world not yet searched; one holding some of it but not both the
    verdicts and the oracle's record is a search in progress or one that
    did not finish, refused by name rather than dropped without a word.
    So is one holding a staging a search kept for inquiry, whatever else
    it holds: a world that stopped is not one never searched. A record
    that cannot be read, or that is not its manifest's — another world,
    pool, arm set, digest, set of conditions, budget, a pool arm's rule
    outside the pool, or a tool arm's use that is not the arm's — is
    refused by name. Every reader of the records comes through here.
    """
    left = _search_left(run_dir)
    if left != [ARMS_DIRNAME] and (unread := _unread_evidence(run_dir)) is not None:
        # Before "not yet searched": a staging kept for inquiry whose claim
        # was removed is a world that was searched, and stopped.
        raise BenchError(unread)
    if not left:
        return None
    if VERDICTS_FILENAME not in left or ORACLE_FILENAME not in left:
        # ``arms/`` alone is also what a search in progress leaves (the
        # claim is made first, the records moved in last), so the
        # sentence says so and prescribes nothing while one may be running.
        raise BenchError(_unfinished(run_dir, left))
    manifest = own_manifest(run_dir)
    verdicts = read_record(run_dir / VERDICTS_FILENAME, Verdicts.loads)
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
    # The arms the manifest names, no fewer and no others: a record
    # missing one was not written by this build's search.
    expected = {(Arm.RANDOM, seed) for seed in manifest.arms.random.search_seeds}
    expected.add((Arm.GRID, 0))
    if manifest.arms.llm is not None:
        expected |= {(Arm.LLM, k) for k in range(manifest.arms.llm.replicates)}
    if manifest.arms.llm_tools is not None:
        expected |= {(Arm.LLM_TOOLS, k) for k in range(manifest.arms.llm_tools.replicates)}
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
    oracle = read_record(run_dir / ORACLE_FILENAME, Oracle.loads)
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
    return Searched(
        manifest=manifest,
        verdicts=verdicts,
        oracle=oracle,
        tool_use=_read_tool_use(run_dir, manifest, verdicts),
        model_use=_read_model_use(run_dir, manifest, verdicts),
    )


def _read_tool_use(run_dir: Path, manifest: Manifest, verdicts: Verdicts) -> ToolUse | None:
    """``tool_use.json`` of a searched directory, held to the manifest and the verdicts beside it.

    Written by a search exactly when its manifest names the tool arm, so
    one missing where it is due, or present where none is, is a directory
    this build's search did not leave.
    """
    path = run_dir / TOOL_USE_FILENAME
    tools = manifest.arms.llm_tools
    if tools is None:
        if path.exists():
            raise BenchError(f"{path}: the manifest beside it names no tool arm to have written it")
        return None
    if not path.is_file():
        raise BenchError(
            f"{run_dir} ran a tool arm and holds no {TOOL_USE_FILENAME}; a search writes it with "
            f"{VERDICTS_FILENAME}"
        )
    use = read_record(path, ToolUse.loads)
    if (use.world_id, use.manifest_digest) != (verdicts.world_id, verdicts.manifest_digest):
        raise BenchError(
            f"{path} is for {use.world_id} under manifest {use.manifest_digest}; "
            f"{VERDICTS_FILENAME} beside it is for {verdicts.world_id} under "
            f"{verdicts.manifest_digest}"
        )
    _hold_use_to_manifest(path, use, tools)
    by_replicate = {r.replicate: r for r in verdicts.results if r.arm is Arm.LLM_TOOLS}
    for spent in use.searches:
        _hold_use_to_result(path, spent, by_replicate[spent.replicate])
    return use


def _read_model_use(run_dir: Path, manifest: Manifest, verdicts: Verdicts) -> ModelUse | None:
    """``model_use.json`` of a searched directory, held to the manifest and the verdicts beside it.

    Written by a search whose manifest names the blind model arm, so one
    present where no such arm is named is a directory this build's search
    did not leave. One ABSENT where the arm is named is not refused: the
    worlds searched before this record existed have none (issue #433), and
    ``None`` is how a table is told so.
    """
    path = run_dir / MODEL_USE_FILENAME
    llm = manifest.arms.llm
    if llm is None:
        if path.exists():
            raise BenchError(
                f"{path}: the manifest beside it names no blind model arm to have written it"
            )
        return None
    if not path.exists():
        return None
    use = read_record(path, ModelUse.loads)
    _hold_model_use(path, use, llm, verdicts)
    return use


def _hold_model_use(where: Path | str, use: ModelUse, llm: LlmArm, verdicts: Verdicts) -> None:
    """Refuse a model-use record that is not of the blind model arm a manifest names, as its verdicts ran it.

    The world and the manifest the verdicts were written under; the model,
    by the label the research package builds one under and files its
    answers by (``provider/model``); the replicates the arm has, each once;
    and for each search what its verdict says of it — a call that was
    counted is an answer of the search (the loop asks once a round, and a
    reply that is text is a round spent), and the search ended on a failed
    seam in the one exactly when it did in the other.
    """
    if (use.world_id, use.manifest_digest) != (verdicts.world_id, verdicts.manifest_digest):
        raise BenchError(
            f"{where} is for {use.world_id} under manifest {use.manifest_digest}; "
            f"{VERDICTS_FILENAME} beside it is for {verdicts.world_id} under "
            f"{verdicts.manifest_digest}"
        )
    label = f"{llm.provider}/{llm.model}"
    if use.model != label:
        raise BenchError(
            f"{where}: the searches are those of the model {use.model}; the manifest's model "
            f"arm names {label}"
        )
    replicates = sorted(search.replicate for search in use.searches)
    if replicates != list(range(llm.replicates)):
        raise BenchError(
            f"{where} holds the model arm's replicate(s) {replicates}; the manifest names "
            f"{llm.replicates}, numbered from 0"
        )
    by_replicate = {r.replicate: r for r in verdicts.results if r.arm is Arm.LLM}
    for spent in use.searches:
        result = by_replicate[spent.replicate]
        snapshot = result.snapshot
        if spent.model_calls != snapshot.answers:
            raise BenchError(
                f"{where}: {result.experiment_id} is said to have had {spent.model_calls} model "
                f"call(s) come back and its ledger holds {snapshot.answers} answer(s); a call "
                f"that came back is one answer"
            )
        if spent.seam_failed != (snapshot.ended is Ended.SEAM_FAILED):
            raise BenchError(
                f"{where}: {result.experiment_id} ended {snapshot.ended.value}, and its record "
                + ("says it ended" if spent.seam_failed else "says it did not end")
                + " on a failed seam"
            )


def _hold_use_to_manifest(where: Path | str, use: ToolUse, tools: LlmToolsArm) -> None:
    """Refuse a tool-use record that is not of the tool arm a manifest names (``tools``).

    The replicates the arm has, each once, and the pack that arm is run
    under (:func:`_hold_pack_to_arm`): the record says what the searches
    were told, and the manifest is what the run is filed under.
    """
    replicates = sorted(search.replicate for search in use.searches)
    if replicates != list(range(tools.replicates)):
        raise BenchError(
            f"{where} holds the tool arm's replicate(s) {replicates}; the manifest names "
            f"{tools.replicates}, numbered from 0"
        )
    _hold_pack_to_arm(where, use.pack, tools)


def _hold_pack_to_arm(where: Path | str, pack: TracePack, tools: LlmToolsArm) -> None:
    """Refuse what a search says it was told when it is not what the manifest's tool arm sets.

    The pack version and the two limits; and the model, by the label the
    research package builds a model under and files its answers by
    (``provider/model``) — a search told as another model's is not this
    arm's, whatever its limits.
    """
    told = (pack.pack_version, pack.limits.lookups, pack.limits.backtests)
    asked = (tools.pack_version, tools.lookups, tools.backtests)
    if told != asked:
        raise BenchError(
            f"{where}: the searches were told pack version {told[0]} under limits of {told[1]} "
            f"call(s) and {told[2]} backtest(s); the manifest sets {asked[0]}, {asked[1]} and "
            f"{asked[2]}"
        )
    label = f"{tools.provider}/{tools.model}"
    if pack.model != label:
        raise BenchError(
            f"{where}: the searches are those of the model {pack.model}; the manifest's tool arm "
            f"names {label}"
        )


def _hold_use_to_result(where: Path | str, use: SearchUse, result: ArmResult) -> None:
    """Refuse what a search is said to have spent when it is not what its verdict says it did.

    A round of the trace is an answer of the search — the loop asks once a
    round — and the search ended on a failed seam in the one exactly when
    it did in the other.
    """
    snapshot = result.snapshot
    if len(use.rounds) != snapshot.answers:
        raise BenchError(
            f"{where}: {result.experiment_id}'s trace answered {len(use.rounds)} round(s) and its "
            f"ledger holds {snapshot.answers} answer(s); a round is one answer"
        )
    if use.seam_failed != (snapshot.ended is Ended.SEAM_FAILED):
        raise BenchError(
            f"{where}: {result.experiment_id} ended {snapshot.ended.value}, and its trace "
            + ("ends on a failed seam" if use.seam_failed else "ends on no failed seam")
        )


def iter_searched(run_dirs: Iterable[Path]) -> Iterator[tuple[Path, Searched]]:
    """Every searched directory of ``run_dirs`` with its records, each world once.

    A directory the walk meets twice (a link to it in the root, a backup
    alias say) is yielded the first time only; one no search has touched
    is skipped; one :func:`read_searched` refuses ends the walk.
    """
    seen: set[Path] = set()
    for run_dir in run_dirs:
        real = run_dir.resolve()
        if real in seen:
            continue
        seen.add(real)
        searched = read_searched(run_dir)
        if searched is not None:
            yield run_dir, searched


RowKey = tuple[WorldKind, str, SeedSet, str, Arm]


def row_key(manifest: Manifest, arm: Arm) -> RowKey:
    """What one row of a table over worlds tallies: one rate is never taken over two of these."""
    world = manifest.world
    return (
        world.kind,
        manifest.null_definition_version,
        world.seed_set,
        conditions_digest(manifest),
        arm,
    )


def row_head(key: RowKey) -> str:
    """What a table's row opens with, so ``calibrate`` and ``report`` label one key alike."""
    kind, version, seed_set, conditions, arm = key
    return f"{kind.value} ({version}) {seed_set.value} [{conditions}] {arm.value}: "


def seam_note(failed: int) -> str:
    """What a table's row closes with when arms' seams failed; they are not in its denominator."""
    return f"; {failed} seam failure(s) not counted" if failed else ""


def effort_note(effort: Sequence[tuple[int, int]]) -> str:
    """The answers and the distinct rules each search spent: what a clean row actually searched.

    A search whose every answer was refused files no rule and cannot call
    anything, so on a null world it reads as no false call; this is what
    tells that from a search of thirty rules.
    """
    answers = [pair[0] for pair in effort]
    rules = [pair[1] for pair in effort]
    return (
        f"answers median {median(answers):g} (min {min(answers)}), distinct rules median "
        f"{median(rules):g} (min {min(rules)})"
    )


def in_row_order(keys: Iterable[RowKey]) -> list[RowKey]:
    """The keys as a table prints them: kind, seed set and arm in their enums' order, the texts in text order."""
    kinds = {kind: index for index, kind in enumerate(WorldKind)}
    seed_sets = {seed_set: index for index, seed_set in enumerate(SeedSet)}
    arms = {arm: index for index, arm in enumerate(Arm)}
    return sorted(
        keys, key=lambda key: (kinds[key[0]], key[1], seed_sets[key[2]], key[3], arms[key[4]])
    )


def calibration_table(run_dirs: Iterable[Path]) -> list[CalibrationRow]:
    """Every searched run directory's verdicts, tallied per row key (the module docstring).

    What is read and refused is :func:`read_searched`'s, over the worlds
    :func:`iter_searched` walks. Rows are keyed by kind, null definition,
    seed set, conditions and arm, and come in that order: the kind, seed
    set and arm in their enums' order, the null definition and the
    conditions digest in text order.
    """
    tally: dict[RowKey, list[int]] = {}
    effort: dict[RowKey, list[tuple[int, int]]] = {}
    for _run_dir, searched in iter_searched(run_dirs):
        for result in searched.verdicts.results:
            key = row_key(searched.manifest, result.arm)
            counts = tally.setdefault(key, [0, 0, 0])
            if result.snapshot.ended is Ended.SEAM_FAILED:
                counts[2] += 1
            else:
                counts[0] += 1
                counts[1] += result.verdict.found
                effort.setdefault(key, []).append(
                    (result.verdict.answers, result.verdict.distinct_rules)
                )
    rows: list[CalibrationRow] = []
    for key in in_row_order(tally):
        searches, found, failed = tally[key]
        rows.append(CalibrationRow(*key, searches, found, failed, tuple(effort.get(key, ()))))
    return rows


def _hold_to_manifest(path: Path, verdicts: Verdicts, manifest: Manifest) -> None:
    """Refuse a verdicts record that was not searched under ``manifest``'s conditions.

    The arms share one set of conditions (``Verdicts`` holds that); here the
    set is held to the manifest's — a record whose experiments carry
    another penalty, split or cost model would be judged by them, since the
    verdict is re-derived from the snapshot. Each arm's answers are bounded
    by the manifest's budget and a spent budget is exactly it (the pool's
    half of those bounds is the record's own, held in ``Verdicts``), and
    every rule a pool arm filed is a rule of the pool (the model may
    propose outside it, plan §4.3).
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
        if outside and result.arm.walks_pool:
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
