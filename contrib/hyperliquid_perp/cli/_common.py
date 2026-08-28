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
from ..persistence.db import Database, SchemaVersionError, apply_migrations


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
      once it does, via :func:`_migrate_owned_store`. See :class:`Database` for
      why that ordering matters.
    """
    if not Path(path).exists():
        print(f"error: database {path!r} does not exist.", file=sys.stderr)
        return None
    try:
        return Database(path, migrate=migrate, defer_migration=defer_migration)
    except SchemaVersionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return None


def _open_owned_store(path: str | Path) -> Database:
    """Open a store for a command that OWNS it (``paper``, ``live``).

    An existing store may be owned by a running daemon — this run's previous
    process, or a sibling run on the same wallet — and the run lease that
    proves otherwise lives inside the store being opened. Migrating at open
    therefore upgraded the schema underneath that daemon on the way to
    refusing (issue #129). So an existing store is opened AS-IS and the
    upgrade is owed to :func:`_migrate_owned_store`, which the caller runs once
    it holds the lease (``paper``) or has proven nobody else does (``live``).
    A store that does not exist yet has no owner, so it is created and
    migrated in full on the way in — the lease table it needs is itself a
    migration. The real ``live-smoke`` run reaches the same state through
    :func:`_open_existing_db` (it never creates).
    """
    exists = Path(path).exists()
    return Database(path, migrate=not exists, defer_migration=exists)


def _migrate_owned_store(db: Database) -> bool:
    """Run the upgrade :func:`_open_owned_store` deferred; True if it was REFUSED.

    A no-op when nothing is owed (the store was created on open, or a dry run
    opened it read-only), so every owning command calls it unconditionally at
    the point it owns the store. The "migrated by a NEWER build" refusal is
    printed here as a named exit 1 — never main()'s exit-2 last resort — and
    a caller that already holds the lease must make this call inside the
    lease-releasing ``try`` so that refusal frees the lease instead of parking
    it on a dead pid for LOCK_STALE_SECONDS.
    """
    if not db.migration_pending:
        return False
    try:
        apply_migrations(db.conn)
    except SchemaVersionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return True
    db.migration_pending = False
    return False


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


def _require_agent_key(network: str, *, remedy: str, demanded_by: str | None = None) -> str | None:
    """The network's agent key, or ``None`` after printing the abort message.

    The one encoding of the "agent key is not set" refusal for every command
    that signs (``live`` when ``require_agent_wallet`` is on, the real
    ``live-smoke`` run), the agent-key counterpart of :func:`_require_api_key`.
    Issue #82 was this logic copied into two subcommands and fixed in one; a
    third signing entry point would have copied it again (issue #126).

    The message is ``error: <VAR> is not set[ but <demanded_by>] — <remedy>
    (<diagnosis>.)``: ``demanded_by`` names the setting that demands the key
    when the command itself does not; ``remedy`` is the instruction, joined
    and terminated here. The ``dotenv_diagnosis`` suffix is appended for the
    network's ACTUAL variable, so no caller can drop it or diagnose the wrong
    one.
    """
    from ..live.secrets import agent_key_env_var, load_agent_key

    agent_key = load_agent_key(network)
    if agent_key is not None:
        return agent_key
    env_var = agent_key_env_var(network)
    because = f" but {demanded_by}" if demanded_by else ""
    print(
        f"error: {env_var} is not set{because} — {remedy.rstrip('.')}. "
        f"({dotenv_diagnosis(env_var)}.)",
        file=sys.stderr,
    )
    return None
