"""Reading the two legs' equities out of their stores — ``sqlite3`` only, and only reads.

The spot package may not be imported (its isolation test), so its store is
read as a file with one query; the perp store is read the same way for
symmetry, and because one query is all that is needed. Both are opened
read-only through the URI form (the perp package's own spelling of a path
as a URI, which a UNC share and a relative path both survive), so this
package cannot write to either even by mistake. The columns, and the
instant each row was written at (carried into the handoff so a reader can
see how old the sizing is):

- perp: ``account_snapshots.account_equity`` and ``timestamp`` of the
  run's latest snapshot (the paper engine writes one per cycle; the
  timestamp is the repository's ISO form, decoded by the function that
  encoded it);
- spot: ``valuations.total_value`` and ``time`` of the run's latest bar
  (the spot engine values the portfolio after every decision; ``time`` is
  the bar's epoch seconds).

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
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .upstream import epoch_ms, parse_instant, sqlite_file_uri

__all__ = ["Equity", "StoreReadError", "perp_equity", "spot_equity"]


class StoreReadError(RuntimeError):
    """The store could not be read as the kind of store it was named as."""


@dataclass(frozen=True)
class Equity:
    """A leg's equity in its quote currency, and when the store wrote that row."""

    value: Decimal
    at_ms: int


def _row(path: Path, query: str, run_id: str, *, leg: str) -> tuple[object, object]:
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
            f"out --{leg}-db and --{leg}-run-id"
        )
    return row[0], row[1]


def _value(raw: object, path: Path, run_id: str, *, leg: str) -> Decimal:
    try:
        value = Decimal(str(raw))
    except InvalidOperation:
        raise StoreReadError(
            f"{leg} store {path}: run {run_id!r} equity {raw!r} is not a number"
        ) from None
    if not value.is_finite():
        raise StoreReadError(f"{leg} store {path}: run {run_id!r} equity {raw!r} is not finite")
    return value


def perp_equity(path: Path, run_id: str) -> Equity:
    """The perp run's latest ``account_equity``, stamped with that snapshot's instant."""
    raw, stamp = _row(
        path,
        "SELECT account_equity, timestamp FROM account_snapshots WHERE run_id = ? "
        "ORDER BY timestamp DESC, snapshot_id DESC LIMIT 1",
        run_id,
        leg="perp",
    )
    try:
        at_ms = epoch_ms(parse_instant(str(stamp)), what="the perp snapshot's timestamp")
    except ValueError as exc:
        raise StoreReadError(f"perp store {path}: run {run_id!r}: {exc}") from None
    return Equity(_value(raw, path, run_id, leg="perp"), at_ms)


def spot_equity(path: Path, run_id: str) -> Equity:
    """The spot run's latest ``total_value``, stamped with that bar's instant."""
    raw, seconds = _row(
        path,
        "SELECT total_value, time FROM valuations WHERE run_id = ? ORDER BY time DESC LIMIT 1",
        run_id,
        leg="spot",
    )
    if isinstance(seconds, bool) or not isinstance(seconds, int) or seconds <= 0:
        raise StoreReadError(f"spot store {path}: run {run_id!r} bar time {seconds!r} is not valid")
    return Equity(_value(raw, path, run_id, leg="spot"), seconds * 1000)
