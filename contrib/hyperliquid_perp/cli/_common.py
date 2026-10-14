"""Shared guards for the CLI subcommands.

The refusal helpers more than one subcommand routes through: open-an-existing
store (never CREATE one implicitly), the standard missing-run and wrong-run-mode
messages, the store verdicts on a ``sqlite3.Error`` — at that open
(:class:`StoreIntegrityFailure`, which :func:`.main` prints as the exit-5
``store integrity failure``) and past it (the same verdict, and the named exit
1 beside it) — the
SIGTERM→KeyboardInterrupt mapping every long-running command installs, and the
OPENROUTER_API_KEY gate.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from ..common.decision_source import decision_source
from ..config import dotenv_diagnosis
from ..persistence import repository as repo
from ..persistence.db import Database
from ..persistence.db_types import (
    SchemaVersionError,
    is_store_integrity_error,
    is_store_refusal_error,
)
from ..runtime.run_identity import OpenedRun, RunIdentityRefusal, RunIdentityStage, open_run

logger = logging.getLogger(__name__)


def _raise_keyboard_interrupt(signum, frame) -> None:
    """SIGTERM → the SIGINT path: systemd/docker/``kill`` stop with the default
    TERM signal, and phase2-data §1.1's shutdown export must fire for them
    exactly as it does for Ctrl-C."""
    raise KeyboardInterrupt


class StoreIntegrityFailure(RuntimeError):
    """The shared open found the ``--db`` is not a SQLite database, or is corrupt.

    Raised by :func:`_open_existing_db` and :func:`_open_run_or_exit` in place
    of the ``sqlite3.DatabaseError`` the store raised, and caught once, in
    :func:`.main`, as the exit-5 ``store integrity failure`` verdict — so every
    subcommand that opens a store through them, the owning ``paper`` / ``live``
    / real ``live-smoke`` included, gives that file the same answer the
    report-only commands do, without each wrapping its open (issue #362). Not
    a ``sqlite3.Error``: a command's own ``except sqlite3.Error`` around the
    open is for what happens AFTER it, and must not re-classify this.
    """


def _store_integrity_failure(exc: BaseException) -> int:
    """The exit-5 verdict on a store a command cannot read AS a store: "the
    store is broken; investigate before trusting results".

    One wording and one code for the report-only ``export`` / ``validate`` /
    ``status`` and the two diagnostics ``live-smoke --gate-status`` /
    ``safe-mode`` (the latter migrates and writes — not report-only, see
    :func:`_open_existing_db`): a ``--db`` that exists and passes the path
    guards but is not SQLite, a corrupt one, an I/O failure mid-read, a cell
    or state the reading model refuses. A store that could not be OPENED is
    the opposite verdict (named exit 1, see :func:`_open_existing_db`) —
    except the file that is not a database, which IS this verdict and is
    raised from the open itself as :class:`StoreIntegrityFailure` for
    :func:`.main` to print in these same words: telling an operator whose
    disk is rotting that they mistyped ``--db`` is the error this split
    exists to avoid.
    """
    print(f"error: store integrity failure — {exc}", file=sys.stderr)
    return 5


def _sqlite_error_exit(exc: sqlite3.Error, *, refused: str) -> int:
    """A command's answer to a ``sqlite3.Error`` past the open guards: three ways.

    A store that is itself broken (``is_store_integrity_error``: not a
    database, corrupt, a disk that fails under it) is the exit-5 verdict.
    What SQLite refuses for a reason outside the store
    (``is_store_refusal_error``: a lock held past ``busy_timeout``, a file
    that denies the write, a full disk) is the named exit 1, printed as
    ``error: <refused> — <what SQLite said>``. Both arms are positive, so
    what is neither — an API misuse, a closed connection, ``no such table``
    from a schema a migration did not build: a tool bug — is re-raised to
    ``main``'s last resort, traceback and exit 2, rather than borrowing an
    operator's code. Issues #357 / #359: before, each of these commands let
    the first kind crash as the third.
    """
    if is_store_integrity_error(exc):
        return _store_integrity_failure(exc)
    if is_store_refusal_error(exc):
        print(f"error: {refused} — {exc}", file=sys.stderr)
        return 1
    raise exc


def _open_existing_db(
    path: str, *, migrate: bool = False, defer_migration: bool = False
) -> Database | None:
    """Open an existing store, or report why not (never CREATE one implicitly).

    ``Database(path)`` would happily create an empty schema — and an offline
    command against a typo'd path would then "succeed" with zero rows.

    ``migrate`` splits the callers by what they actually do:

    * ``False`` (default) for the genuinely REPORT-ONLY commands — ``validate``,
      ``export``, ``status``, ``live-smoke --gate-status``. They take no run lease, so
      migrating would silently upgrade a store a running daemon owns, leaving
      that daemon writing through a schema it does not know. They refuse with
      instructions instead (2026-07-30 migration review). One exception to
      "report-only", still lease-free: ``export --backfill-format-fingerprint``
      fills ``NULL`` cells the daemon never touches again
      (``persistence.backfill``, issue #163) — a write, but not one that
      needs the schema moved or the run owned.
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
    except sqlite3.Error as exc:
        _raise_if_broken_store(exc)
        raise


def _open_run_or_exit(db_path: str | Path, run_id: str, *, create: bool) -> OpenedRun | None:
    """:func:`~..runtime.run_identity.open_run`, or ``None`` after printing why.

    A refusal prints its stage's wording, which ``paper`` and ``live --run-id``
    share; a ``SchemaVersionError`` prints its own text, as
    :func:`_open_existing_db` prints it.
    """
    try:
        return open_run(db_path, run_id, create=create)
    except SchemaVersionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return None
    except sqlite3.Error as exc:
        _raise_if_broken_store(exc)
        raise
    except RunIdentityRefusal as refusal:
        wording = {
            RunIdentityStage.MISSING_RUN: (
                f"run {run_id!r} does not exist in {db_path}. Pass --create "
                "to start it, or fix --run-id / --db to resume the intended run."
            ),
            RunIdentityStage.RUN_EXISTS: (
                f"run {run_id!r} already exists in {db_path}. Drop --create "
                "to resume it, or pick a new --run-id for a fresh run."
            ),
        }
        print(f"error: {wording[refusal.stage]}", file=sys.stderr)
        return None


def _raise_if_broken_store(exc: sqlite3.Error) -> None:
    """The open failures the path guards let through, named for ``main``.

    ``Database.__init__`` and the ``store_identity`` probe narrow themselves
    to ``OperationalError`` at ``connect()`` (every refusal outside the store
    is a named ``SchemaVersionError`` by the time it leaves them), so what
    still escapes as a raw ``sqlite3.Error`` is a file that is not a
    database or a corrupt one — plain ``DatabaseError`` — and, from the
    schema read after the connect (and, for ``migrate=True``, the migration
    steps), a failing disk, a lock or a tool bug. The store itself broken
    (``is_store_integrity_error``: not a database, corrupt, the disk failing
    under it) is the verdict; the rest propagate as they were (the caller's
    own rule, or ``main``'s last resort).
    """
    if is_store_integrity_error(exc):
        raise StoreIntegrityFailure(str(exc)) from exc


def _migrate_owned_store(db: Database, *, run_id: str, now: datetime) -> bool:
    """Pay the upgrade a deferred open owes; True if it was REFUSED.

    The deferred opens are :func:`~..runtime.run_identity.open_run` and
    :func:`_open_existing_db` with ``defer_migration=True``.

    A no-op when nothing is owed (the store was built on open, or a dry run
    opened it read-only), so every owning command calls it unconditionally at
    the point it owns ITS RUN. Owning the run is not owning the file, though:
    the lease is per-``run_id`` while a migration rewrites the whole store, so
    a sibling run in the same file — a paper run, or the other network's run
    that RUNBOOK-live §7.3 keeps in the same ``live_trading.db`` — would have
    its schema upgraded underneath it (issue #129, the store-wide half). When
    an upgrade is actually owed, any OTHER run's fresh lease in this store
    refuses it by name; a store that is already current never refuses, so a
    routine restart beside a running sibling is unaffected.

    The refusal is printed here as a named exit 1 — never main()'s exit-2
    last resort — and a caller that already holds the lease must make this
    call inside the lease-releasing ``try`` so a refusal frees the lease
    instead of parking it on a dead pid for LOCK_STALE_SECONDS. The check is
    a read, not a lock: a sibling that takes its lease a moment later is
    migrated under after all — bounded harm, since every migration is one
    ``BEGIN IMMEDIATE`` step that only ADDs columns and tables.
    """
    from contrib.core.instants import parse_instant

    from ..persistence import repository as repo
    from ..runtime.run_lock import LOCK_STALE_SECONDS, lease_age_label

    if not db.migration_pending:
        return False
    for row in repo.iter_other_run_leases(db.conn, run_id):
        heartbeat_at = parse_instant(row["lock_heartbeat_at"])
        if (now - heartbeat_at).total_seconds() < LOCK_STALE_SECONDS:
            print(
                f"error: this build needs to migrate the store, but run {row['run_id']!r} "
                f"in it is being driven by pid {row['lock_pid']} right now (heartbeat "
                f"{lease_age_label(heartbeat_at, now=now)} ago) — upgrading the schema "
                "underneath that process would "
                "corrupt its state. Stop it (or wait for its lease to go stale), or run "
                "the build it was started with.",
                file=sys.stderr,
            )
            return True
    try:
        db.apply_deferred_migration()
    except SchemaVersionError as exc:
        # A NEWER store is refused at open, so this is only reachable when
        # another build upgraded the store between our open and now. Still a
        # named exit 1: uncaught it would reach main()'s last-resort handler
        # as exit 2, losing the code the RUNBOOK documents and a supervisor
        # branches on.
        print(f"error: {exc}", file=sys.stderr)
        return True
    return False


def _existing_run_row(conn, run_id: str, db_label: str, *, not_found_hint: str = ""):
    """The run's row, or ``None`` after printing the standard missing-run error.

    The one encoding of the "does this run exist" refusal, shared by every
    read-style command (``validate``, ``status``, ``live-smoke --gate-status``,
    and ``live-smoke``'s real-run build) so a wording tweak cannot land on one
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


def announce_engine_config_protection_only(exc, *, where: str, alive: str) -> None:
    """The one wording for "the engine could not be built over live work".

    Both lanes (``paper``'s healthy restart, ``live --loop`` after the §19.1
    verdict; issue #268) reach this when provider construction raised
    ``EngineConfigError`` over live work, and both must say the same thing:
    the cause, that the position is guarded, that NEW cycles are halted, and
    the remedy.
    ``where`` names the run for the log ("on restart of r1", "for live run
    r1"); ``alive`` is the lane's list of what keeps running.
    """
    logger.error(
        "the engine could not be built %s with a live position — entering "
        "protection-only mode: %s",
        where,
        exc,
    )
    print(
        f"ERROR: {exc}\nThis run holds a live position — running in "
        f"protection-only mode: {alive} stay live, NEW decision cycles stay "
        "halted. Fix the environment and restart to resume trading.",
        file=sys.stderr,
    )


def holds_live_work(engine) -> bool:
    """Whether a startup refusal has anything to guard — failing toward "yes".

    The decision both lanes make on an operator-fixable startup fault (a
    missing key on paper, an ``EngineConfigError`` on either): live work means
    protection-only, flat means a named exit. ``has_active_work`` is a store
    read (on paper, behind the engine's own halted-state guard), and a raise
    from it — an operator's export/validate holding the SQLite lock, or
    that guard — must not turn into an exit over a position nobody watches:
    unknown ≠ flat, the shutdown sweep's own rule (#268 review). Startup
    only: the live loop's per-tick settle check reads raw under the loop's
    containment instead, so a lock that persists stays visible (safe mode)
    rather than becoming an ERROR every ~10s that no tool reports.
    """
    try:
        return engine.has_active_work()
    except Exception:  # noqa: BLE001 — unknown ≠ flat; the process must watch the position
        logger.exception("could not read whether the run holds live work — treating it as live")
        return True


def announce_protection_only_settled(cause: str, *, then: str) -> None:
    """The one wording for protection-only's settle-exit over an unbuildable engine.

    The position closed with new cycles halted: no later iteration can do
    anything, so the loop ends loud (exit 1 in both lanes) naming the cause
    the operator has to fix; ``then`` is the lane's tail ("exporting the
    final state and exiting", "exiting").
    """
    print(
        "protection-only mode has nothing left to protect (the position is "
        "closed and new cycles stayed halted because the engine could not be "
        f"built: {cause}) — {then}. Fix the environment and restart to resume "
        "this run.",
        file=sys.stderr,
    )


def note_stranded_attempt(db: Database, run_id: str, *, never: str, restart_will: str) -> None:
    """Protection-only's note about a prior process's in-progress decision.

    Purely informational, for both lanes: only a healthy restart may finish
    the attempt (§3.1 resumes the SAME attempt, without burning its retry
    budget) — terminalizing it here would destroy that resumable state.
    ``never`` is what this mode never does ("polls the scheduler", "builds
    the decision driver"); ``restart_will`` is what the next healthy restart
    does with it ("resumes it", "adopts it"). Both phrases land in both the
    log line and the stderr note.

    Best-effort, like every other write-free courtesy on a startup whose one
    job is keeping SL/TP alive: the lookup is fail-loud on a store holding
    two in-progress rows (the wedge the healthy lane escalates by name), and
    a raise here must not leave the loop before it watches the position —
    logged with its traceback, the note is simply not printed.
    """
    try:
        stranded = repo.find_in_progress_attempt(db.conn, run_id)
    except Exception:  # noqa: BLE001 — a courtesy note must not end a protection-only start
        logger.exception("could not look up a stranded in-progress decision attempt")
        return
    if stranded is None:
        return
    attempt_id = stranded["decision_attempt_id"]
    logger.warning(
        "decision attempt %s remains in_progress; protection-only never %s, so it "
        "stays open until the next healthy restart %s",
        attempt_id,
        never,
        restart_will,
    )
    print(
        f"note: decision attempt {attempt_id!r} from the previous process remains "
        f"in_progress — protection-only mode never {never}; the next healthy "
        f"restart {restart_will}.",
        file=sys.stderr,
    )


def _api_key_satisfied(config: dict) -> bool:
    """Whether the run ``config`` describes has the model key it needs.

    The ONE answer to "does this run need ``OPENROUTER_API_KEY``": an engine
    run needs it set, a ``file_target`` run (``common.decision_source``) reads
    the carry handoff, calls no model and needs nothing. Both daemons read
    this — the fresh-run and restart lanes of ``paper``, ``live --loop`` —
    rather than each composing the provider's requirement by hand.
    """
    return not decision_source(config).drives_engine or bool(os.environ.get("OPENROUTER_API_KEY"))


def _require_api_key(config: dict) -> bool:
    """True when the run has the key it needs (:func:`_api_key_satisfied`); else print the abort message.

    Checked only on paths that will actually drive the AI engine — a fresh paper
    run (always, before the run row is written), a healthy paper restart with
    nothing live to protect, and ``live --loop`` (up front, alongside its config
    validation). A paper restart into protection-only mode never polls the AI,
    so it runs keyless — and a keyless healthy restart holding live work falls
    back to that same mode rather than exiting (the caller owns that fork:
    reconcile has already canceled the plans, so exiting would leave the
    position with nobody watching its SL/TP). ``live`` WITHOUT ``--loop`` is the
    same keyless case: it arms, sweeps and exits without ever polling the AI.
    A ``file_target`` run passes whatever the environment holds: its provider
    reads the carry handoff and calls no model, on either lane.
    """
    if _api_key_satisfied(config):
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
