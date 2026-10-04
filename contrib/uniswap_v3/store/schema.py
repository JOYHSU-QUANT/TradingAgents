"""The store's tables, and the migrations that create them.

A database carries the versions applied to it in ``schema_migrations``.
:func:`migrate` applies whatever is missing, each version in its own
transaction, and refuses a database a newer version of the package wrote.

The file is marked with an SQLite ``application_id``. A database that holds
tables and does not carry the mark is some other program's, and is refused
before anything is written to it.

``sqrt_price_x96`` and ``base_fee_wei`` are decimal text: a uint160 does not
fit SQLite's 64-bit integer, and a base fee is a uint256.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Final

__all__ = ["APPLICATION_ID", "SCHEMA_VERSION", "SchemaError", "migrate", "transaction"]

# "UNI3" in ASCII.
APPLICATION_ID: Final = 0x554E4933

# One entry per version, in order; an entry is that version's statements. An
# entry is never edited once a database may hold it: a change is a new entry.
_MIGRATIONS: Final[tuple[tuple[str, ...], ...]] = (
    (
        """
        CREATE TABLE bars (
            chain_id INTEGER NOT NULL,
            pool TEXT NOT NULL,
            interval_seconds INTEGER NOT NULL,
            time INTEGER NOT NULL,
            close_block INTEGER NOT NULL,
            close_block_hash TEXT NOT NULL,
            close_block_time INTEGER NOT NULL,
            sqrt_price_x96 TEXT NOT NULL,
            tick INTEGER NOT NULL,
            twap_tick INTEGER NOT NULL,
            twap_window_seconds INTEGER NOT NULL,
            base_fee_wei TEXT NOT NULL,
            finality TEXT NOT NULL CHECK (finality IN ('pending', 'final', 'reorged')),
            PRIMARY KEY (chain_id, pool, interval_seconds, time)
        )
        """,
        "CREATE INDEX bars_by_finality ON bars (finality, chain_id)",
    ),
)

SCHEMA_VERSION: Final = len(_MIGRATIONS)


class SchemaError(Exception):
    """The database is not one this version of the package can use."""


@contextmanager
def transaction(connection: sqlite3.Connection) -> Iterator[None]:
    """One write transaction on an autocommit connection: committed, or rolled back on a raise."""
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    connection.execute("COMMIT")


def migrate(connection: sqlite3.Connection) -> None:
    """Bring the database on ``connection`` up to :data:`SCHEMA_VERSION`.

    The connection must be in autocommit mode (``isolation_level=None``):
    the transactions are opened and closed here.
    """
    (application_id,) = connection.execute("PRAGMA application_id").fetchone()
    tables = {
        name for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    if application_id != APPLICATION_ID:
        if tables or application_id != 0:
            raise SchemaError(
                "the database was not created by contrib/uniswap_v3 and is left untouched"
            )
        connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
    connection.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "version INTEGER PRIMARY KEY, applied_at INTEGER NOT NULL)"
    )
    applied = [
        version
        for (version,) in connection.execute("SELECT version FROM schema_migrations ORDER BY version")
    ]
    if applied != list(range(1, len(applied) + 1)):
        raise SchemaError(f"schema_migrations holds the versions {applied}, which is not a history")
    if len(applied) > SCHEMA_VERSION:
        raise SchemaError(
            f"the database is at schema version {len(applied)} and this code knows up to "
            f"{SCHEMA_VERSION}; a newer version of the package wrote it"
        )
    for version in range(len(applied) + 1, SCHEMA_VERSION + 1):
        with transaction(connection):
            for statement in _MIGRATIONS[version - 1]:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (version, int(time.time())),
            )
