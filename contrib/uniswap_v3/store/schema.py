"""The store's tables, and the migrations that create them.

A database carries the versions applied to it in ``schema_migrations``.
:func:`migrate` applies whatever is missing, each version in its own
transaction, and refuses a database a newer version of the package wrote.

The file is marked with an SQLite ``application_id``. A database that holds
tables and does not carry the mark, or that carries another mark, is some
other program's, and is refused before anything is written to it.

``sqrt_price_x96`` and ``base_fee_wei`` are decimal text: a uint160 does not
fit SQLite's 64-bit integer, and a base fee is a uint256. Token amounts,
prices and values are decimal text as well, so that none passes through a
float, and a mapping of them (balances, prices, target weights) or a route
is JSON text.

``bars`` is market data and belongs to no run. ``runs``, ``decisions``,
``fills`` and ``valuations`` are what a run writes, in any mode: a decision
is keyed by its run and its bar's boundary, so a bar is decided once, and a
decision's fills and its valuation hang off that key. ``runs.fills``,
``decisions.outcome`` and ``reason_code`` carry no CHECK of their values: SQLite cannot alter one,
so a new outcome would mean rebuilding three tables, and a value this code
does not know is refused when the row is read.

``sends`` and ``sent_legs`` are what a run whose swaps are signed writes
while it sends them: a ``sends`` row before the first swap of a bar is
sent, and a ``sent_legs`` row as each one fills. The bar's decision, written
when its step ends, settles the send; a ``sends`` row without a decision is
a send that never ended.

``verdicts`` is what an outside judge said of a token at a bar, keyed by
(source, time, symbol), in that order so that a bar's verdicts are one
seek; like ``bars`` it belongs to no run, and a row is never rewritten. ``rating`` carries no CHECK of its values, for the same
reason as ``outcome``. ``decisions.verdict_digests`` is JSON text of the
verdict digests the decision saw, by symbol, and NULL for a run that reads
no verdicts or a decision stored before the column existed.
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
    (
        """
        CREATE TABLE runs (
            run_id TEXT PRIMARY KEY,
            mode TEXT NOT NULL CHECK (mode IN ('backtest', 'paper', 'fork', 'live')),
            chain_id INTEGER NOT NULL,
            quote TEXT NOT NULL,
            strategy TEXT NOT NULL,
            config TEXT NOT NULL,
            balances TEXT NOT NULL,
            gas_eth TEXT NOT NULL,
            created_at INTEGER NOT NULL
        )
        """,
        """
        CREATE TABLE decisions (
            run_id TEXT NOT NULL REFERENCES runs (run_id),
            time INTEGER NOT NULL,
            outcome TEXT NOT NULL,
            target TEXT,
            reason TEXT,
            reason_code TEXT,
            close_block INTEGER NOT NULL,
            close_block_hash TEXT,
            finality TEXT CHECK (finality IN ('pending', 'final', 'reorged')),
            CHECK ((close_block_hash IS NULL) = (finality IS NULL)),
            CHECK ((reason IS NULL) = (reason_code IS NULL)),
            PRIMARY KEY (run_id, time)
        )
        """,
        """
        CREATE TABLE fills (
            run_id TEXT NOT NULL,
            time INTEGER NOT NULL,
            leg INTEGER NOT NULL,
            token_in TEXT NOT NULL,
            token_out TEXT NOT NULL,
            route TEXT NOT NULL,
            amount_in TEXT NOT NULL,
            min_amount_out TEXT NOT NULL,
            amount_out TEXT NOT NULL,
            gas_cost_eth TEXT NOT NULL,
            block INTEGER NOT NULL,
            PRIMARY KEY (run_id, time, leg),
            FOREIGN KEY (run_id, time) REFERENCES decisions (run_id, time)
        )
        """,
        """
        CREATE TABLE valuations (
            run_id TEXT NOT NULL,
            time INTEGER NOT NULL,
            balances TEXT NOT NULL,
            gas_eth TEXT NOT NULL,
            prices TEXT NOT NULL,
            total_value TEXT NOT NULL,
            PRIMARY KEY (run_id, time),
            FOREIGN KEY (run_id, time) REFERENCES decisions (run_id, time)
        )
        """,
    ),
    # Where a run's fills come from. Every run stored before this was filled by the model.
    ("ALTER TABLE runs ADD COLUMN fills TEXT NOT NULL DEFAULT 'model'",),
    # When each decision was made. A decision stored before this has none.
    ("ALTER TABLE decisions ADD COLUMN decided_at INTEGER",),
    # Signed swaps: the block a fork run was forked at, and each bar whose swaps began to
    # be sent, with the legs that filled, written before the bar's decision.
    (
        "ALTER TABLE runs ADD COLUMN fork_block INTEGER",
        """
        CREATE TABLE sends (
            run_id TEXT NOT NULL REFERENCES runs (run_id),
            time INTEGER NOT NULL,
            started_at INTEGER NOT NULL,
            failure TEXT,
            failed_gas_eth TEXT,
            PRIMARY KEY (run_id, time)
        )
        """,
        """
        CREATE TABLE sent_legs (
            run_id TEXT NOT NULL,
            time INTEGER NOT NULL,
            leg INTEGER NOT NULL,
            token_in TEXT NOT NULL,
            token_out TEXT NOT NULL,
            route TEXT NOT NULL,
            amount_in TEXT NOT NULL,
            min_amount_out TEXT NOT NULL,
            amount_out TEXT NOT NULL,
            gas_cost_eth TEXT NOT NULL,
            block INTEGER NOT NULL,
            PRIMARY KEY (run_id, time, leg),
            FOREIGN KEY (run_id, time) REFERENCES sends (run_id, time)
        )
        """,
    ),
    # Verdicts: what an outside judge said of a token at a bar, and, on each
    # decision, the digests of the verdicts it saw.
    (
        """
        CREATE TABLE verdicts (
            source TEXT NOT NULL,
            symbol TEXT NOT NULL,
            time INTEGER NOT NULL,
            rating TEXT NOT NULL,
            model TEXT NOT NULL,
            prompt_version TEXT NOT NULL,
            asked_at INTEGER NOT NULL,
            text_digest TEXT NOT NULL,
            sidecar_path TEXT,
            sidecar_digest TEXT,
            CHECK ((sidecar_path IS NULL) = (sidecar_digest IS NULL)),
            PRIMARY KEY (source, time, symbol)
        )
        """,
        "ALTER TABLE decisions ADD COLUMN verdict_digests TEXT",
    ),
)

SCHEMA_VERSION: Final = len(_MIGRATIONS)


class SchemaError(Exception):
    """The database is not one this version of the package can use."""


@contextmanager
def transaction(connection: sqlite3.Connection) -> Iterator[None]:
    """One write transaction on an autocommit connection: committed, or rolled back on a raise.

    A commit that fails is rolled back as well, so the connection is never
    left inside a transaction. SQLite ends a transaction itself on some
    errors (a full disk); there is then nothing to roll back, and the error
    that is raised is still the first one.
    """
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
        connection.execute("COMMIT")
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise


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
