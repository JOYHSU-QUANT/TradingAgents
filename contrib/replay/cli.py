"""``python -m contrib.replay`` — the offline exam's commands.

One command so far:

- ``score --db paper_trading.db --run-id paper-BTC-7`` — the scorecard
  (plan PR 1): read the run's decisions, mark each against the price that
  followed, print the summary, and with ``--out DIR`` write one CSV row per
  decision beside a copy of the summary. ``--research-db`` fills a missing
  cycle's later mark from the research store; ``--payload-root`` says where
  the run's payloads are when the store was copied away from its host, so
  the questions that already carry a ``.reports.json`` can be counted;
  ``--holdout`` scores the holdout segment too, and says so loudly.

The past-papers command (``replay``, plan PR 2) and the ``--replay-db``
reading of ``score`` arrive with the store they read; the records they will
score through are already this package's (:mod:`.score`).

Exit codes, kept in step with the two neighbouring packages' CLIs: ``0`` the
command did what it says, ``1`` a named operator, store, config, split or
scoring failure (the sentence on stderr says which), ``2`` argparse's own
usage errors, ``130`` interrupted.
"""

from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from collections.abc import Mapping
from pathlib import Path

from .paper_store import load_decisions, load_research_closes, run_facts
from .score import ScoreError, build_split, csv_table, score_run
from .upstream import (
    Database,
    ResearchStore,
    SchemaVersionError,
    SplitError,
    StoreError,
    payload_dir,
)

__all__ = ["main"]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m contrib.replay",
        description="The paper trader's offline exam: score its recorded decisions.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    score = subparsers.add_parser(
        "score",
        help="mark one paper run's decisions against the price that followed",
        description=(
            "Read one paper run's decision attempts (each cycle's final input and its "
            "output), mark every decision at the next close and six closes on, and print "
            "the scorecard: hit rates by mode, "
            "net P&L under the run's own fill model, fail-closed / clamp / flip rates, "
            "confidence calibration, and three baselines. Never writes to the store."
        ),
    )
    score.add_argument("--db", default="paper_trading.db", help="the paper store (SQLite path)")
    score.add_argument("--run-id", required=True)
    score.add_argument(
        "--research-db",
        metavar="PATH",
        help=(
            "an existing autoresearch.sqlite whose candle closes fill the later mark of a "
            "cycle the run has no row for (api_failed, a restart); the CSV says which "
            "marks came from it"
        ),
    )
    score.add_argument(
        "--payload-root",
        metavar="DIR",
        help=(
            "the run's payload directory, for a store copied away from its host; each "
            "question is then checked for a .reports.json beside its payload by file "
            "name. Without it the daemon's own layout beside the store is used when it "
            "exists, and the count is skipped otherwise."
        ),
    )
    score.add_argument(
        "--out",
        metavar="DIR",
        help="write <run-id>-decisions.csv (one row per decision) and <run-id>-summary.txt here",
    )
    score.add_argument(
        "--holdout",
        action="store_true",
        help=(
            "score the holdout segment too. Off by default: the split is the research "
            "package's 60/20/20, holdout newest, and the holdout is what a variant is "
            "judged on ONCE (plan section 5): every look at it is a look spent."
        ),
    )
    score.set_defaults(func=_cmd_score)
    return parser


def _fail(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 1


def _cmd_score(args: argparse.Namespace) -> int:
    db_path = Path(args.db)
    if not db_path.is_file():
        return _fail(f"database {args.db!r} does not exist")
    reports_root: Path | None = None
    if args.payload_root is not None:
        if not args.payload_root:
            return _fail("--payload-root needs a directory, got ''")
        reports_root = Path(args.payload_root)
        if not reports_root.is_dir():
            return _fail(f"--payload-root {args.payload_root!r} is not a directory")
    research_path: Path | None = None
    if args.research_db is not None:
        research_path = Path(args.research_db)
        if not research_path.is_file():
            # Checked before the store is opened: opening a path that does not
            # exist would CREATE an empty research store there.
            return _fail(f"--research-db {args.research_db!r} does not exist")
    try:
        db = Database(db_path, migrate=False)
    except SchemaVersionError as exc:
        return _fail(str(exc))
    with db:
        facts = run_facts(db, args.run_id)
        if facts is None:
            return _fail(f"run {args.run_id!r} not found in {args.db}")
        if facts.mode != "paper":
            return _fail(
                f"run {args.run_id!r} is a {facts.mode} run; the scorecard reads paper runs "
                "(their fill model is what the costs are taken from)"
            )
        if reports_root is None:
            beside = payload_dir(db_path, args.run_id)
            reports_root = beside if beside.is_dir() else None
        decisions = load_decisions(db, args.run_id, reports_root=reports_root)
    questions, answers = decisions.questions, decisions.answers
    if not questions:
        return _fail(f"run {args.run_id!r} has no decision attempt with an input row to score")
    try:
        split = build_split(questions, interval=facts.interval, step_ms=facts.step_ms)
    except SplitError as exc:
        return _fail(
            f"run {args.run_id!r} is too short to cut into train / validation / holdout "
            f"({exc}); the scorecard needs at least three {facts.interval} bars"
        )
    research: Mapping[int, float] = {}
    if research_path is not None:
        try:
            with ResearchStore(research_path) as store:
                # Only the bars the split can ever pair: the lock at the I/O seam.
                research = load_research_closes(
                    store,
                    coin=facts.coin,
                    interval=facts.interval,
                    step_ms=facts.step_ms,
                    since_ms=split.train.start_ms,
                    until_ms=split.loadable_until(holdout=True),
                )
        except StoreError as exc:
            return _fail(str(exc))
        if not research:
            print(
                f"warning: --research-db {args.research_db} holds no {facts.coin} "
                f"{facts.interval} candles; no missing cycle can be filled from it",
                file=sys.stderr,
            )
    card = score_run(
        questions,
        answers,
        step_ms=facts.step_ms,
        costs=facts.costs,
        research_closes=research,
        split=split,
        holdout=args.holdout,
    )
    source = "the run's recorded config" if facts.config_recorded else "defaults, no config_json"
    lines = [
        f"scorecard: run {facts.run_id} ({facts.coin}, {facts.interval} cycle; costs from {source})",
        *decisions.describe(),
        *card.summary().describe(card),
    ]
    for line in lines:
        print(line)
    if args.out is not None:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        header, rows = csv_table(card)
        decisions_csv = out / f"{facts.run_id}-decisions.csv"
        with decisions_csv.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(header)
            writer.writerows(rows)
        summary = out / f"{facts.run_id}-summary.txt"
        summary.write_text("\n".join(lines) + "\n", encoding="utf-8")
        for path in (decisions_csv, summary):
            print(f"wrote {path}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        return 130
    except ScoreError as exc:
        return _fail(str(exc))
    except sqlite3.Error as exc:
        return _fail(f"store read failed: {exc}")
