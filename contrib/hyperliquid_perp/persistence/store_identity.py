"""The pre-connect refusal: may this build open that ``--db`` as a store?

:func:`refuse_a_foreign_store` answers from the filesystem and a read-only
probe, before :func:`~.db.connect` — which writes — touches the file.
:func:`unopenable_error` words the one refusal that can only be made after
that connect has failed. Every refusal is a
:class:`~.db_types.SchemaVersionError`.
"""

from __future__ import annotations

import errno
import logging
import sqlite3
from contextlib import suppress
from pathlib import Path
from stat import S_ISDIR, S_ISREG
from urllib.parse import quote

from .db_types import BOOKKEEPING_TABLE, BUSY_TIMEOUT_MS, IN_MEMORY, SchemaVersionError

__all__ = [
    "STORE_TABLES",
    "is_our_unused_bookkeeping",
    "refuse_a_foreign_store",
    "sqlite_file_uri",
    "unopenable_error",
]

logger = logging.getLogger(__name__)


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

# What makes a file OURS. Deliberately not ``schema_migrations``: that name is
# the convention for Rails/ActiveRecord and golang-migrate among others, so a
# database carrying one is evidence that SOMEBODY migrates it, not that we do.
# These two have existed since v1, so every store a build can meet has them,
# and ``test_every_store_version_carries_the_tables_the_refusal_looks_for``
# pins that against a migration that renames one. ANY of them is enough: a
# rename should degrade this check, never turn a running daemon's own store
# into a refusal.
STORE_TABLES = ("decision_attempts", "scheduler_state")

# A foreign object name is echoed back to the operator; ``sqlite_master.name``
# has no length limit, so one pathological name would swamp the message.
_MAX_NAME_CHARS = 40

# SQLite's write-ahead log and rollback journal, each named by appending to the
# main file's own path. Either can hold the entire content of a database whose
# main file is zero bytes, and both are DESTROYED by opening such a pair — so
# they have to be recognised from the filesystem alone, without asking SQLite
# anything (issue #236). ``-shm`` is deliberately absent: it is a shared-memory
# index rebuilt from the ``-wal``, so one beside an empty main file holds
# nothing and proves nothing.
_SIDECAR_SUFFIXES = ("-wal", "-journal")


def sqlite_file_uri(file: Path) -> str:
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


def is_our_unused_bookkeeping(conn: sqlite3.Connection) -> bool:
    """True when a lone ``schema_migrations`` is OURS and has recorded nothing.

    The one state this project leaves behind with no schema under it: an open
    that died before v1 committed, or an OLDER build's refusal, back when
    :func:`~.db.stored_schema_version` created this table before reading it.
    Both leave it empty and in our shape.

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
        (BOOKKEEPING_TABLE,),
    ).fetchone()
    if is_table is None:
        return False
    # EXACTLY our columns, not merely a superset: nothing in this project has
    # ever added one, so a wider table is somebody else's — and letting one
    # through means building the whole schema into their database, which is the
    # damage this refusal exists to stop.
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({BOOKKEEPING_TABLE})")}
    if columns != {"version", "applied_at"}:
        return False
    return conn.execute(f"SELECT 1 FROM {BOOKKEEPING_TABLE} LIMIT 1").fetchone() is None


def _unreadable_error(file: Path, exc: BaseException, *, probed: bool) -> SchemaVersionError:
    """The one wording of "this build could not read that file at all" (issue #210).

    Two branches of :func:`refuse_a_foreign_store` reach it — the probe's own
    open, and the plain read that stands in for the probe on an empty file — and
    the verdict, the frame and the closing promise are the same for both. What
    differs is the list of things worth checking, because the two lanes cannot
    fail in the same ways.

    The probe lane lists rather than diagnoses, because the error it quotes
    mostly cannot tell its causes apart: ``OperationalError`` covers a
    permission, a lock outliving the wait, a ``-shm`` SQLite may not create
    beside a WAL store, and a failing disk — and the first and the third render
    as the very same ``unable to open database file`` (measured; a lock says so,
    and an I/O fault has its own ``disk I/O error``, which was not staged).
    Picking one would be a diagnosis nothing here measured, the class of claim
    this refusal's own history (PR #209's review) says to avoid.

    The plain read cannot meet two of those four: it waits for nothing SQLite
    would wait on, and needs no sidecar, so it is told nothing about a
    ``busy_timeout`` or a ``-shm``. It CAN meet the other two, and a third the
    probe lane folds into its first: Windows sharing is mandatory rather than
    advisory, so another process holding the file open exclusively fails this
    read as ``[Errno 13] Permission denied`` — every attribute identical to a
    denied ACL (measured). An operator told to check only the permissions would
    find them fine and have nowhere left to go, so this lane names that too.

    It is also the lane with a real errno to read — and the one whose exception
    renders its own filename into its text, quoted, which on Windows is the path
    again with every separator doubled. The errno and its text are quoted
    instead whenever the exception carries a ``strerror``, which every open
    failure measured here does, so both lanes print the path once and in the
    same place.
    """
    if probed:
        checks = (
            "SQLite reports most of these the same way, so check all of: the "
            "file's permissions, whether another process is holding it locked "
            f"past the {BUSY_TIMEOUT_MS / 1000:g}s wait, whether its directory "
            "lets SQLite create the -shm a WAL store needs, and the storage "
            "underneath."
        )
        reason = str(exc)
    else:
        checks = (
            "Check the file's permissions, whether another process has it open "
            "exclusively, and the storage underneath."
        )
        # ``OSError`` renders its own filename into its text, quoted — on Windows
        # that is the path a second time with every separator doubled. The errno
        # and its text are the half worth printing; the path is already in front.
        # Not named ``errno``: this module imports that as a module now, and a
        # local of the same name would shadow it for the whole function body.
        code, strerror = getattr(exc, "errno", None), getattr(exc, "strerror", None)
        reason = f"[Errno {code}] {strerror}" if strerror else str(exc)
    return SchemaVersionError(
        f"{file} could not be opened for reading: {reason}. The path is there, "
        "but this build could not look inside the file to tell whether it is one "
        f"of its stores. {checks} The database file has not been modified."
    )


def _hot_sidecars(file: Path) -> list[tuple[str, bool]]:
    """``file``'s log sidecars that could hold its content, each with whether it was MEASURED.

    Non-empty ones only. SQLite leaves a zero-length ``-wal`` behind as a
    matter of course — a connection creates it before it has a frame to put in
    it — so deleting one loses nothing, and a zero-length main file beside a
    zero-length log is built in full today and goes on being (measured). A
    sidecar with BYTES in it beside a main file with none is the opposite
    case: those bytes are the database.

    Looked for beside the path AS GIVEN and beside its resolved form, because
    a ``--db`` that is a symlink puts those two in different directories and
    the platforms disagree about which one SQLite then uses: the unix VFS
    resolves symlinks when it builds the full pathname it derives the log's
    name from, while ``GetFullPathNameW`` on Windows does not. Neither was
    measured here (this box refuses to create a symlink at all: ``WinError
    1314``), and asking both costs two ``stat`` calls and makes the answer not
    matter. EVERY hot sidecar found is returned, not the first: when both
    bases really are different files, the one the operator has to carry aside
    is whichever one this cannot know, so naming only one names the wrong one
    half the time.

    A sidecar this cannot stat counts as hot, and is flagged as unmeasured so
    the message does not claim data it did not see. Per sidecar and not per
    call: one unmeasurable log must not rewrite the claim about a 20KB one
    sitting beside it that stat-ed perfectly well, which is the same untrue
    statement in the other direction. The question being asked is whether
    anything would be destroyed, and a file that cannot be measured cannot be
    shown to be empty; the cost of answering it wrongly is one refusal over a
    log that was not there to lose. ``ENAMETOOLONG`` is
    the exception, and the one failure that is knowledge rather than
    ignorance: a basename with room for ``-wal`` but not ``-journal`` (POSIX
    ``NAME_MAX``) says that sidecar cannot exist, so there is nothing to fail
    closed over.
    """
    bases = [file]
    try:
        # Non-strict by default since 3.6, which is what the missing-main-file
        # branch needs: a dangling symlink still names where its log would be.
        resolved = file.resolve()
    except OSError as exc:
        # Not suppressed. Every other uncertainty in here fails closed, and
        # swallowing this one would fail OPEN — a symlinked ``--db`` whose
        # resolution raises (a symlink cycle, a directory this may not
        # traverse) would be checked in one place only, and the log beside the
        # other one dies exactly as it did before this guard existed. Refusing
        # is the wrong conservative answer here: most paths that will not
        # resolve have no sidecar anywhere, and refusing every one of them
        # would break stores with nothing wrong with them. So the lookup
        # narrows and SAYS it narrowed — logged for the same reason as the
        # bookkeeping downgrade below, that it changes what this function is
        # able to answer.
        logger.warning(
            "could not resolve %s to look for a log beside its target (%s); "
            "only the path as given was checked.",
            file,
            exc,
        )
    else:
        if resolved != file:
            bases.append(resolved)
    found: list[tuple[str, bool]] = []
    seen: set[str] = set()
    for suffix in _SIDECAR_SUFFIXES:
        for base in bases:
            sidecar = base.parent / (base.name + suffix)
            sized = True
            try:
                empty = sidecar.stat().st_size == 0
            except FileNotFoundError:
                continue
            except OSError as exc:
                if exc.errno == errno.ENAMETOOLONG:
                    continue  # cannot exist, so nothing to be careful about
                empty, sized = False, False
            if empty:
                continue
            # Identity by the canonical path, so the two bases cannot list one
            # file twice: a relative ``--db``, or a Windows 8.3 short name in
            # the path, makes them two SPELLINGS of the same sidecar. What is
            # DISPLAYED stays the spelling this lookup used — the one the
            # operator will recognise.
            key = str(sidecar)
            with suppress(OSError):
                key = str(sidecar.resolve())
            if key in seen:
                continue
            seen.add(key)
            found.append((str(sidecar), sized))
    return found


def _join(names: list[str]) -> str:
    """``a``, ``a and b``, ``a, b and c`` — a list a sentence can hold.

    ``" and ".join`` was fine while a refusal named at most two logs; with both
    suffixes over both lookup bases it can name four.
    """
    if len(names) <= 2:
        return " and ".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def _hot_log_error(
    file: Path, sidecars: list[tuple[str, bool]], *, missing: bool
) -> SchemaVersionError:
    """The one wording of "this database's content is in its log" (issue #236).

    Reached from the two branches that would otherwise call the file an empty
    store — a main file truncated to zero bytes, and one that is not there at
    all — because ``connect`` creates the second as the first and both are
    destroyed identically from there. Only the two clauses that would be false
    for the other differ.

    The log by its PATH, not its bare name: a symlinked ``--db`` can leave it
    in a different directory from the one the operator typed (see
    :func:`_hot_sidecars`), and this whole message is about finding that file.

    Claims data only where data was seen. A sidecar that could not be stat-ed
    is treated as hot and said to be unmeasured, because the alternative is
    the one unhedged assertion in a module whose other two refusal wordings
    (:func:`_unreadable_error`, :func:`unopenable_error`) both list causes
    rather than pick one.

    The one recovery step it names is the one that destroys nothing: MOVING
    the log out of the way. Opening the pair with a SQLite tool is what
    deletes it, so that is left unsaid — but staying silent about the safe
    step too would leave an operator who deleted their own main file with a
    refusal, a correct ``--db``, and nowhere to go.
    """
    # Split by what was actually observed, so one unmeasurable sidecar cannot
    # downgrade the claim about a log that stat-ed perfectly well beside it.
    sized = [name for name, was_sized in sidecars if was_sized]
    unsized = [name for name, was_sized in sidecars if not was_sized]
    claims = []
    if sized:
        claims.append(f"{_join(sized)} {'hold' if len(sized) > 1 else 'holds'} data")
    if unsized:
        claims.append(f"{_join(unsized)} could not be measured and may hold data")
    claim = "; ".join(claims)
    where = "is not there" if missing else "is zero bytes"
    looks_like = (
        "a half-restored backup, and a main file deleted out from under its log"
        if missing
        else "a truncated main file, and a half-restored backup"
    )
    return SchemaVersionError(
        f"{file} {where}, but {claim}. A SQLite database in that shape keeps "
        "its content in the log, not in the main file. Refusing to open it: "
        "this build would read the main file as an empty store and build its "
        "own schema into it, and merely connecting destroys the log on the way "
        "— SQLite reads a log beside an empty main file as stale and deletes "
        "it (measured: 20KB of it gone, and this project's whole schema in "
        "what was left). Nothing here has been touched. Copy the log aside "
        "before anything else opens this path; an ordinary read-write open is "
        f"what deletes it. Then check the --db path — {looks_like} both look "
        "like this. If the log is yours to discard, MOVE it out of the way "
        "rather than deleting it and this path builds as a new store."
    )


def refuse_a_foreign_store(path: str | Path) -> None:
    """Refuse a ``--db`` this build must not open, by name.

    Chiefly a SQLite file belonging to another application, and with it the
    path mistypes that sit either side of one: a directory, and a parent that
    is missing or is not a directory. Those used to reach ``main()``'s last
    resort as an exit-2 ``unable to open database file``, which tells an
    operator nothing about which of them it was — either parent mistype only
    under ``--create``, since every other command stops at its own ``database
    ... does not exist`` first (``Path.exists`` is False for both).

    And the mistype no guard can see anything wrong with: a file that exists,
    stats perfectly well, and still cannot be READ — a permission on it, a
    writer holding it past the probe's bounded wait, a failing disk. Nothing
    about the PATH is wrong there, so the branches keyed on ``stat`` have
    nothing to refuse on, and it used to fail unnamed as a bare
    ``OperationalError`` — from the probe's own open for a file with something
    in it, and from ``connect`` for a zero-length one, which returned above the
    probe (issue #210). Both ways in are named now, through
    :func:`_unreadable_error`: the probe's own open, and — for a zero-length
    file, which is answered without being probed — a plain read that opens
    nothing of SQLite's. The same file being unWRITABLE is a question about
    every store rather than about this ``--db``, and is named one layer out
    (:func:`unopenable_error`, issue #235).

    And the mistype that is not about the main file at all: a zero-length or
    missing main file with a non-empty ``-wal`` or ``-journal`` beside it,
    whose content is entirely in that log. It reads as an empty store to every
    check here, and both this function's probe and the caller's ``connect``
    delete the log outright — so the pair is refused by name instead
    (:func:`_hot_log_error`, issue #236).

    "EMPTY store" used to mean ``MAX(schema_migrations.version) == 0``, which is
    a fact about OUR bookkeeping, not about the file: another application's
    database has no such rows either, so a mistyped ``--db`` was read as an
    empty store. A reporting command then wrote ``schema_migrations`` into that
    file on the way to refusing it, and an owning command (``defer_migration``)
    skipped the refusal entirely and built all of this project's tables into it,
    after which the file looks like a store to whoever opens it next (issue
    #174). EMPTY now means empty: no objects of the file's own.

    Runs BEFORE :func:`~.db.connect`, because connecting is itself a write —
    ``PRAGMA journal_mode = WAL`` rewrites the header of a database not already
    in WAL — and on a READ-ONLY connection, because merely opening a database
    read-write is one too: SQLite checkpoints an uncheckpointed ``-wal`` into
    the main file and removes it when the last connection closes, so a probe
    would have performed a crashed foreign application's recovery for it. Under
    ``mode=ro`` the database file is never modified, whatever journal mode or
    crash state it is in. Its sidecars are another matter, in both directions:
    opening a WAL database read-only materialises SQLite's own empty ``-shm`` /
    ``-wal`` pair beside one that had none, which its owner reclaims on its next
    open — and a ``-wal`` beside a ZERO-LENGTH main file reads as stale and is
    DELETED, which is why an empty file never reaches the probe at all (see the
    branch above it, which answers from the filesystem instead — and refuses
    outright when such a log is there, since the caller's ``connect`` would
    destroy it just the same, issue #236). Unlinking sidecars again would mean
    racing those of a process that may be live — worse than leaving them.

    ``immutable=1`` would leave even those alone but is unusable here: it
    ignores the ``-wal``, so a LIVE foreign database reads back as holding no
    objects at all — the one answer that would have this build write into it.

    A file that is not a SQLite database at all raises ``sqlite3.DatabaseError``
    from the read, as it did from ``connect``.
    """
    if str(path) == IN_MEMORY:
        return  # a fresh private database every time; nothing to inherit
    file = Path(path)
    try:
        info = file.stat()
    except FileNotFoundError:
        parent = file.parent
        if parent.is_dir():
            # Nothing there yet; connect() creates it, as it always has —
            # unless a log is sitting in that directory waiting for the main
            # file somebody deleted out from under it. ``connect`` creates the
            # zero-length database the branch below refuses, which makes that
            # log stale in SQLite's eyes and gone on the same open (measured:
            # the same 20KB). One verdict, two branches, because only one of
            # the two states has a ``stat`` to read.
            hot = _hot_sidecars(file)
            if hot:
                # ``from None`` because the missing main file is the PREMISE of
                # this verdict rather than a failure inside it — the
                # FileNotFoundError being handled would otherwise read as its
                # cause. The sibling branch below needs no such clause: nothing
                # is being handled there.
                raise _hot_log_error(file, hot, missing=True) from None
            return
        # There is nowhere to create it, so ``connect`` raises `unable to open
        # database file`, which main()'s last resort prints as exit 2. Named
        # here for the same reason as the branches below: a mistyped --db must
        # say what is wrong with it. A parent that EXISTS but is not a
        # directory (``--db notes.db/store.db``) is a different sentence — the
        # path is there, it just cannot hold a file. That branch is reachable
        # on Windows only: POSIX raises ENOTDIR rather than ENOENT for it, so
        # there it is named by the ``except OSError`` lane below instead. Both
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
        # Something about the PATH stops us even asking: on POSIX a parent that
        # is a file (ENOTDIR), or a directory we may not traverse. Not the file
        # itself being unreadable — ``stat`` succeeds on one of those, so it is
        # named further down instead — by the probe's own lane, or by the plain
        # read the empty-file branch stands on. ``connect`` would raise
        # here too, but as an OperationalError that reaches main()'s last resort
        # as exit 2, and this function exists to make a bad --db a NAMED exit 1.
        raise SchemaVersionError(
            f"cannot read {file} to tell whether it is one of this project's "
            f"stores: {exc}. Check the --db path and the permissions on its "
            "directory."
        ) from exc
    if S_ISDIR(info.st_mode):
        # Forgetting the filename on --db is an ordinary typo, and a directory
        # stats perfectly well, so it needs saying out loud — and first, for its
        # own sentence. The branch below would now catch a directory (it is not
        # a regular file either) and say something true but useless about it;
        # before that branch existed, a directory reached the empty-file
        # shortcut, whose ``st_size`` test it can satisfy (NTFS reports 0 while
        # the index still fits in the MFT record, such as a fresh tmp dir; ext4
        # reports 4096, tmpfs and XFS a smaller entry-derived size), or the
        # probe, which answers ``unable to open database file`` — the lane below
        # would dress that up as a permissions or lock problem on a directory
        # whose permissions are fine. Before any of these guards, ``connect``
        # failed on one as an unnamed exit 2 on every platform.
        raise SchemaVersionError(
            f"{file} is a directory, not a database file. A store is a single "
            f"file — give --db its name (for example {file / '<name>.db'})."
        )
    if not S_ISREG(info.st_mode):
        # A FIFO or a device node: it stats fine, reports zero bytes, and is
        # not a directory, so every branch below would take it for an empty
        # store — and then READ it. It is not a store whatever happens next,
        # which is reason enough; the sharper reason is that on POSIX opening a
        # FIFO for reading blocks until a writer appears (``open(2)``; not
        # measured here, this box is Windows) and nothing here bounds that —
        # ``busy_timeout`` bounds statements, not opens — so a daemon would
        # hang where it should refuse. On Windows the same guard catches
        # ``--db NUL`` and ``--db CON``, which stat as character devices.
        raise SchemaVersionError(
            f"{file} is not a regular file. A store is an ordinary file on "
            "disk — check the --db path."
        )
    if info.st_size == 0:
        # ``touch``-ed: ours to build in full — unless its content is in a log
        # beside it. A zero-length main file with a non-empty ``-wal`` or
        # ``-journal`` next to it is not an empty database, it is a database
        # every byte of which is in that log: a main file truncated, a backup
        # half-restored, a writer that died before its first commit. Both this
        # function and the ``connect`` on the caller's next line destroy such a
        # log — SQLite reads one beside an empty main file as stale and deletes
        # it, and ``PRAGMA journal_mode = WAL`` is a write like any other
        # (measured: 20KB gone, this project's whole schema built into what was
        # left). Refused by name instead (issue #236), and keyed on the
        # COMBINATION: a ``-wal`` alone says nothing about whose file this is,
        # our own stores all have one.
        #
        # Asked of the FILESYSTEM, never of SQLite. A read-only probe deletes
        # that log for the same reason ``connect`` does — which is why an empty
        # file is not probed at all, and why THIS function, the one that
        # promises in every sentence it raises that the file is untouched, can
        # keep saying so.
        hot = _hot_sidecars(file)
        if hot:
            raise _hot_log_error(file, hot, missing=False)
        # The readability question the probe would have answered is asked here
        # instead, in the one way that opens nothing of SQLite's: an unreadable
        # empty file used to fall through to ``connect`` and its unnamed exit
        # (issue #210). Readability only. Whether a store can be WRITTEN is a
        # different question, and one that has to be asked of every store rather
        # than only an empty one — a zero-length store that reads but cannot be
        # written dies on that same ``PRAGMA journal_mode = WAL``, and is named
        # where every store passes through instead
        # (:meth:`~.db.Database.__init__`, issue #235). A populated store of
        # ours does not reach that: it is already in WAL, so the PRAGMA is a
        # read for it and ``connect`` succeeds (measured; one whose WAL switch
        # silently fell back — see :func:`~.db.connect` — is the exception).
        try:
            with file.open("rb"):
                pass
        except OSError as exc:
            raise _unreadable_error(file, exc, probed=False) from exc
        return
    # The same bounded wait as :func:`~.db.connect`, spelled out so the two
    # cannot drift: this opens a store a sibling daemon may be writing to
    # (RUNBOOK-live §7.3 keeps two live runs in one file), and a lock collision
    # should be a wait rather than an immediate
    # ``OperationalError: database is locked``. ``timeout`` is sqlite3's own
    # spelling of ``PRAGMA busy_timeout``, in seconds; its default happens to
    # equal ``BUSY_TIMEOUT_MS`` today, so passing it changes nothing until the
    # constant does. What it bounds is what SQLite makes waitable: an EXCLUSIVE
    # writer on a non-WAL store is waited out; a RESERVED one never blocks a
    # reader in the first place; and WAL — what the deploy box runs — never
    # blocks a reader at all.
    probe = None
    try:
        try:
            probe = sqlite3.connect(
                f"{sqlite_file_uri(file)}?mode=ro", uri=True, timeout=BUSY_TIMEOUT_MS / 1000
            )
            objects = [row[0] for row in probe.execute(_FOREIGN_OBJECTS_SQL)]
        except sqlite3.OperationalError as exc:
            # The file is there and stats fine, so every guard above had nothing
            # to refuse on, yet SQLite cannot get at it — no read permission
            # (permissions govern opening the file, not the ``stat`` that only
            # asks ABOUT it), a writer still holding it after ``timeout``, or
            # the storage under it. That used to propagate raw: ``validate``
            # catches ``sqlite3.Error`` and printed a pure permissions problem
            # as `store integrity failure` at exit 5 — the code whose meaning is
            # "the ledger does not add up, investigate the accounting" — while
            # an owning command reached main()'s exit-2 last resort, whose
            # ``fatal: unexpected error:`` line names the exception and nothing
            # about the ``--db`` (issue #210). NOT ``DatabaseError``: "file is
            # not a database" is a different verdict with its own established
            # wording and exit 5, deliberately untouched here as in PR
            # #170/#209. ``OperationalError`` is a subclass of it, so catching
            # the narrow one leaves that lane exactly as it was.
            #
            # Only the open and the first read are guarded. Everything below
            # runs against a connection that demonstrably opened, so a failure
            # there is not a "could not open it" and must not be dressed as one.
            raise _unreadable_error(file, exc, probed=True) from exc
        carries_our_tables = any(table in objects for table in STORE_TABLES)
        # Asked once, and never of a store that already proved itself by its
        # tables: the verdict wants it when the bookkeeping table is the file's
        # ONLY object, the message when it sits BESIDE foreign ones.
        #
        # A failure to ANSWER is an answer here: this table is somebody else's
        # unless it is provably ours, so an unreadable one is not ours. That is
        # not hypothetical — a foreign database whose ``schema_migrations`` is a
        # VIRTUAL table over a module this build does not have raises ``no such
        # module`` from the ``PRAGMA table_info`` inside
        # :func:`is_our_unused_bookkeeping` (measured), and such a
        # file is readable, unlocked, and exactly what the refusal further down
        # exists to name. Left to propagate it would escape this function
        # entirely, for ``validate``'s exit-5 "store integrity failure" and an
        # owning command's exit 2 — the two verdicts issue #210 is about.
        #
        # ``OperationalError`` and not its parent, for the same reason as the
        # lane above and with more at stake here: a CORRUPT store raises plain
        # ``DatabaseError: database disk image is malformed`` from the last
        # statement in there, and that must keep reaching ``validate``'s exit 5
        # — swallowing it would tell an operator whose disk is rotting that they
        # had merely mistyped ``--db``.
        try:
            bookkeeping_unused = (
                not carries_our_tables
                and BOOKKEEPING_TABLE in objects
                and is_our_unused_bookkeeping(probe)
            )
        except sqlite3.OperationalError as exc:
            # Logged, not swallowed quietly: this changes a VERDICT, and the
            # sentence it changes it to ("another application's database, check
            # the --db path") is confident about a file whose bookkeeping table
            # this build could not read. A lock arriving between the two reads
            # lands here too, and that operator wants to know a retry might do
            # it. The comparable downgrades in this package
            # (``persistence.backfill``'s left-NULL reasons, four of them) log
            # for the same reason; the silent ``suppress`` calls around this
            # module are all cleanup that changes no verdict.
            logger.warning(
                "could not read %s in %s (%s); treating it as not this "
                "project's bookkeeping.",
                BOOKKEEPING_TABLE,
                file,
                exc,
            )
            bookkeeping_unused = False
        ours = (
            not objects  # EMPTY: nothing here to belong to anyone
            or carries_our_tables
            # Our own leftover bookkeeping and nothing else;
            # ``Database.__init__``'s policies decide whether THIS caller may
            # build on it.
            or (objects == [BOOKKEEPING_TABLE] and bookkeeping_unused)
        )
        # Used in the message and nowhere else.
        stray = not ours and bookkeeping_unused
    finally:
        if probe is not None:
            with suppress(Exception):
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
        f" Its {BOOKKEEPING_TABLE} is empty and in this project's own shape, "
        "so it was most likely left by an OLDER build of this project doing "
        "what this refusal now prevents."
        if stray
        else ""
    )
    raise SchemaVersionError(
        f"{file} is a SQLite database, but not one of this project's stores: it "
        f"holds {len(objects)} object(s) ({shown}{more}) and none of this "
        f"project's tables ({', '.join(STORE_TABLES)}). Refusing to open it — "
        "building this project's tables into another application's database "
        "would leave a file that looks like a store to whoever opens it next, "
        f"this daemon included.{ours_too} The database file has not been "
        "modified; check the --db path."
    )


def unopenable_error(path: str | Path, exc: sqlite3.OperationalError) -> SchemaVersionError:
    """The one wording of "this build could not OPEN that file as a store" (issue #235).

    Opening a store is itself a write — :func:`~.db.connect`'s ``PRAGMA
    journal_mode = WAL`` records the journal mode in the database header — so a
    file this build reads perfectly well can still fail there. A zero-length
    ``--db`` that denies writes is the measured case: ``attempt to write a
    readonly database``, which used to propagate raw for ``validate`` to print
    as `store integrity failure` at exit 5 — the code whose meaning is "the
    ledger does not add up, investigate the accounting" — and for an owning
    command to reach main()'s exit-2 last resort, whose ``fatal: unexpected
    error:`` line names the exception and nothing about the ``--db``. Both send
    an operator after accounting that is fine. It is issue #210's harm from the
    other side: that one named the file this build could not READ.

    Lists rather than diagnoses, like :func:`_unreadable_error`: a denied
    write, a read-only mount and a directory that will not take the ``-wal``
    are not reliably distinguishable from what SQLite says, and picking one
    would be a diagnosis nothing here measured.

    Only the OPEN is wrapped, so a write that fails while the daemon is
    actually writing stays what it is instead of being dressed up as a bad
    ``--db``. A populated store of ours does not arrive here at all: it is
    already in WAL, so that PRAGMA is a read for it and ``connect`` succeeds
    even when the file denies writes (measured) — its writes fail later, where
    they belong.
    """
    return SchemaVersionError(
        f"{path} could not be opened as a store: {exc}. The file is there and "
        "this build could read it, but opening a store WRITES to it — SQLite "
        "records the journal mode in the database header. Check whether the "
        "file or its directory denies writes, whether the volume is mounted "
        "read-only, and whether another process holds it. Nothing of this "
        "project's schema was created in it."
    )
