"""Reads and writes of the store's rows.

A ``bars`` row is one :class:`~..domain.bars.PoolBar`. A row is written once
and its reading is never changed afterwards; the one column that is updated
is ``finality``. Inserting a row whose key is already there raises, so a
caller decides what is missing before it writes, and nothing is silently
replaced.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Final

from ..domain.bars import Finality, PoolBar
from .schema import SchemaError, migrate, transaction

__all__ = ["Store", "StoreError", "open_store"]

_COLUMNS: Final = (
    "chain_id",
    "pool",
    "interval_seconds",
    "time",
    "close_block",
    "close_block_hash",
    "close_block_time",
    "sqrt_price_x96",
    "tick",
    "twap_tick",
    "twap_window_seconds",
    "base_fee_wei",
    "finality",
)
_SELECT: Final = f"SELECT {', '.join(_COLUMNS)} FROM bars"
_SERIES: Final = "chain_id = ? AND pool = ? AND interval_seconds = ?"


class StoreError(Exception):
    """The store cannot be opened, read or written, or holds something it should not."""


@contextmanager
def _sqlite_errors(doing: str) -> Iterator[None]:
    """Turn whatever SQLite raises while ``doing`` into a :class:`StoreError`."""
    try:
        yield
    except sqlite3.Error as exc:
        raise StoreError(f"the store failed while {doing} ({exc})") from exc


def _row(bar: PoolBar) -> tuple[Any, ...]:
    return (
        bar.chain_id,
        bar.pool,
        bar.interval_seconds,
        bar.time,
        bar.close_block,
        bar.close_block_hash,
        bar.close_block_time,
        str(bar.sqrt_price_x96),
        bar.tick,
        bar.twap_tick,
        bar.twap_window_seconds,
        str(bar.base_fee_wei),
        bar.finality.value,
    )


def _bar(row: Sequence[Any]) -> PoolBar:
    values = dict(zip(_COLUMNS, row, strict=True))
    try:
        values["sqrt_price_x96"] = int(values["sqrt_price_x96"])
        values["base_fee_wei"] = int(values["base_fee_wei"])
        values["finality"] = Finality(values["finality"])
        return PoolBar(**values)
    except (TypeError, ValueError) as exc:
        raise StoreError(
            f"the bars row of pool {row[1]!r} at {row[3]!r} is not a valid reading ({exc})"
        ) from exc


class Store:
    """One open database. For one thread; close it, or use it as a context manager."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._connection.close()

    def insert_bars(self, bars: Sequence[PoolBar]) -> None:
        """Write ``bars`` in one transaction: all of them, or none.

        A reading whose key is already stored raises :class:`StoreError`.
        """
        placeholders = ", ".join("?" for _ in _COLUMNS)
        statement = f"INSERT INTO bars ({', '.join(_COLUMNS)}) VALUES ({placeholders})"
        with _sqlite_errors("writing readings"):
            try:
                with transaction(self._connection):
                    self._connection.executemany(statement, [_row(bar) for bar in bars])
            except sqlite3.IntegrityError as exc:
                raise StoreError(f"a reading to insert is already stored ({exc})") from exc

    def bar(self, chain_id: int, pool: str, interval_seconds: int, time: int) -> PoolBar | None:
        """The reading of ``pool`` at the boundary ``time``, when there is one."""
        with _sqlite_errors("reading a bar"):
            row = self._connection.execute(
                f"{_SELECT} WHERE {_SERIES} AND time = ?", (chain_id, pool, interval_seconds, time)
            ).fetchone()
        return None if row is None else _bar(row)

    def previous_bar(
        self, chain_id: int, pool: str, interval_seconds: int, time: int
    ) -> PoolBar | None:
        """The latest reading of ``pool`` before the boundary ``time``, when there is one."""
        with _sqlite_errors("reading a bar"):
            row = self._connection.execute(
                f"{_SELECT} WHERE {_SERIES} AND time < ? ORDER BY time DESC LIMIT 1",
                (chain_id, pool, interval_seconds, time),
            ).fetchone()
        return None if row is None else _bar(row)

    def bar_times(
        self, chain_id: int, pool: str, interval_seconds: int, *, start: int, end: int
    ) -> set[int]:
        """The boundaries from ``start`` to ``end``, both included, that ``pool`` has a reading at."""
        with _sqlite_errors("reading bar times"):
            rows = self._connection.execute(
                f"SELECT time FROM bars WHERE {_SERIES} AND time BETWEEN ? AND ?",
                (chain_id, pool, interval_seconds, start, end),
            ).fetchall()
        return {time for (time,) in rows}

    def latest_times(self, chain_id: int, pool: str, interval_seconds: int, limit: int) -> list[int]:
        """The last ``limit`` boundaries ``pool`` has a reading at, oldest first."""
        with _sqlite_errors("reading bar times"):
            rows = self._connection.execute(
                f"SELECT time FROM bars WHERE {_SERIES} ORDER BY time DESC LIMIT ?",
                (chain_id, pool, interval_seconds, limit),
            ).fetchall()
        return sorted(time for (time,) in rows)

    def extent(
        self, chain_id: int, pool: str, interval_seconds: int
    ) -> tuple[int, int | None, int | None]:
        """How many readings ``pool`` has, and its first and last boundary."""
        with _sqlite_errors("reading a series"):
            count, first, last = self._connection.execute(
                f"SELECT COUNT(*), MIN(time), MAX(time) FROM bars WHERE {_SERIES}",
                (chain_id, pool, interval_seconds),
            ).fetchone()
        return count, first, last

    def pending_bars(self, chain_id: int) -> list[PoolBar]:
        """Every reading on ``chain_id`` still to be checked against the final chain."""
        with _sqlite_errors("reading the pending readings"):
            rows = self._connection.execute(
                f"{_SELECT} WHERE finality = ? AND chain_id = ? "
                "ORDER BY time, interval_seconds, pool",
                (Finality.PENDING.value, chain_id),
            ).fetchall()
        return [_bar(row) for row in rows]

    def set_finality(self, bars: Sequence[PoolBar], finality: Finality) -> None:
        """Record ``finality`` on the stored rows of ``bars``, in one transaction."""
        with _sqlite_errors("recording finality"), transaction(self._connection):
            for bar in bars:
                changed = self._connection.execute(
                    f"UPDATE bars SET finality = ? WHERE {_SERIES} AND time = ?",
                    (finality.value, bar.chain_id, bar.pool, bar.interval_seconds, bar.time),
                ).rowcount
                if changed != 1:
                    raise StoreError(
                        f"the reading of pool {bar.pool} at {bar.time} is not in the store"
                    )


def open_store(path: Path, *, create: bool = True) -> Store:
    """Open the database at ``path`` and bring its schema up to date.

    With ``create`` false a path that is not an existing file is refused, so
    a command that only reads does not leave an empty database behind a
    mistyped path.
    """
    if not create and not path.is_file():
        raise StoreError(f"there is no store at {str(path)!r}")
    try:
        connection = sqlite3.connect(path, isolation_level=None)
    except sqlite3.Error as exc:
        raise StoreError(f"the store at {str(path)!r} cannot be opened ({exc})") from exc
    try:
        migrate(connection)
    except (sqlite3.Error, SchemaError) as exc:
        connection.close()
        raise StoreError(f"the store at {str(path)!r} cannot be used ({exc})") from exc
    return Store(connection)
