"""The offline subcommands over an existing store: ``export`` / ``validate``.

Report-only surfaces (phase2-data §1.1 / phase2-spec §5 / phase3-spec §20.3):
they take no run lease and never migrate the store (see
:func:`._common._open_existing_db` for why). The one write either makes is
``export --backfill-format-fingerprint``, which fills ``NULL``
``ai_inputs.format_fingerprint`` cells from the payload files a run wrote
before schema v11 (:mod:`..persistence.backfill`) — cells the daemon never
touches again, so no lease is needed for it either.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from ..persistence.db import Database
from ._common import _existing_run_row, _open_existing_db


def _cmd_export(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m contrib.hyperliquid_perp export",
        description="Export one run's full dataset as the eight phase2-data CSVs.",
    )
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--db", default="paper_trading.db", help="SQLite store path.")
    parser.add_argument(
        "--backfill-format-fingerprint",
        action="store_true",
        help=(
            "Before exporting, fill the run's NULL ai_inputs.format_fingerprint "
            "cells (rows written before schema v11) from the format text each "
            "row's payload JSON recorded; rows the pass cannot prove stay NULL "
            "and are counted on stderr. The one write this command can make; "
            "rules in RUNBOOK §6."
        ),
    )
    parser.add_argument(
        "--payload-root",
        metavar="DIR",
        help=(
            "With --backfill-format-fingerprint, on a store copied away from "
            "the host that wrote it: read each row's payload by its recorded "
            "file name under DIR instead of at the absolute path the daemon "
            "recorded (which would count every row as missing_payload). The "
            "files must still hash to the rows' input_payload_hash."
        ),
    )
    args = parser.parse_args(argv)
    payload_root: Path | None = None
    if args.payload_root is not None:
        if not args.backfill_format_fingerprint:
            parser.error("--payload-root only applies with --backfill-format-fingerprint")
        if not args.payload_root:
            # ``Path("")`` is the working directory and would pass the check
            # below — an unset shell variable must not quietly become cwd.
            parser.error("--payload-root needs a directory, got ''")
        payload_root = Path(args.payload_root)
        if not payload_root.is_dir():
            # Named here rather than reported as N x missing_payload: a typo in
            # the root would otherwise read exactly like a store whose
            # payloads are really gone.
            print(f"error: --payload-root {args.payload_root!r} is not a directory", file=sys.stderr)
            return 1

    from ..persistence.export import ExportError, export_run

    db = _open_existing_db(args.db)
    if db is None:
        return 1
    with db:
        if args.backfill_format_fingerprint:
            from ..persistence import repository as repo
            from ..persistence.backfill import backfill_format_fingerprints

            # Skipped silently over an unknown run: export_run below refuses
            # it in the one ``export_failed`` wording this command has always
            # used, and a "stamped=0" line about a typo would only compete
            # with it.
            try:
                known = repo.get_run(db.conn, args.run_id) is not None
                report = (
                    backfill_format_fingerprints(db, run_id=args.run_id, payload_root=payload_root)
                    if known
                    else None
                )
            except sqlite3.Error as exc:
                # The pass takes the store's write lock (RUNBOOK §6 says a
                # running daemon is fine): a lock held past busy_timeout is a
                # named refusal here, not a traceback exit 2.
                print(f"error: format_fingerprint backfill failed — {exc}", file=sys.stderr)
                return 1
            if report is not None:
                print(report.summary(args.run_id), file=sys.stderr)
                if (
                    report.stamped == 0
                    and report.missing_payload
                    and not (report.unreadable or report.unverified)
                ):
                    # Every payload the pass looked for was absent (pre_v10
                    # rows aside): far likelier a store away from its host, or
                    # a root at the wrong level, than a tree that lost its
                    # files. The daemons write ``<db dir>/payloads/<run_id>/
                    # <coin>-<stamp>.json`` (cli/paper.py, cli/live.py,
                    # cli/smoke.py), so the candidate beside THIS store is
                    # named — a copy that moved the db and its payloads
                    # together lands there. Only when the counts make the
                    # sentence true: a root under which some name matched
                    # (unverified / unreadable > 0) gets no hint.
                    candidate = Path(args.db).resolve().parent / "payloads" / args.run_id
                    if payload_root is None:
                        print(
                            "hint: every payload is missing at its recorded path; a store "
                            "copied off the host that wrote it needs --payload-root pointing "
                            f"at that run's payload directory (here: {candidate})",
                            file=sys.stderr,
                        )
                    else:
                        print(
                            f"hint: no payload under --payload-root {args.payload_root!r} "
                            "matched a recorded file name; the daemon writes them under "
                            f"<db dir>/payloads/{args.run_id}/ (here: {candidate}) — point "
                            "at that run's own directory",
                            file=sys.stderr,
                        )
        try:
            paths = export_run(db, run_id=args.run_id, output_dir=args.output_dir)
        except ExportError as exc:
            print(f"error: export_failed — {exc}", file=sys.stderr)
            return 1
    for path in paths:
        print(path)
    return 0


def _cmd_validate(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m contrib.hyperliquid_perp validate",
        description=(
            "Acceptance report + verdict. A PAPER run gets the phase2-spec §5 "
            "report (Phase-3 verdict); a LIVE run gets the phase3-spec §20.3 / "
            "§21.4 acceptance report (the profile follows the run's live.mode). "
            "The run's stored mode selects the report — point --db at the right "
            "store (live runs default to live_trading.db)."
        ),
    )
    parser.add_argument("--run-id", required=True)
    parser.add_argument(
        "--db",
        default="paper_trading.db",
        help="SQLite store path (a live run's store is usually live_trading.db).",
    )
    args = parser.parse_args(argv)

    try:
        db = _open_existing_db(args.db)
        if db is None:
            return 1
        with db:
            run_row = _existing_run_row(db.conn, args.run_id, args.db)
            if run_row is None:
                return 1
            if run_row["mode"] == "live":
                return _validate_live(db, args.run_id)
            from ..paper.validation import validate_run

            report = validate_run(db, run_id=args.run_id)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except sqlite3.Error as exc:
        # The store cannot even be read (malformed file, I/O failure mid-scan):
        # the strongest possible "investigate the store" signal — the same
        # exit-5 verdict as a failing report, not a generic tool crash.
        print(f"error: store integrity failure — {exc}", file=sys.stderr)
        return 5
    for line in report.summary_lines():
        print(line)
    if report.phase3_ready:
        return 0
    # Integrity failures and a merely-short run are different operator actions
    # (investigate vs keep running) — give them distinct codes.
    return 5 if report.failures else 4


def _validate_live(db: Database, run_id: str) -> int:
    """The live branch of ``validate`` (§20.3 / §21.4), reusing the 0/4/5 codes.

    Same exit contract as the paper report: 0 = acceptance passed; 5 = an
    integrity failure (dedupe error, orphan, position/replay mismatch, an
    unprotected window, a low kill-switch refresh rate, or — mainnet_tiny — an
    unresolved reconciliation case / breached daily-loss cap); 4 = internally
    consistent but short of the gate (< 30 cycles / orders, smoke tests not yet
    run, a failed/errored smoke test — curable by a ``live-smoke --only``
    re-run, so it is a shortfall, not an integrity verdict; decision
    2026-07-29 — or a recent run of cycles that all reached no decision, issue
    #50). Called inside the caller's ``with db:`` block.
    """
    from ..live.validation import validate_live_run

    report = validate_live_run(db, run_id=run_id)
    for line in report.summary_lines():
        print(line)
    if report.live_ready:
        return 0
    return 5 if report.failures else 4
