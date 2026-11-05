"""``python -m contrib.discovery`` — the benchmark's commands.

One so far:

- ``generate --manifest world.json [--root DIR]`` — read a manifest, build
  its world from the seed, land it in ``<root>/<world id>-<manifest
  digest>/world.sqlite`` through the research package's own backfill,
  check the landed history the way an experiment would (plan PR 2's
  acceptance list), and write the canonical ``manifest.json`` beside it
  (:mod:`.landing` owns the layout and the staged landing). The artifact
  is read-only from then on: an arm copies it (plan §2), and a second
  ``generate`` of a manifest already landed is refused rather than
  overwritten.

What ``generate`` prints is for the operator: the world's kind and rule
are in the manifest, which a model never sees (plan §1 #7). Nothing this
command prints reaches a prompt.

Exit codes, as the research package's CLI has them: ``0`` done; ``1`` a
named refusal — a manifest this build cannot read or run, a world the
generator does not make, a landed history an arm could not measure on, a
store that cannot be opened, a path that cannot be read or written — with
the sentence on stderr; ``2`` argparse's usage errors; ``130`` interrupted.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .landing import ARTIFACT_FILENAME, MANIFEST_FILENAME, default_root, land_world
from .manifest import Manifest, ManifestError
from .upstream import EvaluationError, FeatureError, SplitError, StoreError, render_fetch
from .worlds import WorldError, generate

__all__ = ["main"]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m contrib.discovery",
        description="Discovery Lab: synthetic worlds the research loop is examined on.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    generate_cmd = subparsers.add_parser(
        "generate",
        help="build a world from its manifest, land it in its own store, and check it",
    )
    generate_cmd.add_argument(
        "--manifest", required=True, help="path to the world's manifest (JSON, version 1)"
    )
    # argparse %-formats help text, so a '%' in the repository's path is doubled.
    shown_root = str(default_root()).replace("%", "%%")
    generate_cmd.add_argument(
        "--root",
        default=None,
        help=f"the bench root; the run lands in <root>/<world id>-<digest>/ (default: {shown_root})",
    )
    return parser


def _read_manifest(path: Path) -> Manifest:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ManifestError(f"cannot read the manifest at {path}: {exc}") from exc
    return Manifest.loads(text)


def _cmd_generate(args: argparse.Namespace) -> int:
    manifest = _read_manifest(Path(args.manifest))
    root = Path(args.root) if args.root else default_root()
    # The world is built before anything is written, so a manifest the
    # generator refuses leaves nothing behind.
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
    print(f"manifest: {target / MANIFEST_FILENAME}")
    return 0


_COMMANDS = {"generate": _cmd_generate}


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
        OSError,
    ) as exc:
        # The families a well-formed invocation can still meet, each
        # carrying a sentence written for an operator: the manifest's, the
        # generator's and the split's refusals (listed by class, not as
        # ``ValueError`` — a bare one is a defect and propagates), the
        # store that cannot be opened and the research package's two named
        # measurement refusals (likewise listed, so any other
        # ``RuntimeError`` propagates as the defect it is). ``OSError`` is
        # on the list, unlike in the research package's CLI, because nothing
        # here speaks to a network: an ``OSError`` can only be the manifest
        # or the run directory.
        print(f"error: {exc}", file=sys.stderr)
        return 1
