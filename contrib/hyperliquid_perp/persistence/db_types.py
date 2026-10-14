"""Names :mod:`.db` and :mod:`.store_identity` share, and what a SQLite error means.

The error every refused open raises, the constants the two modules have to
agree on, and the two classifiers of a ``sqlite3.Error`` the CLI routes an
exit code on: a store that is itself broken, and a refusal from outside it.
"""

from __future__ import annotations

import sqlite3

__all__ = [
    "BOOKKEEPING_TABLE",
    "BUSY_TIMEOUT_MS",
    "IN_MEMORY",
    "SchemaVersionError",
    "is_store_integrity_error",
    "is_store_refusal_error",
]


# PR3 runs a 30s market-monitor loop and a 4h scheduler cycle against the same
# store; WAL lets a reader overlap a writer and busy_timeout turns a residual
# lock collision into a bounded wait instead of an immediate SQLITE_BUSY.
BUSY_TIMEOUT_MS = 5000

# sqlite3's own spelling for "a private database in RAM". Named because two
# places now branch on it — ``db.connect`` (WAL never applies) and the
# foreign-store refusal (there is no file to inherit anything from).
IN_MEMORY = ":memory:"

# The table ``db.apply_migrations`` records each applied version in.
BOOKKEEPING_TABLE = "schema_migrations"

# SQLite's primary result codes for "the store itself is broken": IOERR (10,
# the disk fails under it — the whole family, its shm/lock sub-codes
# included), CORRUPT (11), NOTADB (26, the file is not a database). The low
# byte of an error's code is its kind; the rest says which case of it the
# error is. And for "refused for a reason outside the store": PERM (3),
# BUSY (5), LOCKED (6), READONLY (8), FULL (13), CANTOPEN (14), PROTOCOL
# (15, a WAL reader that lost the wal-index lock race too many times).
_INTEGRITY_CODES = frozenset({10, 11, 26})
_REFUSAL_CODES = frozenset({3, 5, 6, 8, 13, 14, 15})


def is_store_integrity_error(error: sqlite3.Error) -> bool:
    """Whether SQLite reported the store itself broken: not a database, corrupt,
    or a disk that fails under it.

    The verdict the report-only commands exit 5 on ("investigate the store
    before trusting results"), told apart from everything SQLite refuses for
    a reason outside the store — a lock (BUSY / LOCKED), a file that denies
    writes (READONLY), a full disk (FULL), a ``-wal``/``-shm`` it cannot
    create (CANTOPEN) — which stay the caller's named refusals (issues #210
    / #235: a permission must never read as a broken ledger). Positive, so
    an error this function does not know is NOT the verdict. By result
    code, not by class: a lock, a denied write and a failing disk are all
    ``OperationalError``. Before Python 3.11 an error carries no code: CORRUPT
    and NOTADB arrive as the bare ``DatabaseError`` class and a failing disk
    as an ``OperationalError`` whose words say so.
    """
    code = getattr(error, "sqlite_errorcode", None)
    if code is None:
        return type(error) is sqlite3.DatabaseError or "I/O error" in str(error)
    return code & 0xFF in _INTEGRITY_CODES


def is_store_refusal_error(error: sqlite3.Error) -> bool:
    """Whether SQLite refused for a reason OUTSIDE the store: another
    connection holds it (BUSY / LOCKED / PROTOCOL), the file denies the
    write (READONLY / PERM), the disk is full (FULL), a ``-wal`` / ``-shm``
    cannot be created (CANTOPEN).

    The named exit 1 of a command, as opposed to the exit-5 verdict of
    :func:`is_store_integrity_error` and to everything else — which is a
    tool bug: ``OperationalError`` is also SQLite's generic SQLITE_ERROR
    (``no such table``, a bad statement in a migration), and that must keep
    its traceback rather than read as "locked, retry later". Positive by
    result code; before Python 3.11 an error carries no code, and the class
    is all there is to go on, so any ``OperationalError`` that is not the
    integrity verdict counts.
    """
    code = getattr(error, "sqlite_errorcode", None)
    if code is None:
        return isinstance(error, sqlite3.OperationalError) and not is_store_integrity_error(error)
    return code & 0xFF in _REFUSAL_CODES


class SchemaVersionError(RuntimeError):
    """The store's schema does not match what this build can safely operate on.

    Or there is no store to read a schema from: a mistyped ``--db`` naming
    another application's database, a directory, something that is not a
    regular file, a path or file that cannot be read, or a database whose
    content is in a log beside an empty main file reaches the same verdict —
    this build will not operate on this file — and reaches it without reading a
    version at all (see :func:`~.store_identity.refuse_a_foreign_store`). So
    does a file this build can read but cannot OPEN as a store, which is
    decided one layer out because opening is where the writing starts (see
    :func:`~.store_identity.unopenable_error`). One type, because nothing
    branches on the difference: every one of them is the CLI's named exit 1,
    and the remedy that does differ is already in the message.
    """
