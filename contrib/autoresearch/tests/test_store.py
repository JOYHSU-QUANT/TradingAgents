"""Opening ``autoresearch.sqlite``, refusing what is not one, and round-tripping rows."""

from __future__ import annotations

import contextlib
import sqlite3
import threading
from decimal import Decimal
from pathlib import Path

import pytest

from contrib.autoresearch.schema import SCHEMA_VERSION
from contrib.autoresearch.store import DB_FILENAME, ResearchStore, StoreError, default_db_path
from contrib.autoresearch.upstream import Candle, FundingPoint

from .conftest import ANCHOR_MS, bars, funding_points


def test_a_fresh_store_is_migrated_to_the_current_version(tmp_path):
    path = tmp_path / "nested" / DB_FILENAME
    with ResearchStore(path) as store:
        assert store.version == SCHEMA_VERSION
    # The parent directory is created by opening, because the default path is
    # a ``data/`` a fresh checkout does not have.
    assert path.exists()


def test_reopening_applies_nothing_and_keeps_one_row_per_version(tmp_path):
    path = tmp_path / DB_FILENAME
    with ResearchStore(path):
        pass
    with ResearchStore(path) as store:
        stamps = store.conn.execute("SELECT version FROM schema_version ORDER BY version").fetchall()
    assert [row[0] for row in stamps] == sorted(range(1, SCHEMA_VERSION + 1))


def test_the_default_path_is_beside_the_repo_not_the_working_directory(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert default_db_path().name == DB_FILENAME
    assert default_db_path().parent.name == "data"
    assert tmp_path not in default_db_path().parents


def test_a_populated_sqlite_file_that_is_not_ours_is_refused_by_name(tmp_path):
    """The operator slip this exists for: ``--db`` aimed at the paper store."""
    foreign = tmp_path / "paper_trading.db"
    conn = sqlite3.connect(foreign)
    conn.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()
    with pytest.raises(StoreError) as caught:
        ResearchStore(foreign)
    assert "not an AutoResearch store" in str(caught.value)
    # Refused BEFORE any write: the foreign file still has exactly its own table.
    conn = sqlite3.connect(foreign)
    names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert names == {"schema_migrations"}


def test_an_empty_file_is_adopted_rather_than_refused(tmp_path):
    """``touch``-ing the path first is a normal thing to do, not a foreign store."""
    path = tmp_path / DB_FILENAME
    path.touch()
    with ResearchStore(path) as store:
        assert store.version == SCHEMA_VERSION


def test_a_directory_is_refused_as_a_path_not_as_a_foreign_store(tmp_path):
    target = tmp_path / "a-directory"
    target.mkdir()
    with pytest.raises(StoreError) as caught:
        ResearchStore(target)
    assert "not a regular file" in str(caught.value)


def test_a_store_from_a_newer_build_is_refused_rather_than_written_through(tmp_path):
    path = tmp_path / DB_FILENAME
    with ResearchStore(path) as store:
        store.conn.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
            (SCHEMA_VERSION + 7, "2026-09-11T00:00:00+00:00"),
        )
    with pytest.raises(StoreError) as caught:
        ResearchStore(path)
    assert f"v{SCHEMA_VERSION + 7}" in str(caught.value)


def test_a_file_that_is_not_a_database_keeps_sqlites_own_diagnosis(tmp_path):
    """Named as unopenable, carrying sqlite's sentence — not relabelled as foreign.

    "This is someone else's store" and "this is not a database at all" send an
    operator to different places, so the refusal quotes the diagnosis it was
    given instead of guessing. It is still a :class:`StoreError`, which is
    what puts it on the CLI's named exit-1 lane rather than in a traceback.
    """
    path = tmp_path / DB_FILENAME
    path.write_bytes(b"this is not a database" * 100)
    with pytest.raises(StoreError) as caught:
        ResearchStore(path)
    message = str(caught.value)
    assert "file is not a database" in message
    assert str(path) in message
    assert "not an AutoResearch store" not in message
    assert isinstance(caught.value.__cause__, sqlite3.DatabaseError)


def test_a_path_that_cannot_be_opened_at_all_is_named_rather_than_a_traceback(tmp_path):
    """A mistyped --db must read as a mistyped --db, not as a defect in here."""
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("", encoding="utf-8")
    with pytest.raises(StoreError) as caught:
        ResearchStore(blocker / "nested" / DB_FILENAME)
    assert "cannot open" in str(caught.value)


def test_candles_round_trip_through_text_without_losing_a_digit(store):
    """The reason decimals are TEXT: a REAL column would round these."""
    exact = Candle(
        open_time=ANCHOR_MS,
        close_time=ANCHOR_MS + 1,
        open=Decimal("0.100000000000000000000001"),
        high=Decimal("123456789.123456789123456789"),
        low=Decimal("0.000000000000000000000001"),
        close=Decimal("1.000000000000000000000000"),
        volume=Decimal("0.1"),
    )
    store.upsert_candles("BTC", "4h", [exact])
    assert list(store.iter_candles("BTC", "4h")) == [exact]


def test_funding_keeps_a_missing_premium_missing(store):
    """``None`` is not zero: a zero premium is a market statement, absence is not."""
    points = [
        FundingPoint(time=ANCHOR_MS, rate=Decimal("0.00001"), premium=None),
        FundingPoint(time=ANCHOR_MS + 1, rate=Decimal("-0.00002"), premium=Decimal("0")),
    ]
    store.upsert_funding("BTC", points)
    assert list(store.iter_funding("BTC")) == points


def test_writing_the_same_bar_again_revises_it_instead_of_duplicating_it(store):
    original = bars(1)[0]
    revised = Candle(
        open_time=original.open_time,
        close_time=original.close_time,
        open=original.open,
        high=original.high + 50,
        low=original.low,
        close=original.close,
        volume=original.volume * 2,
    )
    store.upsert_candles("BTC", "4h", [original])
    store.upsert_candles("BTC", "4h", [revised])
    assert list(store.iter_candles("BTC", "4h")) == [revised]
    assert store.count_candles("BTC", "4h") == 1


def test_series_do_not_leak_across_coin_or_interval(store):
    store.upsert_candles("BTC", "4h", bars(3))
    store.upsert_candles("BTC", "1d", bars(2, interval="1d"))
    store.upsert_candles("ETH", "4h", bars(5))
    assert store.count_candles("BTC", "4h") == 3
    assert store.count_candles("BTC", "1d") == 2
    assert store.count_candles("ETH", "4h") == 5


def test_rows_come_back_oldest_first_whatever_order_they_were_written_in(store):
    written = bars(5)
    store.upsert_candles("BTC", "4h", list(reversed(written)))
    assert [c.open_time for c in store.iter_candles("BTC", "4h")] == [
        c.open_time for c in written
    ]
    store.upsert_funding("BTC", list(reversed(funding_points(4))))
    assert [p.time for p in store.iter_funding("BTC")] == [p.time for p in funding_points(4)]


def test_an_interval_the_vocabulary_does_not_know_is_refused_at_every_verb(store):
    for call in (
        lambda: store.upsert_candles("BTC", "3h", bars(1)),
        lambda: list(store.iter_candles("BTC", "3h")),
        lambda: store.count_candles("BTC", "3h"),
    ):
        with pytest.raises(ValueError):
            call()


def test_a_row_corrupted_in_the_store_fails_the_reader_naming_the_field(store):
    """The reason ``iter_candles`` rebuilds a DTO instead of handing back rows."""
    store.upsert_candles("BTC", "4h", bars(1))
    store.conn.execute("UPDATE candles SET low = '9999999'")
    with pytest.raises(ValueError) as caught:
        list(store.iter_candles("BTC", "4h"))
    assert "OHLC ordering" in str(caught.value)


def test_a_foreign_store_under_a_write_lock_is_still_refused(tmp_path):
    """The verdict must not be decided by a lock that clears a moment later.

    This is the case a separate read-only probe got wrong. That probe set no
    ``busy_timeout``, so a foreign store under a write lock answered it with
    ``database is locked`` immediately; the probe read its own failure as
    "not foreign", and the real connection - which does wait - then opened
    the file and migrated it. Asking on the connection that has the timeout
    removes the disagreement: there is one answer, and it is taken after the
    wait rather than instead of it.

    The writer commits from a timer thread well inside the store's five-second
    timeout, so the lock is real when the open begins and gone before the wait
    expires. ``check_same_thread=False`` is what lets that thread touch it.
    """
    foreign = tmp_path / "paper_trading.db"
    writer = sqlite3.connect(foreign, isolation_level=None, check_same_thread=False)
    writer.execute("PRAGMA journal_mode = DELETE")
    writer.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY)")
    writer.execute("BEGIN EXCLUSIVE")
    writer.execute("INSERT INTO schema_migrations VALUES (1)")
    releaser = threading.Timer(0.4, lambda: writer.execute("COMMIT"))
    releaser.start()
    try:
        with pytest.raises(StoreError) as caught:
            ResearchStore(foreign)
    finally:
        releaser.cancel()
        with contextlib.suppress(sqlite3.Error):
            writer.execute("COMMIT")
        writer.close()
    assert "not an AutoResearch store" in str(caught.value)
    # And the refusal left it alone: still one table, still theirs.
    check = sqlite3.connect(foreign)
    names = {row[0] for row in check.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    check.close()
    assert names == {"schema_migrations"}


def test_refusing_a_foreign_store_does_not_change_its_journal_mode(tmp_path):
    """Nothing may be written to a file this package has not accepted as its own.

    ``PRAGMA journal_mode`` is a write to the database header, and the foreign
    check now runs on the connection that would perform it. Hoisting the
    pragma above the check would convert someone else's store to WAL and only
    then refuse it - a file we must not touch, altered by the call that
    declines to use it.
    """
    foreign = tmp_path / "paper_trading.db"
    writer = sqlite3.connect(foreign, isolation_level=None)
    writer.execute("PRAGMA journal_mode = DELETE")
    writer.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY)")
    writer.close()
    with pytest.raises(StoreError):
        ResearchStore(foreign)
    check = sqlite3.connect(foreign)
    mode = check.execute("PRAGMA journal_mode").fetchone()[0]
    check.close()
    assert str(mode).lower() == "delete"


def test_a_store_path_containing_a_uri_character_is_still_judged(tmp_path):
    """A filesystem path is not URI text, and this file used to treat it as both.

    The foreign check ran on a second connection opened from an interpolated
    ``file:{path}?mode=ro``. A ``#`` in the path opens the URI's fragment, so
    the probe read a file called ``run`` - a different file, with no tables -
    and reported "not foreign" about a store it had never looked at. The real
    connection then opened the file the operator named and migrated it.
    """
    foreign = tmp_path / "run#1.db"
    writer = sqlite3.connect(foreign)
    writer.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY)")
    writer.commit()
    writer.close()
    with pytest.raises(StoreError) as caught:
        ResearchStore(foreign)
    assert "not an AutoResearch store" in str(caught.value)
    check = sqlite3.connect(foreign)
    names = {row[0] for row in check.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    check.close()
    assert names == {"schema_migrations"}


def test_the_default_store_sits_in_the_repo_root_data_directory():
    """Pin the ancestor COUNT, not just the last two segments.

    ``parents[2]`` is the repo root only because this file is two directories
    down. Asserting the parent is named ``data`` holds for any index, because
    the code appends that literal itself - so the index that actually decides
    WHICH ``data`` is the one thing such an assertion cannot see.
    """
    root = Path(__file__).resolve()
    while not (root / "pyproject.toml").exists():
        assert root != root.parent, "no repo root above this test"
        root = root.parent
    assert default_db_path() == root / "data" / DB_FILENAME


def test_a_transaction_that_raises_leaves_the_store_as_it_was(store):
    """The rollback arm, which every passing write in the suite steps around."""
    store.upsert_candles("BTC", "4h", bars(3))
    with pytest.raises(RuntimeError, match="deliberate"), store.transaction() as conn:
        conn.execute("DELETE FROM candles")
        raise RuntimeError("deliberate")
    assert store.count_candles("BTC", "4h") == 3


def test_a_coin_typed_in_lower_case_is_the_same_series(store):
    """One market, one set of rows, however the operator reached them.

    Without this, `--coin btc` builds a second invisible series and
    `gaps --coin BTC` answers "no rows stored" - the shape of a backfill that
    failed, produced by a shift key.
    """
    store.upsert_candles("btc", "4h", bars(5))
    store.upsert_funding(" Btc ", funding_points(3))
    assert store.count_candles("BTC", "4h") == 5
    assert store.count_funding("BTC") == 3
    assert [c.open_time for c in store.iter_candles("bTc", "4h")] == [
        c.open_time for c in bars(5)
    ]


def test_a_read_can_be_bounded_and_stops_at_the_bound(store):
    """The window is INCLUSIVE at both ends, and omitting it reads everything."""
    series = bars(10)
    store.upsert_candles("BTC", "4h", series)
    whole = [c.open_time for c in store.iter_candles("BTC", "4h")]
    assert whole == [c.open_time for c in series]
    windowed = [
        c.open_time
        for c in store.iter_candles(
            "BTC", "4h", since_ms=series[3].open_time, until_ms=series[6].open_time
        )
    ]
    assert windowed == [c.open_time for c in series[3:7]]
    open_ended = [
        c.open_time for c in store.iter_candles("BTC", "4h", since_ms=series[8].open_time)
    ]
    assert open_ended == [c.open_time for c in series[8:]]


def test_a_bounded_funding_read_stops_at_the_bound(store):
    points = funding_points(12)
    store.upsert_funding("BTC", points)
    got = [p.time for p in store.iter_funding("BTC", until_ms=points[4].time)]
    assert got == [p.time for p in points[:5]]


def test_a_series_never_fetched_has_no_recorded_state(store):
    """``None`` is not "empty": nothing fetched and a fetch that found nothing differ."""
    assert store.series_state(coin="BTC", series="4h") is None


def test_the_recorded_state_survives_a_reopen_and_the_latest_write_wins(tmp_path):
    path = tmp_path / DB_FILENAME
    with ResearchStore(path) as store:
        store.upsert_candles("BTC", "4h", bars(4))
        for stopped in ("REACHED_SINCE", "VENUE_EXHAUSTED"):
            store.record_series_state(
                coin="BTC",
                series="4h",
                venue_clock_ms=ANCHOR_MS + 99,
                since_ms=ANCHOR_MS,
                earliest_ms=ANCHOR_MS,
                latest_ms=ANCHOR_MS + 3,
                rows=4,
                stopped=stopped,
            )
    with ResearchStore(path) as reopened:
        state = reopened.series_state(coin="btc", series="4h")
    assert state["stopped"] == "VENUE_EXHAUSTED"
    assert state["rows"] == 4
    assert state["coin"] == "BTC"
