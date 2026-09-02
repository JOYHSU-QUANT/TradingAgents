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

from .schema import LEASE_READABLE_SINCE, MIGRATIONS, SCHEMA_MIGRATIONS_DDL

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


def _newer_build_error(found: int, latest_known: int) -> SchemaVersionError:
    """The one wording of "a NEWER build migrated this store" (see :func:`apply_migrations`)."""
    return SchemaVersionError(
        f"store schema is v{found} but this build only knows v{latest_known} — "
        "it was migrated by a NEWER build. Refusing to open it: this build's SQL "
        "does not know the newer columns and would write through them, corrupting "
        "state the newer build relies on. Run the newer build, or restore a backup "
        "taken before the upgrade."
    )


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

    Idempotent across PROCESSES too: two owning commands started against the
    same behind store in the same moment (two ``live`` on the shared
    ``live_trading.db`` of RUNBOOK-live §7.3) both read the same "not yet
    applied" set up front, and each version is re-checked inside its own
    ``BEGIN IMMEDIATE`` — the loser waits on the winner's write lock, then
    sees the version recorded and skips it. Without that re-read the loser
    re-ran the winner's ``ALTER TABLE ... ADD COLUMN`` and died on
    ``duplicate column name`` (issue #147), a traceback exit 2 from a race
    that ``BEGIN IMMEDIATE`` had already serialized correctly. The skip does
    not ask WHOSE version it skipped: a winner running a newer build has
    carried the store past what this one knows, and skipping its versions
    silently would be exactly the write-through the NEWER refusal below
    exists to stop — so the recorded version is read once more after the
    loop and that refusal raised there. After the loop rather than per step
    because the winner commits one version per transaction: a verdict inside
    a step could only see what had landed by then, while the final read sees
    everything landed up to the moment it runs. A newer build that lands
    after it is the same check-then-act window the lease ordering already
    accepts (issue #129: the sibling check is a read, not a lock). The
    loser's wait is
    bounded by :func:`connect`'s ``busy_timeout``;
    a winner whose single step outlasted it would leave the loser with
    ``database is locked`` (an ``OperationalError``, so main()'s exit 2) —
    every step here is a handful of DDL statements, far inside that bound.

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
        raise _newer_build_error(found, latest_known)
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
            # Re-read under the write lock: the set above was taken outside
            # it, and a concurrent process may have applied this version in
            # between. Nothing has been written yet, so the skip's ROLLBACK
            # undoes nothing — it only releases the lock. Whether that process
            # was a NEWER build is judged once, after the loop (see below).
            landed = conn.execute(
                "SELECT 1 FROM schema_migrations WHERE version = ?", (version,)
            ).fetchone()
            if landed is not None:
                conn.execute("ROLLBACK")
                continue
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
    # A concurrent NEWER build is judged here, not per step: it commits one
    # version per transaction, so a verdict inside any one step could only see
    # what had landed by then, while this read sees everything landed up to
    # the moment it runs — including versions the loop skipped past as
    # "already applied" without knowing whose they were.
    newest = stored_schema_version(conn)
    if newest > latest_known:
        raise _newer_build_error(newest, latest_known)
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
        open a populated store as-is and leave the upgrade to the caller once
        it HOLDS THE LEASE; refused at open only when the store was migrated
        by a NEWER build (nothing has been written yet, and it never becomes
        this build's to upgrade) or predates the lease columns entirely (see
        below), and an EMPTY store — no schema at all — is built in full,
        since nothing can own it.

        The third policy exists because ``migrate=True`` necessarily runs before
        the lease can be taken (the lease lives in the store being opened), so an
        owning command upgraded the schema underneath a running sibling daemon
        and only THEN discovered it had to refuse — doing the damage on the way
        to declining to do it. What the deferring caller may touch before it
        pays the upgrade is declared once, beside the schema: the lease reads
        and writes (``SELECT *`` plus a patch-style upsert of the lease
        columns) are safe against any store from
        :data:`~.schema.LEASE_READABLE_SINCE` up — which is every store a
        current build can meet outside a test — and a populated store OLDER
        than that floor is refused here by name (issue #147), since its first
        lease read would otherwise die as an ``OperationalError`` on a column
        it does not have. That refusal covers ``migrate=False`` too, so the
        reporting commands name the same remedy for such a store instead of
        sending the operator to an owning command that would refuse it again.

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
            if migrate:
                apply_migrations(self._conn)
                return
            found = stored_schema_version(self._conn)
            latest_known = max(MIGRATIONS)
            if found > latest_known:
                # Refused under BOTH non-migrating policies, at open, before
                # the caller has written anything: a deferring command would
                # otherwise stamp its lease into columns it does not know and
                # only then discover the store is not its to upgrade.
                raise _newer_build_error(found, latest_known)
            if 0 < found < LEASE_READABLE_SINCE:
                # Populated, but from before the lease columns existed: a
                # deferring caller's very next read (the lease) would raise
                # sqlite3.OperationalError, which reaches main() as a traceback
                # exit 2 instead of a named exit 1. No current build writes
                # such a store; refusing keeps the deferral's "nothing touched
                # before the lease" promise honest. Like the NEWER refusal
                # above it sits ahead of the policy split because it is a fact
                # about the STORE, not about the policy: the read-only branch
                # below would refuse this store too, but with a remedy
                # (`paper`/`live`) that defers and so lands right back here —
                # one store state, one instruction. The remedy is the one
                # command that migrates on open and takes no lease; it goes on
                # to refuse a paper run (safe mode is live-run state), which
                # is why the message says so.
                raise SchemaVersionError(
                    f"store schema is v{found}; the run lease an owning command "
                    f"consults before upgrading a store arrived in "
                    f"v{LEASE_READABLE_SINCE}, so neither an owning nor a reporting "
                    "command can open this store as-is. Upgrade it with `safe-mode --status "
                    "--run-id <id> --db <this db>`, which migrates on open, after "
                    "confirming no other process has this store open. For a paper "
                    "run it then reports that the run is not a live run — the "
                    "upgrade has already happened at that point; retry this command."
                )
            if defer_migration:
                if found == 0:
                    # No schema at all (a ``touch``, or an open that died
                    # before its first migration committed): nothing can own
                    # it and there is no lease table to consult, so build it
                    # in full — there is nobody to defer to.
                    apply_migrations(self._conn)
                else:
                    # Owed only when the store is behind; a current store owes
                    # nothing, so a caller's "before I migrate" guards stay
                    # quiet on a routine restart.
                    self._migration_pending = found < latest_known
            else:
                # Read-style commands (validate / export / live-smoke
                # --gate-status) pass migrate=False. They take no run lease, so
                # migrating here would silently upgrade a store a RUNNING
                # daemon owns — turning a "just preview the gate" command on the
                # deploy box into a mixed-version corruption vector. Refuse
                # instead and let the operator upgrade deliberately, through a
                # command that does hold the lease (2026-07-30 migration review).
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

        A no-op when nothing is owed (a NEWER store was already refused at
        open, so only a behind store ever reaches here).
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
