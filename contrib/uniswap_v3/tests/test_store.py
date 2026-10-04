"""The store: its schema and versions, the rows it keeps, and the bars built from them."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from decimal import Decimal

import pytest

from contrib.uniswap_v3.config import StrategySpec, UniswapConfig
from contrib.uniswap_v3.constants import ETHEREUM_MAINNET, POOLS, TOKENS
from contrib.uniswap_v3.domain.bars import BarFlag, BarSettings, Finality
from contrib.uniswap_v3.domain.prices import MAX_SQRT_RATIO
from contrib.uniswap_v3.domain.records import BarSeen
from contrib.uniswap_v3.ports import BarSource
from contrib.uniswap_v3.store.bar_source import StoreBarSource, load_bar
from contrib.uniswap_v3.store.repository import StoreError, open_store
from contrib.uniswap_v3.store.schema import APPLICATION_ID, SCHEMA_VERSION, transaction
from contrib.uniswap_v3.tests.fakes.node import (
    BTC_TICK as _BTC_TICK,
    DAY,
    DEFAULT_TICK as _ETH_TICK,
    FIRST_DAY,
    pool_bar as _reading,
)

_TOKENS = TOKENS[ETHEREUM_MAINNET]
_USDC_WETH = POOLS[ETHEREUM_MAINNET]["USDC/WETH-500"]
_WBTC_WETH = POOLS[ETHEREUM_MAINNET]["WBTC/WETH-500"]
_CONFIG = UniswapConfig(
    chain_id=ETHEREUM_MAINNET,
    quote=_TOKENS["USDC"],
    tokens=(_TOKENS["USDC"], _TOKENS["WETH"], _TOKENS["WBTC"]),
    pools=(_USDC_WETH, _WBTC_WETH),
    strategy=StrategySpec(name="fixed_weights", params={}),
)


@pytest.fixture
def store(tmp_path):
    with open_store(tmp_path / "store.db") as opened:
        yield opened


def _series(pool=_USDC_WETH) -> tuple[int, str, int]:
    return ETHEREUM_MAINNET, pool.address, DAY


# --- schema ----------------------------------------------------------------


def test_a_new_store_is_marked_and_at_the_current_schema_version(tmp_path):
    path = tmp_path / "store.db"
    open_store(path).close()
    connection = sqlite3.connect(path)
    assert connection.execute("PRAGMA application_id").fetchone() == (APPLICATION_ID,)
    versions = connection.execute("SELECT version FROM schema_migrations").fetchall()
    assert versions == [(version,) for version in range(1, SCHEMA_VERSION + 1)]
    connection.close()


def test_opening_a_store_again_keeps_its_rows_and_applies_nothing_twice(tmp_path):
    path = tmp_path / "store.db"
    with open_store(path) as first:
        first.insert_bars([_reading()])
    with open_store(path) as second:
        assert second.bar(*_series(), FIRST_DAY) == _reading()
    connection = sqlite3.connect(path)
    assert connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone() == (
        SCHEMA_VERSION,
    )
    connection.close()


def test_a_store_written_by_a_newer_version_is_refused(tmp_path):
    path = tmp_path / "store.db"
    open_store(path).close()
    connection = sqlite3.connect(path)
    connection.execute("INSERT INTO schema_migrations VALUES (?, 0)", (SCHEMA_VERSION + 1,))
    connection.commit()
    connection.close()
    with pytest.raises(StoreError, match="a newer version of the package wrote it"):
        open_store(path)


def test_another_programs_database_is_refused_and_left_untouched(tmp_path):
    path = tmp_path / "other.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY)")
    connection.commit()
    connection.close()
    with pytest.raises(StoreError, match="was not created by contrib/uniswap_v3"):
        open_store(path)
    connection = sqlite3.connect(path)
    tables = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    assert tables == [("schema_migrations",)]
    connection.close()


def test_a_file_that_is_not_a_database_is_refused(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("not a database, and long enough to have a header to misread", encoding="utf-8")
    with pytest.raises(StoreError, match="cannot be used"):
        open_store(path)


def test_a_store_in_a_directory_that_does_not_exist_is_refused(tmp_path):
    with pytest.raises(StoreError, match="cannot be opened"):
        open_store(tmp_path / "missing" / "store.db")


def test_a_read_only_open_does_not_create_the_store(tmp_path):
    path = tmp_path / "store.db"
    with pytest.raises(StoreError, match="there is no store at"):
        open_store(path, create=False)
    assert not path.exists()


# --- rows ------------------------------------------------------------------


def test_a_reading_comes_back_as_it_was_written(store):
    # The largest values a pool and a block can hold do not fit an SQLite integer.
    huge = _reading(
        sqrt_price_x96=MAX_SQRT_RATIO - 1, tick=887_272, twap_tick=-887_272, base_fee_wei=2**256 - 1
    )
    store.insert_bars([huge, _reading(_WBTC_WETH, finality=Finality.PENDING)])
    assert store.bar(*_series(), FIRST_DAY) == huge
    assert store.bar(*_series(_WBTC_WETH), FIRST_DAY) == _reading(
        _WBTC_WETH, finality=Finality.PENDING
    )
    assert store.bar(*_series(), FIRST_DAY + DAY) is None
    assert store.bar(ETHEREUM_MAINNET, _USDC_WETH.address, 3_600, FIRST_DAY) is None


def test_a_reading_already_stored_is_not_replaced_and_its_batch_is_not_written(store):
    store.insert_bars([_reading()])
    with pytest.raises(StoreError, match="already stored"):
        store.insert_bars([_reading(_WBTC_WETH), _reading(tick=_ETH_TICK + 100)])
    assert store.bar(*_series(), FIRST_DAY) == _reading()
    assert store.bar(*_series(_WBTC_WETH), FIRST_DAY) is None


def test_the_same_boundary_is_kept_once_per_interval(store):
    hourly = _reading(interval_seconds=3_600)
    store.insert_bars([_reading(), hourly])
    assert store.bar(*_series(), FIRST_DAY) == _reading()
    assert store.bar(ETHEREUM_MAINNET, _USDC_WETH.address, 3_600, FIRST_DAY) == hourly


def test_the_times_the_previous_reading_and_the_extent_of_a_series(store):
    days = [FIRST_DAY, FIRST_DAY + DAY, FIRST_DAY + 3 * DAY]
    store.insert_bars([_reading(time=time) for time in days])
    store.insert_bars([_reading(_WBTC_WETH, time=FIRST_DAY + 2 * DAY)])

    assert store.bar_times(*_series(), start=FIRST_DAY, end=FIRST_DAY + 3 * DAY) == set(days)
    assert store.bar_times(*_series(), start=FIRST_DAY + DAY, end=FIRST_DAY + 2 * DAY) == {
        FIRST_DAY + DAY
    }
    assert store.latest_times(*_series(), 2) == days[1:]
    assert store.latest_times(*_series(), 10) == days
    assert store.previous_bar(*_series(), FIRST_DAY + 3 * DAY) == _reading(time=FIRST_DAY + DAY)
    assert store.previous_bar(*_series(), FIRST_DAY) is None
    assert store.extent(*_series()) == (3, FIRST_DAY, FIRST_DAY + 3 * DAY)
    assert store.extent(ETHEREUM_MAINNET, _USDC_WETH.address, 3_600) == (0, None, None)


def test_the_twap_windows_of_a_series_and_a_number_sqlite_cannot_hold(store):
    assert store.twap_windows(*_series()) == set()
    store.insert_bars(
        [_reading(), _reading(time=FIRST_DAY + DAY, twap_window_seconds=600), _reading(_WBTC_WETH)]
    )
    assert store.twap_windows(*_series()) == {600, 1_800}
    assert store.twap_windows(*_series(_WBTC_WETH)) == {1_800}
    with pytest.raises(StoreError, match="the store failed while reading bar times"):
        store.latest_times(*_series(), 2**70)


def test_pending_readings_are_listed_until_their_finality_is_recorded(store):
    final = _reading()
    first = _reading(time=FIRST_DAY + DAY, finality=Finality.PENDING)
    second = _reading(_WBTC_WETH, time=FIRST_DAY + DAY, finality=Finality.PENDING)
    other_chain = _reading(chain_id=5, finality=Finality.PENDING)
    store.insert_bars([final, second, first, other_chain])
    assert set(store.pending_bars(ETHEREUM_MAINNET)) == {first, second}

    store.set_finality([first], Finality.FINAL)
    store.set_finality([second], Finality.REORGED)
    assert store.pending_bars(ETHEREUM_MAINNET) == []
    assert store.bar(*_series(), FIRST_DAY + DAY) == replace(first, finality=Finality.FINAL)
    assert store.bar(*_series(_WBTC_WETH), FIRST_DAY + DAY) == replace(
        second, finality=Finality.REORGED
    )
    assert store.pending_bars(5) == [other_chain]


def test_recording_finality_on_a_reading_that_is_not_stored_changes_nothing(store):
    stored = _reading(finality=Finality.PENDING)
    store.insert_bars([stored])
    with pytest.raises(StoreError, match="is not a pending reading in the store"):
        store.set_finality([stored, _reading(time=FIRST_DAY + DAY)], Finality.FINAL)
    assert store.bar(*_series(), FIRST_DAY) == stored


def test_a_verdict_is_given_once_and_is_never_pending(store):
    final = _reading()
    reorged = _reading(_WBTC_WETH, finality=Finality.REORGED)
    store.insert_bars([final, reorged])
    with pytest.raises(StoreError, match="is not a pending reading in the store"):
        store.set_finality([final], Finality.REORGED)
    with pytest.raises(StoreError, match="is not a pending reading in the store"):
        store.set_finality([reorged], Finality.FINAL)
    with pytest.raises(ValueError, match="does not go back to pending"):
        store.set_finality([final], Finality.PENDING)
    assert store.bar(*_series(), FIRST_DAY) == final
    assert store.bar(*_series(_WBTC_WETH), FIRST_DAY) == reorged


def test_a_transaction_that_fails_leaves_the_connection_out_of_one(tmp_path):
    connection = sqlite3.connect(tmp_path / "plain.db", isolation_level=None)
    connection.execute("CREATE TABLE t (x INTEGER PRIMARY KEY)")
    with pytest.raises(sqlite3.IntegrityError), transaction(connection):
        connection.execute("INSERT INTO t VALUES (1)")
        connection.execute("INSERT INTO t VALUES (1)")
    assert connection.in_transaction is False
    assert connection.execute("SELECT COUNT(*) FROM t").fetchone() == (0,)
    # A transaction SQLite has already ended raises the first error, not the rollback's.
    with pytest.raises(RuntimeError, match="the first error"), transaction(connection):
        connection.execute("ROLLBACK")
        raise RuntimeError("the first error")
    connection.close()


@pytest.mark.parametrize("finality", list(Finality))
def test_the_schema_allows_every_finality_and_nothing_else(tmp_path, finality):
    path = tmp_path / "store.db"
    with open_store(path) as opened:
        opened.insert_bars([_reading(finality=finality)])
    connection = sqlite3.connect(path)
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("UPDATE bars SET finality = 'maybe'")
    connection.close()


def test_a_row_that_is_not_a_valid_reading_is_refused_on_the_way_out(tmp_path):
    path = tmp_path / "store.db"
    with open_store(path) as opened:
        opened.insert_bars([_reading()])
    connection = sqlite3.connect(path)
    connection.execute("UPDATE bars SET sqrt_price_x96 = 'much'")
    connection.commit()
    connection.close()
    with open_store(path) as opened, pytest.raises(StoreError, match="is not a valid reading"):
        opened.bar(*_series(), FIRST_DAY)


# --- bars ------------------------------------------------------------------


def test_a_bar_is_built_from_every_configured_pools_reading(store):
    store.insert_bars([_reading(), _reading(_WBTC_WETH)])
    stored = load_bar(store, _CONFIG, FIRST_DAY)
    assert stored.readings == (_reading(), _reading(_WBTC_WETH))
    assert stored.flags == (frozenset(), frozenset())
    assert stored.bar.suspect is False
    assert set(stored.bar.prices) == {"WETH", "WBTC"}
    assert Decimal(1_990) < stored.bar.prices["WETH"] < Decimal(2_010)
    assert Decimal(28_000) < stored.bar.prices["WBTC"] < Decimal(32_000)

    source = StoreBarSource(store, _CONFIG)
    assert isinstance(source, BarSource)
    assert source.bar_at(FIRST_DAY) == stored.bar


def test_a_boundary_without_every_pools_reading_has_no_bar(store):
    store.insert_bars([_reading()])
    assert load_bar(store, _CONFIG, FIRST_DAY) is None
    assert StoreBarSource(store, _CONFIG).bar_at(FIRST_DAY) is None
    assert StoreBarSource(store, _CONFIG).bar_at(FIRST_DAY + DAY) is None


def test_a_bar_reads_only_the_configured_interval(store):
    store.insert_bars([_reading(), _reading(_WBTC_WETH)])
    hourly = replace(_CONFIG, bars=BarSettings(interval_seconds=3_600))
    assert load_bar(store, hourly, FIRST_DAY) is None


def test_the_flags_are_worked_out_against_the_reading_before(store):
    store.insert_bars([_reading(), _reading(_WBTC_WETH)])
    store.insert_bars(
        [
            _reading(time=FIRST_DAY + 2 * DAY),
            _reading(_WBTC_WETH, time=FIRST_DAY + 2 * DAY, twap_tick=_BTC_TICK + 600),
        ]
    )
    stored = load_bar(store, _CONFIG, FIRST_DAY + 2 * DAY)
    assert stored.flags == (
        frozenset({BarFlag.GAP}),
        frozenset({BarFlag.GAP, BarFlag.TWAP_DEVIATION}),
    )
    assert stored.bar.suspect is True

    # Filling the gap in changes what the same stored readings are flagged with.
    store.insert_bars(
        [_reading(time=FIRST_DAY + DAY), _reading(_WBTC_WETH, time=FIRST_DAY + DAY)]
    )
    refilled = load_bar(store, _CONFIG, FIRST_DAY + 2 * DAY)
    assert refilled.flags == (frozenset(), frozenset({BarFlag.TWAP_DEVIATION}))


def test_a_gap_alone_does_not_make_a_bar_suspect(store):
    store.insert_bars([_reading(), _reading(_WBTC_WETH)])
    later = FIRST_DAY + 2 * DAY
    store.insert_bars([_reading(time=later), _reading(_WBTC_WETH, time=later)])
    stored = load_bar(store, _CONFIG, later)
    assert stored.flags == (frozenset({BarFlag.GAP}), frozenset({BarFlag.GAP}))
    assert stored.bar.suspect is False


def test_a_reading_taken_over_another_twap_window_than_the_configs_is_refused(store):
    store.insert_bars([_reading(), _reading(_WBTC_WETH, twap_window_seconds=600)])
    with pytest.raises(
        StoreError, match="WBTC/WETH-500 at .* over 600 seconds, and the settings ask for 1800"
    ):
        load_bar(store, _CONFIG, FIRST_DAY)


def test_a_failure_of_sqlite_is_a_store_error(tmp_path):
    opened = open_store(tmp_path / "store.db")
    opened.close()
    with pytest.raises(StoreError, match="the store failed while reading a bar"):
        opened.bar(*_series(), FIRST_DAY)
    with pytest.raises(StoreError, match="the store failed while writing readings"):
        opened.insert_bars([_reading()])


def test_a_reorged_reading_makes_its_bar_suspect(store):
    store.insert_bars([_reading(), _reading(_WBTC_WETH, finality=Finality.REORGED)])
    stored = load_bar(store, _CONFIG, FIRST_DAY)
    assert stored.flags == (frozenset(), frozenset({BarFlag.REORGED}))
    assert stored.bar.suspect is True


def test_a_stored_bar_says_what_a_decision_keeps_of_it(store):
    store.insert_bars([_reading(), _reading(_WBTC_WETH, finality=Finality.PENDING)])
    stored = load_bar(store, _CONFIG, FIRST_DAY)
    # The bar is as final as its reading furthest from final.
    assert stored.seen == BarSeen(
        close_block_hash=_reading().close_block_hash, finality=Finality.PENDING
    )


def test_where_the_readings_close_on_different_blocks_the_hash_kept_is_the_highest_blocks(store):
    later = "0x" + "cd" * 32
    store.insert_bars(
        [_reading(), _reading(_WBTC_WETH, close_block=1_000, close_block_hash=later)]
    )
    stored = load_bar(store, _CONFIG, FIRST_DAY)
    assert stored.bar.close_block == 1_000
    assert stored.seen.close_block_hash == later
