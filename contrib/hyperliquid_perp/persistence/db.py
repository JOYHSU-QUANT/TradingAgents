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

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path

from .schema import MIGRATIONS, SCHEMA_MIGRATIONS_DDL

__all__ = ["Database", "apply_migrations", "connect"]


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
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
    return conn


def apply_migrations(conn: sqlite3.Connection) -> int:
    """Bring ``conn``'s schema up to the latest version; return that version.

    Idempotent: each ``MIGRATIONS`` version runs once, inside its own
    transaction, and is recorded in ``schema_migrations``. Re-opening an
    up-to-date DB applies nothing.
    """
    conn.execute(SCHEMA_MIGRATIONS_DDL)
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
            conn.execute("ROLLBACK")
            raise
    return latest


class Database:
    """A migrated SQLite store with an explicit, non-nesting transaction boundary."""

    def __init__(self, path: str | Path) -> None:
        self._conn = connect(path)
        self._in_transaction = False
        try:
            apply_migrations(self._conn)
        except BaseException:
            # The caller never receives the instance, so nothing else can
            # release the already-open connection.
            self._conn.close()
            raise

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

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
