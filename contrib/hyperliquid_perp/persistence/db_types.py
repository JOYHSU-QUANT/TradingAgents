"""Names :mod:`.db` and :mod:`.store_identity` share.

The error every refused open raises, and the constants the two modules have to
agree on.
"""

from __future__ import annotations

__all__ = ["BOOKKEEPING_TABLE", "BUSY_TIMEOUT_MS", "IN_MEMORY", "SchemaVersionError"]


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
