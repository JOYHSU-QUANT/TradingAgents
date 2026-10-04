"""Reads and writes of the store's rows.

A ``bars`` row is one :class:`~..domain.bars.PoolBar`. A row is written once
and its reading is never changed afterwards; the one column that is updated
is ``finality``. Inserting a row whose key is already there raises, so a
caller decides what is missing before it writes, and nothing is silently
replaced.

A run's rows are only ever added. :meth:`Store.record` writes one bar's
decision, its fills and its valuation in one transaction, and a second
decision on the same bar of the same run raises. The store is the engine's
:class:`~..ports.Journal`.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Final

from ..domain.bars import Finality, PoolBar
from ..domain.ledger import Ledger, LedgerError
from ..domain.records import (
    BarSeen,
    Decision,
    FillRecord,
    Outcome,
    RejectionCode,
    RunRecord,
    StepRecord,
    Valuation,
)
from ..domain.types import RunMode, TargetWeights
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
    except (sqlite3.Error, OverflowError) as exc:
        # OverflowError: an integer SQLite's 64 bits cannot hold.
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


def _amounts_text(amounts: Mapping[str, Decimal]) -> str:
    """A symbol -> amount mapping as JSON, each amount as decimal text."""
    return json.dumps({symbol: str(amount) for symbol, amount in amounts.items()}, sort_keys=True)


def _amounts(text: str) -> dict[str, Decimal]:
    amounts = json.loads(text)
    if not isinstance(amounts, dict):
        raise ValueError(f"expected a JSON object of amounts, got {text!r}")
    # Written as text; a JSON number here was not written by the store.
    if not all(isinstance(amount, str) for amount in amounts.values()):
        raise ValueError(f"expected every amount as decimal text, got {text!r}")
    return {symbol: Decimal(amount) for symbol, amount in amounts.items()}


def _route(text: str) -> tuple[str, ...]:
    route = json.loads(text)
    if not isinstance(route, list):
        raise ValueError(f"expected a JSON list of pool addresses, got {text!r}")
    return tuple(route)


def _ledger(balances: str, gas_eth: str) -> Ledger:
    return Ledger(balances=_amounts(balances), gas_eth=Decimal(gas_eth))


@contextmanager
def _stored(what: str) -> Iterator[None]:
    """Turn a stored row that no longer reads as ``what`` into a :class:`StoreError`."""
    try:
        yield
    except InvalidOperation as exc:
        # The exception's own text is a list of signal classes.
        raise StoreError(f"the stored {what} holds a value that is not a decimal") from exc
    except (TypeError, ValueError) as exc:
        raise StoreError(f"the stored {what} is not valid ({exc})") from exc


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
                # The key is the table's one UNIQUE constraint; any other
                # integrity failure is reported as what SQLite said it was.
                if "UNIQUE constraint failed" not in str(exc):
                    raise
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

    def twap_windows(self, chain_id: int, pool: str, interval_seconds: int) -> set[int]:
        """Every TWAP window the readings of ``pool`` were taken over."""
        with _sqlite_errors("reading a series"):
            rows = self._connection.execute(
                f"SELECT DISTINCT twap_window_seconds FROM bars WHERE {_SERIES}",
                (chain_id, pool, interval_seconds),
            ).fetchall()
        return {window for (window,) in rows}

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
        """Record the verdict ``finality`` on the stored rows of ``bars``, in one transaction.

        A verdict is ``FINAL`` or ``REORGED``, and it is given once: a row
        that is not stored as ``PENDING`` raises :class:`StoreError` and
        none of ``bars`` is changed.
        """
        if finality is Finality.PENDING:
            raise ValueError("a reading does not go back to pending")
        with _sqlite_errors("recording finality"), transaction(self._connection):
            for bar in bars:
                changed = self._connection.execute(
                    f"UPDATE bars SET finality = ? WHERE {_SERIES} AND time = ? AND finality = ?",
                    (
                        finality.value,
                        bar.chain_id,
                        bar.pool,
                        bar.interval_seconds,
                        bar.time,
                        Finality.PENDING.value,
                    ),
                ).rowcount
                if changed != 1:
                    raise StoreError(
                        f"the reading of pool {bar.pool} at {bar.time} is not a pending "
                        f"reading in the store"
                    )

    def insert_run(self, run: RunRecord) -> None:
        """Start ``run``; a run with its id that is already stored raises :class:`StoreError`."""
        with _sqlite_errors("starting a run"):
            try:
                with transaction(self._connection):
                    self._connection.execute(
                        "INSERT INTO runs (run_id, mode, chain_id, quote, strategy, config, "
                        "balances, gas_eth, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            run.run_id,
                            run.mode.value,
                            run.chain_id,
                            run.quote,
                            run.strategy,
                            run.config,
                            _amounts_text(run.ledger.balances),
                            str(run.ledger.gas_eth),
                            run.created_at,
                        ),
                    )
            except sqlite3.IntegrityError as exc:
                if "UNIQUE constraint failed" not in str(exc):
                    raise
                raise StoreError(f"the run {run.run_id!r} is already stored") from exc

    def run(self, run_id: str) -> RunRecord | None:
        """The run ``run_id``, when there is one."""
        with _sqlite_errors("reading a run"):
            row = self._connection.execute(
                "SELECT mode, chain_id, quote, strategy, config, balances, gas_eth, created_at "
                "FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        mode, chain_id, quote, strategy, config, balances, gas_eth, created_at = row
        with _stored(f"run {run_id!r}"):
            return RunRecord(
                run_id=run_id,
                mode=RunMode(mode),
                chain_id=chain_id,
                quote=quote,
                strategy=strategy,
                config=config,
                ledger=_ledger(balances, gas_eth),
                created_at=created_at,
            )

    def decision(self, run_id: str, time: int) -> Decision | None:
        """The run's decision on the bar at ``time``, when it has made one."""
        with _sqlite_errors("reading a decision"):
            row = self._connection.execute(
                "SELECT outcome, target, reason, reason_code, close_block, close_block_hash, "
                "finality FROM decisions WHERE run_id = ? AND time = ?",
                (run_id, time),
            ).fetchone()
        if row is None:
            return None
        outcome, target, reason, reason_code, close_block, close_block_hash, finality = row
        with _stored(f"decision of run {run_id!r} at {time}"):
            return Decision(
                time=time,
                outcome=Outcome(outcome),
                close_block=close_block,
                target=None if target is None else TargetWeights(_amounts(target)),
                reason=reason,
                reason_code=None if reason_code is None else RejectionCode(reason_code),
                seen=(
                    None
                    if close_block_hash is None
                    else BarSeen(
                        close_block=close_block,
                        close_block_hash=close_block_hash,
                        finality=Finality(finality),
                    )
                ),
            )

    def last_decided(self, run_id: str) -> int | None:
        """The boundary of the latest bar the run has decided, when it has decided any."""
        with _sqlite_errors("reading a run's decisions"):
            (time,) = self._connection.execute(
                "SELECT MAX(time) FROM decisions WHERE run_id = ?", (run_id,)
            ).fetchone()
        return time

    def valuation(self, run_id: str, time: int) -> Valuation | None:
        """The run's valuation after the bar at ``time``, when it has decided that bar."""
        with _sqlite_errors("reading a valuation"):
            row = self._connection.execute(
                "SELECT balances, gas_eth, prices, total_value FROM valuations "
                "WHERE run_id = ? AND time = ?",
                (run_id, time),
            ).fetchone()
        if row is None:
            return None
        balances, gas_eth, prices, total_value = row
        with _stored(f"valuation of run {run_id!r} at {time}"):
            return Valuation(
                time=time,
                ledger=_ledger(balances, gas_eth),
                prices=_amounts(prices),
                total_value=Decimal(total_value),
            )

    def ledger(self, run_id: str) -> Ledger:
        """The run's balances after its latest decision, or its opening ones before any.

        A run that is not stored raises :class:`StoreError`.
        """
        run = self.run(run_id)
        if run is None:
            raise StoreError(f"there is no run {run_id!r} in the store")
        latest = self.last_decided(run_id)
        if latest is None:
            return run.ledger
        valuation = self.valuation(run_id, latest)
        if valuation is None:
            raise StoreError(f"the decision of run {run_id!r} at {latest} has no valuation")
        return valuation.ledger

    def fills(self, run_id: str, time: int | None = None) -> list[FillRecord]:
        """The run's fills, oldest first: all of them, or those of the bar at ``time``."""
        at = "" if time is None else " AND time = ?"
        with _sqlite_errors("reading fills"):
            rows = self._connection.execute(
                "SELECT time, leg, token_in, token_out, route, amount_in, min_amount_out, "
                f"amount_out, gas_cost_eth, block FROM fills WHERE run_id = ?{at} "
                "ORDER BY time, leg",
                (run_id,) if time is None else (run_id, time),
            ).fetchall()
        with _stored(f"fills of run {run_id!r}"):
            return [
                FillRecord(
                    time=row[0],
                    leg=row[1],
                    token_in=row[2],
                    token_out=row[3],
                    route=_route(row[4]),
                    amount_in=Decimal(row[5]),
                    min_amount_out=Decimal(row[6]),
                    amount_out=Decimal(row[7]),
                    gas_cost_eth=Decimal(row[8]),
                    block=row[9],
                )
                for row in rows
            ]

    def _require_follows_on(self, run_id: str, step: StepRecord) -> None:
        """Refuse a step that is not the next one of the run ``run_id``."""
        time = step.decision.time
        before = self.ledger(run_id)
        latest = self.last_decided(run_id)
        if latest is not None and latest >= time:
            what = "the bar" if latest == time else f"the later bar at {latest}, so not the one"
            raise StoreError(f"the run {run_id!r} has already decided {what} at {time}")
        try:
            expected = before.apply(step.fills)
        except LedgerError as exc:
            raise StoreError(
                f"the fills of run {run_id!r} at {time} do not apply to its ledger ({exc})"
            ) from exc
        if step.valuation.ledger != expected:
            raise StoreError(
                f"the ledger of run {run_id!r} at {time} is not its ledger before with the "
                f"step's fills applied"
            )

    def record(self, run_id: str, step: StepRecord) -> None:
        """Write one step's decision, fills and valuation in one transaction: all, or none.

        The step must follow on from what is stored, and that is checked
        inside the transaction, so that two writers on one run cannot both
        pass: its bar is later than every bar the run has decided, and its
        ledger is the run's current one with the step's fills applied. A
        step that does not, or a run that is not stored, raises
        :class:`StoreError`.
        """
        decision, valuation = step.decision, step.valuation
        seen = decision.seen
        with _sqlite_errors("recording a decision"), transaction(self._connection):
            self._require_follows_on(run_id, step)
            self._connection.execute(
                "INSERT INTO decisions (run_id, time, outcome, target, reason, "
                "reason_code, close_block, close_block_hash, finality) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    decision.time,
                    decision.outcome.value,
                    None
                    if decision.target is None
                    else _amounts_text(decision.target.weights),
                    decision.reason,
                    None if decision.reason_code is None else decision.reason_code.value,
                    decision.close_block,
                    None if seen is None else seen.close_block_hash,
                    None if seen is None else seen.finality.value,
                ),
            )
            self._connection.executemany(
                "INSERT INTO fills (run_id, time, leg, token_in, token_out, route, "
                "amount_in, min_amount_out, amount_out, gas_cost_eth, block) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        run_id,
                        decision.time,
                        leg,
                        fill.swap.token_in.symbol,
                        fill.swap.token_out.symbol,
                        json.dumps([pool.address for pool in fill.swap.route]),
                        str(fill.swap.amount_in),
                        str(fill.swap.min_amount_out),
                        str(fill.amount_out),
                        str(fill.gas_cost_eth),
                        fill.block,
                    )
                    for leg, fill in enumerate(step.fills)
                ],
            )
            self._connection.execute(
                "INSERT INTO valuations (run_id, time, balances, gas_eth, prices, "
                "total_value) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    decision.time,
                    _amounts_text(valuation.ledger.balances),
                    str(valuation.ledger.gas_eth),
                    _amounts_text(valuation.prices),
                    str(valuation.total_value),
                ),
            )


def open_store(path: Path, *, create: bool = True) -> Store:
    """Open the database at ``path`` and bring its schema up to date.

    A store an older version of the package wrote is migrated here, whatever
    opens it: a command that only reads its rows still writes the missing
    tables. With ``create`` false a path that is not an existing file is
    refused, so such a command does not leave an empty database behind a
    mistyped path.
    """
    if not create and not path.is_file():
        raise StoreError(f"there is no store at {str(path)!r}")
    try:
        connection = sqlite3.connect(path, isolation_level=None)
    except sqlite3.Error as exc:
        raise StoreError(f"the store at {str(path)!r} cannot be opened ({exc})") from exc
    try:
        # Off unless asked for, per connection: a fill or a valuation without
        # its decision, or a decision without its run, is refused.
        connection.execute("PRAGMA foreign_keys = ON")
        # A build of SQLite without foreign keys takes the pragma and does nothing.
        if connection.execute("PRAGMA foreign_keys").fetchone() != (1,):
            raise SchemaError("this SQLite does not enforce foreign keys")
        migrate(connection)
    except (sqlite3.Error, SchemaError) as exc:
        connection.close()
        raise StoreError(f"the store at {str(path)!r} cannot be used ({exc})") from exc
    return Store(connection)
