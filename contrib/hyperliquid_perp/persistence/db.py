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
from stat import S_ISDIR
from urllib.parse import quote

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

# sqlite3's own spelling for "a private database in RAM". Named because two
# places now branch on it — ``connect`` (WAL never applies) and the
# foreign-store refusal (there is no file to inherit anything from).
_IN_MEMORY = ":memory:"


def connect(path: str | Path) -> sqlite3.Connection:
    """Open a SQLite connection tuned for this store.

    Autocommit mode (``isolation_level = None``) hands transaction control to
    :class:`Database`; ``foreign_keys`` is enabled defensively even though the
    schema keeps referential links soft (the accounting layer resolves them), and
    ``Row`` gives name-addressable rows to the repository. WAL + ``busy_timeout``
    set the concurrency posture (an in-memory DB ignores WAL — fine, it is never
    shared across connections).

    ``sqlite3.connect`` itself is lazy — it opens no file and validates nothing —
    so the first PRAGMA below is what actually fails on a path that is not a
    SQLite database at all (``DatabaseError: file is not a database``). That
    used to leave a live handle nobody holds a reference to, which on Windows
    keeps the file locked against the very ``unlink`` an operator reaches for
    next (issue #175). Every failure here now closes the handle before it
    propagates; the error itself is already clear and is left alone.
    """
    # ``str(path)`` so a Path (incl. the special ":memory:" string) both work.
    conn = sqlite3.connect(str(path), isolation_level=None)
    try:
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
        if str(path) != _IN_MEMORY and str(applied_mode).lower() != "wal":
            logger.warning(
                "journal_mode is %r (not WAL) for %s; reader/writer overlap is "
                "degraded to serialized access (busy_timeout still bounds lock waits).",
                applied_mode,
                path,
            )
        conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
    except BaseException:
        conn.close()
        raise
    return conn


class SchemaVersionError(RuntimeError):
    """The store's schema does not match what this build can safely operate on.

    Or there is no store to read a schema from: a mistyped ``--db`` naming
    another application's database, a directory, or a path that cannot be read
    reaches the same verdict — this build will not operate on this file — and
    reaches it without reading a version at all (see
    :func:`_refuse_a_foreign_store`). One type, because nothing branches on the
    difference: every one of them is the CLI's named exit 1, and the remedy
    that does differ is already in the message.
    """


# SQLite reserves the ``sqlite_`` prefix, so no application can create an object
# with that name: everything matching it is bookkeeping SQLite made for itself
# (``sqlite_sequence`` behind an AUTOINCREMENT column, ``sqlite_stat*`` from
# ANALYZE, the implicit indexes behind UNIQUE constraints). Such an object is
# never evidence of what a file is FOR — it exists only because something else
# does — so a file holding nothing but those counts as empty here. ``_`` is a
# LIKE wildcard, hence the ESCAPE.
_FOREIGN_OBJECTS_SQL = (
    "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite@_%' ESCAPE '@' ORDER BY name"
)
_BOOKKEEPING_TABLE = "schema_migrations"

# What makes a file OURS. Deliberately not ``schema_migrations``: that name is
# the convention for Rails/ActiveRecord and golang-migrate among others, so a
# database carrying one is evidence that SOMEBODY migrates it, not that we do.
# These two have existed since v1, so every store a build can meet has them,
# and ``test_every_store_version_carries_the_tables_the_refusal_looks_for``
# pins that against a migration that renames one. ANY of them is enough: a
# rename should degrade this check, never turn a running daemon's own store
# into a refusal.
_STORE_TABLES = ("decision_attempts", "scheduler_state")

# A foreign object name is echoed back to the operator; ``sqlite_master.name``
# has no length limit, so one pathological name would swamp the message.
_MAX_NAME_CHARS = 40


def _sqlite_file_uri(file: Path) -> str:
    """``file`` as a SQLite URI, for any path this platform allows, relative or not.

    Spells the path only — the read-only flag belongs to the caller, which
    appends ``?mode=ro``.

    Not :meth:`Path.as_uri`, which is wrong here twice. It rejects a relative
    path outright, and ``--db paper.db`` is perfectly ordinary. And for a
    Windows UNC path it produces ``file://server/share/x.db``, whose authority
    SQLite refuses (``invalid uri authority: server``) — so a store on a share
    that opened fine before would stop opening at all. Both are fixed by
    resolving first and always emitting an EMPTY authority, which leaves a UNC
    path as ``file:////server/share/x.db``, the form SQLite reads.

    Percent-encoding matters: a path can hold a space (``C:/Users/JOY HSU``) or
    a ``#``, which SQLite would otherwise read as a fragment. ``:`` is left
    alone so a drive letter reads as itself, exactly as ``as_uri`` renders it.

    A Windows extended-length path (``\\\\?\\C:\\…``, for paths past 260
    characters) survives this too: its ``?`` is encoded to ``%3F``, and SQLite
    decodes it before opening, so the prefix reaches the OS intact. ``as_uri``
    renders the same path as ``file://%3F/C%3A/…``, which SQLite rejects for
    its authority — one more thing this spelling fixes rather than breaks.
    """
    posix = file.resolve().as_posix()
    if not posix.startswith("/"):
        posix = "/" + posix  # a drive-letter path: C:/… → /C:/…
    return "file://" + quote(posix, safe="/:")


def _is_our_unused_bookkeeping(conn: sqlite3.Connection) -> bool:
    """True when a lone ``schema_migrations`` is OURS and has recorded nothing.

    The one state this project leaves behind with no schema under it: an open
    that died before v1 committed, or an OLDER build's refusal, back when
    :func:`stored_schema_version` created this table before reading it. Both
    leave it empty and in our shape.

    Presence of the NAME proves nothing — golang-migrate's sqlite3 driver
    creates ``schema_migrations (version uint64, dirty bool)`` and often
    nothing else, so a database whose only table is that name is as likely to
    be somebody else's as ours. Passing one through on the name alone had two
    ways of going wrong, both reachable from a single mistyped ``--db``: a
    populated foreign one read its ``version`` as a schema number and told the
    operator the store "was migrated by a NEWER build … restore a backup",
    which is the one refusal the RUNBOOK says to treat as a rollback incident;
    an empty foreign one got past every guard and died inside the first
    migration on ``no column named applied_at`` — an unnamed exit 2, the shape
    this refusal exists to replace.
    """
    # A TABLE: ``PRAGMA table_info`` answers for a view too, and a foreign view
    # of that name would otherwise be called ours.
    is_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (_BOOKKEEPING_TABLE,),
    ).fetchone()
    if is_table is None:
        return False
    # EXACTLY our columns, not merely a superset: nothing in this project has
    # ever added one, so a wider table is somebody else's — and letting one
    # through means building the whole schema into their database, which is the
    # damage this refusal exists to stop.
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({_BOOKKEEPING_TABLE})")}
    if columns != {"version", "applied_at"}:
        return False
    return conn.execute(f"SELECT 1 FROM {_BOOKKEEPING_TABLE} LIMIT 1").fetchone() is None


def _refuse_a_foreign_store(path: str | Path) -> None:
    """Refuse a ``--db`` this build must not open, by name.

    Chiefly a SQLite file belonging to another application, and with it the
    path mistypes that sit either side of one: a directory, a parent that is
    missing or is not a directory, a file that cannot be read. Every one of
    those used to reach ``main()``'s last resort as an exit-2 ``unable to open
    database file``, which tells an operator nothing about which of them it was.

    "EMPTY store" used to mean ``MAX(schema_migrations.version) == 0``, which is
    a fact about OUR bookkeeping, not about the file: another application's
    database has no such rows either, so a mistyped ``--db`` was read as an
    empty store. A reporting command then wrote ``schema_migrations`` into that
    file on the way to refusing it, and an owning command (``defer_migration``)
    skipped the refusal entirely and built all of this project's tables into it,
    after which the file looks like a store to whoever opens it next (issue
    #174). EMPTY now means empty: no objects of the file's own.

    Runs BEFORE :func:`connect`, because connecting is itself a write — ``PRAGMA
    journal_mode = WAL`` rewrites the header of a database not already in WAL —
    and on a READ-ONLY connection, because merely opening a database read-write
    is one too: SQLite checkpoints an uncheckpointed ``-wal`` into the main file
    and removes it when the last connection closes, so a probe would have
    performed a crashed foreign application's recovery for it. Under
    ``mode=ro`` the database file is never modified, whatever journal mode or
    crash state it is in. That is also exactly what the refusal claims, and no
    more: opening a WAL database read-only materialises SQLite's own empty
    ``-shm`` / ``-wal`` pair beside one that had none, which its owner reclaims
    on its next open. Unlinking those again would mean racing the sidecars of a
    process that may be live — worse than leaving them.

    ``immutable=1`` would leave even those alone but is unusable here: it
    ignores the ``-wal``, so a LIVE foreign database reads back as holding no
    objects at all — the one answer that would have this build write into it.

    A file that is not a SQLite database at all raises ``sqlite3.DatabaseError``
    from the read, as it did from ``connect``.
    """
    if str(path) == _IN_MEMORY:
        return  # a fresh private database every time; nothing to inherit
    file = Path(path)
    try:
        info = file.stat()
    except FileNotFoundError:
        parent = file.parent
        if parent.is_dir():
            return  # nothing there yet; connect() creates it, as it always has
        # There is nowhere to create it, so ``connect`` raises `unable to open
        # database file`, which main()'s last resort prints as exit 2. Named
        # here for the same reason as the branches below: a mistyped --db must
        # say what is wrong with it. A parent that EXISTS but is not a
        # directory (``--db notes.db/store.db``) is a different sentence — the
        # path is there, it just cannot hold a file. That branch is reachable
        # on Windows only: POSIX raises ENOTDIR rather than ENOENT for it, so
        # there it is named by the ``except OSError`` lane above instead. Both
        # are exit 1; only the sentence differs.
        problem = (
            f"{parent} is not a directory"
            if parent.exists()
            else f"its directory {parent} does not exist"
        )
        raise SchemaVersionError(
            f"cannot open {file}: {problem}. Check the --db path — a store is "
            "created only where its directory already is."
        ) from None
    except OSError as exc:
        # Unreadable for some other reason (permissions, a file where a
        # directory was expected). ``connect`` would raise too, but as an
        # OperationalError that reaches main()'s last resort as exit 2 — and
        # this whole function exists to make a bad --db a NAMED exit 1.
        raise SchemaVersionError(
            f"cannot read {file} to tell whether it is one of this project's "
            f"stores: {exc}. Check the --db path and its permissions."
        ) from exc
    if S_ISDIR(info.st_mode):
        # Forgetting the filename on --db is an ordinary typo, and a directory
        # stats perfectly well, so it needs saying out loud. What it used to do
        # instead depended on the platform, which is its own argument for
        # naming it: on Windows a directory reports st_size == 0 and so read as
        # an empty store; elsewhere it reports a non-zero size (4096 on ext4, a
        # smaller entry-derived one on tmpfs or XFS) and fell into the probe
        # below. Both ended as an unnamed exit 2.
        raise SchemaVersionError(
            f"{file} is a directory, not a database file. A store is a single "
            f"file — give --db its name (for example {file / '<name>.db'})."
        )
    if info.st_size == 0:
        return  # ``touch``-ed: ours to build in full
    probe = sqlite3.connect(f"{_sqlite_file_uri(file)}?mode=ro", uri=True)
    try:
        objects = [row[0] for row in probe.execute(_FOREIGN_OBJECTS_SQL)]
        ours = (
            not objects  # EMPTY: nothing here to belong to anyone
            or any(table in objects for table in _STORE_TABLES)
            # Our own leftover bookkeeping and nothing else; the policies below
            # decide whether THIS caller may build on it.
            or (objects == [_BOOKKEEPING_TABLE] and _is_our_unused_bookkeeping(probe))
        )
        # Only asked on the way to refusing — every store we own carries this
        # table, and the answer is used in the message and nowhere else.
        stray = (
            not ours
            and _BOOKKEEPING_TABLE in objects
            and _is_our_unused_bookkeeping(probe)
        )
    finally:
        probe.close()
    if ours:
        return
    # Marked when cut: the operator is told to recognise their file by these
    # names, and a silently shortened one greps against nothing.
    shown = ", ".join(
        name if len(name) <= _MAX_NAME_CHARS else name[:_MAX_NAME_CHARS] + "…"
        for name in objects[:5]
    )
    more = f", and {len(objects) - 5} more" if len(objects) > 5 else ""
    # Hedged deliberately: all that was measured is an empty table of our
    # shape, and this is advice about someone else's database.
    ours_too = (
        f" Its {_BOOKKEEPING_TABLE} is empty and in this project's own shape, "
        "so it was most likely left by an OLDER build of this project doing "
        "what this refusal now prevents."
        if stray
        else ""
    )
    raise SchemaVersionError(
        f"{file} is a SQLite database, but not one of this project's stores: it "
        f"holds {len(objects)} object(s) ({shown}{more}) and none of this "
        f"project's tables ({', '.join(_STORE_TABLES)}). Refusing to open it — "
        "building this project's tables into another application's database "
        "would leave a file that looks like a store to whoever opens it next, "
        f"this daemon included.{ours_too} The database file has not been "
        "modified; check the --db path."
    )


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
    decide whether opening this store is safe before it is changed. That
    includes not creating the table it reads: it used to ``CREATE TABLE IF NOT
    EXISTS`` first, so every refusal keyed on the version returned here — the
    ones a mistyped ``--db`` reaches included — wrote a table into the file on
    the way to declining to touch it (issue #174). A store with no bookkeeping
    table has recorded no migration, which is what 0 already means.
    :func:`apply_migrations`, the only writer, creates it.
    """
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (_BOOKKEEPING_TABLE,),
    ).fetchone()
    if exists is None:
        return 0
    # An aggregate always returns exactly one row, so only its value can be NULL.
    row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    return 0 if row[0] is None else int(row[0])


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
    # The bookkeeping table is created HERE, by the only function that writes,
    # rather than by the read above (issue #174) — and after the NEWER refusal,
    # which never needs it: a store recorded as newer already has one.
    conn.execute(SCHEMA_MIGRATIONS_DDL)
    applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
    for version in sorted(MIGRATIONS):
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
    # Every version in MIGRATIONS is applied or was already there, so the store
    # now stands at the newest this build knows. This used to be accumulated in
    # the loop, which could only ever arrive back at ``latest_known`` — the
    # accumulator advanced on skipped versions too (issue #175).
    return latest_known


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
        below), and an EMPTY store — a file holding no objects at all — is
        built in full, since nothing can own it.

        Ahead of all three policies sits a fact about the FILE rather than
        about any policy: a SQLite database holding objects that are not this
        project's is refused by name, before a connection is even tuned (see
        :func:`_refuse_a_foreign_store`). Every policy needs that refusal — the
        two below would otherwise read such a file as an empty store, and
        ``migrate=True`` would build this project's tables straight into it.

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
        _refuse_a_foreign_store(path)  # before connect(), which writes
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
                    # No migration recorded (a ``touch``, an open that died
                    # before its first migration committed, or a file left
                    # holding nothing but the empty bookkeeping table an older
                    # build's refusal wrote into it): nothing can own it and
                    # there is no lease table to consult, so build it in full —
                    # there is nobody to defer to. A file with objects that are
                    # NOT ours never reaches here; it was refused above.
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
