"""``autoresearch.sqlite`` — opening it, migrating it, and reading/writing history.

One small class rather than a repository layer: this store has three tables
and a handful of verbs, and the perp package's split (``db`` +
``persistence.repository``) earns itself on fifteen tables and a transaction
contract this package does not have. What IS copied from it, because both were
bought by incidents there: WAL plus a busy timeout, an explicit autocommit
connection so transactions begin where this module says, and a refusal — by
name, before any write — of a file that is not this package's store.

The refusal matters more here than the size of the module suggests. The slip
it exists for is ``--db`` pointing at the paper store: opening that file and
running migrations against it would add tables to a store a live paper run is
writing to, which plan §3.2 forbids outright. So an existing file that has
tables but no ``schema_version`` is refused, named, and left untouched.

The two stores do NOT sit side by side, and the reason to say so is that the
opposite is the easy thing to assume: the perp CLI defaults ``--db`` to a
RELATIVE ``paper_trading.db`` and its runbook is written from inside
``contrib/hyperliquid_perp/``, while this one defaults to the repo root's
``data/``. So the slip is not a directory listing offering two similar names
— it is that ``--db`` takes any path at all, and a paper store is the file
every other command in this repo is pointed at.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager, suppress
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from .schema import MIGRATIONS, SCHEMA_VERSION, SCHEMA_VERSION_DDL
from .upstream import Candle, FundingPoint, parse_interval

__all__ = [
    "DB_FILENAME",
    "ResearchStore",
    "StoreError",
    "canonical_coin",
    "default_db_path",
]

# Named once: the CLI's ``--db`` help text and the default path both say it.
DB_FILENAME = "autoresearch.sqlite"

_BUSY_TIMEOUT_MS = 5000
_IN_MEMORY = ":memory:"


class StoreError(RuntimeError):
    """This store cannot be opened or operated on, and the message says which.

    One type because nothing branches on the reason: every caller here is a
    CLI that prints the sentence and exits 1. The REASON is in the sentence —
    a foreign file, a store written by a newer build, a path that is not a
    file — because that is what tells an operator what to do next.
    """


def default_db_path() -> Path:
    """``<repo root>/data/autoresearch.sqlite`` — where the store lives unless told otherwise.

    Anchored on this file's own location, not on the working directory: the
    fetch is a long backfill an operator starts from wherever they happen to
    be standing, and a cwd-relative default would scatter half-filled stores
    around the repo. ``parents[2]`` is the repo root — this file is
    ``contrib/autoresearch/store.py`` — and it is stated here once so the
    layout has a single answer (plan §3.3 makes the path configurable;
    ``--db`` is that configuration).
    """
    return Path(__file__).resolve().parents[2] / "data" / DB_FILENAME


def canonical_coin(coin: str) -> str:
    """The one spelling of a coin this store files rows under.

    Applied by every verb rather than at the CLI, so the store cannot hold two
    spellings of one market however it is reached. Without it ``--coin btc``
    wrote a second, invisible series and ``gaps --coin BTC`` then reported "no
    rows stored" — which reads as a backfill that failed, not as a shift key.

    Deliberately NOT the vendor's ``normalize_symbol``: that one resolves to
    Yahoo Finance symbols and would turn ``BTC`` into ``BTC-USD``, which names
    a spot pair on a different venue, not the Hyperliquid perp this package
    reads. A perp coin is the venue's own bare ticker, so upper-casing it is
    the whole of the rule.
    """
    return coin.strip().upper()


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _window_clause(column: str, since_ms: int | None, until_ms: int | None):
    """The optional ``AND column BETWEEN ...`` half of a read, as (SQL, params).

    Both bounds are INCLUSIVE and both are optional, so a caller that names
    neither reads the whole series exactly as before. Built as SQL rather than
    filtered in Python because the point of the bounds is that the rows past
    them are never read at all: an evaluator that must not compute a holdout
    metric should not be holding the holdout rows either, and "it did not ask
    for them" is a property of the call a test can check.
    """
    clauses, values = "", []
    if since_ms is not None:
        clauses += f" AND {column} >= ?"
        values.append(since_ms)
    if until_ms is not None:
        clauses += f" AND {column} <= ?"
        values.append(until_ms)
    return clauses, values


def _unopenable(path: str | Path, exc: Exception) -> str:
    """The ONE sentence for "this path cannot be opened as a store".

    Written once because the failure arrives at three different moments —
    creating the parent directory, connecting, and the first statement that
    actually touches the file — and an operator reading the line does not
    care which. What they need is the path they typed and the underlying
    diagnosis, so both are in it and neither is paraphrased.
    """
    return f"cannot open {path} as a store: {exc}"


def _is_foreign(conn: sqlite3.Connection) -> bool:
    """True if the OPEN connection's database has tables but none of ours.

    Asked on the connection that is about to be written through, deliberately,
    and not on a second read-only connection opened beside it. That earlier
    arrangement built a SQLite URI by interpolation — ``f"file:{path}?mode=ro"``
    — and a path is not URI text. Measured on this box: with ``--db
    'run#1.db'`` the ``#`` opened the URI's fragment, so the probe read a file
    called ``run``, found no tables in it, and answered "not foreign" about a
    store it had never looked at. The real connection then opened the file the
    operator actually named and migrated it. The one guard this module exists
    for was decided by a different file, and a live paper store would have
    gained two tables in silence.

    The general shape was worse than that one character: an error anywhere in
    the probe returned False as well, so "could not find out" and "confirmed
    not ours" gave the same permissive answer. Here there is no second
    connection to disagree with, no URI to mis-parse, and a read that fails is
    not a verdict at all — it propagates, and the caller names it through
    :func:`_unopenable`.

    The cost of asking on the write connection is that a foreign file is
    opened read-write rather than read-only. Reading ``sqlite_master`` writes
    nothing; the one case where opening could modify the file is recovering a
    hot journal left by a crashed writer — and that case is strictly better
    than before, because a hot journal is exactly what a read-only probe
    cannot recover, so it used to fail, return False, and migrate the file.

    An empty database (no tables) is not foreign: a freshly created store,
    and a path an operator pre-created with ``touch``, both look like that.
    """
    names = {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    return bool(names) and "schema_version" not in names


class ResearchStore:
    """An open ``autoresearch.sqlite``. Use as a context manager.

    Constructing it opens the file, refuses it if it belongs to something
    else, and brings it to :data:`~contrib.autoresearch.schema.SCHEMA_VERSION`.
    Creating the parent directory is part of opening: the default path is
    ``data/`` beside the repo, which a fresh checkout does not have, and
    failing there would make the first fetch of a new clone an error about a
    directory rather than about a store.
    """

    def __init__(self, path: str | Path = _IN_MEMORY) -> None:
        self.path = path
        in_memory = str(path) == _IN_MEMORY
        if not in_memory:
            resolved = Path(path)
            if resolved.exists() and not resolved.is_file():
                raise StoreError(f"--db {resolved} is not a regular file")
            try:
                resolved.parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise StoreError(_unopenable(path, exc)) from exc
        try:
            self.conn = sqlite3.connect(str(path), isolation_level=None)
        except sqlite3.Error as exc:
            raise StoreError(_unopenable(path, exc)) from exc
        try:
            self.conn.row_factory = sqlite3.Row
            # ORDER IS THE POINT here, and asking the foreign question on THIS
            # connection is what makes the order matter at all.
            #
            # ``busy_timeout`` first: it touches no file, and it is what lets
            # the read below wait out a writer rather than fail against one.
            #
            # Then the foreign check, before anything writes. ``journal_mode``
            # is a write to the database header, so an edit that hoisted it
            # above the check would convert a foreign store's journal mode and
            # only then refuse it — a file this package must not touch at all,
            # altered by the very call that declines to use it. The obligation
            # arrived with the check: on the separate connection it used to
            # run on, nothing of ours had been opened yet.
            self.conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
            if not in_memory and _is_foreign(self.conn):
                raise StoreError(
                    f"{path} is a SQLite database but not an AutoResearch store "
                    f"(no schema_version table) — refusing to migrate someone else's "
                    f"file; point --db at a new path or at an existing {DB_FILENAME}"
                )
            if not in_memory:
                self.conn.execute("PRAGMA journal_mode = WAL")
            self.version = self._migrate()
        except sqlite3.Error as exc:
            # sqlite3.connect itself is lazy — it opens no file and validates
            # nothing — so a directory that does not exist, a path that cannot
            # be written, and a file that is not a database all surface HERE,
            # on the first PRAGMA or the first DDL. Left to propagate they are
            # an OperationalError traceback and exit 2, which reads as a defect
            # in this package rather than as the mistyped --db it almost always
            # is. Named instead, with sqlite's own sentence carried inside.
            self.close()
            raise StoreError(_unopenable(path, exc)) from exc
        except BaseException:
            # A close that itself fails must not replace the failure being
            # propagated — and must not leave the handle holding the file.
            self.close()
            raise

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self) -> ResearchStore:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        with suppress(sqlite3.Error):
            self.conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """One flat write transaction. IMMEDIATE, so the write lock is taken up front."""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            with suppress(sqlite3.Error):
                self.conn.execute("ROLLBACK")
            raise
        self.conn.execute("COMMIT")

    def _migrate(self) -> int:
        """Apply every unapplied migration in order; return the resulting version."""
        self.conn.execute(SCHEMA_VERSION_DDL)
        found = self.conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        if found is not None and found > SCHEMA_VERSION:
            raise StoreError(
                f"{self.path} was written at schema v{found}; this build knows "
                f"v{SCHEMA_VERSION} — refusing to write through a store a newer "
                f"build migrated (upgrade, or use a different --db)"
            )
        applied = {row[0] for row in self.conn.execute("SELECT version FROM schema_version")}
        for version in sorted(MIGRATIONS):
            if version in applied:
                continue
            with self.transaction() as conn:
                for statement in MIGRATIONS[version]:
                    conn.execute(statement)
                conn.execute(
                    "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                    (version, _utcnow_iso()),
                )
        return SCHEMA_VERSION

    # -- writes ------------------------------------------------------------

    def upsert_candles(self, coin: str, interval: str, candles: Iterable[Candle]) -> int:
        """Write ``candles`` into the ``coin``/``interval`` series; return the row count.

        An upsert, not an insert, because the backfill pages over ground it
        may already hold (a resumed run, an overlapping window) and because a
        venue that revises a bar should revise the stored one. The count
        returned is rows WRITTEN, which is not the same as rows ADDED — the
        report asks the second question through :meth:`count_candles`, since
        "the fetch wrote 6000 bars" says nothing about whether any were new.
        """
        coin, key = canonical_coin(coin), parse_interval(interval).value
        rows = [
            (
                coin,
                key,
                c.open_time,
                c.close_time,
                str(c.open),
                str(c.high),
                str(c.low),
                str(c.close),
                str(c.volume),
            )
            for c in candles
        ]
        if not rows:
            return 0
        with self.transaction() as conn:
            conn.executemany(
                "INSERT INTO candles"
                " (coin, interval, open_time, close_time, open, high, low, close, volume)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(coin, interval, open_time) DO UPDATE SET"
                " close_time = excluded.close_time, open = excluded.open,"
                " high = excluded.high, low = excluded.low, close = excluded.close,"
                " volume = excluded.volume",
                rows,
            )
        return len(rows)

    def upsert_funding(self, coin: str, points: Iterable[FundingPoint]) -> int:
        """Write ``points`` into ``coin``'s funding series; return the row count."""
        coin = canonical_coin(coin)
        rows = [
            (coin, p.time, str(p.rate), None if p.premium is None else str(p.premium))
            for p in points
        ]
        if not rows:
            return 0
        with self.transaction() as conn:
            conn.executemany(
                "INSERT INTO funding (coin, time, rate, premium) VALUES (?, ?, ?, ?)"
                " ON CONFLICT(coin, time) DO UPDATE SET"
                " rate = excluded.rate, premium = excluded.premium",
                rows,
            )
        return len(rows)

    # -- reads -------------------------------------------------------------

    def iter_candles(
        self,
        coin: str,
        interval: str,
        *,
        since_ms: int | None = None,
        until_ms: int | None = None,
    ) -> Iterator[Candle]:
        """Every stored bar of the series, oldest first, rebuilt as a :class:`Candle`.

        Rebuilt, not handed back as rows: the DTO re-applies the OHLC
        ordering, positive-price and decodable-stamp invariants the venue's
        own bars were checked against, so a row corrupted in the store fails
        HERE — naming the field — rather than as a nonsense number deep in a
        backtest. That is the trade this reader makes on purpose: it costs a
        ``Decimal`` per price, and it is why the gap scan reads through it
        instead of selecting the two stamps it needs.
        """
        key = parse_interval(interval).value
        clauses, values = _window_clause("open_time", since_ms, until_ms)
        cursor = self.conn.execute(
            "SELECT open_time, close_time, open, high, low, close, volume FROM candles"
            f" WHERE coin = ? AND interval = ?{clauses} ORDER BY open_time",
            (canonical_coin(coin), key, *values),
        )
        for row in cursor:
            yield Candle(
                open_time=row["open_time"],
                close_time=row["close_time"],
                open=Decimal(row["open"]),
                high=Decimal(row["high"]),
                low=Decimal(row["low"]),
                close=Decimal(row["close"]),
                volume=Decimal(row["volume"]),
            )

    def iter_funding(
        self,
        coin: str,
        *,
        since_ms: int | None = None,
        until_ms: int | None = None,
    ) -> Iterator[FundingPoint]:
        """Every stored funding point for ``coin``, oldest first, rebuilt as a DTO."""
        clauses, values = _window_clause("time", since_ms, until_ms)
        cursor = self.conn.execute(
            f"SELECT time, rate, premium FROM funding WHERE coin = ?{clauses} ORDER BY time",
            (canonical_coin(coin), *values),
        )
        for row in cursor:
            yield FundingPoint(
                time=row["time"],
                rate=Decimal(row["rate"]),
                premium=None if row["premium"] is None else Decimal(row["premium"]),
            )

    def count_candles(self, coin: str, interval: str) -> int:
        """How many bars of that series the store holds."""
        key = parse_interval(interval).value
        return self.conn.execute(
            "SELECT COUNT(*) FROM candles WHERE coin = ? AND interval = ?",
            (canonical_coin(coin), key),
        ).fetchone()[0]

    def count_funding(self, coin: str) -> int:
        """How many funding points the store holds for ``coin``."""
        return self.conn.execute(
            "SELECT COUNT(*) FROM funding WHERE coin = ?", (canonical_coin(coin),)
        ).fetchone()[0]

    def candle_span(self, coin: str, interval: str) -> tuple[int | None, int | None]:
        """``(earliest, latest)`` ``open_time`` of that series, or ``(None, None)``.

        Asked in SQL rather than by iterating: the span is two aggregates, and
        rebuilding every row into a DTO to take a minimum would make recording
        where a backfill reached cost as much as the backfill.
        """
        row = self.conn.execute(
            "SELECT MIN(open_time), MAX(open_time) FROM candles WHERE coin = ? AND interval = ?",
            (canonical_coin(coin), parse_interval(interval).value),
        ).fetchone()
        return row[0], row[1]

    def funding_span(self, coin: str) -> tuple[int | None, int | None]:
        """``(earliest, latest)`` settlement time for ``coin``, or ``(None, None)``."""
        row = self.conn.execute(
            "SELECT MIN(time), MAX(time) FROM funding WHERE coin = ?", (canonical_coin(coin),)
        ).fetchone()
        return row[0], row[1]

    # -- what the last backfill reached ------------------------------------

    def record_series_state(
        self,
        *,
        coin: str,
        series: str,
        venue_clock_ms: int,
        since_ms: int,
        earliest_ms: int | None,
        latest_ms: int | None,
        rows: int,
        stopped: str,
    ) -> None:
        """Record what the last backfill of one series actually reached.

        Upserted, one row per series: the question is about the present, not a
        log. ``stopped`` is the fact that cannot be recovered later - the rows
        never say which ending produced them - and it is what separates "the
        venue serves nothing older" from "this backfill was interrupted", two
        stores the gap scan reports identically.
        """
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO series_state"
                " (coin, series, venue_clock_ms, since_ms, earliest_ms, latest_ms,"
                "  rows, stopped, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(coin, series) DO UPDATE SET"
                " venue_clock_ms = excluded.venue_clock_ms, since_ms = excluded.since_ms,"
                " earliest_ms = excluded.earliest_ms, latest_ms = excluded.latest_ms,"
                " rows = excluded.rows, stopped = excluded.stopped,"
                " updated_at = excluded.updated_at",
                (
                    canonical_coin(coin),
                    series,
                    venue_clock_ms,
                    since_ms,
                    earliest_ms,
                    latest_ms,
                    rows,
                    stopped,
                    _utcnow_iso(),
                ),
            )

    def series_state(self, *, coin: str, series: str) -> sqlite3.Row | None:
        """The recorded state of one series, or ``None`` if it was never fetched.

        ``None`` is not "empty": a series nothing ever fetched and a series
        whose fetch found nothing are different situations, and only the first
        has no row here.
        """
        return self.conn.execute(
            "SELECT * FROM series_state WHERE coin = ? AND series = ?",
            (canonical_coin(coin), series),
        ).fetchone()
