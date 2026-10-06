"""Reads and writes of the store's rows.

A ``bars`` row is one :class:`~..domain.bars.PoolBar`. A row is written once
and its reading is never changed afterwards; the one column that is updated
is ``finality``. Inserting a row whose key is already there raises, so a
caller decides what is missing before it writes, and nothing is silently
replaced.

A run's rows are only ever added, but for what stopped a send, written once,
and a send taken back before anything of it was sent.
:meth:`Store.record` writes one bar's decision, its fills and its valuation
in one transaction, and a second decision on the same bar of the same run
raises. A run whose swaps are signed first writes the bar's send
(:meth:`Store.begin_send`) and each leg as it fills
(:meth:`Store.record_leg`); the decision then settles the send. The store
is the engine's :class:`~..ports.Journal`.

A ``verdicts`` row is one :class:`~..domain.verdicts.VerdictRecord`. It
belongs to no run and is written once: inserting a row whose key is already
there raises, as a bar's does, and nothing updates one.

The database is kept in SQLite's write-ahead log mode, which
:func:`open_store` turns on and the file then keeps: beside ``store.db``
there are a ``store.db-wal`` and a ``store.db-shm`` while it is open.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Final

from ..domain.bars import Finality, PoolBar
from ..domain.ledger import Ledger, LedgerError
from ..domain.records import (
    REASON_CODES,
    BarSeen,
    Decision,
    FillRecord,
    FillSource,
    OpenSend,
    Outcome,
    RejectionCode,
    RunRecord,
    StepRecord,
    Valuation,
)
from ..domain.types import Fill, RunMode, TargetWeights
from ..domain.verdicts import Rating, Verdict, VerdictRecord
from .schema import SchemaError, migrate, transaction

__all__ = ["Store", "StoreBusy", "StoreError", "open_store"]

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
# SQLite's primary result code for a write to a database that is read-only.
_SQLITE_READONLY: Final = frozenset({8})
# SQLite's primary result codes for a database, or a table, another connection holds.
_SQLITE_BUSY: Final = frozenset({5, 6})
_SELECT: Final = f"SELECT {', '.join(_COLUMNS)} FROM bars"
_SERIES: Final = "chain_id = ? AND pool = ? AND interval_seconds = ?"


class StoreError(Exception):
    """The store cannot be opened, read or written, or holds something it should not."""


class StoreBusy(StoreError):
    """Another connection held the store for longer than this one waits: later, it may not."""


def _is_kind(error: sqlite3.Error, codes: frozenset[int], words: str) -> bool:
    """Whether SQLite's error is of one of the primary result ``codes``.

    The low byte of an error's code is its kind, and the rest says which
    case of it the error is. Before Python 3.11 an error carries no code,
    only SQLite's words, which are then looked for.
    """
    code = getattr(error, "sqlite_errorcode", None)
    if code is None:
        return words in str(error)
    return code & 0xFF in codes


def _is_busy(error: sqlite3.Error) -> bool:
    """Whether SQLite refused because another connection holds the database or a table."""
    return _is_kind(error, _SQLITE_BUSY, "is locked")


def _store_error(message: str, error: Exception) -> StoreError:
    """``message`` as a :class:`StoreBusy` when ``error`` is a lock, else a :class:`StoreError`."""
    busy = isinstance(error, sqlite3.Error) and _is_busy(error)
    return (StoreBusy if busy else StoreError)(f"{message} ({error})")


@contextmanager
def _sqlite_errors(doing: str) -> Iterator[None]:
    """Turn whatever SQLite raises while ``doing`` into a :class:`StoreError`."""
    try:
        yield
    except (sqlite3.Error, OverflowError) as exc:
        # OverflowError: an integer SQLite's 64 bits cannot hold.
        raise _store_error(f"the store failed while {doing}", exc) from exc


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


_FILL_COLUMNS: Final = (
    "time, leg, token_in, token_out, route, amount_in, min_amount_out, amount_out, "
    "gas_cost_eth, block"
)


def _fill_row(run_id: str, time: int, leg: int, fill: Fill) -> tuple[Any, ...]:
    """``fill``, leg ``leg`` of the bar at ``time``, as a row of ``fills`` or ``sent_legs``."""
    return (
        run_id,
        time,
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


def _fill_record(row: Sequence[Any]) -> FillRecord:
    """A row of :data:`_FILL_COLUMNS` read back."""
    return FillRecord(
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


_DECISION_COLUMNS: Final = (
    "time, outcome, target, reason, reason_code, close_block, close_block_hash, finality, "
    "decided_at, verdict_digests"
)
_VALUATION_COLUMNS: Final = "time, balances, gas_eth, prices, total_value"
_VERDICT_COLUMNS: Final = (
    "source",
    "symbol",
    "time",
    "rating",
    "model",
    "prompt_version",
    "asked_at",
    "text_digest",
    "sidecar_path",
    "sidecar_digest",
)
_SELECT_VERDICT: Final = f"SELECT {', '.join(_VERDICT_COLUMNS)} FROM verdicts"


def _digests_text(digests: Mapping[str, str]) -> str:
    """A symbol -> digest mapping as JSON."""
    return json.dumps(dict(digests), sort_keys=True)


def _verdict_row(record: VerdictRecord) -> tuple[Any, ...]:
    verdict = record.verdict
    return (
        verdict.source,
        verdict.symbol,
        verdict.time,
        verdict.rating.value,
        record.model,
        record.prompt_version,
        record.asked_at,
        verdict.digest,
        record.sidecar_path,
        record.sidecar_digest,
    )


def _verdict_record(row: Sequence[Any]) -> VerdictRecord:
    """A row of :data:`_VERDICT_COLUMNS` read back."""
    source, symbol, time, rating, model, prompt_version, asked_at, digest, path, path_digest = row
    return VerdictRecord(
        verdict=Verdict(
            source=source, symbol=symbol, time=time, rating=Rating(rating), digest=digest
        ),
        model=model,
        prompt_version=prompt_version,
        asked_at=asked_at,
        sidecar_path=path,
        sidecar_digest=path_digest,
    )


def _decision(row: Sequence[Any]) -> Decision:
    (
        time,
        outcome_text,
        target,
        reason,
        code,
        close_block,
        close_block_hash,
        finality,
        decided_at,
        verdict_digests,
    ) = row
    # The decision checks the shape of what was written; the text ``null``, which
    # the store never writes, would otherwise read as a run that reads no verdicts.
    digests = None if verdict_digests is None else json.loads(verdict_digests)
    if verdict_digests is not None and digests is None:
        raise ValueError("verdict_digests holds the JSON null, and the store writes NULL for none")
    outcome = Outcome(outcome_text)
    # An outcome that says nothing has no codes of its own to read one in; the
    # row is then refused, by the enum or by the decision, for carrying one.
    reason_code = None if code is None else REASON_CODES.get(outcome, RejectionCode)(code)
    return Decision(
        time=time,
        outcome=outcome,
        close_block=close_block,
        target=None if target is None else TargetWeights(_amounts(target)),
        reason=reason,
        reason_code=reason_code,
        seen=(
            None
            if close_block_hash is None
            else BarSeen(
                close_block=close_block,
                close_block_hash=close_block_hash,
                finality=Finality(finality),
            )
        ),
        decided_at=decided_at,
        verdicts=digests,
    )


def _valuation(row: Sequence[Any]) -> Valuation:
    time, balances, gas_eth, prices, total_value = row
    return Valuation(
        time=time,
        ledger=_ledger(balances, gas_eth),
        prices=_amounts(prices),
        total_value=Decimal(total_value),
    )


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

    @contextmanager
    def reading(self) -> Iterator[None]:
        """One view of the database for every read made inside the ``with``.

        A commit another connection makes after the first of those reads is
        not seen by the later ones, so rows read one statement after another
        (a run's decisions, then its valuations) belong together.
        """
        with _sqlite_errors("opening a read"):
            self._connection.execute("BEGIN")
        try:
            yield
        finally:
            # Nothing was written, so there is nothing a failed rollback could lose,
            # and what was raised inside is not to be replaced by it.
            with suppress(sqlite3.Error):
                if self._connection.in_transaction:
                    self._connection.execute("ROLLBACK")

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
        self, chain_id: int, pool: str, interval_seconds: int, *, start: int, end: int | None
    ) -> set[int]:
        """The boundaries from ``start`` to ``end``, both included, that ``pool`` has a reading at.

        With no ``end``, every one from ``start`` on.
        """
        with _sqlite_errors("reading bar times"):
            rows = self._connection.execute(
                f"SELECT time FROM bars WHERE {_SERIES} AND time >= ? AND time <= IFNULL(?, time)",
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

    def insert_verdict(self, record: VerdictRecord) -> None:
        """Write ``record``; a verdict of its source, token and bar that is already stored raises.

        A verdict is never rewritten: :class:`StoreError`, and nothing changes.
        """
        placeholders = ", ".join("?" for _ in _VERDICT_COLUMNS)
        with _sqlite_errors("writing a verdict"):
            try:
                with transaction(self._connection):
                    self._connection.execute(
                        f"INSERT INTO verdicts ({', '.join(_VERDICT_COLUMNS)}) "
                        f"VALUES ({placeholders})",
                        _verdict_row(record),
                    )
            except sqlite3.IntegrityError as exc:
                if "UNIQUE constraint failed" not in str(exc):
                    raise
                verdict = record.verdict
                raise StoreError(
                    f"the verdict of {verdict.source!r} on {verdict.symbol} at {verdict.time} "
                    f"is already stored, and a verdict is never rewritten"
                ) from exc

    def verdict(self, source: str, symbol: str, time: int) -> VerdictRecord | None:
        """What ``source`` said of ``symbol`` at the bar ``time``, when it said anything."""
        with _sqlite_errors("reading a verdict"):
            row = self._connection.execute(
                f"{_SELECT_VERDICT} WHERE source = ? AND symbol = ? AND time = ?",
                (source, symbol, time),
            ).fetchone()
        if row is None:
            return None
        with _stored(f"verdict of {source!r} on {symbol} at {time}"):
            return _verdict_record(row)

    def verdicts_at(self, source: str, time: int) -> list[VerdictRecord]:
        """Every verdict of ``source`` at the bar ``time``, by token symbol."""
        with _sqlite_errors("reading verdicts"):
            rows = self._connection.execute(
                f"{_SELECT_VERDICT} WHERE source = ? AND time = ? ORDER BY symbol",
                (source, time),
            ).fetchall()
        records = []
        for row in rows:
            with _stored(f"verdict of {source!r} on {row[1]} at {time}"):
                records.append(_verdict_record(row))
        return records

    def verdict_models(self, source: str) -> list[str]:
        """Every model that gave a stored verdict of ``source``, sorted; empty when it has none."""
        with _sqlite_errors("reading verdicts"):
            rows = self._connection.execute(
                "SELECT DISTINCT model FROM verdicts WHERE source = ? ORDER BY model", (source,)
            ).fetchall()
        return [model for (model,) in rows]

    def insert_run(self, run: RunRecord) -> None:
        """Start ``run``; a run with its id that is already stored raises :class:`StoreError`."""
        with _sqlite_errors("starting a run"):
            try:
                with transaction(self._connection):
                    self._connection.execute(
                        "INSERT INTO runs (run_id, mode, chain_id, quote, strategy, config, "
                        "balances, gas_eth, created_at, fills, fork_block) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                            run.fills.value,
                            run.fork_block,
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
                "SELECT mode, chain_id, quote, strategy, config, balances, gas_eth, created_at, "
                "fills, fork_block "
                "FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        (
            mode,
            chain_id,
            quote,
            strategy,
            config,
            balances,
            gas_eth,
            created_at,
            fills,
            fork_block,
        ) = row
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
                fills=FillSource(fills),
                fork_block=fork_block,
            )

    def decision(self, run_id: str, time: int) -> Decision | None:
        """The run's decision on the bar at ``time``, when it has made one."""
        with _sqlite_errors("reading a decision"):
            row = self._connection.execute(
                f"SELECT {_DECISION_COLUMNS} FROM decisions WHERE run_id = ? AND time = ?",
                (run_id, time),
            ).fetchone()
        if row is None:
            return None
        with _stored(f"decision of run {run_id!r} at {time}"):
            return _decision(row)

    def decisions(self, run_id: str) -> list[Decision]:
        """Every decision of the run, oldest first."""
        with _sqlite_errors("reading a run's decisions"):
            rows = self._connection.execute(
                f"SELECT {_DECISION_COLUMNS} FROM decisions WHERE run_id = ? ORDER BY time",
                (run_id,),
            ).fetchall()
        decisions = []
        for row in rows:
            with _stored(f"decision of run {run_id!r} at {row[0]}"):
                decisions.append(_decision(row))
        return decisions

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
                f"SELECT {_VALUATION_COLUMNS} FROM valuations WHERE run_id = ? AND time = ?",
                (run_id, time),
            ).fetchone()
        if row is None:
            return None
        with _stored(f"valuation of run {run_id!r} at {time}"):
            return _valuation(row)

    def valuations(self, run_id: str) -> list[Valuation]:
        """The run's valuation after every bar it has decided, oldest first."""
        with _sqlite_errors("reading a run's valuations"):
            rows = self._connection.execute(
                f"SELECT {_VALUATION_COLUMNS} FROM valuations WHERE run_id = ? ORDER BY time",
                (run_id,),
            ).fetchall()
        valuations = []
        for row in rows:
            with _stored(f"valuation of run {run_id!r} at {row[0]}"):
                valuations.append(_valuation(row))
        return valuations

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
                f"SELECT {_FILL_COLUMNS} FROM fills WHERE run_id = ?{at} ORDER BY time, leg",
                (run_id,) if time is None else (run_id, time),
            ).fetchall()
        with _stored(f"fills of run {run_id!r}"):
            return [_fill_record(row) for row in rows]

    def _sent_legs(self, run_id: str, time: int) -> list[FillRecord]:
        rows = self._connection.execute(
            f"SELECT {_FILL_COLUMNS} FROM sent_legs WHERE run_id = ? AND time = ? ORDER BY leg",
            (run_id, time),
        ).fetchall()
        with _stored(f"sent legs of run {run_id!r} at {time}"):
            return [_fill_record(row) for row in rows]

    def _sends_open(self, run_id: str) -> list[tuple[Any, ...]]:
        """The rows of the run's sends that no decision has settled, oldest first."""
        return self._connection.execute(
            "SELECT time, started_at, failure, failed_gas_eth FROM sends s "
            "WHERE run_id = ? AND NOT EXISTS (SELECT 1 FROM decisions d "
            "WHERE d.run_id = s.run_id AND d.time = s.time) ORDER BY time",
            (run_id,),
        ).fetchall()

    def _open_failure(self, run_id: str, time: int) -> str | None:
        """What stopped the open send at ``time``, or ``None``; a send that is not open is :class:`StoreError`."""
        found = self._connection.execute(
            "SELECT failure FROM sends s WHERE run_id = ? AND time = ? AND NOT EXISTS "
            "(SELECT 1 FROM decisions d WHERE d.run_id = s.run_id AND d.time = s.time)",
            (run_id, time),
        ).fetchone()
        if found is None:
            raise StoreError(f"the run {run_id!r} has no open send at {time}")
        return found[0]

    def _leg_count(self, run_id: str, time: int) -> int:
        (count,) = self._connection.execute(
            "SELECT COUNT(*) FROM sent_legs WHERE run_id = ? AND time = ?", (run_id, time)
        ).fetchone()
        return count

    def open_send(self, run_id: str) -> OpenSend | None:
        """The run's bar whose swaps began to be sent and which has no decision, when there is one.

        A run is left with at most one: a second is refused when it is begun.
        """
        with _sqlite_errors("reading a run's open send"):
            rows = self._sends_open(run_id)
            if not rows:
                return None
            if len(rows) > 1:
                raise StoreError(
                    f"the run {run_id!r} has {len(rows)} open sends, at "
                    f"{[row[0] for row in rows]}; a run is left with one at most"
                )
            time, started_at, failure, failed_gas_eth = rows[0]
            legs = self._sent_legs(run_id, time)
        with _stored(f"send of run {run_id!r} at {time}"):
            return OpenSend(
                time=time,
                started_at=started_at,
                legs=tuple(legs),
                failure=failure,
                failed_gas_eth=None if failed_gas_eth is None else Decimal(failed_gas_eth),
            )

    def begin_send(self, run_id: str, time: int, *, started_at: int) -> None:
        """Mark the bar at ``time`` as having its swaps sent, before the first one is.

        Refused, with :class:`StoreError`, for a run that is not stored or
        does not sign its swaps, that has an open send, or that has decided
        this bar or a later one.
        """
        with _sqlite_errors("beginning a send"), transaction(self._connection):
            run = self.run(run_id)
            if run is None:
                raise StoreError(f"there is no run {run_id!r} in the store")
            if not run.fills.signs:
                raise StoreError(
                    f"the run {run_id!r} fills from the {run.fills.value} and signs nothing, "
                    f"so it sends nothing"
                )
            opened = self._sends_open(run_id)
            if opened:
                raise StoreError(
                    f"the run {run_id!r} has an open send at {opened[0][0]}; a send is "
                    f"begun only when none is open"
                )
            latest = self.last_decided(run_id)
            if latest is not None and latest >= time:
                raise StoreError(
                    f"the run {run_id!r} has decided the bar at {latest}, so no send is "
                    f"begun at {time}"
                )
            try:
                self._connection.execute(
                    "INSERT INTO sends (run_id, time, started_at) VALUES (?, ?, ?)",
                    (run_id, time, started_at),
                )
            except sqlite3.IntegrityError as exc:
                if "UNIQUE constraint failed" not in str(exc):
                    raise
                raise StoreError(
                    f"the run {run_id!r} has already begun a send at {time}"
                ) from exc

    def abandon_send(self, run_id: str, time: int) -> None:
        """Delete the open send at ``time``, which has no leg and no failure: nothing of it was sent.

        A send that is settled, has a leg, or has failed is
        :class:`StoreError`, and stays.
        """
        with _sqlite_errors("abandoning a send"), transaction(self._connection):
            if self._open_failure(run_id, time) is not None or self._leg_count(run_id, time):
                raise StoreError(
                    f"the send of run {run_id!r} at {time} has a leg or a failure, and stays"
                )
            self._connection.execute(
                "DELETE FROM sends WHERE run_id = ? AND time = ?", (run_id, time)
            )

    def record_leg(self, run_id: str, time: int, leg: int, fill: Fill) -> None:
        """Write ``fill`` as leg ``leg`` of the open send at ``time``.

        The legs are written in order, from 0, and only to a send that is
        open and has not failed; anything else is :class:`StoreError`.
        """
        with _sqlite_errors("recording a sent leg"), transaction(self._connection):
            if self._open_failure(run_id, time) is not None:
                raise StoreError(f"the send of run {run_id!r} at {time} has failed")
            written = self._leg_count(run_id, time)
            if leg != written:
                raise StoreError(
                    f"the send of run {run_id!r} at {time} has {written} leg(s), so the next "
                    f"is leg {written}, not {leg}"
                )
            self._connection.execute(
                f"INSERT INTO sent_legs (run_id, {_FILL_COLUMNS}) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                _fill_row(run_id, time, leg, fill),
            )

    def fail_send(
        self, run_id: str, time: int, *, failure: str, gas_eth: Decimal | None
    ) -> None:
        """Write what stopped the open send at ``time``, once, and the gas its failed swap cost.

        ``gas_eth`` is ``None`` when that gas is not known.
        """
        if not isinstance(failure, str) or not failure.strip():
            raise ValueError(f"failure must be a non-empty string, got {failure!r}")
        if gas_eth is not None and (
            not isinstance(gas_eth, Decimal) or not gas_eth.is_finite() or gas_eth.is_signed()
        ):
            raise ValueError(f"gas_eth must be a non-negative Decimal or None, got {gas_eth!r}")
        with _sqlite_errors("recording a failed send"), transaction(self._connection):
            if self._open_failure(run_id, time) is not None:
                raise StoreError(f"the send of run {run_id!r} at {time} has already failed")
            self._connection.execute(
                "UPDATE sends SET failure = ?, failed_gas_eth = ? WHERE run_id = ? AND time = ?",
                (failure, None if gas_eth is None else str(gas_eth), run_id, time),
            )

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
        self._require_settles(run_id, step)

    def _require_settles(self, run_id: str, step: StepRecord) -> None:
        """Refuse a step that leaves a send open, or whose fills are not its send's legs.

        A run whose fills come from the chain writes the legs of a step that
        filled before the step itself: such a step without a send is refused.
        """
        time = step.decision.time
        # The step's bar is later than every decided one, so a send of it is an open one.
        failures = {opened: failure for opened, _, failure, _ in self._sends_open(run_id)}
        for opened in failures:
            if opened != time:
                raise StoreError(
                    f"the run {run_id!r} has an open send at {opened}, so the bar at {time} "
                    f"is not decided"
                )
        if time not in failures:
            partial = step.decision.outcome is Outcome.PARTIAL
            run = self.run(run_id) if step.fills else None
            if run is not None and run.fills.signs:
                raise StoreError(
                    f"the run {run_id!r} fills on the chain, and its fills at {time} were not "
                    f"written as the legs of a send"
                )
            if partial:
                # Only a signed rebalance is left half done, and its legs are a send's.
                raise StoreError(
                    f"the decision of run {run_id!r} at {time} is partial, and a partial "
                    f"rebalance is a signing run's, written as a send"
                )
            return
        if failures[time] is not None:
            raise StoreError(
                f"the send of run {run_id!r} at {time} failed, and a failed send is not "
                f"settled by a decision"
            )
        legs = self._connection.execute(
            f"SELECT {_FILL_COLUMNS} FROM sent_legs WHERE run_id = ? AND time = ? ORDER BY leg",
            (run_id, time),
        ).fetchall()
        # Compared as the rows they are written as: the legs were written from the same fills.
        fills = [_fill_row(run_id, time, leg, fill)[1:] for leg, fill in enumerate(step.fills)]
        if [tuple(row) for row in legs] != fills:
            raise StoreError(
                f"the fills of run {run_id!r} at {time} are not the {len(legs)} leg(s) its "
                f"send wrote"
            )

    def record(self, run_id: str, step: StepRecord) -> None:
        """Write one step's decision, fills and valuation in one transaction: all, or none.

        The step must follow on from what is stored, and that is checked
        inside the transaction, so that two writers on one run cannot both
        pass: its bar is later than every bar the run has decided, and its
        ledger is the run's current one with the step's fills applied. The
        run has no open send but this bar's, which the decision settles;
        that send has not failed, and its legs are the step's fills. A step
        that does not, or a run that is not stored, raises
        :class:`StoreError`.
        """
        decision, valuation = step.decision, step.valuation
        seen = decision.seen
        with _sqlite_errors("recording a decision"), transaction(self._connection):
            self._require_follows_on(run_id, step)
            self._connection.execute(
                "INSERT INTO decisions (run_id, time, outcome, target, reason, "
                "reason_code, close_block, close_block_hash, finality, decided_at, "
                "verdict_digests) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                    decision.decided_at,
                    None if decision.verdicts is None else _digests_text(decision.verdicts),
                ),
            )
            self._connection.executemany(
                f"INSERT INTO fills (run_id, {_FILL_COLUMNS}) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    _fill_row(run_id, decision.time, leg, fill)
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


def _is_read_only(error: sqlite3.OperationalError) -> bool:
    """Whether SQLite refused a write because the database is read-only."""
    return _is_kind(error, _SQLITE_READONLY, "readonly")


def open_store(path: Path, *, create: bool = True, durable: bool = True) -> Store:
    """Open the database at ``path`` and bring its schema up to date.

    A store an older version of the package wrote is migrated here, whatever
    opens it: a command that only reads its rows still writes the missing
    tables, and the file is put in write-ahead log mode. A file that cannot
    be written (a read-only one) is left in the mode it has, and can be read
    as long as its schema is up to date. With ``create`` false a path that
    is not an existing file is refused, so such a command does not leave an
    empty database behind a mistyped path.

    With ``durable`` false this connection's commits return before they
    reach the disk. A power cut can then lose the latest of them and cannot
    damage the file: what is lost is whole transactions this connection
    wrote, the newest first. That is for a writer whose rows can be made
    again, as a backtest's can. It holds only in write-ahead log mode, so a
    database that cannot be put in it (one in memory) stays durable.
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
        try:
            (journal_mode,) = connection.execute("PRAGMA journal_mode = WAL").fetchone()
        except sqlite3.OperationalError as exc:
            # A file that cannot be written keeps the mode it has, and can still
            # be read. Any other failure, a lock among them, stops.
            if not _is_read_only(exc):
                raise
            (journal_mode,) = connection.execute("PRAGMA journal_mode").fetchone()
        if not durable and journal_mode == "wal":
            connection.execute("PRAGMA synchronous = NORMAL")
        migrate(connection)
    except (sqlite3.Error, SchemaError) as exc:
        connection.close()
        raise _store_error(f"the store at {str(path)!r} cannot be used", exc) from exc
    return Store(connection)
