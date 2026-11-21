"""``python -m contrib.discovery`` — the benchmark's commands.

Six in the order a world goes through them (plan §3):

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
  is the one arm that costs money. Where that arm is the one with tools,
  what its searches spent is written beside the verdicts
  (``tool_use.json``), and a failure of the look-ups' server stops the run
  with a refusal that names what was kept.
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
  A tool arm's rows also say what its searches spent; ``--price-in`` and
  ``--price-out`` (US dollars a million tokens, given together) add an
  estimated cost.

And one beside that order, for a person (tools plan §3.3):

- ``tools --run DIR`` — the training-window look-ups of a landed world
  served over MCP on this process's stdin and stdout, to try the tools by
  hand from an MCP client. The manifest is read here for the coin, the
  split, the costs and the indicator window; the server is a child
  started on those and a throw-away copy of the artifact
  (:mod:`.tools_server`), so the process that answers was never handed
  the manifest. Development seeds only. Its stdout is the protocol's:
  what this command says, it says on stderr.

What the commands print is for the operator: the world's kind and rule
are in the manifest, which a model never sees (plan §1 #7). Nothing
printed here reaches a prompt.

Exit codes, as the research package's CLI has them: ``0`` done; ``1`` a
named refusal — a manifest this build cannot read or run, a world the
generator does not make, a landed history an arm could not measure on, a
run directory searched before or not landed, a record a later build
cannot read, a store that cannot be opened, a path that cannot be read or
written — with the sentence on stderr; ``2`` argparse's usage errors;
``3`` a search stopped on its tool arm's side — the look-ups' server, the
arm's own files, an arm that could not be opened, or a trace that could
not be read back as the search it was — with what it left kept to be
looked into (:class:`~.bench.KeptForInquiry`): a code of its own, so that a batch
over many worlds can stop on it and skip a world that is merely searched
already; ``130`` interrupted, with a search's staging removed — unless its
records were already in, when the staging holding the stores ``arms/`` did
not receive is kept and the next ``search`` names it, or the interrupt
landed while a search that had stopped on its tool arm's side was marking
what it keeps, when the staging and the claim are kept (and marked as for
``3``, if the interrupt came after the staging was renamed).
"""

from __future__ import annotations

import argparse
import logging
import math
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

from .bench import (
    ORACLE_FILENAME,
    VERDICTS_FILENAME,
    BenchError,
    KeptForInquiry,
    calibration_table,
    model_hypothesist,
    preview_oracle,
    run_world,
    tool_arm,
    tools_experiment,
)
from .landing import ARTIFACT_FILENAME, MANIFEST_FILENAME, default_root, land_world
from .manifest import Manifest, ManifestError
from .records import RecordError
from .scoring import SCORES_FILENAME, report_table, require_this_scorer, score_world
from .tools_server import IMPORT_ROOT, LaunchError, ServerParams, require_sdk
from .tooluse import TOOL_USE_FILENAME, Prices
from .upstream import (
    EvaluationError,
    FeatureError,
    SplitError,
    StoreError,
    accept_any_text_on_output,
    render_fetch,
)
from .worlds import WorldError, generate

__all__ = ["EXIT_KEPT_FOR_INQUIRY", "main"]

# What ``search`` exits with when it stopped on its tool arm's side and kept
# what it left (the module docstring's exit codes).
EXIT_KEPT_FOR_INQUIRY = 3


def _price(text: str) -> float:
    """A price off the command line: a finite number of US dollars, at or above zero.

    Refused here, as the flag is read, so the refusal names the flag: a
    usage error like any other argument that is not what its flag takes.
    """
    try:
        value = float(text)
    except ValueError:
        value = math.nan
    if not math.isfinite(value) or value < 0:
        raise argparse.ArgumentTypeError(
            f"expected a number of US dollars at or above zero, got {text!r}"
        )
    return value


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
        "--manifest",
        required=True,
        help="path to the world's manifest (JSON, version 4; version 3 is still read)",
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
    for flag, what in (("--price-in", "input"), ("--price-out", "output")):
        report_cmd.add_argument(
            flag,
            type=_price,
            default=None,
            metavar="USD",
            help=(
                f"what a million {what} tokens of the tool arm's model cost, in US dollars; "
                f"given with the other, the tool arm's rows print an estimated cost beside "
                f"their token counts"
            ),
        )

    tools_cmd = subparsers.add_parser(
        "tools",
        help=(
            "serve a landed world's training-window look-ups over MCP on stdin and stdout "
            "(development seeds)"
        ),
    )
    tools_cmd.add_argument(
        "--run", required=True, help="a landed run directory (manifest.json and world.sqlite)"
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
    if manifest.arms.llm_tools is not None:
        # And the tool arm's: its model, the SDK its client is built on,
        # and a pack version that is this build's.
        tool_arm(manifest.arms.llm_tools)
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
    if (run_dir / TOOL_USE_FILENAME).is_file():
        print(f"tool use: {run_dir / TOOL_USE_FILENAME}")
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
    # The prices first: a table is not printed and then found to be
    # missing half of what it was asked to say.
    if (args.price_in is None) != (args.price_out is None):
        raise BenchError(
            "--price-in and --price-out are given together: a cost is estimated from both"
        )
    prices = None if args.price_in is None else Prices(input=args.price_in, output=args.price_out)
    root, run_dirs = _root_and_runs(args)
    rows = report_table(run_dirs)
    if not rows:
        print(f"no searched world under {root}")
        return 0
    for row in rows:
        for line in row.describe(unseal=args.unseal, prices=prices):
            print(line)
    if prices is not None and not any(row.uses for row in rows):
        # On stderr, beside the table and not in it: prices that priced
        # nothing are worth a word, and are no row.
        print(
            f"note: no row under {root} is a tool arm's, so --price-in and --price-out priced "
            f"nothing",
            file=sys.stderr,
        )
    return 0


def _cmd_tools(args: argparse.Namespace) -> int:
    run_dir = Path(args.run)
    experiment = tools_experiment(run_dir)
    # Asked here as the child will ask it, so a missing SDK is said before
    # the artifact is copied for a server that cannot be built.
    require_sdk()
    # A copy, not the artifact: opening a store migrates one an older build
    # wrote, and the artifact's digest is what the directory is held to.
    # Cleanup errors are let go: an interrupted child may still hold the
    # file for a moment on Windows, and the copy is in the system's temp.
    # (A parent KILLED while it serves cleans nothing up: the copy stays in
    # the temp directory, and the child serves on until its stdin closes.)
    # Ctrl-Break is read as an interrupt from before the copy is made, so
    # that it, too, takes the copy with it.
    with (
        _break_as_interrupt(),
        tempfile.TemporaryDirectory(prefix="discovery-tools-", ignore_cleanup_errors=True) as scratch,
    ):
        store = Path(scratch) / ARTIFACT_FILENAME
        shutil.copyfile(run_dir / ARTIFACT_FILENAME, store)
        params = ServerParams.for_experiment(store, experiment)
        # "Starting", not "serving": the child has yet to read the store and
        # warm its frame, and says so itself if it cannot.
        print(
            f"starting a server for {experiment.coin}'s training window on stdin and stdout; "
            f"close stdin or interrupt to stop",
            file=sys.stderr,
        )
        return _serve_child(params)


def _serve_child(params: ServerParams) -> int:
    """Run the server as a child on this process's own stdin and stdout; its exit code is this command's.

    A child ended by a signal reports minus the signal's number (POSIX);
    as an exit code that is 128 plus it, which is how a shell would say it.
    """
    code = subprocess.run(params.command(), cwd=IMPORT_ROOT, check=False).returncode
    return 128 - code if code < 0 else code


@contextmanager
def _break_as_interrupt() -> Iterator[None]:
    """Read Windows' Ctrl-Break as the interrupt Ctrl-C is, while the ``tools`` command holds its copy.

    Left alone it ends this process without unwinding, and the copy of the
    artifact the child reads stays in the temp directory. As an interrupt
    it ends the command the way Ctrl-C does: the copy is removed and the
    exit code is 130. The child handles its own.
    """
    if not hasattr(signal, "SIGBREAK") or threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = signal.signal(signal.SIGBREAK, signal.default_int_handler)
    try:
        yield
    finally:
        # ``None`` is a handler that was not installed from Python, which
        # cannot be put back from here: the default is.
        signal.signal(signal.SIGBREAK, signal.SIG_DFL if previous is None else previous)


_COMMANDS = {
    "generate": _cmd_generate,
    "oracle": _cmd_oracle,
    "search": _cmd_search,
    "calibrate": _cmd_calibrate,
    "score": _cmd_score,
    "report": _cmd_report,
    "tools": _cmd_tools,
}


def main(argv: list[str] | None = None) -> int:
    # First thing: with output redirected on Windows, a line holding an emoji
    # (a path with one in its name is enough) raised from ``print`` — a
    # traceback here, since nothing below names it. The autoresearch command
    # line was shown to stop a search the same way (issue #430, with a
    # scripted model). From here on the two standard streams can print any
    # text (``contrib/core/console``).
    accept_any_text_on_output()
    # WARNING, not INFO: the backfill logs a line per walk, and over a
    # synthetic port that is the same fact ``render_fetch`` prints below.
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    args = _build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    try:
        return _COMMANDS[args.command](args)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except KeptForInquiry as exc:
        # Its own code (decided 2026-11-19): the one refusal a batch over
        # many worlds must not run past, where "searched before" (1) is one
        # it skips. The next world in line would meet the same server.
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_KEPT_FOR_INQUIRY
    except (
        ManifestError,
        WorldError,
        SplitError,
        StoreError,
        EvaluationError,
        FeatureError,
        RecordError,
        LaunchError,
        OSError,
    ) as exc:
        # ``LaunchError``: the ``tools`` command's server cannot be started
        # (the SDK it is built on is not installed).
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
        # here: an ``OSError`` can only be the manifest or the run directory
        # (the tool arm's own files are the bench's to answer for: it stops
        # the run on one, as on its server, and keeps what was written).
        print(f"error: {exc}", file=sys.stderr)
        return 1
