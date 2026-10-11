"""The store's targets: written once, read by source and bar, and loaded for the config that reads them."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from decimal import Decimal

import pytest

from contrib.uniswap_v3.store.repository import StoreError, open_store
from contrib.uniswap_v3.store.schema import SCHEMA_VERSION
from contrib.uniswap_v3.store.target_source import load_targets
from contrib.uniswap_v3.tests.fakes.engine import DAY, FIRST_DAY, config as _plain_config
from contrib.uniswap_v3.tests.fakes.node import store_at as _store_at
from contrib.uniswap_v3.tests.fakes.targets import SOURCE, config as _config, record, target

_CONFIG = _config()


@pytest.fixture
def store(tmp_path):
    with open_store(tmp_path / "store.db") as opened:
        yield opened


def _day(day: int) -> int:
    return FIRST_DAY + day * DAY


# --- targets -----------------------------------------------------------------


def test_a_target_comes_back_as_it_was_written(store):
    aimed = record()
    store.insert_target(aimed)
    assert store.target(SOURCE, "WETH", FIRST_DAY) == aimed
    assert store.target(SOURCE, "WETH", _day(1)) is None
    assert store.target(SOURCE, "WBTC", FIRST_DAY) is None
    assert store.target("another-coordinator", "WETH", FIRST_DAY) is None


def test_a_weight_keeps_its_digits(store):
    store.insert_target(record(weight="0.1234"))
    assert store.target(SOURCE, "WETH", FIRST_DAY).target.weight == Decimal("0.1234")
    assert store._connection.execute("SELECT weight FROM targets").fetchone() == ("0.1234",)


def test_a_target_is_written_once_and_never_rewritten(store):
    store.insert_target(record())
    with pytest.raises(StoreError, match="never rewritten"):
        store.insert_target(record(weight="0.9"))
    assert store.target(SOURCE, "WETH", FIRST_DAY).target.weight == Decimal("0.3")


def test_the_targets_at_a_bar_are_those_of_one_source_by_symbol(store):
    for aimed in (
        record("WETH", 0),
        record("WBTC", 0, "0.2"),
        record("WETH", 1, "0.5"),
        record("WETH", 0, source="another-coordinator"),
    ):
        store.insert_target(aimed)
    assert store.targets_at(SOURCE, _day(0)) == [record("WBTC", 0, "0.2"), record("WETH", 0)]
    assert store.targets_at(SOURCE, _day(1)) == [record("WETH", 1, "0.5")]
    assert store.targets_at(SOURCE, _day(2)) == []
    assert store.targets_at("another-coordinator", _day(0)) == [
        record("WETH", 0, source="another-coordinator")
    ]


@pytest.mark.parametrize(
    ("statement", "match"),
    [
        ("UPDATE targets SET weight = '1.5'", "is not valid"),
        ("UPDATE targets SET weight = 'lots'", "is not a decimal"),
        ("UPDATE targets SET digest = 'xyz'", "is not valid"),
    ],
)
def test_a_stored_target_that_no_longer_reads_is_a_store_error(tmp_path, statement, match):
    path = tmp_path / "store.db"
    with open_store(path) as store:
        store.insert_target(record())
    connection = sqlite3.connect(path)
    connection.execute(statement)
    connection.commit()
    connection.close()
    with open_store(path) as store:
        with pytest.raises(StoreError, match=match):
            store.target(SOURCE, "WETH", FIRST_DAY)
        with pytest.raises(StoreError, match=match):
            store.targets_at(SOURCE, FIRST_DAY)
        with pytest.raises(StoreError, match=match):
            load_targets(store, _CONFIG, FIRST_DAY)


def test_a_store_from_before_targets_gains_the_table_and_keeps_its_rows(tmp_path):
    path = tmp_path / "store.db"
    connection = _store_at(path, 6)
    connection.execute(
        "INSERT INTO verdicts VALUES (?, 'WETH', ?, 'Buy', 'm', 'v', 0, ?, NULL, NULL)",
        (SOURCE, FIRST_DAY, "ab" * 32),
    )
    connection.close()
    with open_store(path) as store:
        assert store.targets_at(SOURCE, FIRST_DAY) == []
        store.insert_target(record())
        assert store.target(SOURCE, "WETH", FIRST_DAY) == record()
        assert store.verdict(SOURCE, "WETH", FIRST_DAY) is not None
    connection = sqlite3.connect(path)
    assert connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone() == (
        SCHEMA_VERSION,
    )
    connection.close()


# --- what a config reads -----------------------------------------------------


def test_a_config_without_a_targets_section_reads_none(store):
    store.insert_target(record())
    assert load_targets(store, _plain_config(), FIRST_DAY) == {}


def test_a_config_reads_its_sources_targets_on_the_tokens_it_trades(store):
    for aimed in (
        record("WETH", 0),
        record("WBTC", 0, "0.2"),
        # The quote token, a token the config does not trade, another source, another bar.
        record("USDC", 0),
        record("LINK", 0),
        record("WETH", 0, "0.9", source="another-coordinator"),
        record("WETH", 1, "0.5"),
    ):
        store.insert_target(aimed)
    aimed = load_targets(store, _CONFIG, FIRST_DAY)
    assert aimed == {"WETH": target("WETH", 0), "WBTC": target("WBTC", 0, "0.2")}
    with pytest.raises(TypeError):
        aimed["LINK"] = target("LINK", 0)  # type: ignore[index]
    assert load_targets(store, _config("another-coordinator"), FIRST_DAY) == {
        "WETH": target("WETH", 0, "0.9", source="another-coordinator")
    }
    # The source set nothing at this bar: a mapping, and an empty one.
    assert load_targets(store, _CONFIG, _day(2)) == {}


def test_the_fakes_config_reads_the_test_source_over_the_engine_fakes():
    assert _CONFIG.targets is not None and _CONFIG.targets.source == SOURCE
    assert replace(_CONFIG, targets=None) == _plain_config()
