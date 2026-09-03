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
    args = parser.parse_args(argv)

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
                    backfill_format_fingerprints(db, run_id=args.run_id) if known else None
                )
            except sqlite3.Error as exc:
                # The pass takes the store's write lock (RUNBOOK §6 says a
                # running daemon is fine): a lock held past busy_timeout is a
                # named refusal here, not a traceback exit 2.
                print(f"error: format_fingerprint backfill failed — {exc}", file=sys.stderr)
                return 1
            if report is not None:
                print(report.summary(args.run_id), file=sys.stderr)
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
