"""Reading the two legs' equities out of their stores — ``sqlite3`` only, and only reads.

The spot package may not be imported (its isolation test), so its store is
read as a file with one query; the perp store is read the same way for
symmetry, and because one query is all that is needed. Both are opened
read-only through the URI form (the perp package's own spelling of a path
as a URI, which a UNC share and a relative path both survive), so this
package cannot write to either even by mistake. The columns:

- perp: ``account_snapshots.account_equity`` of the run's latest snapshot
  (the paper engine writes one per cycle);
- spot: ``valuations.total_value`` of the run's latest bar (the spot engine
  values the portfolio in the quote token after every decision).

No row is :class:`StoreReadError`, not "unknown": a mistyped run id reads
exactly like a run too new to have a row, and sizing the spot leg as if
the two legs held equal capital on a typo must not pass in silence. The
operator who really has a brand-new run says so by leaving the store flags
out for that one visit. A missing file or a store without the table is the
same error.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .upstream import sqlite_file_uri

__all__ = ["StoreReadError", "perp_equity", "spot_equity"]


class StoreReadError(RuntimeError):
    """The store could not be read as the kind of store it was named as."""


def _read_one(path: Path, query: str, run_id: str, *, leg: str) -> Decimal:
    if not path.is_file():
        raise StoreReadError(f"{leg} store {path}: no such file")
    try:
        with closing(sqlite3.connect(f"{sqlite_file_uri(path)}?mode=ro", uri=True)) as conn:
            row = conn.execute(query, (run_id,)).fetchone()
    except sqlite3.Error as exc:
        raise StoreReadError(f"{leg} store {path}: {exc}") from None
    if row is None:
        raise StoreReadError(
            f"{leg} store {path}: run {run_id!r} has no equity row; a mistyped run id reads "
            f"the same, and a run too new to have one is sized as equal capital by leaving "
            f"out --{leg}-db"
        )
    try:
        value = Decimal(str(row[0]))
    except InvalidOperation:
        raise StoreReadError(
            f"{leg} store {path}: run {run_id!r} equity {row[0]!r} is not a number"
        ) from None
    if not value.is_finite():
        raise StoreReadError(f"{leg} store {path}: run {run_id!r} equity {row[0]!r} is not finite")
    return value


def perp_equity(path: Path, run_id: str) -> Decimal:
    """The perp run's latest ``account_equity``."""
    return _read_one(
        path,
        "SELECT account_equity FROM account_snapshots WHERE run_id = ? "
        "ORDER BY timestamp DESC, snapshot_id DESC LIMIT 1",
        run_id,
        leg="perp",
    )


def spot_equity(path: Path, run_id: str) -> Decimal:
    """The spot run's latest ``total_value``."""
    return _read_one(
        path,
        "SELECT total_value FROM valuations WHERE run_id = ? ORDER BY time DESC LIMIT 1",
        run_id,
        leg="spot",
    )
