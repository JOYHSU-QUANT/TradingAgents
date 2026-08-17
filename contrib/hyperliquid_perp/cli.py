"""Subcommand CLI for the Hyperliquid perp module.

The subcommands (plus full Phase 1/2 backward compatibility):

- ``python -m contrib.hyperliquid_perp paper --coin BTC`` — the long-running
  paper run: restart reconciliation (execution §1.2), then the 30-second
  monitor/scheduler loop (rolling 4h decisions, TWAP execution, SL/TP,
  funding), with CSV export after every completed cycle and on shutdown.
- ``python -m contrib.hyperliquid_perp export --run-id <id> --output-dir <dir>``
  — manual full-dataset CSV export (phase2-data §1.1).
- ``python -m contrib.hyperliquid_perp validate --run-id <id>`` — the acceptance
  report and verdict. A PAPER run gets the phase2-spec §5 report; a LIVE run gets
  the phase3-spec §20.3 / §21.4 report (the run's stored mode selects which).
- ``python -m contrib.hyperliquid_perp live --config <yaml>`` — the Phase 3
  startup skeleton (phase3-spec PR 1): load the ``live:`` config gates, verify
  the agent-wallet authorization, print the effective notional caps, and exit
  without entering any trading loop (that is ``live --run-id --loop``, PR 5).
  Places no orders.
- ``python -m contrib.hyperliquid_perp live-smoke --run-id <id> --config <yaml>``
  — the phase3-spec §20.2 testnet smoke checklist (PR 6): drive each signed
  exchange action against testnet, record the verdicts, and report the §20.2
  cycle-entry gate. ``--gate-status`` reads the stored gate (no network);
  ``--dry-run`` validates config + wiring and places no orders.
- ``python -m contrib.hyperliquid_perp safe-mode --run-id <id> --status`` —
  live-run safe-mode inspection and the manual lane (``--release``,
  ``--stamp-case``) for §12.3/§13.5 operator actions.

Empty argv and flag-style invocations (first argument starting with ``-``) —
including the Phase 1 ``--context-only`` smoke run and the single-shot engine
run — are delegated verbatim to :mod:`.main`, whose behaviour is unchanged.
A bare first argument that is not a known subcommand is an error (exit 1):
the legacy CLI takes no positionals, so it can only be a subcommand typo.

Exit codes: ``0`` success (for ``validate``: Phase-3 ready), ``1`` named
operator/config/environment errors — including a protection-only ``paper`` run
that self-terminates after its position closes (final export written; the
books never re-verified, the API key was never supplied, or the engine
failed to import — stderr says which), ``2`` unexpected error, ``4``
not-yet-at-the-gate outcomes (``validate``: short of the 30-cycle gate or a
red/missing smoke test — curable by a ``live-smoke`` re-run;
``live-smoke``: the §20.2 gate is not satisfied, incl. a pre-flight abort;
``live --loop``: the §20.2 smoke gate is not open on this run;
``live`` without ``--loop``: recovery ran but judged unclean; ``safe-mode
--status``: a safe mode is latched, recoverable OR manual — 4 means "latched",
not "human action required"), ``5`` (``validate`` only) the
run has integrity failures — orphans, snapshot or replay mismatches, or a
store so corrupt the checks themselves cannot run ("the store is broken;
investigate before trusting results"), ``130`` interrupted before a graceful
lane could take over (Ctrl-C in ``export``/``validate``/``live``/``live-smoke``
or during ``paper`` startup/reconciliation — SIGTERM likewise once ``paper``,
``live`` or ``live-smoke`` has installed its handler; once the ``paper`` loop
runs, both signals take the shutdown-export lane instead of ``130``). For
``live-smoke`` the handler is installed with the run lease, so an interrupt
after that point still runs the suite's cleanup (sweep resting probes, close the
staged long, disarm the switch — in that order, because flattening first makes
the exchange auto-cancel the reduce-only probes) rather than stranding it.
Legacy delegated invocations keep
:mod:`.main`'s own exit contract.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from functools import partial
from pathlib import Path

from .config import CONFIG_LOAD_ERRORS, dotenv_diagnosis, load_config, load_dotenv_files
from .persistence.db import Database, SchemaVersionError, apply_migrations

logger = logging.getLogger(__name__)

_SUBCOMMANDS = ("paper", "export", "validate", "live", "live-smoke", "safe-mode")

# Version stamp for the ai_inputs.prompt_version column: bump when the injected
# context/format contract changes shape (the payload hash tracks content).
PROMPT_VERSION = "phase2-target-v3"


def _raise_keyboard_interrupt(signum, frame) -> None:
    """SIGTERM → the SIGINT path: systemd/docker/``kill`` stop with the default
    TERM signal, and phase2-data §1.1's shutdown export must fire for them
    exactly as it does for Ctrl-C."""
    raise KeyboardInterrupt


def main(argv: list[str] | None = None) -> int:
    # Before anything reads os.environ: the OPENROUTER_API_KEY startup checks
    # (fresh `paper` runs, healthy keyless-restart triage) run long before the
    # lazily-imported engine package would load the .env files itself.
    load_dotenv_files()
    argv = list(sys.argv[1:]) if argv is None else list(argv)
    if not argv or argv[0].startswith("-"):
        # Phase 1/2 compatibility path: identical flags, identical behaviour.
        # Legacy accepts no positionals, so flag-shaped/empty argv is lossless.
        from .main import main as legacy_main

        return legacy_main(argv)
    if argv[0] not in _SUBCOMMANDS:
        # A bare unknown word is almost certainly a subcommand typo; delegating
        # it would surface the legacy parser's usage (which never mentions the
        # subcommands) under exit code 2 ("unexpected error").
        print(
            f"error: unknown subcommand {argv[0]!r} (expected one of: "
            f"{', '.join(_SUBCOMMANDS)}; legacy flag invocations like "
            "--context-only pass through unchanged).",
            file=sys.stderr,
        )
        return 1
    command, rest = argv[0], argv[1:]
    try:
        if command == "export":
            return _cmd_export(rest)
        if command == "validate":
            return _cmd_validate(rest)
        if command == "live":
            return _cmd_live(rest)
        if command == "live-smoke":
            return _cmd_live_smoke(rest)
        if command == "safe-mode":
            return _cmd_safe_mode(rest)
        return _cmd_paper(rest)
    except KeyboardInterrupt:
        print("interrupted.", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 — last-resort handler, mirrors main.main
        logger.exception("unexpected error in %s", command)
        print(f"fatal: unexpected error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


# --------------------------------------------------------------------------
# export / validate — offline commands over an existing store
# --------------------------------------------------------------------------


def _open_existing_db(
    path: str, *, migrate: bool = False, defer_migration: bool = False
) -> Database | None:
    """Open an existing store, or report why not (never CREATE one implicitly).

    ``Database(path)`` would happily create an empty schema — and an offline
    command against a typo'd path would then "succeed" with zero rows.

    ``migrate`` splits the callers by what they actually do:

    * ``False`` (default) for the genuinely REPORT-ONLY commands — ``validate``,
      ``export``, ``live-smoke --gate-status``. They take no run lease, so
      migrating would silently upgrade a store a running daemon owns, leaving
      that daemon writing through a schema it does not know. They refuse with
      instructions instead (2026-07-30 migration review).
    * ``True`` for ``safe-mode``, which legitimately CHANGES the run
      (``--release`` / ``--stamp-case`` write, and ``--status`` is how an
      operator diagnoses a latched run). Refusing it would disable exactly the
      diagnostic tool an upgrade is most likely to need: the exit check found
      ``safe-mode`` blocked by the very condition it exists to investigate
      (2026-07-31). It takes no lease, so this remains a deliberate exception.
    * ``defer_migration=True`` for the real ``live-smoke`` run, which owns the
      store but cannot prove it until it holds the lease — it migrates itself
      once it does. See :class:`Database` for why that ordering matters.
    """
    if not Path(path).exists():
        print(f"error: database {path!r} does not exist.", file=sys.stderr)
        return None
    try:
        return Database(path, migrate=migrate, defer_migration=defer_migration)
    except SchemaVersionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return None


def _existing_run_row(conn, run_id: str, db_label: str, *, not_found_hint: str = ""):
    """The run's row, or ``None`` after printing the standard missing-run error.

    The one encoding of the "does this run exist" refusal, shared by every
    read-style command (``validate``, ``live-smoke --gate-status``, and
    ``live-smoke``'s real-run build) so a wording tweak cannot land on one
    surface and not the other. ``not_found_hint`` lets a caller that can offer
    a concrete remedy (e.g. "create it first with ...") append one without
    forking the base message (2026-07-30 simplify pass).
    """
    from .persistence import repository as repo

    row = repo.get_run(conn, run_id)
    if row is None:
        suffix = not_found_hint or "."
        print(f"error: run {run_id!r} does not exist in {db_label}{suffix}", file=sys.stderr)
    return row


def _require_live_run_mode(run_row, run_id: str, db_label: str, *, extra: str = "") -> bool:
    """True if ``run_row`` is a ``live``-mode run, else prints the refusal.

    Shared by every command that only makes sense against a live run
    (``live-smoke --gate-status``, ``live-smoke``'s real-run build) so the
    "wrong run mode" wording can't drift between them (2026-07-30 simplify
    pass). ``extra`` lets a caller insert a clause before the closing
    "Fix --run-id / --db." sentence.
    """
    if run_row["mode"] != "live":
        detail = f" — {extra}" if extra else ""
        print(
            f"error: run {run_id!r} in {db_label} is a {run_row['mode']} run{detail}. "
            "Fix --run-id / --db.",
            file=sys.stderr,
        )
        return False
    return True


def _cmd_export(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m contrib.hyperliquid_perp export",
        description="Export one run's full dataset as the eight phase2-data CSVs.",
    )
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--db", default="paper_trading.db", help="SQLite store path.")
    args = parser.parse_args(argv)

    from .persistence.export import ExportError, export_run

    db = _open_existing_db(args.db)
    if db is None:
        return 1
    with db:
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
            from .paper.validation import validate_run

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
    run, or a failed/errored smoke test — curable by a ``live-smoke --only``
    re-run, so it is a shortfall, not an integrity verdict; decision
    2026-07-29). Called inside the caller's ``with db:`` block.
    """
    from .live.validation import validate_live_run

    report = validate_live_run(db, run_id=run_id)
    for line in report.summary_lines():
        print(line)
    if report.live_ready:
        return 0
    return 5 if report.failures else 4


# --------------------------------------------------------------------------
# safe-mode — inspect / manually release a live run's safe mode (§13.6)
# --------------------------------------------------------------------------


def _cmd_safe_mode(argv: list[str]) -> int:
    """§13.6: the ONE manual-release interface (no config-flag release exists).

    ``--status`` (the default) prints the persisted current state and recent
    history — exit 0 when not in safe mode, 4 while one is latched, so a
    supervisor probe can branch without parsing stdout; ``--release --reason
    "<人工確認說明>"`` releases a MANUAL safe mode, writing the
    ``safe_mode_released`` audit event. Releasing does not resume trading:
    the run must still pass its next full reconciliation (§13.6 rule 3) —
    the released state only lifts the manual latch.
    """
    parser = argparse.ArgumentParser(
        prog="python -m contrib.hyperliquid_perp safe-mode",
        description="Inspect or manually release a live run's safe mode (§13.6).",
    )
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--db", default="live_trading.db", help="SQLite store path.")
    action = parser.add_mutually_exclusive_group()
    action.add_argument(
        "--status",
        action="store_true",
        help="Print the current safe-mode state, recent history and the open "
        "reconciliation cases (the default action). Exit 0 when not in safe "
        "mode, 4 while one is latched.",
    )
    action.add_argument(
        "--release",
        action="store_true",
        help="Release a MANUAL safe mode (writes the §13.6 audit event).",
    )
    action.add_argument(
        "--stamp-case",
        type=int,
        default=None,
        metavar="EVENT_ID",
        help="Record a human disposition on an open §12.3 reconciliation case "
        "(see --status for the ids). A fill_malformed case stops blocking the "
        "verdict once stamped; a fill_unmapped one is refused (it resolves by "
        "booking the fill, not by stamping). Requires --action.",
    )
    parser.add_argument(
        "--reason",
        default=None,
        help="Required with --release: the human confirmation recorded on the release event.",
    )
    parser.add_argument(
        "--action",
        default=None,
        help="Required with --stamp-case: what the human decided, recorded as "
        "the case's action_taken.",
    )
    parser.add_argument(
        "--released-by",
        default=None,
        help="Operator identity for the audit trail (default: the OS user name).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="With --status: how many recent history rows and open cases to "
        "print (default 10; 0 prints everything).",
    )
    args = parser.parse_args(argv)

    import getpass

    from .live.safe_mode import SafeModeManager
    from .persistence import repository as repo

    # migrate=True: safe-mode WRITES (--release / --stamp-case), and --status is
    # how an operator diagnoses a latched run. Refusing it on a store that needs
    # upgrading would disable the diagnostic tool at exactly the moment it is
    # needed — an upgrade that latches safe mode (2026-07-31 exit check).
    db = _open_existing_db(args.db, migrate=True)
    if db is None:
        return 1
    with db:
        run_row = repo.get_run(db.conn, args.run_id)
        if run_row is None:
            print(f"error: run {args.run_id!r} does not exist in {args.db}.", file=sys.stderr)
            return 1
        if run_row["mode"] != "live":
            # Same run-identity discipline as the live/paper resume guards: a
            # paper run has no safe-mode state, and letting the id through
            # would answer "safe_mode: none" (or a misleading release refusal)
            # instead of naming the actual mistake — a wrong --run-id / --db.
            print(
                f"error: run {args.run_id!r} in {args.db} is a "
                f"{run_row['mode']} run — safe mode is live-run state (§13.6). "
                "Fix --run-id / --db.",
                file=sys.stderr,
            )
            return 1
        # gate=None: this is the offline wiring — no live process, so the
        # persisted state IS the whole effect; the next startup hydrates it.
        manager = SafeModeManager(db=db, run_id=args.run_id, gate=None)
        state = manager.current()

        if args.stamp_case is not None:
            return _stamp_reconciliation_case(db, repo, args)

        if not args.release:
            if args.reason is not None or args.released_by is not None or args.action is not None:
                # Named rejection, not silence: these flags only mean something
                # on the release/stamp paths, and an operator who typed them
                # almost certainly meant one of those — dropping them without a
                # word would read as "release recorded".
                print(
                    "error: --reason/--released-by apply only with --release and "
                    "--action only with --stamp-case; the status action records nothing.",
                    file=sys.stderr,
                )
                return 1
            if args.limit is not None and args.limit < 0:
                print(
                    "error: --limit must be >= 0 (0 prints the full history).",
                    file=sys.stderr,
                )
                return 1
            if state is None:
                print("safe_mode: none")
            else:
                print(f"safe_mode: {state.safe_mode_type}")
                print(f"reason: {state.reason}")
                print(f"entered_at: {state.entered_at}")
            # Default 10 keeps the probe output lean; a long manual episode (one
            # reason_added row per distinct reason) can push the episode's
            # anchoring safe_mode_entered row out of a fixed tail, so the
            # operator can widen it (--limit 0 = all) without querying the DB
            # (decided 2026-07-17).
            limit = 10 if args.limit is None else args.limit
            history = repo.iter_safe_mode_events(db.conn, args.run_id)
            if history:
                rows = history if limit == 0 else history[-limit:]
                print("history (most recent last):")
                for row in rows:
                    who = f" by {row['released_by']}" if row["released_by"] else ""
                    print(
                        f"  {row['timestamp']}  {row['event_type']}"
                        f"{'' if row['safe_mode_type'] is None else ' ' + row['safe_mode_type']}"
                        f"{'' if row['reason'] is None else ': ' + row['reason']}{who}"
                    )
            # The §12.3 cases still open. Printed HERE because this is the only
            # place their event_ids surface: an open case can hold the verdict
            # unclean, and the safe-mode detail names a count, not ids.
            #
            # TWO resolution models, so two predicates — keying everything on
            # action_taken would list an unmapped sighting as stampable, and
            # stamping it changes NO verdict while hiding it from this very
            # listing (the operator's only enumeration of the backlog):
            #   - fill_unmapped  → open until the fill BOOKS (an anti-join that
            #     never reads action_taken); resolved by §8.3 re-ingest.
            #   - everything else → open until a human stamps action_taken.
            unmapped_open = repo.iter_unresolved_fill_sightings(db.conn, args.run_id)
            unmapped_ids = {row["event_id"] for row in unmapped_open}
            open_cases = [
                row
                for row in repo.iter_exchange_reconciliation_events(db.conn, args.run_id)
                if (
                    row["event_id"] in unmapped_ids
                    if row["case_type"] == "fill_unmapped"
                    else row["action_taken"] is None
                )
            ]
            if open_cases:
                shown = open_cases if limit == 0 else open_cases[-limit:]
                print(
                    f"open reconciliation cases ({len(open_cases)}; stamp the "
                    'human-disposable ones with --stamp-case <event_id> --action "<disposition>"):'
                )
                for row in shown:
                    # Name the resolution path per row: an operator following a
                    # blanket "stamp these" instruction onto an unmapped sighting
                    # would believe they had cleared a block they had not.
                    how = (
                        " — resolved by booking the fill (§8.3 re-ingest), not by stamping"
                        if row["case_type"] == "fill_unmapped"
                        else ""
                    )
                    print(
                        f"  [{row['event_id']}] {row['timestamp']}  {row['case_type']}"
                        f"{'' if row['symbol'] is None else ' ' + row['symbol']}"
                        f"{'' if row['exchange_value'] is None else ' ' + row['exchange_value']}"
                        f"{how}"
                    )
            # Exit 4 while a safe mode is latched (0 = none): a supervisor
            # probe can branch without parsing stdout — the same multi-code
            # convention as validate's 4/5 and live's 0/4/1 (decided
            # 2026-07-17).
            return 0 if state is None else 4

        if args.limit is not None:
            # Same named-rejection discipline as --reason on the status path:
            # the flag only means something when history is printed.
            print(
                "error: --limit applies only with --status; the release action prints no history.",
                file=sys.stderr,
            )
            return 1
        if args.action is not None:
            print(
                "error: --action applies only with --stamp-case; a release records "
                "its human confirmation via --reason.",
                file=sys.stderr,
            )
            return 1
        if args.reason is None or not args.reason.strip():
            print(
                'error: --release requires --reason "<人工確認說明>" — the release '
                "event must record why a human decided the state is safe (§13.6 rule 2).",
                file=sys.stderr,
            )
            return 1
        # A RUNNING live process holds its gate flags in memory and only
        # hydrates them at startup — a release landing under it takes effect
        # in the store but not in that process until its next restart. Warn,
        # don't block: the §13.6 release is deliberately store-first.
        state_row = repo.get_scheduler_state(db.conn, args.run_id)
        if state_row is not None and state_row["lock_pid"] is not None:
            print(
                f"warning: run {args.run_id!r} has a run-lock held by pid "
                f"{state_row['lock_pid']} (heartbeat {state_row['lock_heartbeat_at']}) — "
                "a live process that is still running will not see this release "
                "until it restarts.",
                file=sys.stderr,
            )
        released_by = args.released_by or getpass.getuser()
        try:
            manager.release_manual(released_by=released_by, reason=args.reason)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(f"manual safe mode released by {released_by}.")
        if state is not None:
            # Name what was cleared: the current-state trio carries the episode's
            # FIRST reason (entered_at anchors it) — additional reasons this
            # episode accrued are the safe_mode_reason_added rows in --status
            # history. Printing it lets the operator confirm the release matched
            # the fact they reviewed. (state was read just above; the CLI is
            # single-threaded, so nothing changed it before release_manual.)
            print(f"released episode: {state.reason} (entered {state.entered_at}).")
        print(
            "NOTE: trading does not resume yet — the run must pass its next full "
            "reconciliation before new orders are allowed (§13.6 rule 3)."
        )
        return 0


def _stamp_reconciliation_case(db, repo, args) -> int:
    """`safe-mode --stamp-case`: the §12.3 "人工核對後標記 action_taken" path.

    The tool that rule always implied and never had. Several case types can
    only ever be disposed of by a human — a ``fill_malformed`` sighting keyed
    by content digest can never be auto-resolved (nothing will ever join it to
    a booked fill), and money/fee-drift observations describe already-booked
    fills. Since an un-actioned case holds the verdict unclean, without this
    the exchange emitting ONE unparseable payload would wedge the run in safe
    mode permanently, with hand-written SQL against the live store as the only
    remedy.

    Stamping does not resume trading: the next reconciliation pass still has to
    prove the books (§13.6 rule 3's discipline, for the same reason).
    """
    if args.reason is not None or args.released_by is not None:
        print(
            "error: --reason/--released-by apply only with --release; a case "
            'disposition is recorded with --action "<disposition>".',
            file=sys.stderr,
        )
        return 1
    if args.limit is not None:
        print(
            "error: --limit applies only with --status; the stamp action prints no history.",
            file=sys.stderr,
        )
        return 1
    if args.action is None or not args.action.strip():
        print(
            'error: --stamp-case requires --action "<disposition>" — the audit row '
            "must record what the human decided about this case (§12.3).",
            file=sys.stderr,
        )
        return 1
    # Scoped to THIS run: the id comes off a --status listing, and a typo'd
    # --run-id/--db must name that mistake rather than stamp another run's case.
    match = [
        row
        for row in repo.iter_exchange_reconciliation_events(db.conn, args.run_id)
        if row["event_id"] == args.stamp_case
    ]
    if not match:
        print(
            f"error: run {args.run_id!r} has no reconciliation case with event_id "
            f"{args.stamp_case} — see `safe-mode --status` for the open cases.",
            file=sys.stderr,
        )
        return 1
    (row,) = match
    if row["case_type"] == "fill_unmapped":
        # Stamping this would be a verdict NO-OP that also HIDES the row: an
        # unmapped sighting's block is an anti-join against the fills table
        # (repo.iter_unresolved_fill_sightings) which never reads action_taken,
        # so the pass stays unclean while the operator's only enumeration of the
        # backlog loses the row — the very wedge --stamp-case exists to prevent.
        print(
            f"error: case {args.stamp_case} is a fill_unmapped sighting — the "
            "exchange reported a fill the ledger still lacks, and it resolves by "
            "BOOKING that fill (§8.3 re-ingest / the next backfill), never by "
            "stamping: its block is an anti-join that does not read action_taken, "
            "so a stamp would clear no verdict and only hide the row from --status.",
            file=sys.stderr,
        )
        return 1
    if row["action_taken"] is not None:
        # Append-only in spirit: the first disposition is the audit record, and
        # silently overwriting it would erase what a human already attested.
        print(
            f"error: case {args.stamp_case} ({row['case_type']}) was already "
            f"disposed of as {row['action_taken']!r} — a recorded disposition is "
            "not overwritten.",
            file=sys.stderr,
        )
        return 1
    with db.transaction() as conn:
        # Re-tested INSIDE the write, not just above: this command takes no run
        # lease, so between that read and here the daemon's own reconciliation
        # pass can stamp the machine disposition — and a plain UPDATE would
        # erase what the system recorded it DID, with no error and no audit row
        # (2026-07-30 concurrency review).
        stamped = repo.stamp_reconciliation_action_if_unset(conn, args.stamp_case, args.action)
    if not stamped:
        print(
            f"error: case {args.stamp_case} was disposed of by another writer "
            "while this command was running (the live daemon stamps cases its "
            "reconciliation pass resolves) — nothing was overwritten. Re-check "
            "`safe-mode --status` and stamp again only if it is still open.",
            file=sys.stderr,
        )
        return 1
    print(f"case {args.stamp_case} ({row['case_type']}) stamped: {args.action}")
    print(
        "NOTE: trading does not resume yet — the run must pass its next full "
        "reconciliation before new orders are allowed."
    )
    return 0


# --------------------------------------------------------------------------
# live — Phase 3 startup: gates + authorization (PR 1), §19.1 recovery (PR 4)
# --------------------------------------------------------------------------


def _cmd_live(argv: list[str]) -> int:
    """Load the ``live:`` gates, verify agent authorization, print caps, exit.

    The Phase 3 startup sequence: everything here must pass before the live
    loop may run, and every failure is a named exit 1. Without --run-id the
    command is config-only and can never place an order; with --run-id it runs
    the §19.1 startup recovery, and --loop then continues into the PR 5 live
    trading loop (the one lane that trades). Config/env problems fail fast
    (nothing else is checkable without them); the network-dependent gates all
    run and report every failure in one pass.
    """
    parser = argparse.ArgumentParser(
        prog="python -m contrib.hyperliquid_perp live",
        description=(
            "Phase 3 live startup: validate the config gates + agent "
            "authorization and print effective caps; with --run-id, run the "
            "full §19.1 startup recovery (arm kill switch, reconcile, cancel "
            "stale bot-owned orders) and report the verdict; add --loop to "
            "continue into the live trading loop."
        ),
    )
    parser.add_argument("--config", default=None, help="Config YAML path.")
    parser.add_argument(
        "--db",
        default="live_trading.db",
        help="SQLite store path for the live run (only used with --run-id).",
    )
    parser.add_argument(
        "--run-id",
        default=None,
        help=(
            "Run the full §19.1 startup recovery against this run. Omitted: "
            "config-check mode (the PR 1 gates), which never signs anything."
        ),
    )
    parser.add_argument(
        "--create",
        action="store_true",
        help=(
            "Create the run (genesis from the live exchange snapshot) instead "
            "of resuming an existing one — same explicit-identity rule as the "
            "paper subcommand."
        ),
    )
    parser.add_argument(
        "--adopt-positions",
        action="store_true",
        help=(
            "Allow --create to seed an EXISTING exchange position into the new "
            "run's genesis. Without it, --create refuses a non-flat account — "
            "a typo'd --run-id must not silently adopt a live position into a "
            "fresh ledger."
        ),
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help=(
            "After the §19.1 startup recovery passes, run the PR 5 live trading "
            "loop (~10s tick, inside the 30s kill-switch budget: WS drain → "
            "kill-switch refresh → reconciliation → SL/TP protection → due "
            "slices, with the 4h AI decision cycle off the tick thread). "
            "Ctrl-C / SIGTERM stops it and runs the §18.2 shutdown sweep. "
            "Without it, --run-id is the one-shot recovery check."
        ),
    )
    args = parser.parse_args(argv)

    if args.run_id is None and (args.create or args.adopt_positions or args.loop):
        # Named rejection, not silent-ignore: without --run-id this command is
        # config-check mode — it creates and seeds nothing and runs no loop — so
        # --create / --adopt-positions / --loop have no effect. An operator who
        # passed them almost certainly meant the §19.1 recovery (and, for --loop,
        # the live trading loop) and would otherwise read the "gates OK" exit 0
        # as "run started". Same discipline as the resume and safe-mode guards.
        print(
            "error: --create / --adopt-positions / --loop require --run-id — without "
            "it this command only checks the config gates. Pass --run-id to run the "
            "§19.1 startup recovery (add --loop to continue into the live trading loop).",
            file=sys.stderr,
        )
        return 1

    # Same rationale as ``paper``: startup diagnostics need timestamps; the
    # basicConfig no-ops when an embedding application already configured one.
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    from .config import wallet_address
    from .domains.perp.risk_gate import RiskConfig
    from .exchanges.hyperliquid.account import HyperliquidAccount
    from .exchanges.hyperliquid.errors import ExchangeError
    from .exchanges.hyperliquid.sdk_client import HyperliquidClient
    from .exchanges.hyperliquid.signed_client import HyperliquidSignedClient
    from .live.authorization import (
        EXPIRY_WARNING_HORIZON,
        AgentAuthorizationError,
        verify_agent_authorization,
    )
    from .live.config import (
        EXCHANGE_MIN_ORDER_NOTIONAL_USDC,
        ExecutionMode,
        LiveConfig,
        compute_notional_caps,
        validate_live_risk_consistency,
    )
    from .live.secrets import agent_key_env_var, load_agent_key

    try:
        config = load_config(args.config)
    except CONFIG_LOAD_ERRORS as exc:
        print(f"error: invalid config — {exc}. Fix the YAML and re-run.", file=sys.stderr)
        return 1
    raw_live = config.get("live")
    if raw_live is None:
        print(
            "error: config has no live: block — the live subcommand needs one "
            "(phase3-spec §4). Add it to the YAML and re-run.",
            file=sys.stderr,
        )
        return 1
    try:
        live_cfg = LiveConfig.from_dict(raw_live)
    except ValueError as exc:
        print(f"error: invalid live: config — {exc}. Fix the YAML and re-run.", file=sys.stderr)
        return 1
    if live_cfg.mode is ExecutionMode.PAPER:
        print(
            "error: live.mode is 'paper' — use the paper subcommand for paper "
            "runs; the live subcommand needs testnet_live or mainnet_tiny.",
            file=sys.stderr,
        )
        return 1
    # The AI gate (risk:) and the live hard caps (live.safety:) must agree on
    # the sizing regime — PR 5's loop wires them together, so a divergent
    # pair is a config mistake at load time, not a runtime surprise. The block
    # and its cross-checked fields must be operator-written (§24 — see
    # validate_live_risk_consistency for the vacuous-pass rationale).
    raw_risk = config.get("risk")
    if raw_risk is None:
        print(
            "error: config has no risk: block — the live subcommand refuses to "
            "run the AI gate on implicit defaults; write the block explicitly "
            "so the risk:/live.safety cross-check compares operator intent.",
            file=sys.stderr,
        )
        return 1
    try:
        risk_cfg = RiskConfig.from_dict(raw_risk)
        validate_live_risk_consistency(live_cfg, risk_cfg, raw_risk)
    except ValueError as exc:
        print(
            f"error: invalid risk:/live: config — {exc}. Fix the YAML and re-run.", file=sys.stderr
        )
        return 1

    # PR 5 (decided 2026-07-22): --loop consumes the risk:/decision: grid the
    # same way the paper engine does — validate those blocks HERE, where a typo
    # is a named exit-1 config error, never after a passing recovery where the
    # loop would be silently skipped and exit 0 would read as a clean run to a
    # supervisor. (_cmd_paper makes the same up-front check.)
    loop_cfgs = None
    if args.loop:
        from .main import _load_risk_decision

        loop_cfgs = _load_risk_decision(config)
        if loop_cfgs is None:
            return 1
        # The AI key, checked here for the same reason the config blocks above
        # are: the loop drives a 4h AI cycle, and without a key EVERY cycle
        # records api_failed — which never counts toward the §20.3 >=30-cycle
        # gate. A real-money run could otherwise burn days producing nothing
        # gateable, with no named error anywhere. _cmd_paper has always checked
        # this; the live path did not (added 2026-07-30). After the config
        # validation, so a typo in risk:/decision: still reports as the config
        # error it is rather than being masked by a missing key.
        if not _require_api_key():
            return 1

    # A top-level ``network:`` that disagrees with ``live.network`` is legal —
    # the same file can drive paper reads on mainnet while live drills on
    # testnet — but it is also how a stale key silently points somewhere
    # unexpected, so say which one the live run uses.
    # load_config validates the top-level key case-insensitively but stores it
    # raw — normalise before comparing or `network: TestNet` would warn
    # spuriously against an equal live.network.
    top_network = config.get("network")
    if isinstance(top_network, str) and top_network.strip().lower() != live_cfg.network:
        print(
            f"warning: live run uses live.network {live_cfg.network!r} and "
            f"ignores the top-level network: {top_network!r} (only the paper "
            "subcommand reads that key; live does still inherit the top-level "
            "network_timeout_s and wallet_address).",
            file=sys.stderr,
        )

    addr = wallet_address(config)
    if not addr:
        print(
            "error: wallet_address is not configured — the live subcommand needs "
            "the main wallet address for agent authorization and account reads.",
            file=sys.stderr,
        )
        return 1

    env_var = agent_key_env_var(live_cfg.network)
    agent_key = load_agent_key(live_cfg.network)
    if agent_key is None and live_cfg.require_agent_wallet:
        # §6 rule 6 rides this check too: allow_real_orders: true implies
        # require_agent_wallet: true (a LiveConfig construction invariant), so
        # "real orders asked for, no key" always lands here — a named hard
        # fail, never a silent downgrade into an order-less run.
        detail = (
            "live.allow_real_orders is true (§6 rule 6: a missing key can "
            "never mean orders still on)"
            if live_cfg.allow_real_orders
            else "live.require_agent_wallet is true"
        )
        print(
            f"error: {env_var} is not set but {detail} — export the "
            f"{live_cfg.network} agent key, or set require_agent_wallet: false "
            f"(with allow_real_orders: false) for a keyless gate check. "
            f"({dotenv_diagnosis(env_var)}.)",
            file=sys.stderr,
        )
        return 1

    try:
        # Live runs are pinned to ``live.network``, not the top-level Phase 1/2
        # ``network:`` key — the override keeps ``network_timeout_s`` resolution
        # in its one seam instead of re-implementing it here.
        client = HyperliquidClient.from_config(config, network=live_cfg.network)
    except ExchangeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    # The three network-dependent gates below (authorization, account read +
    # caps, signed health check) are independent given a constructed client —
    # run them ALL and report every failure in one pass, so an operator with
    # two broken things fixes both before the next run instead of discovering
    # them one re-run at a time.
    failures: list[str] = []

    auth = None
    if agent_key is not None:
        # §6.1: run the authorization check whenever a key is present — even
        # with real orders off, a bad key/approval is operator-actionable now.
        try:
            auth = verify_agent_authorization(client.info, wallet_address=addr, agent_key=agent_key)
        except (AgentAuthorizationError, ExchangeError) as exc:
            failures.append(f"agent authorization failed — {exc}")
        else:
            print(
                f"agent {auth.agent_address} authorized for {addr} until "
                f"{auth.valid_until.isoformat()}",
                file=sys.stderr,
            )
            if auth.expires_within(EXPIRY_WARNING_HORIZON):
                print(
                    f"warning: agent authorization expires at "
                    f"{auth.valid_until.isoformat()} — less than "
                    f"{EXPIRY_WARNING_HORIZON.days} days away; re-approve the "
                    "agent before a long run (§6.1).",
                    file=sys.stderr,
                )
        # Prove the signed transport end-to-end (construction + a read on the
        # live network) so a bad SDK/network surfaces now, not on the first
        # real order a --loop run places. The bound gate is fresh-from-config, i.e.
        # fail-closed: no runtime condition is proven in this config-only
        # command, so the client could not place an order even if asked.
        try:
            from .live.order_gate import RealOrderGate

            signed = HyperliquidSignedClient(
                live_cfg.network,
                agent_key,
                wallet_address=addr,
                gate=RealOrderGate.from_config(live_cfg),
                timeout=client.timeout,
            )
            signed.health_check()
        except ExchangeError as exc:
            failures.append(f"signed client health check failed — {exc}")
        else:
            print(f"signed client healthy: {signed!r}", file=sys.stderr)

    snapshot = None
    caps = None
    try:
        snapshot = HyperliquidAccount(client).get_account_snapshot(addr)
    except ExchangeError as exc:
        failures.append(f"account read failed — {exc}")
    except ValueError as exc:
        failures.append(f"account snapshot unusable (margin-called / empty / invalid?) — {exc}")
    else:
        caps = compute_notional_caps(snapshot.account_value, live_cfg.safety)
        # §5 rule 3: compute AND record both caps at startup.
        logger.info(
            "startup caps for %s: account_equity=%s pct_cap_notional=%s effective_notional_cap=%s",
            live_cfg.mode.value,
            snapshot.account_value,
            caps.pct_cap_notional,
            caps.effective_notional_cap,
        )
        if caps.below_exchange_minimum:
            failures.append(
                f"effective_notional_cap ({caps.effective_notional_cap} USDC) "
                f"is below the exchange minimum order value "
                f"({EXCHANGE_MIN_ORDER_NOTIONAL_USDC} USDC) — the run could never "
                "place an order (§5 rule 4). Fund the account or raise the caps."
            )

    if failures:
        for failure in failures:
            print(f"error: {failure}", file=sys.stderr)
        return 1

    print(f"mode: {live_cfg.mode.value}")
    print(f"network: {live_cfg.network}")
    print(f"allow_real_orders: {'true' if live_cfg.allow_real_orders else 'false'}")
    if auth is not None:
        # Part of the machine-readable contract: a deploy preflight wrapping
        # this gate check can capture the expiry without scraping stderr.
        print(f"agent_address: {auth.agent_address}")
        print(f"authorization_valid_until: {auth.valid_until.isoformat()}")
    print(f"account_equity: {snapshot.account_value} USDC")
    print(f"pct_cap_notional: {caps.pct_cap_notional} USDC")
    print(f"effective_notional_cap: {caps.effective_notional_cap} USDC")
    if args.run_id is None:
        print(
            "live startup gates OK — exiting (config-check mode; pass --run-id "
            "to run the full §19.1 startup recovery).",
            file=sys.stderr,
        )
        return 0
    return _live_startup_recovery(
        args,
        config=config,
        raw_live=raw_live,
        live_cfg=live_cfg,
        client=client,
        wallet=addr,
        agent_key=agent_key,
        snapshot=snapshot,
        loop_cfgs=loop_cfgs,
    )


# This command's worst-case wall time between two kill-switch tick() calls is
# one reconciliation sweep (network reads, seconds) — 30s is generous and keeps
# the constructor invariant honest under the default 120s/30s switch config.
# ONE constant, read by both the pre-side-effect preflight check and the
# KillSwitchManager construction below, so the number the preflight proves is
# the number the constructor enforces.
_RECOVERY_MAX_TICK_GAP_SECONDS = 30.0
# The narrowest dead-man cover the smoke suite will run under, whatever the
# config says. The suite refreshes once per TEST rather than on a fixed tick,
# and a test is an unbounded place/poll/cancel round-trip, so the config's
# daemon-shaped invariant does not bound it. 120s is what the suite guaranteed
# before the value was wired from config at all (2026-07-31).
_SMOKE_MIN_KILL_SWITCH_DEADLINE = timedelta(seconds=120)


def _live_startup_recovery(
    args,
    *,
    config: dict,
    raw_live: dict,
    live_cfg,
    client,
    wallet: str,
    agent_key: str | None,
    snapshot,
    loop_cfgs,
) -> int:
    """The §19.1 startup recovery tail of ``live --run-id`` (steps 5–16).

    Steps 1–4 (config gates, §6.1 authorization, exchange client, account
    read) were proven by the caller; this builds the PR 2–4 components — a
    runtime-flagged gate, the signed client, kill switch, safe-mode machine
    and reconciler — creates or resumes the live run, and hands off to
    :func:`~.live.startup.run_startup_recovery`. Without --loop the command is
    one-shot: it reports the verdict, runs the §18.2 shutdown sweep, and
    exits; with --loop a passing verdict hands off to :func:`_run_live_loop`
    (which keeps the kill switch refreshed) before the same sweep runs on the
    way out. Exit codes: 0 = the §19.1 step-16 verdict allows a new AI cycle;
    4 = recovery executed but the verdict is unclean (the run is in safe
    mode); 1 = hard failure (config/arming/creation errors).
    """
    import signal
    from decimal import Decimal

    from .exchanges.hyperliquid.mapper import map_account_snapshot
    from .exchanges.hyperliquid.sdk_client import call_sdk
    from .exchanges.hyperliquid.signed_client import HyperliquidSignedClient
    from .live.fill_backfill import FillBackfiller
    from .live.fills import LiveFillProcessor
    from .live.kill_switch import KillSwitchManager, refresh_across_blocking_work
    from .live.order_gate import RealOrderGate
    from .live.reconcile import LiveReconciler
    from .live.safe_mode import SafeModeManager
    from .live.startup import run_startup_recovery
    from .paper import accounting
    from .paper.run_lock import RunLockError, acquire_run_lock, release_run_lock
    from .persistence import repository as repo
    from .persistence.models import PositionState
    from .persistence.schema import SCHEMA_VERSION

    if agent_key is None:
        print(
            "error: the §19.1 startup recovery signs exchange actions (kill "
            "switch, stale-order cancels) — it needs the agent key. Run without "
            "--run-id for a keyless gate check.",
            file=sys.stderr,
        )
        return 1
    if not live_cfg.allow_real_orders:
        print(
            "error: live.allow_real_orders is false — the §19.1 startup recovery "
            "arms the kill switch and cancels stale bot-owned orders, which are "
            "signed exchange actions. Enable it, or run without --run-id for a "
            "gate check that never signs anything.",
            file=sys.stderr,
        )
        return 1

    if _timing_preflight(live_cfg, client) != 0:
        return 1

    run_id: str = args.run_id
    coin = live_cfg.safety.allowed_symbols[0]
    db_path = Path(args.db)
    payload_dir = db_path.resolve().parent / "payloads" / run_id
    now = datetime.now(timezone.utc)

    # The runtime gate: config pins the wire conditions; §6.1 passed above.
    gate = RealOrderGate.from_config(live_cfg)
    gate.agent_authorized = True
    signed = HyperliquidSignedClient(
        live_cfg.network,
        agent_key,
        wallet_address=wallet,
        gate=gate,
        timeout=client.timeout,
    )

    def fetch_clearinghouse():
        return call_sdk(client.info.user_state, wallet)

    # Same guard the paper daemon applies, and it matters more here: Database()
    # creates AND migrates a store, so a wrong CWD or typo'd --db would leave an
    # empty live store behind before failing on "run does not exist" — and with
    # --create it would silently open a SECOND live ledger over the same real
    # wallet, each blind to the other's orders and books.
    if not Path(db_path).exists() and not args.create:
        print(
            f"error: database {str(db_path)!r} does not exist. Pass --create to "
            "start a new store, or point --db at the existing one.",
            file=sys.stderr,
        )
        return 1

    with Database(db_path) as db:
        existing_run = repo.get_run(db.conn, run_id)
        is_restart = existing_run is not None
        if not is_restart and not args.create:
            print(
                f"error: run {run_id!r} does not exist in {db_path}. Pass --create "
                "to start it, or fix --run-id / --db to resume the intended run.",
                file=sys.stderr,
            )
            return 1
        if is_restart and args.create:
            print(
                f"error: run {run_id!r} already exists in {db_path}. Drop --create "
                "to resume it, or pick a new --run-id for a fresh run.",
                file=sys.stderr,
            )
            return 1
        # BEFORE --create writes the run row and before any wire action: a
        # refusal taken later left a half-created run behind, and the operator's
        # corrected re-run was then rejected as "already exists" (2026-07-31
        # exit check). own_network comes from this session's config because a
        # not-yet-created run has no genesis to read; the SIBLING's network is
        # still read from the store.
        #
        # The same per-WALLET hazard `live-smoke` refuses, on the path that runs
        # with REAL money. This command arms and clears the account-wide
        # scheduleCancel and runs the §19.3 stale-order sweep, whose bot-ownership
        # lookup (get_cloid_by_hex) carries no run_id — so a sibling live run on
        # this wallet has its resting orders cancelled by our sweep, and whichever
        # of us shuts down cleanly first strips the other's dead-man cover. The
        # run lease cannot see this: it is per-run_id, and both runs hold their
        # own quite happily. Guarding only the testnet suite and not this was the
        # most asymmetric gap of the 2026-07-31 review.
        conflict = _conflicting_run_lease(db, run_id, own_network=_norm_network(raw_live))
        if conflict is not None:
            other_run, other_pid = conflict
            print(
                f"error: run {other_run!r} in {args.db} is being driven by pid "
                f"{other_pid} right now, on this same network — the same wallet. "
                "This command's kill-switch arm/clear and §19.3 stale-order sweep are "
                "ACCOUNT-wide, not run-scoped, so the two runs would cancel each "
                "other's resting orders and strip each other's dead-man cover. Stop "
                "that process, or wait for its lease to go stale. Moving either run "
                "to a different --db does NOT help: the hazard is per-WALLET, so a "
                "separate store only hides them from this check.",
                file=sys.stderr,
            )
            return 1
        if existing_run is not None:
            # Resume validates the run's IDENTITY before any side effect (the
            # lock, arming the wallet-wide kill switch, reconciliation writes)
            # — the same discipline as the paper daemon's resume (decided
            # 2026-07-17). A typo'd --run-id/--db pointing at a paper run
            # would otherwise arm the kill switch over a paper ledger and
            # write live snapshots into it; a coin edit under an existing run
            # would re-enter manual safe mode every pass with nothing naming
            # the true cause.
            if existing_run["mode"] != "live":
                print(
                    f"error: run {run_id!r} in {db_path} is a {existing_run['mode']} "
                    "run — resuming it here would arm the kill switch and "
                    f"reconcile a {existing_run['mode']} ledger against the live "
                    "exchange. Fix --run-id / --db.",
                    file=sys.stderr,
                )
                return 1
            drift = _config_drift_report(existing_run["config_json"], config, coin)
            if drift is not None:
                kind, message = drift
                if kind in _HARD_DRIFT_KINDS:
                    print(f"error: {message}", file=sys.stderr)
                    return 1
                logger.warning("config drift on live resume for %s: %s", run_id, message)
                print(f"WARNING: {message}", file=sys.stderr)
            # The STORE's own off-coin exposure, mirroring the paper daemon's
            # resume guard. The --create branch below checks the same thing from
            # the exchange snapshot, but resume never did: a live store carrying a
            # non-flat off-coin current_positions row (an older build, a
            # hand-seeded genesis, a coin edit that passed as soft "params" drift)
            # would resume and trade with that exposure invisible to every equity
            # and SL/TP computation — exactly the harm the paper message names.
            store_off_coin = sorted(
                p.coin
                for p in repo.get_all_current_positions(db.conn, run_id)
                if p.coin != coin and not p.is_flat
            )
            if store_off_coin:
                print(
                    f"error: run {run_id!r} holds open position(s) in "
                    f"{', '.join(map(repr, store_off_coin))} but this run trades "
                    f"only {coin!r} — they would be excluded from equity and "
                    "SL/TP protection, and the reconciler flags them as unknown "
                    "exchange positions (manual safe mode) on every pass. "
                    "Resolve them before resuming (export/validate still work).",
                    file=sys.stderr,
                )
                return 1
            if args.adopt_positions:
                # Named rejection, not silence: the flag seeds a NEW run's
                # genesis and has no meaning on resume — an operator who
                # passed it may believe the current exchange position was
                # adopted into the resumed books (it was not; a mismatch
                # surfaces as a reconciliation case instead).
                print(
                    "error: --adopt-positions seeds a new run's genesis and "
                    "requires --create — resuming an existing run never "
                    "re-seeds its books.",
                    file=sys.stderr,
                )
                return 1
        else:
            # Same convention as the paper path's initial_positions guard: a
            # run manages exactly one coin. An off-coin position CAN'T be
            # adopted meaningfully — the reconciler classifies every non-run
            # coin position as §13.5 "unknown exchange position" (manual safe
            # mode, re-entered every pass), so --adopt-positions over it would
            # create a run that is unreleasable from the first sweep (decided
            # 2026-07-17). Named rejection before anything is written.
            off_coin = sorted({p.coin for p in snapshot.positions} - {coin})
            if off_coin:
                print(
                    f"error: the account holds position(s) in "
                    f"{', '.join(map(repr, off_coin))} but this run trades only "
                    f"{coin!r} — a live run manages exactly one coin, and the "
                    "reconciler would flag any other coin's position as an "
                    "unknown exchange position (manual safe mode) on every "
                    "pass. Close or move those positions, or run them under a "
                    "separate wallet.",
                    file=sys.stderr,
                )
                return 1
            if snapshot.positions and not args.adopt_positions:
                # Creating a live-money ledger over a non-flat account must be
                # explicit: a typo'd --run-id plus --create would otherwise
                # silently adopt a live position into a fresh ledger with zero
                # history explaining it (decided 2026-07-16).
                held = ", ".join(f"{p.coin} {p.size}" for p in snapshot.positions)
                print(
                    f"error: the account already holds a position ({held}) — pass "
                    "--adopt-positions to seed it into the new run's genesis, or "
                    "resume the run that owns it.",
                    file=sys.stderr,
                )
                return 1
            # Live genesis = the exchange snapshot, verbatim: the opening
            # ledger balance is equity net of unrealized PnL (wallet form) and
            # any existing position is seeded as-is, so the first equity
            # reconciliation compares like against like. Historical fills that
            # PREDATE this run belong to other runs' orders (or none) and are
            # routed to unmapped/cross-run audit by the PR 3 processor — they
            # can never double-book onto this genesis.
            unrealized = sum((p.unrealized_pnl for p in snapshot.positions), Decimal(0))
            seeds = [
                PositionState(coin=p.coin, size=p.size, entry_price=p.entry_price)
                for p in snapshot.positions
            ]
            subset = _run_config_subset(config, coin)
            subset["live"] = raw_live
            accounting.initialize_run(
                db,
                run_id=run_id,
                mode="live",
                initial_balance_usdc=snapshot.account_value - unrealized,
                schema_version=SCHEMA_VERSION,
                initial_positions=seeds,
                config_json=json.dumps(subset, ensure_ascii=False, default=str),
                created_at=now,
            )
            print(f"created live run {run_id!r} in {db_path}", file=sys.stderr)

        # §20.2 gate: testnet_live cycles may not start until the smoke suite has
        # passed on THIS run. (mainnet_tiny relies on the testnet smoke pass per
        # §21.3 — a different run/network — so this same-run gate is testnet-only;
        # the one-shot recovery check, without --loop, never trades and so is not
        # gated.) Checked here, before arming: a fresh --create run has no smoke
        # results, so --loop on it is refused with the create → smoke → loop path.
        from .live.config import ExecutionMode

        if args.loop and live_cfg.mode is ExecutionMode.TESTNET_LIVE:
            from .live.smoke import smoke_gate_report

            gate_ok, gate_missing, gate_failed, gate_errored = smoke_gate_report(db.conn, run_id)
            if not gate_ok:
                if not is_restart:
                    print(
                        "error: --loop on a freshly-created testnet_live run needs the "
                        "§20.2 smoke suite first. Create the run, run "
                        f"`live-smoke --run-id {run_id}`, then re-run with --loop.",
                        file=sys.stderr,
                    )
                else:
                    parts = [
                        f"{label.replace('_', ' ')}: {', '.join(keys)}"
                        for label, keys in _smoke_gate_buckets(
                            gate_missing, gate_failed, gate_errored
                        )
                        if keys
                    ]
                    print(
                        "error: testnet_live cycles are gated on the §20.2 smoke suite "
                        f"(all must pass) — {'; '.join(parts)}. Run "
                        f"`live-smoke --run-id {run_id}` and re-run with --loop.",
                        file=sys.stderr,
                    )
                # Exit 4, not 1: "the gate is not open" is the same
                # not-yet-at-the-gate fact `live-smoke` itself reports as 4 (the
                # module exit contract lists it there), and a supervisor must be
                # able to tell "gate closed — human action needed" from a
                # config/auth failure's exit 1 (decision 2026-07-29).
                return 4
            # Gate open: say how stale the proof is. Passes never expire (a hard
            # max-age is a policy call deliberately not made here, 2026-07-27),
            # so an operator returning after weeks should at least SEE the age
            # and re-run live-smoke after significant code/config changes.
            latest_smoke = repo.latest_smoke_test_results(db.conn, run_id)
            if latest_smoke:
                oldest_iso = min(row["executed_at"] for row in latest_smoke.values())
                print(
                    f"§20.2 smoke gate open — oldest passing result recorded {oldest_iso}; "
                    "passes never expire, so re-run live-smoke after significant "
                    "code/config changes.",
                    file=sys.stderr,
                )

        try:
            acquire_run_lock(db, run_id, pid=os.getpid(), now=now)
        except RunLockError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)
        try:
            kill_switch = KillSwitchManager(
                client=signed,
                gate=gate,
                db=db,
                run_id=run_id,
                config=live_cfg.kill_switch,
                # The preflight above already proved this invariant with the
                # SAME constant and the SAME timeout, so this constructor cannot
                # raise on timing. Both are passed explicitly: the timeout used to
                # be probed off the client with getattr, which silently dropped
                # the term on every production manager.
                max_tick_gap_seconds=_RECOVERY_MAX_TICK_GAP_SECONDS,
                network_timeout_s=signed.timeout,
                payload_dir=payload_dir,
            )
            safe_mode = SafeModeManager(db=db, run_id=run_id, gate=gate)
            processor = LiveFillProcessor(
                db=db,
                run_id=run_id,
                payload_dir=payload_dir,
                wallet_address=signed.wallet_address,
            )

            # §18.2: both of these block the single-threaded tick for far longer
            # than one round-trip (a paged backfill, a per-order orderStatus
            # sweep), and the tick's own refresh happens before either runs — so
            # they refresh across their own work (2026-07-31 deadline review).
            def _refresh_across_sweep() -> None:
                refresh_across_blocking_work(kill_switch, what="reconciliation")

            backfiller = FillBackfiller(
                fetch=signed.user_fills_by_time,
                processor=processor,
                refresh_kill_switch=_refresh_across_sweep,
            )
            reconciler = LiveReconciler(
                db=db,
                run_id=run_id,
                coin=coin,
                fetch_open_orders=signed.open_orders,
                fetch_clearinghouse=fetch_clearinghouse,
                query_order_by_cloid=signed.query_order_by_cloid,
                fetch_fills=signed.user_fills_by_time,
                backfiller=backfiller,
                payload_dir=payload_dir,
                refresh_kill_switch=_refresh_across_sweep,
            )
            shutdown_problem: str | None = None
            superseded = False
            # False until the §19.1 verdict PASSES — a recovery that raised
            # counts as unclean too. The ``finally`` sweep below keys its
            # keep-the-SL/TP decision off this OR'd with the EXIT-TIME safe
            # mode (both decided 2026-07-22): over a --loop run the boot
            # verdict goes stale, and safe mode latched mid-loop means the
            # next boot's verdict will refuse to start — exactly the
            # "no repair machinery" case the keep exists for.
            verdict_passed = False
            # True when the shutdown-time safe-mode read RAISED: the keep
            # decision then acted on unknown ≠ clean, and the exit code below
            # must not let a luckier later read report "all quiet" over
            # deliberately-kept orders.
            exit_safe_mode_unknown = False
            try:
                result = run_startup_recovery(
                    db=db,
                    run_id=run_id,
                    client=signed,
                    fetch_clearinghouse=fetch_clearinghouse,
                    gate=gate,
                    kill_switch=kill_switch,
                    reconciler=reconciler,
                    safe_mode=safe_mode,
                    payload_dir=payload_dir,
                )
                verdict_passed = result.passed
                # PR 5: with --loop, a passing recovery hands off to the live
                # trading loop; it returns on Ctrl-C / SIGTERM, and the §18.2
                # shutdown sweep in the ``finally`` below then disarms the switch.
                if args.loop and result.passed:
                    _run_live_loop(
                        cfgs=loop_cfgs,
                        db=db,
                        run_id=run_id,
                        coin=coin,
                        config=config,
                        live_cfg=live_cfg,
                        client=client,
                        signed=signed,
                        gate=gate,
                        kill_switch=kill_switch,
                        safe_mode=safe_mode,
                        reconciler=reconciler,
                        processor=processor,
                        payload_dir=payload_dir,
                        fetch_clearinghouse=fetch_clearinghouse,
                    )
            except RunLockError as exc:
                # §18.2 lease takeover (raised out of the loop's heartbeat): a
                # successor process owns the run now — its store, its resting
                # orders (the SL/TP included) and the wallet's dead-man's
                # switch. ANY exchange action or store write from this process
                # sabotages the successor: the §18.2 sweep in the ``finally``
                # below would cancel the successor's live protection orders and
                # leave ITS position naked. Exit with nothing but the
                # pid-guarded lock release (a no-op once the successor holds
                # the lease) — the same contract as the paper loop's
                # RunLockError exit.
                superseded = True
                logger.error("run lease lost: %s", exc)
                print(f"error: {exc}", file=sys.stderr)
                return 1
            except Exception as exc:  # noqa: BLE001 — arming is the one hard-error step
                # Full traceback to the log (this is the signed live path);
                # the message alone would leave a failure here undiagnosable.
                logger.exception("startup recovery failed")
                print(f"error: startup recovery failed — {exc}", file=sys.stderr)
                return 1
            finally:
                # Re-ASKED, not merely remembered: ``superseded`` is only True
                # when a heartbeat raised, and the Ctrl-C / SIGTERM lane reaches
                # here without one (see _still_owns_run).
                if superseded or not _still_owns_run(
                    db, run_id, pid=os.getpid(), now=datetime.now(timezone.utc)
                ):
                    # Lease lost: the successor owns every resting order and
                    # the dead-man's switch — skip the position re-read and the
                    # §18.2 sweep ENTIRELY; only the caller's pid-guarded lock
                    # release runs (and no-ops).
                    logger.info("lease takeover — §18.2 shutdown sweep skipped")
                else:
                    # §12.2 rule 8 (the --loop path): the loop has been placing
                    # orders since the startup verdict pass — reconcile once
                    # more so the sweep below works from fresh exchange state.
                    # Runs FIRST, before the position/safe-mode reads below:
                    # this pass can itself ENTER safe mode (an unclean final
                    # reconcile), and a keep decision computed from the
                    # pre-reconcile snapshot would strip SL/TP at exactly the
                    # moment the problem was found. Best-effort: a failure must
                    # not block the sweep.
                    if args.loop:
                        try:
                            reconciler.reconcile_and_apply(
                                "shutdown",
                                safe_mode=safe_mode,
                                ws_restored=True,
                                kill_switch_active=not kill_switch.stop_new_orders,
                            )
                        except Exception:  # noqa: BLE001
                            logger.exception(
                                "§12.2 pre-shutdown reconciliation failed (sweep proceeds)"
                            )
                    # Decided 2026-07-16, revised 2026-07-22: on a PASSING
                    # verdict the §18.2 semantics stand (shutdown cancels ALL
                    # bot-owned orders, the §19.3-kept SL/TP included), but
                    # never silently over a live position. On an UNCLEAN exit
                    # (verdict failed, recovery raised, or safe mode active at
                    # exit — the boot verdict goes stale over a loop) the sweep
                    # now KEEPS
                    # the resting SL/TP over a live — or unreadable, and
                    # unreadable ≠ flat is the startup sweep's own rule —
                    # position: stripping reduce-only protection trades an
                    # unclean verdict for a naked position, and the repair
                    # machinery that could re-cover it is exactly what an
                    # unclean verdict refuses to start. The position that
                    # decides both the warning and the keep is read FRESH here
                    # (decided 2026-07-17): the boot snapshot can be minutes
                    # stale after arming/backfill/two reconcile passes, and a
                    # position acquired mid-recovery would otherwise lose its
                    # protection to the sweep below without a word.
                    # None = could not read (unknown ≠ flat), the same sentinel
                    # convention as ``_safe_fetch_open_orders``.
                    fresh_positions: list | None
                    try:
                        fresh_positions = map_account_snapshot(fetch_clearinghouse()).positions
                    except Exception:  # noqa: BLE001 — the warning must not mask the verdict
                        logger.exception("shutdown position re-read failed")
                        fresh_positions = None
                    # Exit-time safe mode is read FRESH for the same reason the
                    # position is: the boot verdict is stale after a loop. A
                    # failed read is unknown ≠ clean — fail toward keeping.
                    try:
                        exit_safe_mode = safe_mode.active
                    except Exception:  # noqa: BLE001
                        logger.exception("shutdown safe-mode read failed")
                        exit_safe_mode = True
                        exit_safe_mode_unknown = True
                    keep_protective = (not verdict_passed or exit_safe_mode) and (
                        fresh_positions is None or bool(fresh_positions)
                    )
                    # Three distinct causes, three truthful notes: a FAILED
                    # read is not "safe mode is active" — claiming so would
                    # contradict the fresh `safe_mode:` line printed later
                    # when the second read succeeds and finds none.
                    if not verdict_passed:
                        unclean_note = "the startup verdict did not pass"
                    elif exit_safe_mode_unknown:
                        unclean_note = (
                            "the exit-time safe-mode state could NOT be read (unknown ≠ clean)"
                        )
                    else:
                        unclean_note = "safe mode is active at exit"
                    if fresh_positions is None:
                        if keep_protective:
                            print(
                                "WARNING: positions could NOT be re-read at shutdown "
                                f"(unknown ≠ flat) and {unclean_note} "
                                "— the §18.2 shutdown sweep leaves the bot's resting "
                                "SL/TP STANDING (reduce-only) and cancels other bot "
                                "orders. Re-run `live --run-id ...` (--loop only once the §20.2 smoke gate is open), or intervene manually.",
                                file=sys.stderr,
                            )
                        else:
                            # Truthful wording: "could not look" is not "holds" —
                            # but the operator action is the same (unknown ≠ flat).
                            print(
                                "WARNING: positions could NOT be re-read at shutdown "
                                "and this command's §18.2 shutdown sweep cancels "
                                "bot-owned protection orders — any live position is "
                                "UNPROTECTED after exit until a --loop run or "
                                "manual action re-covers it.",
                                file=sys.stderr,
                            )
                    elif fresh_positions:
                        held = ", ".join(f"{p.coin} {p.size}" for p in fresh_positions)
                        if keep_protective:
                            print(
                                f"WARNING: the account holds a live position ({held}) "
                                f"and {unclean_note} — the §18.2 "
                                "shutdown sweep leaves the bot's resting SL/TP "
                                "STANDING (reduce-only) and cancels other bot orders. "
                                "Re-run `live --run-id ...` (--loop only once the §20.2 smoke gate is open), or intervene manually.",
                                file=sys.stderr,
                            )
                        else:
                            print(
                                f"WARNING: the account holds a live position ({held}) "
                                "and this command's §18.2 shutdown sweep "
                                "cancels bot-owned protection orders — the position "
                                "is UNPROTECTED after exit until a --loop run "
                                "or manual action re-covers it.",
                                file=sys.stderr,
                            )
                    # Leave nothing resting behind a dead man's switch nobody
                    # will refresh — except the deliberately-kept SL/TP when
                    # ``keep_protective`` is set (reduce-only; the disarm below
                    # is what lets them outlive the wallet-wide trigger). The
                    # §18.2 shutdown sweep cancels the rest of the bot-owned
                    # open orders and disarms only on a clean sweep.
                    # (The §12.2 "before shutdown" reconciliation: for the
                    # one-shot command it is the verdict pass that just ran — no
                    # orders are placed after it; the --loop path runs the fresh
                    # pass above.)
                    if kill_switch.armed:
                        # Guarded because a raise inside this ``finally`` would
                        # DISCARD the computed verdict (nothing prints, the
                        # documented 0/4/1 contract becomes a generic exit 2) —
                        # shutdown()'s audit writes are fail-loud by design, so a
                        # busy DB here is a realistic raise, not an edge case.
                        try:
                            kill_switch.shutdown(keep_protective=keep_protective)
                        except Exception as exc:  # noqa: BLE001
                            logger.exception("§18.2 shutdown sweep raised")
                            shutdown_problem = f"shutdown sweep raised: {exc}"
                        else:
                            if kill_switch.armed:
                                # shutdown() disarms only on a clean sweep: still
                                # armed means bot orders may rest and the
                                # wallet-wide scheduleCancel WILL fire at the
                                # deadline — taking out non-bot orders §25 says
                                # never to touch. Loud, and never exit 0.
                                shutdown_problem = (
                                    "shutdown sweep left the kill switch armed — bot "
                                    "orders may still rest and the wallet-wide "
                                    "scheduleCancel will fire at the deadline"
                                )
                                logger.error("§18.2 %s", shutdown_problem)
                        if shutdown_problem is not None:
                            # Surfaced HERE, inside the ``finally``: when the body
                            # above raised (the except path already returned 1),
                            # the summary prints below never run — and "the
                            # wallet-wide trigger is still armed" is the one fact
                            # that must never exit silently, on any path.
                            print(
                                f"error: §18.2 shutdown unclean — {shutdown_problem}",
                                file=sys.stderr,
                            )
                            if keep_protective and kill_switch.armed:
                                # The calm "left STANDING" warning above and the
                                # armed trigger are the SAME orders' fate — say
                                # so, or the operator reads two disconnected
                                # facts and misses that the kept SL/TP die at
                                # the scheduleCancel deadline.
                                print(
                                    "NOTE: the SL/TP described above as kept "
                                    "STANDING are NOT safe while the wallet-wide "
                                    "scheduleCancel stays armed — it cancels them "
                                    "too at its deadline. Re-run `live --run-id ...`: its "
                                    "clean shutdown sweep disarms the switch on "
                                    "exit. The recovery itself ARMS the switch, and "
                                    "a --loop run is refused while the §20.2 gate is "
                                    "shut — or clear the trigger manually.",
                                    file=sys.stderr,
                                )

            print(f"startup_reconciliation_passed: {'true' if result.passed else 'false'}")
            print(f"canceled_stale_orders: {len(result.canceled_stale)}")
            print(f"kept_orders: {len(result.kept_orders)}")
            state = safe_mode.current()
            print(f"safe_mode: {'none' if state is None else state.safe_mode_type}")
            if state is not None:
                print(f"safe_mode_reason: {state.reason}")
            if result.sweep_failures:
                for failure in result.sweep_failures:
                    print(f"error: stale-order sweep — {failure}", file=sys.stderr)
            if result.passed:
                if shutdown_problem is not None:
                    # Decided 2026-07-17: exit 0 means "all quiet" to a
                    # supervisor — a passing verdict with an unclean shutdown
                    # sweep is NOT that; it folds into the same
                    # executed-but-unclean code 4 the verdict path uses (the
                    # "§18.2 shutdown unclean" line above carries the detail).
                    return 4
                if args.loop:
                    if state is not None or (exit_safe_mode_unknown and keep_protective):
                        # Sibling of the keep decision (2026-07-22): the boot
                        # verdict is stale after a loop, and a run that latched
                        # safe mode mid-loop must not hand exit 0 ("all quiet")
                        # to its supervisor. Same executed-but-unclean code 4
                        # the one-shot path returns when ITS verdict finds safe
                        # mode active. A FAILED shutdown safe-mode read that
                        # actually KEPT orders counts too — the sweep just
                        # acted on unknown ≠ clean, and a luckier later read
                        # must not talk the exit code back down to 0 over
                        # deliberately-kept orders. (Unknown read over a
                        # confirmed-flat book kept nothing and changed nothing:
                        # exit stays state-driven, no supervisor false alarm.)
                        print(
                            "live loop exited IN SAFE MODE — see safe_mode above; "
                            "resolve it (manual release if required) before resuming."
                            if state is not None
                            else "live loop exited with protective orders kept behind "
                            "a FAILED shutdown safe-mode read (unknown ≠ clean) — "
                            "inspect the run store before resuming.",
                            file=sys.stderr,
                        )
                        return 4
                    print(
                        "live loop exited — §18.2 shutdown sweep done; re-run "
                        "with --loop to resume this run.",
                        file=sys.stderr,
                    )
                else:
                    print(
                        "startup recovery passed — a live loop can start from "
                        "this state (re-run with --loop).",
                        file=sys.stderr,
                    )
                return 0
            print(
                "startup recovery did NOT pass — the run is in safe mode; see the "
                "reconciliation events / safe-mode state above (§19.1 step 15).",
                file=sys.stderr,
            )
            # Exit 4, not 1: "executed fine, verdict unclean" is an operator
            # signal distinct from a hard failure — same multi-code convention
            # as validate's 4/5 (decided 2026-07-16).
            return 4
        finally:
            # Guarded: the release opens its own transaction (BEGIN IMMEDIATE),
            # which can raise on a busy store — and a raise in this ``finally``
            # would clobber the 0/4/1 verdict the body just computed,
            # surfacing as a generic exit 2. A lock row left behind is
            # diagnosable and reapable; a clobbered verdict is not.
            try:
                release_run_lock(db, run_id, pid=os.getpid(), now=datetime.now(timezone.utc))
            except Exception:  # noqa: BLE001
                logger.exception("run-lock release failed (the verdict above stands)")


# The live loop's tick period. Kept well inside the kill-switch tick budget
# (max_tick_gap 30s) so the §18.2 refresh never lands late even when a tick does
# real work (reconciliation network reads, an SL repair). engine.tick() refreshes
# the switch every call and across its own blocking work; the AI's LLM call runs
# off-thread, but the decision cycle's MARKET-DATA reads (_build_context) run on
# this thread inside driver.pump(), which is why the loop refreshes between the
# two (2026-08-01 lifecycle review).
_LIVE_TICK_SECONDS = 10.0


def _day_baseline_from_exchange(fetch_clearinghouse, kill_switch) -> Decimal:
    """§10.3 rule 1's UTC-day baseline: the EXCHANGE's reconciled accountValue.

    Module level, not a closure inside the loop, so it can be called directly by
    a test — the first version was nested and its only "coverage" was a string
    search for its name, which passed happily while the call site had the wrong
    arity and the whole path was dead.

    The refresh is in ``finally`` for the same reason protection's orderStatus
    read is: this is a bare full-timeout REST call on the single-threaded tick,
    landing between the protection sync and the slice submits, and the call that
    TIMES OUT is both the expensive one and the one that leaves by exception.
    """
    from .exchanges.hyperliquid.mapper import map_account_snapshot
    from .live.kill_switch import refresh_across_blocking_work

    try:
        return map_account_snapshot(fetch_clearinghouse()).account_value
    finally:
        refresh_across_blocking_work(kill_switch, what="day-roll baseline read")


def _contain_as_recoverable_safe_mode(safe_mode, *, log_message: str, detail: str) -> None:
    """The live loop's ONE containment idiom: log, then best-effort safe mode.

    Used by every except-branch that must keep the loop alive (tick/pump
    errors, heartbeat blips): the failure is logged with its traceback, the run
    drops into recoverable safe mode so new risk pauses until a clean reconcile
    releases it, and a safe-mode write that ITSELF fails is swallowed too —
    nothing here may end the loop, because the caller's teardown would sweep
    the resting SL/TP off a live position.
    """
    from .live.safe_mode import REASON_LIVE_TICK_ERROR

    logger.exception(log_message)
    try:
        safe_mode.enter("recoverable", REASON_LIVE_TICK_ERROR, detail=detail)
    except Exception:  # noqa: BLE001 — a safe-mode write miss must not itself end the loop
        logger.exception("failed to enter safe mode after containment (%s)", detail)


def _live_heartbeat(db, run_id: str, *, pid: int, now, safe_mode) -> None:
    """§18.2 lease heartbeat, contained like a tick error.

    ``RunLockError`` (this pid was superseded by a newer process) stays FATAL —
    two writers must never flip-flop the lease. Any OTHER failure here is a
    transient store error (an operator's export/validate holding the SQLite
    lock, say): letting it tear the loop down would run the §18.2 shutdown
    sweep, cancelling the resting SL/TP — a naked position bought with a
    heartbeat blip. Contain it exactly like a tick error instead: log, enter
    recoverable safe mode, retry on the next tick's heartbeat.
    """
    from .paper import run_lock

    try:
        run_lock.heartbeat_run_lock(db, run_id, pid=pid, now=now)
    except run_lock.RunLockError:
        raise
    except Exception:  # noqa: BLE001 — a transient store error must not strip SL/TP
        _contain_as_recoverable_safe_mode(
            safe_mode,
            log_message=(
                "run-lock heartbeat failed transiently — entering recoverable "
                "safe mode and continuing"
            ),
            detail="run-lock heartbeat write failed (see log)",
        )


def _timing_preflight(live_cfg, client) -> int:
    """The §18.2 refresh-timing preflight: ``0`` to proceed, ``1`` to refuse.

    Shared by EVERY entry point that arms the dead man's switch, which is the
    whole point. `live` refused a violating config here with a named exit 1 that
    says which two knobs to move; `live-smoke` had no such check, so the same
    bad config reached ``KillSwitchManager.__init__``, raised, was contained as
    a ``SmokePreflightError`` and surfaced as exit 4. RUNBOOK §5 answers exit 4
    with "the run state is unclean — check `safe-mode --status`", which is the
    wrong investigation entirely, and §8 tells a supervisor to branch its
    restart policy on 1-vs-4, so a script mis-routes it too (2026-07-31).

    Refusing BEFORE any side effect (run row, run lock, wire action) is the
    point of a preflight; ``kill_switch_timing_violation``'s docstring owns the
    invariant itself.
    """
    from .live.kill_switch import (
        kill_switch_timing_violation,
        network_timeout_warning,
        sl_repair_delay_warning,
    )

    violation = kill_switch_timing_violation(
        live_cfg.kill_switch, _RECOVERY_MAX_TICK_GAP_SECONDS, client.timeout
    )
    if violation is not None:
        print(
            f"error: {violation}. Raise live.kill_switch.schedule_cancel_seconds, "
            "lower live.kill_switch.refresh_interval_seconds, or lower "
            "network_timeout_s — the failed attempt's own timeout is part of the "
            "budget, so the 30s default does not fit a 120s scheduled cancel "
            "(RUNBOOK §1.5 uses 8).",
            file=sys.stderr,
        )
        return 1
    # Its advisory sister (decided 2026-07-22 "soft mitigation"): warn — do
    # not refuse — when the per-request REST timeout cannot keep the same
    # max_tick_gap promise. Beside the enforced check so the two halves of
    # the §18.2 timing story stay in one place.
    for advisory in (
        network_timeout_warning(client.timeout, _RECOVERY_MAX_TICK_GAP_SECONDS),
        sl_repair_delay_warning(
            float(live_cfg.protection.sl_repair_retry_delay_seconds),
            _RECOVERY_MAX_TICK_GAP_SECONDS,
        ),
    ):
        if advisory is not None:
            print(f"WARNING: {advisory}", file=sys.stderr)
    return 0


def _run_genesis_network(config_json: str | None) -> object | None:
    """``live.network`` from a run's genesis config, or None if unreadable.

    Same defensive shape as :func:`live.validation.execution_mode`: a corrupt or
    non-object ``config_json`` degrades to None rather than crashing a guard.

    NORMALISED through the same helper the drift report uses, because LiveConfig
    reads network case-insensitively: two runs whose genesis says "Testnet" and
    "testnet" are the same exchange and the same wallet. Comparing the raw
    strings made them look like different networks, sending the guard below
    fail-OPEN in the one direction its own docstring forbids — "the cost of a
    false pass is a stripped dead-man switch on a live wallet" (2026-07-31).
    """
    if not config_json:
        return None
    try:
        parsed = json.loads(config_json)
    except (ValueError, TypeError):
        return None
    live = parsed.get("live") if isinstance(parsed, dict) else None
    if isinstance(live, dict) and isinstance(live.get("network"), str):
        return _norm_network(live)
    return None


def _conflicting_run_lease(
    db, run_id: str, *, own_network: object | None = None
) -> tuple[str, int] | None:
    """``(run_id, pid)`` of a SAME-NETWORK sibling holding a FRESH lease, else None.

    The run lease is per-``run_id``; the kill switch, ``updateLeverage`` and the
    §19.3 sweep are per-WALLET. So "my run's lease is free" does not mean "no one
    else is on this wallet" (2026-07-30 concurrency review).

    But "same store" is NOT the same as "same wallet": RUNBOOK-live §7.3
    deliberately creates the mainnet_tiny run in the SAME ``live_trading.db`` as
    the testnet run, and those are different exchanges with different accounts —
    a testnet ``scheduleCancel`` cannot reach a mainnet order. Keying the refusal
    on the store alone told the operator to stop a real-money run in order to
    smoke-test testnet. It is keyed on the sibling's genesis ``live.network``
    instead (2026-07-31 exit check).

    Both networks are read from the STORE (each run's own genesis), not from the
    caller's session, so the guard is self-contained and cannot disagree with
    what the runs were actually created as.

    ``own_network`` overrides that read for the one caller that has no genesis to
    read yet: `live --create` must refuse BEFORE it writes the run row, or a
    refusal leaves a half-created run whose re-run is then rejected as "already
    exists" (2026-07-31 exit check). The sibling's network still comes from the
    store, so the comparison is never made against the caller's own idea of what
    the OTHER run is.

    Either side being UNREADABLE is treated as a conflict: a corrupt genesis is
    not evidence of safety, and the cost of a false refusal is one operator
    message, while the cost of a false pass is a stripped dead-man switch on a
    live wallet.
    """
    from .paper.run_lock import LOCK_STALE_SECONDS
    from .paper.scheduler import parse_instant
    from .persistence import repository as repo

    if own_network is None:
        own = repo.get_run(db.conn, run_id)
        own_network = None if own is None else _run_genesis_network(own["config_json"])
    now = datetime.now(timezone.utc)
    for row in repo.iter_other_run_leases(db.conn, run_id):
        age = (now - parse_instant(row["lock_heartbeat_at"])).total_seconds()
        if age >= LOCK_STALE_SECONDS:
            continue
        sibling = repo.get_run(db.conn, str(row["run_id"]))
        if sibling is not None and sibling["mode"] != "live":
            # A PAPER run. It signs nothing and holds no wallet, so it cannot be
            # harmed by an account-wide action and cannot take one. Checked
            # before the network comparison because a paper genesis has no
            # ``live`` block at all: it would read as an unreadable network and
            # be refused fail-closed, with a message about stripping a dead-man
            # cover it never had (2026-07-31).
            continue
        sibling_network = None if sibling is None else _run_genesis_network(sibling["config_json"])
        if (
            own_network is not None
            and sibling_network is not None
            and sibling_network != own_network
        ):
            # A different exchange entirely — its wallet cannot be touched by
            # anything this suite does.
            continue
        return str(row["run_id"]), int(row["lock_pid"])
    return None


def _still_owns_run(db, run_id: str, *, pid: int, now) -> bool:
    """Positively re-verify the lease before the §18.2 shutdown sweep.

    The ``superseded`` flag is absence-of-evidence: it is set only when the
    loop's heartbeat actually RAISED. A Ctrl-C / SIGTERM exits the loop through
    ``except KeyboardInterrupt`` with no heartbeat at all, so after a tick that
    blocked past ``LOCK_STALE_SECONDS`` a successor can already own the run
    while this process still reads ``superseded is False`` — and the sweep then
    cancels the successor's live SL/TP and clears the wallet's dead-man switch.
    Stopping a hung process with SIGTERM is precisely how an operator reaches
    that lane, so this asks the store instead of trusting the flag
    (2026-07-30 concurrency review).

    A transient store failure is not proof of supersession: the lease is most
    likely still ours and skipping the sweep would leave resting orders behind,
    so it is logged and treated as owned. Only ``RunLockError`` gives the run
    away.
    """
    from .paper import run_lock

    try:
        run_lock.heartbeat_run_lock(db, run_id, pid=pid, now=now)
    except run_lock.RunLockError:
        return False
    except Exception as exc:  # noqa: BLE001 — see docstring: not proof of supersession
        logger.warning(
            "could not re-verify the run lease before the §18.2 sweep (%s: %s) — "
            "proceeding as the owner",
            type(exc).__name__,
            exc,
        )
    return True


def _run_live_loop(
    *,
    cfgs,
    db,
    run_id: str,
    coin: str,
    config: dict,
    live_cfg,
    client,
    signed,
    gate,
    kill_switch,
    safe_mode,
    reconciler,
    processor,
    payload_dir: Path,
    fetch_clearinghouse,
) -> None:
    """The PR 5 live trading loop (§9/§11.4): tick the engine + pump the 4h cycle.

    Builds the execution engine, §17 protection manager, §10 loss guards and the
    off-thread decision worker/driver over the recovery components, then loops
    every ~10s (well inside the kill-switch tick budget). Returns on Ctrl-C /
    SIGTERM (SIGTERM is already mapped to KeyboardInterrupt by the caller); the
    caller's §18.2 shutdown sweep disarms the switch afterwards.

    v1 scope note: this wires a :class:`LiveWsStream` WITHOUT a live socket
    connection — fills are ingested by the reconciler's REST backfill at the
    implemented §12.2 timings (post-fill / 5-minute heartbeat /
    protection-change / pre-shutdown) rather than in real time. The live WS
    connection wiring lands in a later pass (§11 / PR 6).

    A ``RunLockError`` from the lease heartbeat propagates OUT of this function
    by design: the caller exits without the §18.2 sweep (the successor process
    owns the run's orders — see the caller's ``except RunLockError``).
    """

    from .exchanges.hyperliquid.market_data import HyperliquidMarketData
    from .live.decision import LiveDecisionDriver, LiveDecisionWorker
    from .live.engine import LiveExecutionEngine
    from .live.kill_switch import refresh_across_blocking_work
    from .live.loss_guards import LossGuards
    from .live.orders import LiveOrderSubmitter
    from .live.protection import ProtectionManager
    from .live.ws_stream import LiveWsStream
    from .paper.clock import WallClock
    from .paper.engine import AssetSpec
    from .paper.market_feed import PortSnapshotProvider
    from .paper.stops import StopConfig
    from .persistence import repository as repo

    # ``cfgs`` was validated by _cmd_live's front gate (decided 2026-07-22): a
    # bad risk:/decision:/paper_trading: block is an exit-1 up front, so this
    # function can no longer be reached with an unusable grid and silently
    # skip the loop behind a passing recovery's exit 0.
    risk_cfg, decision_cfg = cfgs
    clock = WallClock()
    market = HyperliquidMarketData(client)
    sz_decimals, schedule = market.get_asset_meta(coin)
    asset = AssetSpec(coin=coin, sz_decimals=sz_decimals, margin_schedule=schedule)
    provider = PortSnapshotProvider(market, clock)
    submitter = LiveOrderSubmitter(
        client=signed, gate=gate, db=db, run_id=run_id, payload_dir=payload_dir, clock=clock
    )
    protection = ProtectionManager(
        db=db,
        run_id=run_id,
        coin=coin,
        client=signed,
        gate=gate,
        tick_size=asset.tick_size,
        qty_step=asset.qty_step,
        stop_config=StopConfig(),
        max_slippage_pct=live_cfg.execution.max_slippage_pct,
        protection_config=live_cfg.protection,
        owner_prefix=live_cfg.order_owner_prefix,
        clock=clock,
        kill_switch=kill_switch,
    )

    loss_guards = LossGuards(
        db=db,
        run_id=run_id,
        safety=live_cfg.safety,
        safe_mode=safe_mode,
        # §10.3 rule 1: the UTC-day baseline is the EXCHANGE's reconciled
        # accountValue, fetched once at each day roll (per-tick drawdown
        # evaluation stays on the local ledger — zero extra REST per tick).
        #
        # Rare, but it is a bare full-timeout REST call landing between the
        # protection sync and the slice submits, so it refreshes across itself
        # like every other blocking read on this thread (§18.2).
        #
        # Bound with partial rather than a lambda ON PURPOSE: the first version
        # wrapped a zero-arg closure in ``lambda: helper(kill_switch)``, so every
        # day roll raised TypeError, LossGuards' except-Exception swallowed it,
        # and the baseline silently fell back to the local ledger — defeating the
        # whole point of §10.3 rule 1, with nothing on any surface to say so.
        # partial binds the arguments where they are declared, so an arity
        # mismatch cannot be written here (2026-08-01 incremental review).
        day_baseline_source=partial(_day_baseline_from_exchange, fetch_clearinghouse, kill_switch),
    )
    ledger = repo.get_current_account_state(db.conn, run_id)
    if ledger is not None:
        loss_guards.ensure_settlement_anchor(ledger.wallet_balance, now=clock.now())
    ws_stream = LiveWsStream()
    engine = LiveExecutionEngine(
        db=db,
        run_id=run_id,
        asset=asset,
        live_config=live_cfg,
        risk_config=risk_cfg,
        decision_config=decision_cfg,
        provider=provider,
        submitter=submitter,
        gate=gate,
        kill_switch=kill_switch,
        safe_mode=safe_mode,
        reconciler=reconciler,
        protection=protection,
        loss_guards=loss_guards,
        fill_processor=processor,
        ws_stream=ws_stream,
        fetch_open_orders=signed.open_orders,
        clock=clock,
    )
    # §10.4: a flat reached while the process was down (an SL filled offline,
    # backfilled by startup recovery) never crosses _detect_settlement — score
    # that segment now, before the first tick, so the loss counter cannot merge
    # it into the next one.
    engine.settle_offline_flat()
    decision_provider = _EngineDecisionProvider(
        config,
        risk_cfg=risk_cfg,
        decision_cfg=decision_cfg,
        payload_dir=payload_dir,
        # build_input runs on THIS thread inside driver.pump(); its four market
        # reads are the longest back-to-back REST chain in the system. Refreshing
        # between them keeps the unrefreshed run at the submit chain's 3 instead
        # of 4, which is what lets the operator advisory stay at a ~10s timeout
        # rather than demanding 7.5s from a cycle that cannot retry.
        on_blocking_read=partial(
            refresh_across_blocking_work, kill_switch, what="decision market data"
        ),
    )
    worker = LiveDecisionWorker(provider=decision_provider)
    driver = LiveDecisionDriver(
        db=db,
        run_id=run_id,
        coin=coin,
        asset=asset,
        risk_config=risk_cfg,
        decision_config=decision_cfg,
        engine=engine,
        worker=worker,
        provider=decision_provider,
        clock=clock,
    )
    # §3.1: adopt a prior process's stranded in-progress decision (resume from
    # its stored response, or fail it closed) — without this the deterministic
    # attempt id collides every tick and the driver never decides again.
    adopted = driver.resume_startup()
    if adopted is not None:
        logger.info("decision driver startup adoption: %s", adopted)
    pid = os.getpid()
    print(
        f"live loop started for {run_id!r} ({live_cfg.mode.value}) — Ctrl-C to stop",
        file=sys.stderr,
    )
    try:
        while True:
            tick_started = time.monotonic()
            now = clock.now()
            _live_heartbeat(db, run_id, pid=pid, now=now, safe_mode=safe_mode)
            try:
                tick = engine.tick()
                # The seam between the two blocking halves of one iteration.
                # engine.tick() refreshes at its top and across its own blocking
                # work, but driver.pump() then runs _build_context ON THIS THREAD:
                # constructing the SDK client fetches perp meta, then snapshot,
                # candles and funding — four back-to-back REST calls on the same
                # network_timeout_s, with no refresh of their own. Without this
                # line the two chains are consecutive, so the run of unrefreshed
                # calls is their SUM, not the max the budget constant assumes
                # (2026-08-01 lifecycle review).
                refresh_across_blocking_work(kill_switch, what="decision pump")
                cycle = driver.pump()
                # Per-tick operator visibility: the live loop is otherwise silent
                # between the startup banner and whatever individual components
                # warn about (the paper loop logs its cycle events likewise). Only
                # a tick that DID something is logged, so an idle 10s cadence stays
                # quiet; the decision-cycle tag is logged whenever it advances.
                if tick.events or tick.slices_submitted or tick.fills_ingested:
                    logger.info(
                        "live tick %s: fills=%d slices=%d protection=%s events=%s",
                        tick.status.value,
                        tick.fills_ingested,
                        tick.slices_submitted,
                        None if tick.protection is None else tick.protection.value,
                        list(tick.events),
                    )
                if cycle is not None:
                    logger.info("live decision cycle: %s", cycle)
            except Exception:  # noqa: BLE001 — a tick error must not tear down the loop or strip SL/TP
                # A single transient tick failure (DB lock, a reconciler read, an
                # unexpected raise) must not propagate out to the caller's §18.2
                # shutdown sweep, which would cancel the resting SL/TP and leave the
                # position naked. Log, enter recoverable safe mode (new orders pause
                # until the next clean reconcile auto-releases), and keep ticking —
                # protection re-attempts next tick. KeyboardInterrupt is a
                # BaseException, so Ctrl-C / SIGTERM still reaches the handler below.
                _contain_as_recoverable_safe_mode(
                    safe_mode,
                    log_message="live tick raised — entering recoverable safe mode and continuing",
                    detail="live tick raised (see log)",
                )
            # The sleep DEDUCTS the tick's own wall time (decided 2026-07-22):
            # ``max_tick_gap_seconds`` is a promise about the wall clock BETWEEN
            # tick() calls, and a fixed sleep would stack on top of a slow tick —
            # a degraded (slow, not dead) network could then push the §18.2
            # refresh past the exchange-side deadline and cancel the resting
            # SL/TP. Deducting keeps the cadence near-constant; the residual
            # risk of a single call outlasting the gap is warned about at
            # startup and owned by PR 6's network-layer rework.
            time.sleep(max(0.0, _LIVE_TICK_SECONDS - (time.monotonic() - tick_started)))
    except KeyboardInterrupt:
        print("\nlive loop stopping — running the §18.2 shutdown sweep...", file=sys.stderr)
        # Let the off-thread AI decision settle so no worker thread writes to the
        # store after teardown begins (§11.4 single writer).
        worker.join(timeout=5.0)
        # A decision that finished during the shutdown window is a paid-for
        # answer only the next pump would have persisted — store its raw
        # response so resume_startup resumes it after restart (§3.1) instead
        # of failing the cycle closed and idling up to 4h. Fully contained:
        # shutdown proceeds on any failure.
        driver.salvage_shutdown()


# --------------------------------------------------------------------------
# live-smoke — the §20.2 testnet smoke checklist (PR 6)
# --------------------------------------------------------------------------


def _smoke_gate_buckets(
    missing: tuple[str, ...],
    failed: tuple[str, ...],
    errored: tuple[str, ...],
) -> list[tuple[str, tuple[str, ...]]]:
    """``(label, keys)`` pairs for the §20.2 gate's non-passed buckets.

    The one list BOTH renderings draw from (``live-smoke``'s per-line report and
    the ``--loop`` refusal's one-liner), so a future bucket cannot appear on one
    operator surface and not the other.
    """
    return [("not_yet_run", missing), ("failed", failed), ("errored", errored)]


def _print_smoke_gate(
    passed: bool,
    missing: tuple[str, ...],
    failed: tuple[str, ...],
    errored: tuple[str, ...],
) -> None:
    """Print the §20.2 cycle-entry gate verdict (stdout: the machine contract).

    The three non-passed buckets are printed separately so an operator triaging
    a real-money go/no-go sees whether a test never ran, the exchange refused it,
    or the harness itself broke — without querying live_smoke_tests.
    """
    print(f"smoke_gate_passed: {'yes' if passed else 'no'}")
    for label, keys in _smoke_gate_buckets(missing, failed, errored):
        if keys:
            print(f"{label}: {', '.join(keys)}")


def _cmd_live_smoke(argv: list[str]) -> int:
    """Run the §20.2 testnet smoke checklist and report the cycle-entry gate.

    Each of the 18 tests drives a real signed exchange action against testnet and
    records its verdict in ``live_smoke_tests``; the gate (§20.2: all pass) then
    lets ``live --loop`` start the testnet_live cycles. ``--gate-status`` reports
    the stored gate without touching the network; ``--dry-run`` validates the
    config and wiring and records every selected test ``skipped`` (places no
    orders — the offline check the unit tests exercise). A real run takes the
    run's lease first (refused with exit 1 while ``live``/``paper`` holds it) and,
    when the selection places probe orders, runs one passing §19.1 pre-flight
    recovery before the first test. Exit: 0 = the FULL §20.2 gate is open (every
    one of the 18 tests' latest real result is ``passed``) — so ``--only`` on a
    subset still exits 4 until the whole suite has passed — EXCEPT ``--dry-run``,
    which exits 0 once the wiring check completes (its gate is never open); 4 =
    ran (or read) but the gate is not satisfied, including a pre-flight recovery
    failure that aborted the suite before any test; 1 = a named config / env /
    network / lease error.

    The restart tests (15–17) EACH drive a real §19.1 startup recovery (three
    arms and three reconcile passes over the run, not one);
    the operator stages their preconditions (an existing position / a stale
    bot-owned order) on testnet before running the suite (docs/RUNBOOK-live.md).
    """
    parser = argparse.ArgumentParser(
        prog="python -m contrib.hyperliquid_perp live-smoke",
        description=(
            "Run the phase3-spec §20.2 testnet smoke checklist against a live run, "
            "record each verdict, and report the cycle-entry gate."
        ),
    )
    parser.add_argument("--config", default=None, help="Config YAML path.")
    parser.add_argument("--db", default="live_trading.db", help="SQLite store path for the run.")
    parser.add_argument(
        "--run-id", required=True, help="The live run to record smoke results under."
    )
    parser.add_argument(
        "--only",
        nargs="+",
        default=None,
        metavar="TEST_KEY",
        help="Run only these smoke-test keys (default: all 18, in canonical order).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate config + wiring and record every selected test skipped; place NO orders.",
    )
    parser.add_argument(
        "--gate-status",
        action="store_true",
        help="Print the §20.2 gate for the run from the store and exit (no network, no orders).",
    )
    args = parser.parse_args(argv)

    from .live.smoke import (
        SmokePreflightError,
        SmokeTestRunner,
        smoke_gate_report,
        validate_only_keys,
    )

    # Mutual exclusion BEFORE key validation: under --gate-status the --only
    # keys are never going to be used, so answering a typo in them names the
    # wrong mistake and sends the operator off correcting a key instead of
    # dropping the flag.
    if args.gate_status and (args.dry_run or args.only is not None):
        print(
            "error: --gate-status only reads the stored gate — drop --dry-run/--only.",
            file=sys.stderr,
        )
        return 1
    only: list[str] | None = None
    if args.only is not None:
        try:
            only = list(validate_only_keys(args.only))
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # --gate-status: a pure store read — no config, no network, no orders.
    if args.gate_status:
        db = _open_existing_db(args.db)
        if db is None:
            return 1
        with db:
            run_row = _existing_run_row(db.conn, args.run_id, args.db)
            if run_row is None:
                return 1
            if not _require_live_run_mode(
                run_row, args.run_id, args.db, extra="smoke results are live-run state"
            ):
                return 1
            # The §20.2 gate is testnet-only state: a mainnet_tiny run's
            # live_smoke_tests is empty BY DESIGN (§21.3 — smoke is proven on
            # the separate testnet run), so reporting its raw buckets would
            # print "not_yet_run: <all 18>" + exit 4 and read as "go smoke-test
            # mainnet" — the exact misreading `validate` renders as "n/a
            # (§21.3)". Refuse, mirroring the real-run testnet-only guard
            # (decision 2026-07-28).
            from .live.validation import execution_mode

            genesis_mode = execution_mode(run_row["config_json"])
            if genesis_mode != "testnet_live":
                print(
                    f"error: run {args.run_id!r} has genesis live.mode "
                    f"{genesis_mode!r} — the §20.2 smoke gate applies only to "
                    "testnet_live runs (a mainnet run relies on the smoke "
                    "proven on the separate testnet run, §21.3); there is no "
                    "smoke gate to report here.",
                    file=sys.stderr,
                )
                return 1
            passed, missing, failed, errored = smoke_gate_report(db.conn, args.run_id)
            _print_smoke_gate(passed, missing, failed, errored)
            return 0 if passed else 4

    # This command OWNS the db handle: opened here, closed in the finally no
    # matter how the build or the run fails (a raise, not just an int return) —
    # the try/finally makes the cleanup structural, not dependent on every
    # _build_* failure path returning an int (silent-failure review, 2026-07-27).
    # Schema policy splits on --dry-run, not on the command:
    #
    # * a dry run takes NO lease (see below) and touches only this store to check
    #   wiring, which makes it a reporting command by the same test `validate` and
    #   `--gate-status` are judged by. Migrating there was the sharpest remaining
    #   version of the hazard the reporting/owning split exists to close: the one
    #   command advertised as the safe offline check was the one that silently
    #   upgraded a store a running daemon owned.
    # * a real run does own the store — but it cannot take the lease until the
    #   store is open, so migrating AT open still upgraded the schema underneath a
    #   sibling daemon and only then reached the conflict check that refuses. It
    #   defers instead and migrates below, once it actually owns the run.
    db = _open_existing_db(args.db, defer_migration=not args.dry_run)
    if db is None:
        return 1
    preflight_error: str | None = None
    import signal

    from .paper.run_lock import RunLockError, acquire_run_lock, release_run_lock

    try:
        session = _build_smoke_session(args, db)
        if isinstance(session, int):
            return session
        lock_pid: int | None = None
        if not args.dry_run:
            # The suite places real orders and runs §19.1 recoveries — the same
            # actions the run lease exists to keep single-owner. A concurrent
            # `live --loop` on this run would race the probe orders, the
            # recovery's stale-order sweep, and the account-wide kill switch;
            # refuse instead (a dry run touches only this store, no lease needed).
            # The lease is per-run_id; the damage this suite can do is per-WALLET.
            # A sibling run in the same store shares the wallet, so its lease
            # would be acquired happily while this suite arms/clears the
            # ACCOUNT-WIDE kill switch, writes the account's leverage, and runs a
            # §19.3 sweep whose bot-ownership lookup is not run-scoped — i.e. it
            # cancels the sibling's resting orders. Refuse before any of that
            # (2026-07-30 concurrency review).
            conflict = _conflicting_run_lease(db, args.run_id)
            if conflict is not None:
                other_run, other_pid = conflict
                print(
                    f"error: run {other_run!r} in {args.db} is being driven by pid "
                    f"{other_pid} right now, on this same network. "
                    "The smoke suite's kill-switch arm/clear, updateLeverage and §19.3 "
                    "stale-order sweep are ACCOUNT-wide, not run-scoped, so running it "
                    "now would strip that run's dead-man cover and cancel its resting "
                    "orders. Stop that process, or wait for its lease to go stale. "
                    "Moving either run to a different --db does NOT help: the hazard "
                    "is per-WALLET and same-network runs share the wallet, so a "
                    "separate store only hides them from this check. A run on the "
                    "OTHER network is a different exchange and does not conflict.",
                    file=sys.stderr,
                )
                return 1
            try:
                acquire_run_lock(db, args.run_id, pid=os.getpid(), now=datetime.now(timezone.utc))
            except RunLockError as exc:
                print(
                    f"error: {exc} — the smoke suite places real orders and runs "
                    "recoveries on this run; stop that process (or wait for its "
                    "lease to expire) first.",
                    file=sys.stderr,
                )
                return 1
            lock_pid = os.getpid()
            # The same handler `live` (cli.py) and `paper` install right after
            # their own lock. Without it, `kill <pid>` — systemd's and docker's
            # default, and what a `timeout` wrapper sends — kills this process
            # outright: runner.run()'s finally never runs, so the staged long is
            # left open, the probes are left resting, the account-wide kill
            # switch is left armed, the two operator WARNINGs below are never
            # printed, and the lease is left held for LOCK_STALE_SECONDS. This
            # command needs it MORE than its two siblings, not less: they have a
            # next tick and the §18.2 shutdown sweep behind them, while this
            # finally is the only cleanup that exists (2026-07-31).
            signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)
        try:
            if lock_pid is not None:
                # The lease is ours: NOW the schema upgrade is safe, because no
                # sibling can be mid-write against the old one. Deferred from
                # open (see _open_existing_db) so a refusal above cannot leave a
                # migrated store behind as its only lasting effect.
                #
                # Deferring also moved the "migrated by a NEWER build" refusal
                # here, so it has to be caught: uncaught it reached main()'s
                # last-resort handler as exit 2 ("fatal: unexpected error"),
                # losing the named exit 1 the RUNBOOK documents and a supervisor
                # branches on — the very failure the sibling commit was fixing.
                # Inside the lease-releasing try, not before it. A `return 1`
                # taken above the block that owns `finally: release_run_lock`
                # left the lease stamped with this now-dead pid for the full
                # LOCK_STALE_SECONDS, so the operator's corrected re-run was
                # refused for 15 minutes by a message naming a process that no
                # longer exists (2026-07-31 exit check).
                try:
                    apply_migrations(db.conn)
                except SchemaVersionError as exc:
                    print(f"error: {exc}", file=sys.stderr)
                    return 1
            runner = SmokeTestRunner(session)
            try:
                try:
                    runner.run(only=only)
                except SmokePreflightError as exc:
                    # No test executed, no verdict recorded; the exit disarm has
                    # already run inside runner.run()'s finally.
                    preflight_error = str(exc)
                except RunLockError as exc:
                    # The per-test heartbeat found this process superseded: a
                    # successor legitimately took over the stale lease and now owns
                    # the run AND the wallet's kill switch (the runner suppressed
                    # its exit disarm for exactly that reason). Completed verdicts
                    # are durable; stop by name.
                    print(
                        f"error: {exc} — the run lease was superseded mid-suite; the "
                        "suite stopped and left the account-wide kill switch to the "
                        "new owner. Re-run live-smoke once this run has a single owner.",
                        file=sys.stderr,
                    )
                    return 1
                passed, missing, failed, errored = smoke_gate_report(db.conn, args.run_id)
            finally:
                # In a ``finally`` on purpose: the disarm runs inside
                # runner.run()'s own finally on EVERY exit path, so its failure
                # flag must be surfaced on every path too — an unexpected
                # mid-suite exception (a wire/store error escaping to main()'s
                # generic handler) is exactly the situation most likely to
                # co-occur with a failed disarm, and the operator must not lose
                # this warning under that stack trace (silent-failure review,
                # 2026-07-29).
                if runner.kill_switch_disarm_failed:
                    print(
                        "WARNING: the end-of-suite kill-switch disarm FAILED — the "
                        "wallet may still hold an armed scheduleCancel that will "
                        "cancel every resting order (including any SL/TP) at its "
                        "deadline. Verify and clear it manually, or run `live "
                        "--run-id ...` without --loop (it re-arms, sweeps, then "
                        "disarms on a clean exit). --loop is NOT the remedy here: a "
                        "suite whose disarm failed usually has red tests, so the "
                        "§20.2 gate is shut and --loop exits 4 before arming.",
                        file=sys.stderr,
                    )
                if runner.staged_long_residual is not None:
                    # The trigger block's staged long closes BETWEEN tests, so a
                    # failed close has no step row to land in. Say it here or a
                    # real funded position is left on the wire with only a log
                    # line to show for it (review round 2026-07-29).
                    print(
                        "WARNING: the trigger-block staging position may still be "
                        f"OPEN — {runner.staged_long_residual}. Close it manually, then use a "
                        "NEW run-id for acceptance: a post-genesis manual fill has "
                        "no local order row, so it books fill_unmapped and pins "
                        "this run's validate at exit 5. Re-running the suite does "
                        "NOT flatten this residual — a fresh runner only closes the "
                        "staging long it opened itself. Note the new run-id starts "
                        "with an EMPTY §20.2 gate: re-run the full live-smoke suite "
                        "under it (including the operator-staged preconditions for "
                        "tests 16/17) or `live --loop` exits 4 with all 18 not_yet_run.",
                        file=sys.stderr,
                    )
                if runner.position_residuals:
                    # Accidental fills (a far IOC the book crossed anyway, a
                    # slice that filled before its sibling aborted) whose
                    # cleanup did not fully succeed. The staging long has had a
                    # warning since 2026-07-29; these are the same thing —
                    # a real funded position — and used to be visible only in
                    # the detail column of a PASSED row.
                    for note in runner.position_residuals:
                        print(
                            f"WARNING: a probe position may still be OPEN — {note}. "
                            "Close it manually, then use a NEW run-id for acceptance: "
                            "a manual fill has no local order row, so it books "
                            "fill_unmapped and pins this run's validate at exit 5. "
                            "The new run-id starts with an EMPTY §20.2 gate — re-run "
                            "the full live-smoke suite under it, or `live --loop` "
                            "exits 4 with all 18 tests not_yet_run.",
                            file=sys.stderr,
                        )
                if runner.probe_residual is not None:
                    # Same reasoning as the staging long, different artefact: a
                    # trigger probe books no orders row, so the ONLY signal that
                    # one was stranded used to be the detail column of a GREEN
                    # row. It is not cosmetic — see the runner's note for why it
                    # ends in a permanent exit 5 (2026-07-31 lifecycle review).
                    print(
                        f"WARNING: {runner.probe_residual}.",
                        file=sys.stderr,
                    )
        finally:
            if lock_pid is not None:
                release_run_lock(db, args.run_id, pid=lock_pid, now=datetime.now(timezone.utc))
    finally:
        db.close()
    _print_smoke_gate(passed, missing, failed, errored)
    if preflight_error is not None:
        print(f"error: {preflight_error}", file=sys.stderr)
        return 4
    if args.dry_run:
        # A dry run places nothing, so the gate can never pass — that is the
        # point (a wiring check, not a cycle-entry proof). Exit 0 to signal the
        # wiring check itself completed; the operator reads "smoke_gate_passed:
        # no" and knows a real run is still required.
        print("dry-run complete — no orders placed; run without --dry-run for the real gate.")
        return 0
    return 0 if passed else 4


def _build_smoke_session(args, db):
    """Build the :class:`~.live.smoke.SmokeContext` for a real or dry run.

    ``db`` is the caller-owned, already-open store (the caller closes it), so
    every failure path here just returns an ``int`` exit code. A dry run stops
    after config validation (it needs no network): the context carries no signed
    client (``signed=None``) and market seams the runner never calls, so
    ``--dry-run`` works fully offline.
    """
    from decimal import Decimal

    from .domains.perp.risk_gate import RiskConfig
    from .live.config import ExecutionMode, LiveConfig, validate_live_risk_consistency
    from .live.smoke import SmokeContext
    from .paper.clock import WallClock

    try:
        config = load_config(args.config)
    except CONFIG_LOAD_ERRORS as exc:
        print(f"error: invalid config — {exc}. Fix the YAML and re-run.", file=sys.stderr)
        return 1
    raw_live = config.get("live")
    if raw_live is None:
        print(
            "error: config has no live: block — live-smoke needs one (phase3-spec §4).",
            file=sys.stderr,
        )
        return 1
    try:
        live_cfg = LiveConfig.from_dict(raw_live)
    except ValueError as exc:
        print(f"error: invalid live: config — {exc}. Fix the YAML and re-run.", file=sys.stderr)
        return 1
    if live_cfg.mode is ExecutionMode.PAPER:
        print("error: live.mode is 'paper' — the smoke suite is a live-mode tool.", file=sys.stderr)
        return 1
    if live_cfg.mode is not ExecutionMode.TESTNET_LIVE:
        # The §20.2 smoke suite is a TESTNET pre-flight: it opens/closes real
        # positions and rests/cancels real SL/TP triggers. mainnet_tiny relies on
        # the smoke proven on the SEPARATE testnet run (§21.3) and is never
        # smoke-tested on mainnet — refuse here (both --dry-run and real) so a
        # mis-pointed config can never drive a real order onto mainnet.
        print(
            f"error: live-smoke runs only against a testnet_live run — live.mode is "
            f"'{live_cfg.mode.value}'. mainnet_tiny relies on the smoke suite proven on "
            "the separate testnet run (§21.3); it is never smoke-tested on mainnet.",
            file=sys.stderr,
        )
        return 1
    raw_risk = config.get("risk")
    if raw_risk is None:
        print(
            "error: config has no risk: block — required for the live gate (§24).", file=sys.stderr
        )
        return 1
    try:
        risk_cfg = RiskConfig.from_dict(raw_risk)
        validate_live_risk_consistency(live_cfg, risk_cfg, raw_risk)
    except ValueError as exc:
        print(f"error: invalid risk:/live: config — {exc}.", file=sys.stderr)
        return 1

    coin = live_cfg.safety.allowed_symbols[0]
    clock = WallClock()
    # The db is owned and closed by the caller (_cmd_live_smoke's try/finally),
    # so every error path here just returns an int — no close needed.
    not_found_hint = f" — create it first with `live --run-id {args.run_id} --create`."
    run_row = _existing_run_row(db.conn, args.run_id, args.db, not_found_hint=not_found_hint)
    if run_row is None:
        return 1
    if not _require_live_run_mode(run_row, args.run_id, args.db):
        return 1
    # Same identity discipline as `live` resume (decision 2026-07-28): the suite
    # runs a real §19.1 recovery and books probe orders ON this run, so a typo'd
    # --run-id pointing at another live run in the same store — the §21.4
    # mainnet acceptance run lives in the same default db — would reconcile the
    # TESTNET exchange against that run's ledger and file integrity cases that
    # the §5 cumulative policy makes permanent. coin / live.network drift is a
    # hard refusal (the mainnet run trips the network check); parameter drift
    # warns, as on resume.
    drift = _config_drift_report(run_row["config_json"], config, coin)
    if drift is not None:
        kind, message = drift
        if kind in _HARD_DRIFT_KINDS:
            print(f"error: {message}", file=sys.stderr)
            return 1
        print(f"WARNING: {message}", file=sys.stderr)

    if args.dry_run:
        # No network: signed stays None and the seams below are never called.
        def _unavailable() -> Decimal:
            raise RuntimeError("dry-run places no orders; mark_price is not fetched")

        return SmokeContext(
            signed=None,
            db=db,
            run_id=args.run_id,
            coin=coin,
            network=live_cfg.network,
            payload_dir=Path(args.db).resolve().parent / "payloads" / args.run_id,
            owner_prefix=live_cfg.order_owner_prefix,
            mark_price=_unavailable,
            qty_step=Decimal(1),
            tick_size=Decimal(1),
            now=clock.now,
            dry_run=True,
            run_recovery=None,
        )

    return _build_real_smoke_session(
        args, config=config, live_cfg=live_cfg, coin=coin, clock=clock, db=db
    )


def _build_real_smoke_session(args, *, config, live_cfg, coin, clock, db):
    """The network half of :func:`_build_smoke_session` (real, order-placing).

    Verifies the §6.1 agent authorization, builds the runtime-armed signed
    client, reads the asset meta and mark, and wires the ``run_recovery`` seam
    to one real §19.1 startup recovery over the run. Returns the context or an
    ``int`` exit code.
    """

    from .config import wallet_address
    from .exchanges.hyperliquid.errors import ExchangeError
    from .exchanges.hyperliquid.market_data import HyperliquidMarketData
    from .exchanges.hyperliquid.sdk_client import HyperliquidClient
    from .exchanges.hyperliquid.signed_client import HyperliquidSignedClient
    from .live.authorization import AgentAuthorizationError, verify_agent_authorization
    from .live.order_gate import RealOrderGate
    from .live.secrets import agent_key_env_var, load_agent_key
    from .live.smoke import SmokeContext
    from .paper.engine import AssetSpec

    if not live_cfg.allow_real_orders:
        print(
            "error: live.allow_real_orders is false — the smoke suite places real "
            "signed orders on testnet. Enable it (with the agent key), or use "
            "--dry-run for an offline wiring check.",
            file=sys.stderr,
        )
        return 1
    addr = wallet_address(config)
    if not addr:
        print(
            "error: wallet_address is not configured (needed for the smoke suite).", file=sys.stderr
        )
        return 1
    agent_key = load_agent_key(live_cfg.network)
    if agent_key is None:
        env_var = agent_key_env_var(live_cfg.network)
        print(
            f"error: {env_var} is not set — the smoke suite signs real testnet orders. "
            f"Export the {live_cfg.network} agent key, or use --dry-run.",
            file=sys.stderr,
        )
        return 1
    try:
        client = HyperliquidClient.from_config(config, network=live_cfg.network)
    except ExchangeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        verify_agent_authorization(client.info, wallet_address=addr, agent_key=agent_key)
    except (AgentAuthorizationError, ExchangeError) as exc:
        print(f"error: agent authorization failed — {exc}", file=sys.stderr)
        return 1

    # The SAME preflight `live` runs, so one bad config gets one exit code and
    # one remedy. Without it the violation surfaced from KillSwitchManager's
    # constructor as a contained SmokePreflightError → exit 4, which RUNBOOK §5
    # answers with "check the run state" — the wrong investigation, and a
    # supervisor branching on 1-vs-4 mis-routes it too. Needs `client` for the
    # timeout advisory, so it sits here rather than with the config checks.
    if _timing_preflight(live_cfg, client) != 0:
        return 1

    gate = RealOrderGate.from_config(live_cfg)
    gate.agent_authorized = True
    signed = HyperliquidSignedClient(
        live_cfg.network, agent_key, wallet_address=addr, gate=gate, timeout=client.timeout
    )
    try:
        signed.health_check()
        market = HyperliquidMarketData(client)
        sz_decimals, schedule = market.get_asset_meta(coin)
    except ExchangeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    asset = AssetSpec(coin=coin, sz_decimals=sz_decimals, margin_schedule=schedule)

    def _mark() -> Decimal:
        return market.get_market_snapshot(coin).mark_price

    payload_dir = Path(args.db).resolve().parent / "payloads" / args.run_id

    def _run_recovery():
        return _smoke_startup_recovery(
            db=db,
            run_id=args.run_id,
            coin=coin,
            live_cfg=live_cfg,
            signed=signed,
            gate=gate,
            client=client,
            wallet=addr,
            payload_dir=payload_dir,
        )

    def _heartbeat() -> None:
        # Keeps the lease _cmd_live_smoke acquired fresh across the suite;
        # raises RunLockError once superseded (the runner aborts on it).
        from .paper.run_lock import heartbeat_run_lock

        heartbeat_run_lock(db, args.run_id, pid=os.getpid(), now=datetime.now(timezone.utc))

    return SmokeContext(
        signed=signed,
        db=db,
        run_id=args.run_id,
        coin=coin,
        network=live_cfg.network,
        payload_dir=payload_dir,
        owner_prefix=live_cfg.order_owner_prefix,
        mark_price=_mark,
        qty_step=asset.qty_step,
        tick_size=asset.tick_size,
        now=clock.now,
        dry_run=False,
        # The regime smoke test 2 writes is the RUN'S OWN (§4 live.safety) —
        # the suite must never leave the account in a leverage/margin mode the
        # config did not declare (decision 2026-07-29).
        leverage=int(live_cfg.safety.leverage),
        is_cross=live_cfg.safety.margin_mode.value == "cross",
        # The run's OWN §18 deadline, not the dataclass default: the pre-flight
        # recovery arms the switch from live.kill_switch.schedule_cancel_seconds,
        # and the suite's per-test refresh then overwrites that deadline. Leaving
        # it at 120s silently NARROWED a longer configured cover to 120s for the
        # whole suite — so a test making a few round-trips on a slow network
        # could let the switch fire and cancel the resting probe, exactly the
        # failure the refresh exists to prevent (2026-07-30 concurrency review).
        # ...but a FLOOR, not a plain hand-over. The config invariant only
        # requires schedule_cancel > 5s and >= 2x refresh_interval, both judged
        # against `live`'s 30s tick model — while the suite refreshes once per
        # TEST, and a test is a place/poll/cancel round-trip with no upper bound.
        # So a config that is perfectly legal for the daemon (say 40s/5s) would
        # hand the suite a 40s cover, narrower than the 120s it had before the
        # value was wired through at all, and the switch could fire mid-test and
        # cancel the resting probe — recorded as "the exchange refused", which
        # sends the operator to check config and market state rather than the
        # clock. max() keeps 3ae0087's intent (a LONGER cover is honoured) while
        # never going below what the suite used to guarantee (2026-07-31).
        kill_switch_deadline=max(
            timedelta(seconds=live_cfg.kill_switch.schedule_cancel_seconds),
            _SMOKE_MIN_KILL_SWITCH_DEADLINE,
        ),
        run_recovery=_run_recovery,
        heartbeat=_heartbeat,
    )


def _smoke_startup_recovery(
    *, db, run_id, coin, live_cfg, signed, gate, client, wallet, payload_dir
):
    """One real §19.1 startup recovery for the restart smoke tests (15–17).

    Builds the same recovery components ``live --run-id`` does and returns the
    :class:`~.live.startup.StartupResult`. It arms the kill switch and reconciles
    the run against the exchange — exactly the restart-reconciliation the smoke
    tests assert is clean given the operator-staged preconditions.
    """
    from .exchanges.hyperliquid.sdk_client import call_sdk
    from .live.fill_backfill import FillBackfiller
    from .live.fills import LiveFillProcessor
    from .live.kill_switch import KillSwitchManager, refresh_across_blocking_work
    from .live.reconcile import LiveReconciler
    from .live.safe_mode import SafeModeManager
    from .live.startup import run_startup_recovery

    def fetch_clearinghouse():
        return call_sdk(client.info.user_state, wallet)

    kill_switch = KillSwitchManager(
        client=signed,
        gate=gate,
        db=db,
        run_id=run_id,
        config=live_cfg.kill_switch,
        max_tick_gap_seconds=_RECOVERY_MAX_TICK_GAP_SECONDS,
        network_timeout_s=signed.timeout,
        payload_dir=payload_dir,
        # This manager belongs to a live-smoke run: its arm and its tick-driven
        # refreshes happen inside the suite, so they are cover but not evidence
        # that the DAEMON exercised the switch — the marker drops these rows
        # from the §20.3 sample floor AND from the clean-shutdown daemon
        # verdict alike.
        suite_authored=True,
    )
    safe_mode = SafeModeManager(db=db, run_id=run_id, gate=gate)
    processor = LiveFillProcessor(
        db=db,
        run_id=run_id,
        payload_dir=payload_dir,
        wallet_address=signed.wallet_address,
    )

    # §18.2: the same wiring as the live loop's, and for the same reason — this
    # path ARMS the switch (it is handed to run_startup_recovery below) under the
    # same _RECOVERY_MAX_TICK_GAP_SECONDS, so its sweep can lapse the deadline
    # and cancel the wallet mid-recovery exactly as the live one can. Wiring only
    # the live lane would have left the smoke restart tests (15–17) running the
    # unrefreshed version of the very sweep they exist to exercise
    # (2026-07-31 deadline review).
    def _refresh_across_sweep() -> None:
        refresh_across_blocking_work(kill_switch, what="reconciliation")

    backfiller = FillBackfiller(
        fetch=signed.user_fills_by_time,
        processor=processor,
        refresh_kill_switch=_refresh_across_sweep,
    )
    reconciler = LiveReconciler(
        db=db,
        run_id=run_id,
        coin=coin,
        fetch_open_orders=signed.open_orders,
        fetch_clearinghouse=fetch_clearinghouse,
        query_order_by_cloid=signed.query_order_by_cloid,
        fetch_fills=signed.user_fills_by_time,
        backfiller=backfiller,
        payload_dir=payload_dir,
        refresh_kill_switch=_refresh_across_sweep,
    )
    return run_startup_recovery(
        db=db,
        run_id=run_id,
        client=signed,
        fetch_clearinghouse=fetch_clearinghouse,
        gate=gate,
        kill_switch=kill_switch,
        reconciler=reconciler,
        safe_mode=safe_mode,
        payload_dir=payload_dir,
    )


# --------------------------------------------------------------------------
# paper — the long-running run
# --------------------------------------------------------------------------


# Every behaviour-defining block the resume drift check compares — the single
# source shared by the genesis record and the comparison loop, so a new key
# can't be recorded but silently never drift-checked (or vice versa).
_DRIFT_COMPARED_KEYS = ("risk", "decision", "paper_trading", "engine", "market_data", "indicators")


def _run_config_subset(config: dict, coin: str) -> dict:
    """The behaviour-defining blocks recorded at run genesis (never network/wallet).

    ``engine`` (model/analysts), ``market_data`` (candle window feeding the
    context), and ``indicators`` (signal set + warm-up gate) are here because
    each redefines every subsequent decision at least as much as a
    risk-parameter tweak does; ``paper_trading`` is stored whole for the audit
    record even though only its ``execution`` sub-block is compared on resume
    (``account`` is genesis-only).
    """
    subset: dict = {key: config.get(key) for key in _DRIFT_COMPARED_KEYS}
    subset["coin"] = coin
    return subset


# Genesis records written before these keys joined the subset lack them;
# absence there means "unknown", not "was empty" — skip the comparison rather
# than false-flag every pre-upgrade run whose config carries the block today.
_DRIFT_KEYS_ADDED_LATER = frozenset({"engine", "market_data", "indicators"})


def _resume_effective(key: str, block: object) -> object:
    """Project a stored/current config block onto what actually applies on resume.

    ``paper_trading.account`` (initial balance, seed positions) is consumed
    only at run genesis — a resume-time edit there changes nothing, so it must
    not trip the "behaviour changes from here on" warning. Everything else
    applies as-is.
    """
    if key == "paper_trading" and isinstance(block, dict):
        return block.get("execution")
    return block


def _norm_network(live_block: dict) -> object:
    """``live.network`` normalised the way LiveConfig reads it (case-insensitive)."""
    net = live_block.get("network")
    return net.strip().lower() if isinstance(net, str) else net


# The drift kinds that are run IDENTITY (a different instrument or a different
# exchange). Every entry point that resumes/targets an existing run refuses
# these with exit 1; any other kind is parameter drift and only warns. One
# shared datum so the three call sites (paper resume, live resume, live-smoke)
# can never diverge on what counts as hard.
_HARD_DRIFT_KINDS = frozenset({"coin", "network"})


def _config_drift_report(
    stored_json: str | None, config: dict, coin: str
) -> tuple[str, str] | None:
    """Compare today's config against the run's genesis record.

    Returns ``("coin", msg)`` for a coin mismatch or ``("network", msg)`` for a
    ``live.network`` mismatch (both hard errors — a different instrument or a
    different exchange is a different run, not a resumption), ``("params",
    msg)`` for risk/decision/paper_trading(execution)/engine/market_data/
    indicators / non-network ``live:`` drift (warning — behaviour changes
    mid-run but the operator may intend it; genesis-only
    ``paper_trading.account`` edits are inert on resume and don't warn, and a
    genesis record predating a key in ``_DRIFT_KEYS_ADDED_LATER`` skips that
    comparison rather than false-flagging), or ``None`` when nothing drifted or
    no record exists (a pre-drift-check store). A genesis record this process
    cannot parse also reports as ``("params", ...)``: the homogeneity check
    became impossible, which is breadcrumb-grade — never a startup abort (that
    would fire before the protection-only fork, leaving a live position
    unwatched). The ``live:`` checks fire only for records that stored the
    block (a live run's genesis — a paper run never stores it), so paper
    resumes reach neither the network hard-fail nor the live-block warning.
    """
    if not stored_json:
        return None
    try:
        stored = json.loads(stored_json)
    except ValueError as exc:
        return ("params", f"could not verify config drift (corrupt stored config_json: {exc})")
    if not isinstance(stored, dict):
        return (
            "params",
            "could not verify config drift (corrupt stored config_json: not an object)",
        )
    # Round-trip today's subset through JSON so both sides compare in the same
    # serialized shape (default=str stringifies any non-JSON scalar).
    current = json.loads(json.dumps(_run_config_subset(config, coin), default=str))
    if stored.get("coin") != current.get("coin"):
        return (
            "coin",
            f"run was created for coin {stored.get('coin')!r} but this resume "
            f"targets {coin!r} — refusing to continue a run on a different "
            "instrument (use a new --run-id).",
        )
    # live.network is run IDENTITY, like coin (decided 2026-07-17): a
    # testnet↔mainnet swap on resume would arm the wallet-wide kill switch and
    # reconcile the WRONG exchange against this ledger — every position/equity
    # leg mismatches and the operator sees "reconciliation mismatch" instead of
    # the true cause. The live create path stores the whole ``live:`` block; a
    # paper run never does, so ``stored_live`` is absent there and the check
    # is skipped. current_live is round-tripped through JSON to match the
    # stored block's serialized shape.
    stored_live = stored.get("live")
    raw_current_live = config.get("live")
    current_live = (
        json.loads(json.dumps(raw_current_live, default=str))
        if isinstance(raw_current_live, dict)
        else None
    )
    both_have_live_block = isinstance(stored_live, dict) and isinstance(current_live, dict)
    if both_have_live_block and _norm_network(stored_live) != _norm_network(current_live):
        return (
            "network",
            f"run was created against live.network {_norm_network(stored_live)!r} "
            f"but this resume targets {_norm_network(current_live)!r} — refusing to "
            "arm the kill switch and reconcile a different exchange (use a new "
            "--run-id).",
        )
    drifted = sorted(
        key
        for key in _DRIFT_COMPARED_KEYS
        if not (key in _DRIFT_KEYS_ADDED_LATER and key not in stored)
        and _resume_effective(key, stored.get(key)) != _resume_effective(key, current.get(key))
    )
    # Non-network ``live:`` drift is a warning (network already hard-failed
    # above): safety caps, kill-switch timings, allow_real_orders wiring etc.
    # redefine behaviour mid-run and the operator should be told, even though
    # they may intend it. Compared with network excluded so an equal-network
    # block that changed elsewhere still surfaces.
    if both_have_live_block:
        stored_live_rest = {k: v for k, v in stored_live.items() if k != "network"}
        current_live_rest = {k: v for k, v in current_live.items() if k != "network"}
        if stored_live_rest != current_live_rest:
            drifted = sorted([*drifted, "live"])
    if drifted:
        return (
            "params",
            f"config drift on resume: {', '.join(drifted)} differ from the values "
            "recorded at run creation — this run's behaviour changes from here on.",
        )
    return None


def _require_api_key() -> bool:
    """True when OPENROUTER_API_KEY is set; else print the abort message.

    Checked only on paths that will actually drive the AI engine — a fresh paper
    run (always, before the run row is written), a healthy paper restart with
    nothing live to protect, and ``live --loop`` (up front, alongside its config
    validation). A paper restart into protection-only mode never polls the AI,
    so it runs keyless — and a keyless healthy restart holding live work falls
    back to that same mode rather than exiting (the caller owns that fork:
    reconcile has already canceled the plans, so exiting would leave the
    position with nobody watching its SL/TP). ``live`` WITHOUT ``--loop`` is the
    same keyless case: it arms, sweeps and exits without ever polling the AI.
    """
    if os.environ.get("OPENROUTER_API_KEY"):
        return True
    print(
        "error: OPENROUTER_API_KEY is not set — the run drives the AI engine every "
        "4h, and without a key every cycle records api_failed (which never counts "
        "toward the §20.3 cycle gate). Use --context-only (legacy CLI) for a "
        f"keyless dev loop. ({dotenv_diagnosis('OPENROUTER_API_KEY')}.)",
        file=sys.stderr,
    )
    return False


def _cmd_paper(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m contrib.hyperliquid_perp paper",
        description="Long-running Phase 2 paper run: 4h AI cycles + 30s paper execution.",
    )
    parser.add_argument("--coin", default=None, help="Coin symbol, e.g. BTC.")
    parser.add_argument("--config", default=None, help="Config YAML path.")
    parser.add_argument("--db", default="paper_trading.db", help="SQLite store path.")
    parser.add_argument(
        "--run-id",
        default=None,
        help="Run id; defaults to paper-<COIN>. Reuse the same id to resume a run.",
    )
    parser.add_argument(
        "--export-dir",
        default=None,
        help="CSV export directory; defaults to <db dir>/exports/<run_id>.",
    )
    parser.add_argument(
        "--create",
        action="store_true",
        help=(
            "Allow creating a new store/run. Without it, a missing DB file or an "
            "unknown run_id is an error — so a wrong working directory or a typo'd "
            "path can never silently fork a run's history into a fresh database."
        ),
    )
    args = parser.parse_args(argv)

    # The one multi-day daemon in this module: its ERROR/WARNING diagnostics
    # (corrupt funding rows, snapshot write failures, funding-history
    # escalations) need timestamps and logger names for a post-mortem, and its
    # INFO trail (e.g. which plans a restart canceled) must not be dropped.
    # basicConfig no-ops when an embedding application already installed
    # handlers; the single-shot subcommands (export/validate/legacy) stay
    # unconfigured on purpose.
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Checked before any network/key work: a wrong CWD or typo'd --db must be
    # an error, not a silently forked fresh store (and it needs no credentials
    # to diagnose).
    db_path = Path(args.db)
    if not db_path.exists() and not args.create:
        print(
            f"error: database {str(db_path)!r} does not exist. Pass --create to "
            "start a new store, or point --db at the existing one.",
            file=sys.stderr,
        )
        return 1

    import signal

    # Heavy/engine imports deferred so `export`/`validate` stay light (the
    # engine/scheduler stack is imported by _run_locked once the lease is in
    # hand).
    from .exchanges.hyperliquid.errors import ExchangeError
    from .exchanges.hyperliquid.market_data import HyperliquidMarketData
    from .exchanges.hyperliquid.sdk_client import HyperliquidClient
    from .main import _load_risk_decision, _resolve_coin
    from .paper.clock import WallClock
    from .paper.config import PaperTradingConfig
    from .paper.engine import AssetSpec
    from .paper.run_lock import RunLockError, acquire_run_lock, release_run_lock
    from .persistence import repository as repo

    try:
        config = load_config(args.config)
    except CONFIG_LOAD_ERRORS as exc:
        print(f"error: invalid config — {exc}. Fix the YAML and re-run.", file=sys.stderr)
        return 1
    coin = _resolve_coin(args, config)
    cfgs = _load_risk_decision(config)
    if cfgs is None:
        return 1
    risk_cfg, decision_cfg = cfgs
    # Cannot raise for operator input: _load_risk_decision above already parsed
    # this same block inside its named exit-1 lane (bad values return None).
    paper_cfg = PaperTradingConfig.from_dict(config.get("paper_trading"))

    clock = WallClock()
    try:
        client = HyperliquidClient.from_config(config)
        market = HyperliquidMarketData(client)
        sz_decimals, schedule = market.get_asset_meta(coin)
    except ExchangeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    asset = AssetSpec(coin=coin, sz_decimals=sz_decimals, margin_schedule=schedule)

    run_id = args.run_id or f"paper-{coin}"
    export_dir = (
        Path(args.export_dir) if args.export_dir else db_path.resolve().parent / "exports" / run_id
    )
    funding_source = _HistoryFundingSource(market)

    with Database(db_path) as db:
        existing_run = repo.get_run(db.conn, run_id)
        is_restart = existing_run is not None
        now = clock.now()
        if not is_restart and not args.create:
            print(
                f"error: run {run_id!r} does not exist in {db_path}. Pass --create "
                "to start it, or fix --run-id / --db to resume the intended run.",
                file=sys.stderr,
            )
            return 1
        if is_restart and args.create:
            # The flag exists to make store identity explicit in BOTH
            # directions: silently resuming here would append to an old run
            # (old position, old ledger, old schedule) when the operator
            # plausibly meant a fresh acceptance run — contaminating every
            # §5 metric without a word.
            print(
                f"error: run {run_id!r} already exists in {db_path}. Drop --create "
                "to resume it, or pick a new --run-id for a fresh run.",
                file=sys.stderr,
            )
            return 1
        if existing_run is not None and existing_run["mode"] != "paper":
            # Same identity discipline as the live resume (decided
            # 2026-07-17): a live run's genesis carries the same coin, so the
            # drift check alone would wave a typo'd --run-id/--db through —
            # and the paper daemon would then trade over a LIVE run's books.
            # Checked before the run lock touches the row.
            print(
                f"error: run {run_id!r} in {db_path} is a "
                f"{existing_run['mode']} run — the paper daemon would trade "
                "over its books. Fix --run-id / --db.",
                file=sys.stderr,
            )
            return 1
        # One live process per run: taken BEFORE the destructive restart
        # reconciliation, which assumes the previous process is dead.
        try:
            acquire_run_lock(db, run_id, pid=os.getpid(), now=now)
        except RunLockError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        # From here the lease is ours; release it on every exit (the except
        # paths below return, a crash leaves it to go stale on its own).
        signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)

        def _run_locked() -> int:
            """The lease-holding tail of ``paper``: create/reconcile, then the loop."""
            from .main import EngineImportError
            from .paper import accounting
            from .paper.engine import PaperExecutionEngine
            from .paper.market_feed import PortSnapshotProvider
            from .paper.reconcile import ReconciliationError, reconcile_on_restart
            from .paper.scheduler import PaperScheduler
            from .persistence import repository as repo
            from .persistence.models import PositionState
            from .persistence.schema import SCHEMA_VERSION

            trading_halted = False
            # Built pre-flight on a fresh run (before the run row exists);
            # a restart builds it after reconciliation settles that it trades.
            provider = None

            def _build_provider():
                return _EngineDecisionProvider(
                    config,
                    risk_cfg=risk_cfg,
                    decision_cfg=decision_cfg,
                    payload_dir=db_path.resolve().parent / "payloads" / run_id,
                )

            if not is_restart:
                # Seeds are genesis-only and the engine manages exactly the
                # run coin: an off-coin seed would sit in the store all run —
                # excluded from the equity the AI sizes against, with no
                # SL/TP, liquidation watch, or funding — silently misstating
                # net worth. Config error, before anything is written.
                off_coin = sorted({p.coin for p in paper_cfg.account.initial_positions} - {coin})
                if off_coin:
                    print(
                        f"error: initial_positions seed coin(s) "
                        f"{', '.join(map(repr, off_coin))} do not match the run "
                        f"coin {coin!r} — a paper run manages exactly one coin. "
                        "Fix paper_trading.account.initial_positions or start a "
                        "separate run for that coin.",
                        file=sys.stderr,
                    )
                    return 1
                # A fresh run always drives the AI — demand the key before
                # writing the run row, so a keyless ``--create`` fails cleanly
                # instead of leaving a half-created run that the next attempt
                # then rejects as "already exists".
                if not _require_api_key():
                    return 1
                # The decision provider is the other operator-fixable
                # pre-flight: its construction triggers the process's first
                # tradingagents import, which detonates on a corrupt repo
                # .env (see _build_engine_config). Same ordering rule as the
                # key check — fail before the run row exists, or the retry
                # after fixing the file is rejected as "already exists".
                try:
                    provider = _build_provider()
                except EngineImportError as exc:
                    print(f"error: {exc}", file=sys.stderr)
                    return 1
                seeds = [
                    PositionState(coin=p.coin, size=p.size, entry_price=p.entry_price)
                    for p in paper_cfg.account.initial_positions
                ]
                accounting.initialize_run(
                    db,
                    run_id=run_id,
                    mode="paper",
                    initial_balance_usdc=paper_cfg.account.initial_balance_usdc,
                    schema_version=SCHEMA_VERSION,
                    initial_positions=seeds,
                    # Only the behaviour-defining blocks — never network/wallet keys.
                    config_json=json.dumps(
                        _run_config_subset(config, coin), ensure_ascii=False, default=str
                    ),
                    created_at=now,
                )
                print(f"created paper run {run_id!r} in {db_path}", file=sys.stderr)
            else:
                # Resuming an existing run under drifted config would silently
                # change behaviour mid-run while every metric treats it as one
                # homogeneous run: a coin mismatch is a hard error, parameter
                # drift a loud warning.
                # ``existing_run`` was fetched once at the top of the block
                # (is_restart proved it non-None); no writer touches the runs
                # row between there and here.
                drift = _config_drift_report(existing_run["config_json"], config, coin)
                if drift is None:
                    # Stamp clean resumes too, so a reverted config doesn't
                    # leave a stale "drift" as the last word in the store.
                    _stamp_breadcrumb(db, run_id, "config_drift", "ok", None)
                else:
                    kind, message = drift
                    if kind in _HARD_DRIFT_KINDS:
                        print(f"error: {message}", file=sys.stderr)
                        return 1
                    logger.warning("config drift on resume for %s: %s", run_id, message)
                    print(f"WARNING: {message}", file=sys.stderr)
                    _stamp_breadcrumb(db, run_id, "config_drift", "drift", message)
                # The same single-coin invariant as the fresh-run seed guard,
                # enforced at the other daemon entry point: a store created by
                # direct initialize_run (or an older build) may hold off-coin
                # positions that replay verifies symmetrically yet every
                # equity/protection computation silently excludes. Checked
                # before reconcile writes anything.
                off_coin = sorted(
                    p.coin
                    for p in repo.get_all_current_positions(db.conn, run_id)
                    if p.coin != coin and not p.is_flat
                )
                if off_coin:
                    print(
                        f"error: run {run_id!r} holds open position(s) in "
                        f"{', '.join(map(repr, off_coin))} but this daemon "
                        f"manages only {coin!r} — they would be excluded from "
                        "equity and SL/TP protection. The paper daemon cannot "
                        "run this store (export/validate still work).",
                        file=sys.stderr,
                    )
                    return 1
                try:
                    report = reconcile_on_restart(
                        db, run_id=run_id, now=now, funding_source=funding_source
                    )
                except ReconciliationError as exc:
                    # Flat run over corrupt/unverifiable books: refuse to start
                    # — but leave the durable breadcrumb first ("mismatch" or
                    # "failed" per the refusal's lane), so the refusal is
                    # visible to a post-mortem even when stderr wasn't captured.
                    _stamp_breadcrumb(db, run_id, "replay", exc.replay_status, str(exc))
                    print(f"error: {exc}", file=sys.stderr)
                    return 1
                _stamp_breadcrumb(db, run_id, "replay", report.replay_status, report.replay_error)
                trading_halted = report.replay_mismatch
                if report.replay_mismatch:
                    logger.error("restart over unverifiable books: %s", report.replay_error)
                    print(
                        "ERROR: accounting replay could not verify this run's "
                        "books on restart — running in protection-only mode: "
                        "SL/TP protection and the market monitor stay live, NEW "
                        "decision cycles stay halted until the store verifies "
                        "again.",
                        file=sys.stderr,
                    )
                print(
                    f"restart reconciliation for {run_id!r}: "
                    f"{len(report.canceled_plan_ids)} plan(s) canceled, "
                    f"{report.funding_posted} funding event(s) backfilled"
                    + (", immediate cycle forced" if report.forced_immediate_cycle else ""),
                    file=sys.stderr,
                )
            engine = PaperExecutionEngine(
                db=db,
                run_id=run_id,
                asset=asset,
                clock=clock,
                provider=PortSnapshotProvider(market, clock),
                risk_config=risk_cfg,
                decision_config=decision_cfg,
                paper_config=paper_cfg,
                funding_source=funding_source,
            )
            if is_restart and engine.has_active_work():
                # §1.2 step 6: the restart was a blind window — the first tick must
                # treat a crossed SL as a gap stop, not a normal trigger-price fill.
                # Armed only when the blind window had something to watch (a position
                # or live protection): on a flat restart the forced immediate cycle
                # can open a NEW position via poll() before any tick ever runs, and
                # an unconsumed flag would mislabel that fresh position's first real
                # SL trigger as a restart gap fill.
                engine.flag_restart_gap()
            halt_reason = "replay" if trading_halted else None
            if not trading_halted and not os.environ.get("OPENROUTER_API_KEY"):
                # Only the restart lane reaches here keyless — a fresh run
                # demanded the key before writing the run row. A keyless
                # healthy restart over live work must NOT exit: reconcile
                # already canceled its plans, so exiting would leave the
                # position with nobody watching SL/TP — the exact harm
                # protection-only mode exists to prevent (a replay-mismatch
                # restart, with *less* trustworthy books, already gets that
                # protection). Flat, there is nothing to protect and the
                # plain abort stands.
                if engine.has_active_work():
                    trading_halted = True
                    halt_reason = "missing-key"
                    logger.error(
                        "OPENROUTER_API_KEY missing on restart of %s with a live "
                        "position — entering protection-only mode",
                        run_id,
                    )
                    print(
                        "ERROR: OPENROUTER_API_KEY is not set but this run holds "
                        "a live position — running in protection-only mode: SL/TP "
                        "protection and the market monitor stay live, NEW decision "
                        "cycles stay halted. Set the key and restart to resume "
                        f"trading. ({dotenv_diagnosis('OPENROUTER_API_KEY')}.)",
                        file=sys.stderr,
                    )
                else:
                    _require_api_key()  # prints the standard abort message
                    return 1
            if not trading_halted and provider is None:
                # Only the healthy restart lane reaches here without a provider
                # — the fresh lane built it pre-flight. Same protection-only
                # rule as the keyless restart above: an EngineImportError is
                # operator-fixable (see _build_engine_config for the causes),
                # so over live work exiting would leave the position with
                # nobody watching SL/TP; flat, the named exit 1 stands.
                try:
                    provider = _build_provider()
                except EngineImportError as exc:
                    if engine.has_active_work():
                        trading_halted = True
                        halt_reason = "import-error"
                        logger.error(
                            "tradingagents import failed on restart of %s with "
                            "a live position — entering protection-only mode: %s",
                            run_id,
                            exc,
                        )
                        print(
                            f"ERROR: {exc}\nThis run holds a live position — "
                            "running in protection-only mode: SL/TP protection "
                            "and the market monitor stay live, NEW decision "
                            "cycles stay halted. Fix the environment and "
                            "restart to resume trading.",
                            file=sys.stderr,
                        )
                    else:
                        print(f"error: {exc}", file=sys.stderr)
                        return 1
            if trading_halted:
                # Protection-only never polls the AI, and nothing ever un-halts
                # a protection-only process — the loop never touches the
                # scheduler. Skip building it (and the decision provider's deep
                # tradingagents import behind it): each would only add failure
                # modes to a startup whose one job is keeping SL/TP alive, the
                # same principle that lets this mode start keyless.
                scheduler = None
                stranded = repo.find_in_progress_attempt(db.conn, run_id)
                if stranded is not None:
                    # Purely informational: only a healthy restart's first
                    # poll may finish this attempt (§3.1 resumes the SAME
                    # attempt, without burning its retry budget) — terminalizing
                    # it here would destroy that resumable state and write a
                    # next_decision_at onto books this mode exists to distrust.
                    attempt_id = stranded["decision_attempt_id"]
                    logger.warning(
                        "decision attempt %s remains in_progress; protection-only "
                        "never polls the scheduler, so it stays open until the "
                        "next healthy restart resumes it",
                        attempt_id,
                    )
                    print(
                        f"note: decision attempt {attempt_id!r} from the previous "
                        "process remains in_progress — protection-only mode never "
                        "resumes it; the next healthy restart will.",
                        file=sys.stderr,
                    )
            else:
                scheduler = PaperScheduler(
                    db=db,
                    run_id=run_id,
                    engine=engine,
                    clock=clock,
                    provider=provider,
                    asset=asset,
                    risk_config=risk_cfg,
                    decision_config=decision_cfg,
                )
            interval = paper_cfg.execution.market_monitor.interval_seconds
            try:
                return _paper_loop(
                    db,
                    run_id,
                    engine,
                    scheduler,
                    clock,
                    interval,
                    export_dir,
                    funding_source,
                    trading_halted=trading_halted,
                    halt_reason=halt_reason,
                )
            except KeyboardInterrupt:
                print("\nshutting down — final export...", file=sys.stderr)
                # Same last-resolution pass as the settle-exit lane: pending
                # funding posted now is funding the final CSVs won't be missing.
                _retry_pending_funding(db, run_id, now=clock.now(), funding_source=funding_source)
                _post_cycle_export(db, run_id, export_dir)
                return 0
            except RunLockError as exc:
                # The lease was taken over while this process stalled: the
                # successor already reconciled away this process's plans and
                # owns the run now. Exit WITHOUT the shutdown export or any
                # other store write — each one would corrupt the successor's
                # view (the pid-guarded release below no-ops for the same
                # reason).
                logger.error("run lease lost: %s", exc)
                print(f"error: {exc}", file=sys.stderr)
                return 1

        try:
            return _run_locked()
        finally:
            # Same guard as the live path: a raise here would clobber
            # _run_locked()'s exit code into a generic exit 2.
            try:
                release_run_lock(db, run_id, pid=os.getpid(), now=clock.now())
            except Exception:  # noqa: BLE001
                logger.exception("run-lock release failed (the exit code above stands)")


def _paper_loop(
    db,
    run_id,
    engine,
    scheduler,
    clock,
    interval: int,
    export_dir: Path,
    funding_source,
    *,
    trading_halted: bool,
    halt_reason: str | None = None,
) -> int:
    """The production loop: tick (when the monitor is on) → poll → sleep.

    Tick before poll so a restart handles liquidation / gap SL / TP on the
    reconciled position *before* the immediate new cycle plans against it
    (execution §1.2 step 6 before step 8). Sleep is bounded by the monitor
    interval while any market work is live (execution §5.5) and stretches
    toward ``next_decision_at`` when everything is quiet, so an idle run
    issues no market-data requests.

    A replay mismatch stops NEW decisions (the books can no longer be trusted
    for sizing) but keeps the engine ticking — SL/TP protection and the market
    monitor must survive (spec §5 / data §1.1). ``trading_halted=True`` enters
    that mode from the first iteration (a restart over mismatched books with a
    live position). Because halted mode never polls the scheduler, it retries
    pending funding on its own wall-clock cadence
    (``_HALTED_FUNDING_RETRY_SECONDS``) instead of at cycle boundaries, and
    both exit lanes (settle-exit below, Ctrl-C/SIGTERM in the caller) give
    pending funding one last resolution pass before the closing export.

    Protection-only mode exists *for* that live position — once it is gone
    (SL/TP closed it and nothing else is live), halted trading means there is
    nothing left this process may ever do. Rather than idle as a zombie for
    days (holding the lease, with the closing fill never reaching the CSVs),
    it exports the final state and returns exit code 1 so a supervisor or
    operator gets the signal to investigate the store.

    Every iteration refreshes the single-instance lease, so the sleep is
    capped at 60s (well inside ``LOCK_STALE_SECONDS``). The cap bounds only
    the wake cadence, not the market-data request rate: ``engine.tick()`` is
    throttled to the configured monitor interval, keeping the two cadences
    decoupled (defensive — config already rejects intervals above the 30s TWAP
    slice cadence; early wakes touch only SQLite). A superseded
    lease raises
    :class:`~.paper.run_lock.RunLockError` out of the loop (see
    :func:`~.paper.run_lock.heartbeat_run_lock`).
    """
    from .paper.reconcile import backfill_pending_funding
    from .paper.run_lock import heartbeat_run_lock
    from .paper.scheduler import CycleEvent

    pid = os.getpid()
    next_tick_at: datetime | None = None  # None → the first tick fires immediately
    # Halted mode never polls the scheduler, so it never reaches the
    # cycle-terminal funding retry below — without its own timer, pending
    # funding would stay unposted for the run's whole halted lifetime
    # (execution §6.5's 稍後補帳). Both entries into halted mode have just run
    # a backfill (restart reconciliation, or the cycle-terminal retry in the
    # iteration that halts mid-run), so the first in-loop retry waits a full
    # period.
    next_funding_retry_at: datetime | None = (
        clock.now() + timedelta(seconds=_HALTED_FUNDING_RETRY_SECONDS) if trading_halted else None
    )
    while True:
        now = clock.now()
        heartbeat_run_lock(db, run_id, pid=pid, now=now)
        # Tick cadence is the configured monitor interval, decoupled from the
        # 60s heartbeat wake cap below: were the interval ever to exceed the
        # cap, the loop would wake for the lease but skip the tick (and its
        # market-data fetch) until the interval elapsed. Config currently
        # rejects intervals above the 30s TWAP slice cadence, so this is a
        # defensive invariant, not an operator-facing mode.
        if engine.has_active_work() and (next_tick_at is None or now >= next_tick_at):
            engine.tick()
            next_tick_at = now + timedelta(seconds=interval)
        if trading_halted and next_funding_retry_at is not None and now >= next_funding_retry_at:
            _retry_pending_funding(db, run_id, now=now, funding_source=funding_source)
            next_funding_retry_at = now + timedelta(seconds=_HALTED_FUNDING_RETRY_SECONDS)
        result = scheduler.poll() if not trading_halted else None
        if result is not None:
            print(
                f"[{clock.now().isoformat(timespec='seconds')}] cycle event: "
                f"{result.event.value} (attempt {result.decision_attempt_id})",
                file=sys.stderr,
            )
            if result.event.is_cycle_terminal:
                # Retry any backfillable pending funding at every cycle
                # boundary (execution §6.5's 稍後補帳 — not restart-only).
                backfill_pending_funding(
                    db, run_id=run_id, now=clock.now(), funding_source=funding_source
                )
                if not _post_cycle_export(db, run_id, export_dir):
                    trading_halted = True
                    halt_reason = "replay"
                    # The cycle-terminal backfill above just ran; start the
                    # halted-mode funding-retry timer one full period out.
                    next_funding_retry_at = clock.now() + timedelta(
                        seconds=_HALTED_FUNDING_RETRY_SECONDS
                    )
                    # Mirror the restart lane's cancel sweep: the halting cycle
                    # may have just started a plan (zero slices consumed), and
                    # letting it fill — or a flip open its reverse leg — would
                    # build exposure on the very books that failed to verify,
                    # diverging from what a kill-and-restart would produce.
                    canceled = engine.cancel_active_plans()
                    logger.error(
                        "halting NEW decision cycles for %s: accounting replay "
                        "can no longer rebuild the books%s",
                        run_id,
                        " (in-flight execution plan canceled)" if canceled else "",
                    )
                    print(
                        "ERROR: accounting replay can no longer rebuild this run's "
                        "books — halting NEW decision cycles and canceling any "
                        "in-flight execution plan. The engine keeps ticking "
                        "(SL/TP protection and monitor stay live). "
                        "Investigate the store; a restart stays in this "
                        "protection-only mode until the books verify again.",
                        file=sys.stderr,
                    )
            if result.event is CycleEvent.API_FAILED:
                logger.warning(
                    "decision API failed %s times for %s — holding position until the next cycle",
                    result.attempt_count,
                    run_id,
                )
                print(
                    f"decision API failed {result.attempt_count} times — holding "
                    "position until the next cycle.",
                    file=sys.stderr,
                )
        # One post-tick/poll read serves both the exit check and the delay
        # branch — nothing between them mutates the engine's work state.
        active = engine.has_active_work()
        if trading_halted and not active:
            # Protection-only mode exists for a live position; with the
            # position closed (or none left) and new cycles halted, no future
            # iteration can ever do anything — a zombie would hold the lease
            # for days while the closing fill never reaches the CSVs. Export
            # the final state and exit loud so a supervisor/operator gets the
            # signal.
            logger.error(
                "protection-only run %s has nothing left to protect — "
                "exporting final state and exiting",
                run_id,
            )
            if halt_reason == "missing-key":
                print(
                    "protection-only mode has nothing left to protect (the "
                    "position is closed and new cycles stayed halted for the "
                    "missing OPENROUTER_API_KEY) — exporting the final state "
                    "and exiting. Set the key to resume this run.",
                    file=sys.stderr,
                )
            elif halt_reason == "import-error":
                print(
                    "protection-only mode has nothing left to protect (the "
                    "position is closed and new cycles stayed halted because "
                    "the tradingagents engine failed to import) — exporting "
                    "the final state and exiting. Fix the environment (see "
                    "the startup error) to resume this run.",
                    file=sys.stderr,
                )
            else:
                print(
                    "protection-only mode has nothing left to protect (the "
                    "position is closed and new cycles stay halted) — exporting "
                    "the final state and exiting. The books never re-verified; "
                    "investigate the store before starting a new run.",
                    file=sys.stderr,
                )
            # The final CSVs should be as complete as the store allows: give
            # any still-pending funding one last resolution pass first.
            _retry_pending_funding(db, run_id, now=clock.now(), funding_source=funding_source)
            _post_cycle_export(db, run_id, export_dir)
            return 1
        now = clock.now()
        due = scheduler.next_due_at() if not trading_halted else None
        # Halted-with-work and pending-retry (due is None while not halted)
        # both tick at the monitor interval; the halted-and-idle case exited
        # above, so ``due is None`` can no longer mean "idle heartbeat".
        delay = float(interval) if active or due is None else max(1.0, (due - now).total_seconds())
        # The 60s cap keeps the lease heartbeat fresh; waking early is a cheap
        # SQLite poll — engine.tick() above is throttled to the configured
        # interval, so an early wake never issues extra market-data requests.
        time.sleep(min(delay, 60.0))


def _stamp_breadcrumb(db, run_id: str, kind: str, status: str, error: str | None) -> None:
    """Durably stamp a ``scheduler_state`` breadcrumb trio (``last_<kind>_*``).

    Export failures, replay outcomes, and config drift on resume are
    warn-and-carry-on (the mid-run replay halt only lives in process memory) —
    without this record none would leave any trace once the process exits.
    ``kind`` is a code-owned literal ("export" / "replay" / "config_drift");
    the column vocabulary stays validated by ``upsert_scheduler_state``'s
    fixed keyword signature.
    """
    from .persistence import repository as repo

    stamp = datetime.now(timezone.utc)
    with db.transaction() as conn:
        repo.upsert_scheduler_state(
            conn,
            run_id,
            updated_at=stamp,
            **{
                f"last_{kind}_status": status,
                f"last_{kind}_error": error,
                f"last_{kind}_at": stamp,
            },
        )


_UNVERIFIED_MARKER = "REPLAY_UNVERIFIED.json"


def _mark_export_verification(
    export_dir: Path, run_id: str, replay_ok: bool, reason: str | None
) -> None:
    """Record, in-band next to the CSVs, whether the exported set passed replay.

    ``scheduler_state`` (which carries the ``last_replay_*`` breadcrumb) is NOT
    one of the exported tables, so a consumer reading the CSVs alone cannot tell
    a mismatch cycle's export from a healthy one. When replay did not verify we
    drop ``REPLAY_UNVERIFIED.json`` beside the CSVs; when it verifies we remove
    any stale marker a previous bad cycle left behind (the export dir is reused
    every cycle). Best-effort: a marker failure is logged, never raised — the
    loop must survive, and the ``last_replay_*`` breadcrumb remains authoritative.
    """
    marker = Path(export_dir) / _UNVERIFIED_MARKER
    try:
        if replay_ok:
            marker.unlink(missing_ok=True)
        else:
            marker.write_text(
                json.dumps(
                    {"run_id": run_id, "replay_verified": False, "reason": reason},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
    except OSError as exc:
        logger.error("failed to update replay-verification marker for %s: %s", run_id, exc)


def _post_cycle_export(db, run_id: str, export_dir: Path) -> bool:
    """Replay-verify then export (phase2-data §1.1); returns whether the books verified.

    An export failure never stops trading (spec: record ``export_failed`` and
    carry on) — but it IS durably recorded on ``scheduler_state``
    (``last_export_status`` / ``last_export_error`` / ``last_export_at``), so a
    post-mortem can tell how long the CSV view had been stale even when stderr
    was not captured. A replay mismatch or replay *failure* returns ``False``
    so the caller can stop opening new positions on unverifiable books; both
    outcomes (and healthy verifications) stamp the ``last_replay_*`` breadcrumb.
    """
    from .paper.reconcile import classify_replay
    from .persistence.export import ExportError, export_run

    # classify_replay contains a raising replay ("failed" — unverifiable books
    # are treated like inconsistent ones), so this lane cannot kill the loop.
    replay_status, replay_detail, _cause = classify_replay(db, run_id=run_id)
    replay_ok = replay_status == "ok"
    if replay_status == "mismatch":
        logger.error("accounting replay mismatch for %s: %s", run_id, replay_detail)
        print(
            f"WARNING: accounting replay mismatch for {run_id!r}: {replay_detail} — "
            "investigate before trusting this run's results.",
            file=sys.stderr,
        )
    elif replay_status == "failed":
        logger.error("accounting replay failed for %s: %s", run_id, replay_detail)
        print(f"WARNING: accounting replay failed: {replay_detail}", file=sys.stderr)
    _stamp_breadcrumb(db, run_id, "replay", replay_status, replay_detail)
    export_ok = False
    try:
        export_run(db, run_id=run_id, output_dir=export_dir)
        export_ok = True
        _stamp_breadcrumb(db, run_id, "export", "ok", None)
        print(f"exported CSVs to {export_dir}", file=sys.stderr)
    except ExportError as exc:
        # §1.1: record export_failed, keep the monitor and protections running.
        logger.error("export_failed for %s: %s", run_id, exc)
        print(f"WARNING: export_failed — {exc}", file=sys.stderr)
        _stamp_breadcrumb(db, run_id, "export", "failed", str(exc))
    # Mark the freshly written set as unverified when replay didn't pass, so a
    # consumer reading the CSVs alone isn't misled (see _mark_export_verification).
    # Only when a full set was actually (re)written — a failed export leaves the
    # previous set and its marker untouched.
    if export_ok:
        _mark_export_verification(export_dir, run_id, replay_ok, replay_detail)
    return replay_ok


# Protection-only mode never reaches the cycle-terminal funding retry (the
# scheduler is never polled), so the loop retries on this wall-clock cadence
# instead. Hour-grained to match funding's own settlement granularity; when
# nothing is pending the pass is a single cheap SQLite query.
_HALTED_FUNDING_RETRY_SECONDS = 3600


def _retry_pending_funding(db, run_id: str, *, now: datetime, funding_source) -> None:
    """Best-effort pending-funding retry for the protection and shutdown lanes.

    The cycle-terminal lane calls :func:`backfill_pending_funding` directly and
    stays fail-loud (a store-level error there must kill the loop — pinned by
    test). These lanes exist to keep SL/TP alive (the halted-mode timer) or to
    flush the most complete final CSVs the store allows (settle-exit and
    Ctrl-C/SIGTERM exports) — a raising retry must not take either down, so any
    failure is contained to an ERROR log and the hourly timer (or the export
    itself) carries on. ``record_funding`` posts exactly-once, so repeated
    passes are safe.
    """
    from .paper.reconcile import backfill_pending_funding

    try:
        backfill_pending_funding(db, run_id=run_id, now=now, funding_source=funding_source)
    except Exception:
        logger.exception("pending-funding retry failed for %s (best-effort lane)", run_id)


# --------------------------------------------------------------------------
# production seams: funding-rate history + the AI decision provider
# --------------------------------------------------------------------------


class _HistoryFundingSource:
    """Funding rates from the public fundingHistory endpoint (execution §6.5).

    Serves the engine's hourly settlements and the pending-event backfill
    (restart + every cycle boundary). Responses are cached briefly so a
    backfill loop over many pending hours does not re-fetch per event; the
    fetch window widens to cover however old the requested settlement is, so a
    long-pending event can always resolve. A missing hour returns ``None``
    (the caller records/keeps a ``pending`` event — never a fabricated rate).
    """

    _MIN_WINDOW_DAYS = 7
    _CACHE_TTL_SECONDS = 900
    # After this many consecutive fetch failures the log escalates to ERROR: a
    # chronic integration break (auth, endpoint drift) must read differently
    # from the ordinary "rate not published yet" warning it otherwise mimics —
    # events would pile up pending forever behind an easy-to-miss line.
    _FAILURE_ESCALATION_THRESHOLD = 3

    def __init__(self, market) -> None:
        self._market = market
        # coin -> (fetched_at_monotonic, window_days_fetched, {hour: rate})
        self._cache: dict[str, tuple[float, int, dict[datetime, object]]] = {}
        self._consecutive_failures: dict[str, int] = {}

    def rate_at(self, coin: str, funding_timestamp: datetime):
        hour = funding_timestamp.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
        age = datetime.now(timezone.utc) - hour
        needed_days = max(self._MIN_WINDOW_DAYS, age.days + 2)
        cached = self._cache.get(coin)
        if (
            cached is None
            or time.monotonic() - cached[0] > self._CACHE_TTL_SECONDS
            or cached[1] < needed_days
        ):
            try:
                points = self._market.get_funding_history(coin, needed_days)
            except Exception as exc:  # noqa: BLE001 — a rate fetch failure means "pending"
                failures = self._consecutive_failures.get(coin, 0) + 1
                self._consecutive_failures[coin] = failures
                log = (
                    logger.error
                    if failures >= self._FAILURE_ESCALATION_THRESHOLD
                    else logger.warning
                )
                log(
                    "funding history fetch failed for %s (%d consecutive): %s",
                    coin,
                    failures,
                    exc,
                )
                return None
            self._consecutive_failures.pop(coin, None)
            by_hour = {}
            for point in points:
                stamp = datetime.fromtimestamp(point.time / 1000, tz=timezone.utc)
                by_hour[stamp.replace(minute=0, second=0, microsecond=0)] = point.rate
            cached = (time.monotonic(), needed_days, by_hour)
            self._cache[coin] = cached
        return cached[2].get(hour)


class _EngineDecisionProvider:
    """Production :class:`~.paper.scheduler.DecisionProvider`: the TradingAgents engine.

    ``build_input`` fetches market data and persists the full payload JSON
    (phase2-data §5: SQLite keeps summary + path + hash); ``request_decision``
    drives the unmodified engine and parses the structured target. External
    failures are classified into the §6.2 retry vocabulary and raised as
    :class:`RetryableDecisionError`; contract violations are NOT errors — they
    come back as an invalid ``ParsedDecision`` (fail-closed downstream).

    NOT a pure function of its argument (PR 6 hazard): ``request_decision``
    reads ``_context_text`` / ``_format_text`` that the LAST ``build_input``
    call stashed on the instance, not fields of ``decision_input`` — and the
    one shared instance is handed to both the background worker thread and the
    main-thread driver. Today this is safe only because the driver's busy-gate
    serializes ``build_input() → submit()`` strictly. Any future re-send path
    (a within-cycle retry ladder, a replay harness) MUST re-run ``build_input``
    immediately before each ``request_decision`` — or first fold the prompt
    texts into the decision-input type — else it sends a STALE cycle's prompt
    while the audit trail records the fresh input.
    """

    # Class-level default so an instance built without __init__ (the tests use
    # object.__new__ to skip the engine import) still answers the attribute —
    # a missing hook must degrade to "no refresh", never to AttributeError
    # inside build_input, which the §6.2 classifier would relabel as an API
    # failure.
    _on_blocking_read = None

    def __init__(
        self,
        config: dict,
        *,
        risk_cfg,
        decision_cfg,
        payload_dir: Path,
        on_blocking_read=None,
    ) -> None:
        from .main import _build_engine_config

        self._config = config
        self._risk = risk_cfg
        self._decision = decision_cfg
        self._payload_dir = payload_dir
        # Live only: ``build_input`` runs on the single-threaded tick and makes
        # the longest unrefreshed REST chain in the system, so the live wiring
        # passes a kill-switch refresh here. ``None`` for paper and the one-shot
        # CLI paths, which hold no dead man's switch (2026-08-01 lifecycle review).
        self._on_blocking_read = on_blocking_read
        self._engine_config, self._analysts = _build_engine_config(config)

    def build_input(self, *, coin: str, as_of: datetime):
        from .domains.perp import risk_gate
        from .domains.perp.prompt_context import render_market_context
        from .domains.perp.target_decision import decision_format_instructions
        from .exchanges.hyperliquid.errors import ExchangeError
        from .exchanges.hyperliquid.market_data import interval_to_ms
        from .main import _build_context, _context_refusal_error
        from .paper.scheduler import DecisionInput, RetryableDecisionError

        try:
            ctx, _client = _build_context(
                self._config, coin, on_blocking_read=self._on_blocking_read
            )
        except ExchangeError as exc:
            raise RetryableDecisionError("connection", str(exc)) from exc
        # All three pre-LLM context guards (under-warm data, fully-dead
        # indicator set, missing/dead regime indicators atr_14/ema_20/ema_50),
        # shared with the one-shot path (see main._context_refusal_error) — a
        # dead or absent regime indicator would otherwise let every cycle
        # trade on a fabricated-calm RANGING regime.
        # Deliberate (reviewed): they ride the §3.1 ladder as "server_error" →
        # api_failed — the closed §6.2 vocabulary has no data-availability
        # label, and the failure precedes the AI call so nothing is spent. A
        # gappy feed heals by the next try/cycle; a too-young listing or a
        # broken indicator engine produces a recurring api_failed cycle every
        # 4h until it warms up / is fixed.
        refusal = _context_refusal_error(ctx, coin, self._config)
        if refusal is not None:
            raise RetryableDecisionError("server_error", refusal)
        context_text = render_market_context(ctx)
        format_text = decision_format_instructions(
            self._decision,
            max_pct=risk_gate.effective_max_target_margin_pct(self._risk, self._decision),
        )
        payload = {
            "coin": coin,
            "as_of": as_of.isoformat(),
            "prompt_version": PROMPT_VERSION,
            "context_text": context_text,
            "format_instructions": format_text,
        }
        raw = json.dumps(payload, ensure_ascii=False, indent=2)
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        # Microsecond-stamped: each retry try builds its own payload, and two
        # tries landing in the same wall-clock second must not overwrite each
        # other (the earlier try's stored hash would falsely alias the later
        # file) — same per-try-distinctness rule as input_id/output_id.
        path = self._payload_dir / f"{coin}-{as_of.strftime('%Y%m%dT%H%M%S_%fZ')}.json"
        try:
            self._payload_dir.mkdir(parents=True, exist_ok=True)
            path.write_text(raw, encoding="utf-8")
        except OSError as exc:
            # An audit-artifact filesystem failure (disk full, permissions)
            # must not tear down the daemon and strand the live SL/TP monitor:
            # nothing is in the store yet and the AI has not been called, so
            # ride the §3.1 ladder like the sibling environmental failures —
            # worst case a recurring api_failed cycle whose error_message
            # names the cause, with the position held and protection alive.
            raise RetryableDecisionError("server_error", f"payload write failed: {exc}") from exc
        candle_end = ctx.as_of
        candle_start = candle_end - timedelta(milliseconds=interval_to_ms(ctx.candle_interval))
        self._context_text = context_text
        self._format_text = format_text
        return DecisionInput(
            context=ctx,
            candle_start=candle_start,
            candle_end=candle_end,
            input_payload_path=str(path),
            input_payload_hash=f"sha256:{digest}",
            prompt_version=PROMPT_VERSION,
            model=self._engine_config["deep_think_llm"],
        )

    def request_decision(self, decision_input):
        from .domains.perp.target_decision import parse_target_decision
        from .integration.trading_graph import build_graph
        from .paper.scheduler import RetryableDecisionError

        graph = build_graph(
            perp_context_text=self._context_text,
            config=self._engine_config,
            selected_analysts=self._analysts,
            output_format_text=self._format_text,
        )
        coin = decision_input.context.coin
        # Drive the base engine off the cycle's own as_of, not wall-clock now:
        # a late/recovery cycle (process was down across schedule points) must
        # feed the base news/sentiment analysts the same time base as the perp
        # market context they reason alongside, and a single read can't straddle
        # a UTC midnight between the two.
        trade_date = decision_input.context.as_of.strftime("%Y-%m-%d")
        try:
            propagated = graph.propagate(coin, trade_date, asset_type="crypto")
        except Exception as exc:  # noqa: BLE001 — engine-run failures are external (§3.1)
            raise RetryableDecisionError(_classify_engine_error(exc), str(exc)) from exc
        if (
            not isinstance(propagated, (tuple, list))
            or len(propagated) < 2
            or not isinstance(propagated[0], dict)
        ):
            # A drifted return contract is indistinguishable from a broken
            # response — retryable server_error, and api_failed after 3 tries.
            raise RetryableDecisionError(
                "server_error",
                f"engine.propagate returned an unexpected shape ({type(propagated).__name__})",
            )
        return parse_target_decision(propagated[0].get("final_trade_decision"), self._decision)


def _classify_engine_error(exc: Exception) -> str:
    """Map an engine-run exception onto the §6.2 error-type vocabulary."""
    text = f"{type(exc).__name__}: {exc}".lower()
    if "timeout" in text or "timed out" in text:
        return "timeout"
    # No bare "429": those three digits match any larger number that contains
    # them — an oid, an epoch-ms timestamp, a price — so "run 1429 failed" filed
    # as a rate limit. The sibling classifier in exchanges/hyperliquid/
    # sdk_client.py deleted exactly this marker for exactly this reason and kept
    # the phrases; this copy was missed. A real rate limit still lands here
    # through the SDK's exception CLASS name (``RateLimitError`` → "ratelimit")
    # or the phrase itself (2026-08-01 round-18 concept scan). The two lists are
    # deliberately NOT identical and must not be merged: that one gates retry
    # and §17.2 escalation on VENUE errors and carries the SDK-ism "slow down",
    # while this one only labels an LLM-engine failure for the audit trail
    # (``decision_attempts.error_type``, a closed vocabulary the scheduler
    # retries identically whatever it says).
    if "rate limit" in text or "ratelimit" in text or "too many requests" in text:
        return "rate_limit"
    if "connection" in text or "connect" in text or "network" in text:
        return "connection"
    return "server_error"
