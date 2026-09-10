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

from ..common import store_layout
from ..persistence.db import Database
from ._common import _existing_run_row, _open_existing_db


def _every_payload_missing(report) -> bool:
    """The pass found NOTHING at the paths it tried (pre_v10 rows aside).

    ``report`` is a ``persistence.backfill.FingerprintBackfill``. Far likelier
    a store away from its host, or a root at the wrong level, than a tree
    that lost every file — so this is what makes the reader try the
    directory beside the store, and what makes a hint true. A pass under
    which SOME name matched (``unreadable`` / ``unverified`` > 0) is neither:
    the files are where it looked, and the counts already say what is wrong
    with them.
    """
    return (
        report.stamped == 0
        and bool(report.missing_payload)
        and not (report.unreadable or report.unverified)
    )


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
            "the host that wrote it WITHOUT its payload directory beside it: "
            "read each row's payload by its recorded file name under DIR "
            "instead of at the absolute path the daemon recorded (which would "
            "count every row as missing_payload). A copy that kept the "
            f"daemon's <db dir>/{store_layout.PAYLOADS_DIRNAME}/<run-id>/ layout "
            "next to the db is read from there without this flag. The files "
            "must still hash to the rows' input_payload_hash."
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

            # The layout the daemons write (cli/paper.py, cli/live.py,
            # cli/smoke.py, all through common.store_layout): this run's
            # payloads beside THIS store. A copy that moved the db and its
            # payloads together lands there, so it is the reader's second try
            # without a flag, and the directory every hint below names.
            beside = store_layout.payload_dir(args.db, args.run_id)
            retried_beside = False

            def _pass(root: Path | None):
                report = backfill_format_fingerprints(db, run_id=args.run_id, payload_root=root)
                print(report.summary(args.run_id), file=sys.stderr)
                return report

            # Skipped silently over an unknown run: export_run below refuses
            # it in the one ``export_failed`` wording this command has always
            # used, and a "stamped=0" line about a typo would only compete
            # with it.
            try:
                known = repo.get_run(db.conn, args.run_id) is not None
                report = _pass(payload_root) if known else None
                if (
                    report is not None
                    and payload_root is None
                    and _every_payload_missing(report)
                    and beside.is_dir()
                ):
                    # Nothing at the recorded paths, and a directory in the
                    # daemon's layout beside the store: read there before
                    # asking for --payload-root. Safe to run the pass twice —
                    # the first stamped nothing, and only NULL cells are ever
                    # written, so the second sees the same rows. Said on
                    # stderr, so a stamped line after a missing_payload line
                    # is not read as a contradiction.
                    retried_beside = True
                    print(
                        "note: every payload is missing at its recorded path; reading "
                        f"them under the directory beside this store instead ({beside})",
                        file=sys.stderr,
                    )
                    report = _pass(beside)
            except sqlite3.Error as exc:
                # The pass takes the store's write lock (RUNBOOK §6 says a
                # running daemon is fine): a lock held past busy_timeout is a
                # named refusal here, not a traceback exit 2.
                print(f"error: format_fingerprint backfill failed — {exc}", file=sys.stderr)
                return 1
            if report is not None and _every_payload_missing(report):
                # Only when the counts make the sentence true (see the
                # predicate): which sentence depends on where the pass looked.
                layout = f"<db dir>/{store_layout.PAYLOADS_DIRNAME}/"
                if args.payload_root is not None:
                    print(
                        f"hint: no payload under --payload-root {args.payload_root!r} "
                        "matched a recorded file name; the daemon writes them under "
                        f"{layout}{args.run_id}/ (here: {beside}) — point at that run's "
                        "own directory",
                        file=sys.stderr,
                    )
                elif retried_beside:
                    print(
                        f"hint: no payload beside this store ({beside}) matched a recorded "
                        f"file name either; a copy that did not keep the daemon's "
                        f"{layout}<run-id>/ layout needs --payload-root pointing at that "
                        "run's payload directory",
                        file=sys.stderr,
                    )
                else:
                    # ``is_dir()`` is false for a missing path AND for a file
                    # of that name, so "no payload directory" is the sentence
                    # that is true in both cases.
                    print(
                        "hint: every payload is missing at its recorded path, and there "
                        f"is no payload directory beside this store at {beside}; a store "
                        "copied off the host that wrote it needs --payload-root pointing "
                        "at that run's payload directory",
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
        # The store opened but its CONTENT does not hold up: a malformed file,
        # an I/O failure mid-scan. The strongest possible "investigate the
        # store" signal — the same exit-5 verdict as a failing report, not a
        # generic tool crash. A store that could not be OPENED is a different
        # verdict and no longer arrives here: the guard in ``persistence.db``
        # names the file this build must not open (issue #210 for one it cannot
        # read, #236 for one whose content is in a log beside it), and
        # ``Database`` names the one it cannot open as a store even though it
        # reads (#235) — all of them ``SchemaVersionError``, which the open
        # above turns into a named exit 1. What is left for this handler is
        # what the open SUCCEEDED at and the scan then found.
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
