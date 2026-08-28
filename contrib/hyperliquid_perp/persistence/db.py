"""SQLite connection, transaction boundary, and schema migrations.

:class:`Database` owns one connection in autocommit mode (``isolation_level =
None``) so *this* module — not sqlite3's implicit-transaction heuristics —
controls exactly when a transaction opens and closes. All writes go through
:meth:`Database.transaction`, whose contract is phase2-data §1:

    BEGIN → (fill, fee/PnL, position + account update, slice mark, order events) → COMMIT

A crash or exception before ``COMMIT`` rolls the whole unit back; once
``COMMIT`` returns the change is durable and the UNIQUE constraints keep a retry
from re-applying it. Nesting is rejected (a single flat transaction per unit of
work), so a caller can never accidentally commit half its work.

:func:`apply_migrations` runs the versioned DDL from :mod:`.schema` in order and
records each version in ``schema_migrations``, so opening an existing DB is
idempotent and a future schema change is an append to ``MIGRATIONS``.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path

from .schema import MIGRATIONS, SCHEMA_MIGRATIONS_DDL

__all__ = [
    "Database",
    "SchemaVersionError",
    "apply_migrations",
    "connect",
    "stored_schema_version",
]

logger = logging.getLogger(__name__)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# PR3 runs a 30s market-monitor loop and a 4h scheduler cycle against the same
# store; WAL lets a reader overlap a writer and busy_timeout turns a residual
# lock collision into a bounded wait instead of an immediate SQLITE_BUSY.
_BUSY_TIMEOUT_MS = 5000


def connect(path: str | Path) -> sqlite3.Connection:
    """Open a SQLite connection tuned for this store.

    Autocommit mode (``isolation_level = None``) hands transaction control to
    :class:`Database`; ``foreign_keys`` is enabled defensively even though the
    schema keeps referential links soft (the accounting layer resolves them), and
    ``Row`` gives name-addressable rows to the repository. WAL + ``busy_timeout``
    set the concurrency posture (an in-memory DB ignores WAL — fine, it is never
    shared across connections).
    """
    # ``str(path)`` so a Path (incl. the special ":memory:" string) both work.
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL can silently fall back to the prior journal mode when the underlying
    # VFS lacks shared-memory support (some network mounts / exclusive-locking
    # setups) — no error is raised either way. An in-memory DB legitimately
    # ignores WAL and is never shared, so only a file-backed store that failed
    # to switch is worth flagging. Warn rather than raise: the store is still
    # correct, just degraded to serialized reader/writer access, which busy_timeout
    # keeps bounded — the concurrency posture PR3 relies on, not correctness.
    applied_mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
    if str(path) != ":memory:" and str(applied_mode).lower() != "wal":
        logger.warning(
            "journal_mode is %r (not WAL) for %s; reader/writer overlap is "
            "degraded to serialized access (busy_timeout still bounds lock waits).",
            applied_mode,
            path,
        )
    conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
    return conn


class SchemaVersionError(RuntimeError):
    """The store's schema does not match what this build can safely operate on."""


def stored_schema_version(conn: sqlite3.Connection) -> int:
    """The highest migration recorded in the store (0 for a fresh/empty one).

    Reads ``schema_migrations`` WITHOUT applying anything, so a caller can
    decide whether opening this store is safe before it is changed.
    """
    conn.execute(SCHEMA_MIGRATIONS_DDL)
    row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    return 0 if row is None or row[0] is None else int(row[0])


def apply_migrations(conn: sqlite3.Connection) -> int:
    """Bring ``conn``'s schema up to the latest version; return that version.

    Idempotent: each ``MIGRATIONS`` version runs once, inside its own
    transaction, and is recorded in ``schema_migrations``. Re-opening an
    up-to-date DB applies nothing.

    A store NEWER than this build is refused rather than used. Nothing else
    reads ``schema_migrations``, so without this an older binary opens a
    migrated store silently and writes through it with the newer columns
    unknown to its own SQL — e.g. a rolled-back build's ``upsert_current_position``
    has no ``exchange_liquidation_price`` clause, so a stale mirrored
    liquidation price survives a flip/flatten/re-open that the current build
    would have cleared, and the §17 band can then read a liquidation that
    belongs to a position that no longer exists (2026-07-30 migration review).
    """
    latest_known = max(MIGRATIONS)
    found = stored_schema_version(conn)
    if found > latest_known:
        raise SchemaVersionError(
            f"store schema is v{found} but this build only knows v{latest_known} — "
            "it was migrated by a NEWER build. Refusing to open it: this build's SQL "
            "does not know the newer columns and would write through them, corrupting "
            "state the newer build relies on. Run the newer build, or restore a backup "
            "taken before the upgrade."
        )
    applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
    latest = 0
    for version in sorted(MIGRATIONS):
        latest = max(latest, version)
        if version in applied:
            continue
        # Each version is one atomic step: either every statement and the
        # bookkeeping row commit together, or none do. IMMEDIATE for the same
        # reason as Database.transaction(): this is a write transaction, so
        # take the write lock up front and let busy_timeout bound the wait.
        conn.execute("BEGIN IMMEDIATE")
        try:
            for statement in MIGRATIONS[version]:
                conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (version, _utcnow_iso()),
            )
            conn.execute("COMMIT")
        except BaseException:
            # Mirror Database.transaction(): suppress a secondary ROLLBACK error
            # (e.g. a lock/busy condition that also broke the migration) so the
            # original migration failure is what propagates, not a rollback-time
            # error masking the real root cause.
            with suppress(Exception):
                conn.execute("ROLLBACK")
            raise
    return latest


class Database:
    """A migrated SQLite store with an explicit, non-nesting transaction boundary.

    Concurrency contract (PR2): one :class:`Database` wraps one connection opened
    with the sqlite3 default ``check_same_thread=True``, and both the nesting flag
    and ``BEGIN``/``COMMIT`` sequencing assume a single owning thread — cross-thread
    use fails loud rather than racing the check-then-``BEGIN``. When PR3 adds its
    30s monitor and 4h scheduler loops it owns the concurrency model (a per-loop
    ``Database`` each with its own connection, an explicit lock, or a cooperative
    single thread); this class deliberately does not pick one yet.
    """

    def __init__(
        self, path: str | Path, *, migrate: bool = True, defer_migration: bool = False
    ) -> None:
        """Open the store under one of three schema policies.

        ``migrate=True`` (default) — this command owns the store and upgrades it
        on open. ``migrate=False`` — a reporting command: refuse either mismatch
        rather than touch a store a daemon may own. ``defer_migration=True`` —
        open as-is, refuse nothing, and leave the upgrade to the caller once it
        HOLDS THE LEASE.

        The third policy exists because ``migrate=True`` necessarily runs before
        the lease can be taken (the lease lives in the store being opened), so an
        owning command upgraded the schema underneath a running sibling daemon
        and only THEN discovered it had to refuse — doing the damage on the way
        to declining to do it. The lease columns arrived in migration v3 and
        every later migration only ADDS columns and tables, so the lease reads
        and writes (``SELECT *`` plus a patch-style upsert of the v3 columns)
        are safe against any store from v3 up — which is every store a current
        build can meet outside a test.

        A caller that defers MUST call :meth:`apply_deferred_migration` once it
        owns the run; that is the point of deferring, so the timing is
        deliberately the caller's to choose and is not enforced here
        (2026-07-31 review). :attr:`migration_pending` says whether it still
        owes one.
        """
        if defer_migration and migrate:
            raise ValueError(
                "defer_migration requires migrate=False — it IS the deferral, and "
                "migrate=True would already have upgraded the store on open"
            )
        self._conn = connect(path)
        self._in_transaction = False
        self._migration_pending = False
        try:
            if defer_migration:
                # Owed only when the store is not at this build's version —
                # behind, or ahead (the deferred apply then refuses by name).
                # A current store owes nothing, so a caller's "before I
                # migrate" guards stay quiet on a routine restart.
                self._migration_pending = stored_schema_version(self._conn) != max(MIGRATIONS)
            if migrate:
                apply_migrations(self._conn)
            elif not defer_migration:
                # Read-style commands (validate / export / live-smoke
                # --gate-status) pass migrate=False. They take no run lease, so
                # migrating here would silently upgrade a store a RUNNING
                # daemon owns — turning a "just preview the gate" command on the
                # deploy box into a mixed-version corruption vector. Refuse
                # instead and let the operator upgrade deliberately, through a
                # command that does hold the lease (2026-07-30 migration review).
                found = stored_schema_version(self._conn)
                latest_known = max(MIGRATIONS)
                if found > latest_known:
                    raise SchemaVersionError(
                        f"store schema is v{found} but this build only knows "
                        f"v{latest_known} — it was migrated by a NEWER build. "
                        "Run the newer build, or restore a backup taken before "
                        "the upgrade."
                    )
                if found < latest_known:
                    raise SchemaVersionError(
                        f"store schema is v{found}; this build needs v{latest_known}. "
                        "This is a reporting command and will not migrate the store — "
                        "a daemon may be running against it. Stop that daemon, then run "
                        "the command that OWNS this store to migrate it: `paper "
                        "--run-id <id> --db <this db>` for a paper store, or `live "
                        "--run-id <id> --db <this db>` for a live one. Then retry."
                    )
        except BaseException:
            # The caller never receives the instance, so nothing else can
            # release the already-open connection.
            self._conn.close()
            raise

    @property
    def migration_pending(self) -> bool:
        """True while the upgrade a ``defer_migration`` open owes is still unpaid.

        Answered by the handle itself, not by a local the caller must keep in
        step with the constructor call (issue #129); cleared only by a
        successful :meth:`apply_deferred_migration`.
        """
        return self._migration_pending

    def apply_deferred_migration(self) -> None:
        """Pay the deferred upgrade — the caller now owns the store.

        A no-op when nothing is owed. Raises :class:`SchemaVersionError` for a
        store migrated by a NEWER build (nothing is written first), and leaves
        :attr:`migration_pending` set in that case.
        """
        if not self._migration_pending:
            return
        apply_migrations(self._conn)
        self._migration_pending = False

    @property
    def conn(self) -> sqlite3.Connection:
        """The underlying connection (read queries; writes go through ``transaction``)."""
        return self._conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Run a unit of work atomically: BEGIN, then COMMIT, or ROLLBACK on error.

        Rejects nesting: the accounting layer commits one fill/funding event per
        transaction, and a nested ``BEGIN`` would silently no-op and let an inner
        failure leave a partially-applied outer transaction committed.

        ``BEGIN IMMEDIATE``: every unit of work here writes, and a deferred
        ``BEGIN`` would take a read snapshot first and only upgrade to the write
        lock at the first INSERT — an upgrade that fails *immediately* with
        ``SQLITE_BUSY_SNAPSHOT`` (not subject to ``busy_timeout``) if another
        writer committed meanwhile. Taking the write lock up front turns that
        collision into the bounded ``busy_timeout`` wait ``connect`` promises.
        The nesting flag is only set once ``BEGIN`` succeeds: a failed ``BEGIN``
        (e.g. a lock timeout) leaves no transaction open, so it must not leave
        the flag stuck and brick every later unit of work.
        """
        if self._in_transaction:
            raise RuntimeError("Database.transaction() cannot be nested")
        self._conn.execute("BEGIN IMMEDIATE")
        self._in_transaction = True
        try:
            yield self._conn
        except BaseException:
            # Suppress a secondary ROLLBACK error (e.g. a lock/busy hiccup that
            # also broke this unit) so the original body exception is what
            # propagates — mirrors the COMMIT-failure branch below and
            # apply_migrations, so a rollback-time error can't mask the real cause.
            with suppress(Exception):
                self._conn.execute("ROLLBACK")
            raise
        else:
            try:
                self._conn.execute("COMMIT")
            except BaseException:
                # A COMMIT that itself fails (e.g. SQLITE_BUSY) can leave the
                # transaction open; roll back so the next unit starts clean rather
                # than folding this unit's pending writes into it. Suppress a
                # secondary rollback error so the original COMMIT failure is what
                # propagates.
                with suppress(Exception):
                    self._conn.execute("ROLLBACK")
                raise
        finally:
            self._in_transaction = False

    @contextmanager
    def read_transaction(self) -> Iterator[sqlite3.Connection]:
        """Give every read inside one consistent snapshot (no write lock taken).

        A deferred ``BEGIN``: the snapshot is pinned at the first SELECT and
        WAL lets it overlap the PR3 writer loops without blocking them. Replay
        needs this — the reads it compares against each other (fills, funding,
        adjustments, seeds, ``current_*``) would otherwise interleave with a
        concurrent COMMIT and report a spurious (or masked) mismatch. Reads only: ``PRAGMA query_only`` makes a stray write inside
        the block fail loud instead of being silently discarded by the closing
        ROLLBACK. Nesting is rejected for the same reason as ``transaction``.
        """
        if self._in_transaction:
            raise RuntimeError("Database.read_transaction() cannot be nested")
        self._conn.execute("PRAGMA query_only = ON")
        try:
            self._conn.execute("BEGIN")
            self._in_transaction = True
            try:
                yield self._conn
            finally:
                self._in_transaction = False
                # A read transaction has nothing to persist; ROLLBACK simply
                # releases the snapshot (and is what makes query_only safe).
                with suppress(Exception):
                    self._conn.execute("ROLLBACK")
        finally:
            # Same masking guard as the ROLLBACKs above: if turning query_only
            # back off itself raised while a body exception is propagating, it
            # would replace the original with a less useful PRAGMA error.
            with suppress(Exception):
                self._conn.execute("PRAGMA query_only = OFF")

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
