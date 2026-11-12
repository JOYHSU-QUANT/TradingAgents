"""``python -m contrib.discovery`` — the benchmark's commands.

Six so far, in the order a world goes through them (plan §3):

- ``generate --manifest world.json [--root DIR]`` — read a manifest, build
  its world from the seed, land it in ``<root>/<world id>-<manifest
  digest>/world.sqlite`` through the research package's own backfill,
  check the landed history the way an experiment would (plan PR 2's
  acceptance list), and write the canonical ``manifest.json`` beside it
  with ``search_space.pool_size`` and ``artifact_sha256`` filled in — the
  two fields the run directory's digest sets aside (:mod:`.landing` owns the layout and the
  staged landing). The artifact and the manifest are read-only from then
  on: an arm copies the artifact (plan §2), the manifest is never written
  again, and a second ``generate`` of a manifest already landed is
  refused rather than overwritten.
- ``oracle --run DIR`` — the planted rule's train and validation figures
  against the gate at the end of the budget, on a throw-away copy, before
  any arm has searched: the reading that sizes ``beta`` on development
  seeds, and refused on a test seed (plan §4.2, §4.4). Writes nothing.
- ``search --run DIR`` — the Random replicates and the Grid arm, each in
  its own copy of the artifact under ``arms/``, each judged at the end of
  its budget (``verdicts.json``), then the oracle's figures and the whole
  pool's reference (``oracle.json``), in that order (:mod:`.bench` keeps
  it). A world is searched once; a manifest that is not the directory's
  own is refused. A manifest that names a model arm runs it too, last: it
  is the one arm that costs money.
- ``calibrate --root DIR`` — every searched run directory under the root
  tallied by world kind, seed set, set of conditions (a digest printed on
  each row, so two calibrations under one root are two rows) and arm: on
  null worlds the false-call rate under the null definition named, on
  conditional worlds the rate of non-empty verdicts (plan §4.3's null
  calibration).
- ``score --run DIR`` — how close each arm's selected rule came to the
  planted one on the holdout (structural distance, direction agreement,
  holdout performance, plan §4.6), written to ``scores.json`` (:mod:`.scoring`
  owns the definitions). Pure over the records and the Grid arm's store,
  so it may be re-run.
- ``report --root DIR`` — every searched and scored world under the root
  in one table per arm and set of conditions: recovery, false calls, the
  oracle's pass rate and the recovery where it had room, the diagnostics
  (plan §3's last step). Reads no store. A test seed's rows keep everything
  read from the holdout sealed unless ``--unseal`` is given (plan §4.2).

What the commands print is for the operator: the world's kind and rule
are in the manifest, which a model never sees (plan §1 #7). Nothing
printed here reaches a prompt.

Exit codes, as the research package's CLI has them: ``0`` done; ``1`` a
named refusal — a manifest this build cannot read or run, a world the
generator does not make, a landed history an arm could not measure on, a
run directory searched before or not landed, a record a later build
cannot read, a store that cannot be opened, a path that cannot be read or
written — with the sentence on stderr; ``2`` argparse's usage errors;
``130`` interrupted, with a search's staging removed — unless its records
were already in, when the staging holding the stores ``arms/`` did not
receive is kept and the next ``search`` names it.
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import replace
from pathlib import Path

from .bench import (
    ORACLE_FILENAME,
    VERDICTS_FILENAME,
    BenchError,
    calibration_table,
    model_hypothesist,
    preview_oracle,
    run_world,
)
from .landing import ARTIFACT_FILENAME, MANIFEST_FILENAME, default_root, land_world
from .manifest import Manifest, ManifestError
from .records import RecordError
from .scoring import SCORES_FILENAME, report_table, require_this_scorer, score_world
from .upstream import EvaluationError, FeatureError, SplitError, StoreError, render_fetch
from .worlds import WorldError, generate

__all__ = ["main"]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m contrib.discovery",
        description="Discovery Lab: synthetic worlds the research loop is examined on.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    # argparse %-formats help text, so a '%' in the repository's path is doubled.
    shown_root = str(default_root()).replace("%", "%%")

    generate_cmd = subparsers.add_parser(
        "generate",
        help="build a world from its manifest, land it in its own store, and check it",
    )
    generate_cmd.add_argument(
        "--manifest", required=True, help="path to the world's manifest (JSON, version 3)"
    )
    generate_cmd.add_argument(
        "--root",
        default=None,
        help=f"the bench root; the run lands in <root>/<world id>-<digest>/ (default: {shown_root})",
    )

    oracle_cmd = subparsers.add_parser(
        "oracle",
        help="the planted rule's figures against the gate, before any search (development seeds)",
    )
    oracle_cmd.add_argument(
        "--run", required=True, help="a landed run directory (manifest.json and world.sqlite)"
    )

    search_cmd = subparsers.add_parser(
        "search",
        help="run the arms the manifest names on a landed world, judge them, then read the oracle",
    )
    search_cmd.add_argument(
        "--run", required=True, help="a landed run directory (manifest.json and world.sqlite)"
    )

    calibrate_cmd = subparsers.add_parser(
        "calibrate",
        help=(
            "tally every searched world under a root by kind, null definition, seed set, "
            "conditions (a digest printed on each row) and arm"
        ),
    )
    calibrate_cmd.add_argument(
        "--root", default=None, help=f"the bench root to walk (default: {shown_root})"
    )

    score_cmd = subparsers.add_parser(
        "score",
        help="how close each arm's selected rule came to the planted one (writes scores.json)",
    )
    score_cmd.add_argument("--run", required=True, help="a searched run directory")

    report_cmd = subparsers.add_parser(
        "report",
        help="every scored world under a root, in one table per arm and set of conditions",
    )
    report_cmd.add_argument(
        "--root", default=None, help=f"the bench root to walk (default: {shown_root})"
    )
    report_cmd.add_argument(
        "--unseal",
        action="store_true",
        help="print a test seed's recovery and holdout figures, which are sealed by default",
    )
    return parser


def _cmd_generate(args: argparse.Namespace) -> int:
    # The manifest's read writes the pool and the world is built before
    # anything is written, so a manifest the language or the generator
    # refuses leaves nothing behind. The pool's size is filled in here, so
    # the landed manifest records it and is never rewritten after (the run
    # directory's name sets it and the artifact's digest aside, so it names
    # the same place).
    manifest = Manifest.read(Path(args.manifest))
    # A world is searched once: one this build could not score would spend
    # that search and only then be refused.
    require_this_scorer(manifest)
    if manifest.arms.llm is not None:
        # The same: a model that cannot be built is refused here, not after
        # the backfill (the provider is part of the directory's identity,
        # so it cannot be corrected in place).
        model_hypothesist(manifest.arms.llm)
    space = manifest.search_space
    manifest = replace(manifest, search_space=replace(space, pool_size=len(space.candidates)))
    root = Path(args.root) if args.root else default_root()
    history = generate(manifest)
    target, fetched, landing = land_world(root, history)
    print(f"store: {target / ARTIFACT_FILENAME}")
    for result in fetched:
        print(render_fetch(result))
    for line in landing.describe():
        print(line)
    if manifest.world.planted is not None:
        count = manifest.world.bars.count
        print(
            f"planted rule: holds a position on {history.planted_exposure} of {count} decision "
            f"bars ({history.planted_exposure / count:.1%})"
        )
        drift = history.unconditional_drift
        print(
            f"unconditional drift: {drift:+.3g} per decision bar "
            f"({drift / manifest.world.price.vol:+.3f} of a bar's vol): what a long position "
            f"held on every bar earns without reading the rule"
        )
    space = manifest.search_space
    print(f"pool: {len(space.candidates)} candidate(s) ({space.enumerated} enumerated)")
    print(f"manifest: {target / MANIFEST_FILENAME}")
    return 0


def _cmd_oracle(args: argparse.Namespace) -> int:
    for line in preview_oracle(Path(args.run)).describe():
        print(line)
    return 0


def _cmd_search(args: argparse.Namespace) -> int:
    run_dir = Path(args.run)
    # Reported as it goes: a search is minutes of indicator walks, and the
    # arm that finished is worth seeing before the next one starts.
    run_world(run_dir, on_line=print)
    print(f"verdicts: {run_dir / VERDICTS_FILENAME}")
    print(f"oracle: {run_dir / ORACLE_FILENAME}")
    return 0


def _root_and_runs(args: argparse.Namespace) -> tuple[Path, list[Path]]:
    """The bench root a table command walks, and the directories in it."""
    root = Path(args.root) if args.root else default_root()
    if not root.is_dir():
        raise BenchError(f"{root} is not a directory")
    return root, sorted(p for p in root.iterdir() if p.is_dir())


def _cmd_calibrate(args: argparse.Namespace) -> int:
    root, run_dirs = _root_and_runs(args)
    rows = calibration_table(run_dirs)
    if not rows:
        print(f"no searched world under {root}")
        return 0
    for row in rows:
        print(row.describe())
    return 0


def _cmd_score(args: argparse.Namespace) -> int:
    run_dir = Path(args.run)
    score_world(run_dir, on_line=print)
    print(f"scores: {run_dir / SCORES_FILENAME}")
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    root, run_dirs = _root_and_runs(args)
    rows = report_table(run_dirs)
    if not rows:
        print(f"no searched world under {root}")
        return 0
    for row in rows:
        for line in row.describe(unseal=args.unseal):
            print(line)
    return 0


_COMMANDS = {
    "generate": _cmd_generate,
    "oracle": _cmd_oracle,
    "search": _cmd_search,
    "calibrate": _cmd_calibrate,
    "score": _cmd_score,
    "report": _cmd_report,
}


def main(argv: list[str] | None = None) -> int:
    # WARNING, not INFO: the backfill logs a line per walk, and over a
    # synthetic port that is the same fact ``render_fetch`` prints below.
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    args = _build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    try:
        return _COMMANDS[args.command](args)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except (
        ManifestError,
        WorldError,
        SplitError,
        StoreError,
        EvaluationError,
        FeatureError,
        RecordError,
        OSError,
    ) as exc:
        # The families a well-formed invocation can still meet, each
        # carrying a sentence written for an operator: the manifest's, the
        # generator's and the split's refusals (listed by class, not as
        # ``ValueError`` — a bare one is a defect and propagates), the
        # store that cannot be opened, the research package's two named
        # measurement refusals, and this package's own records' refusals
        # (one base, ``RecordError``, so any other ``RuntimeError`` — the
        # ledger's, say — propagates as the defect it is). ``OSError`` is
        # on the list, unlike in the research package's CLI, because a
        # model's network errors become a failed seam before they reach
        # here: an ``OSError`` can only be the manifest or the run directory.
        print(f"error: {exc}", file=sys.stderr)
        return 1
