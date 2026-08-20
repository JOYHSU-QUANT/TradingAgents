"""Shared guards for the CLI subcommands.

The refusal helpers more than one subcommand routes through: open-an-existing
store (never CREATE one implicitly), the standard missing-run and wrong-run-mode
messages, the SIGTERM→KeyboardInterrupt mapping every long-running command
installs, and the OPENROUTER_API_KEY gate.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from ..config import dotenv_diagnosis
from ..persistence.db import Database, SchemaVersionError


def _raise_keyboard_interrupt(signum, frame) -> None:
    """SIGTERM → the SIGINT path: systemd/docker/``kill`` stop with the default
    TERM signal, and phase2-data §1.1's shutdown export must fire for them
    exactly as it does for Ctrl-C."""
    raise KeyboardInterrupt


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
    from ..persistence import repository as repo

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
