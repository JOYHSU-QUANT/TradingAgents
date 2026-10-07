"""The two equity reads: the latest row with its instant, no row, and a store that is not that store."""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from contrib.carry.stores import Equity, StoreReadError, perp_equity, spot_equity

from .conftest import day, write_perp_store, write_spot_store


def test_perp_equity_is_the_latest_snapshot_of_the_run_with_its_instant(tmp_path: Path):
    store = write_perp_store(
        tmp_path / "paper.db",
        [
            ("2026-02-10T00:00:00+00:00", "carry-ETH-1", "10000"),
            ("2026-02-10T04:00:00+00:00", "carry-ETH-1", "10050.5"),
            ("2026-02-10T08:00:00+00:00", "paper-BTC-8", "99999"),
        ],
    )
    assert perp_equity(store, "carry-ETH-1") == Equity(Decimal("10050.5"), day(40) + 4 * 3_600_000)
    assert perp_equity(store, "paper-BTC-8").value == Decimal("99999")


def test_spot_equity_is_the_latest_valuation_of_the_run_with_its_bar(tmp_path: Path):
    store = write_spot_store(
        tmp_path / "uniswap.db",
        [
            ("paper-carry-1", day(40) // 1000, "10000"),
            ("paper-carry-1", day(41) // 1000, "10120.25"),
            ("paper-trend-1", day(41) // 1000, "5"),
        ],
    )
    assert spot_equity(store, "paper-carry-1") == Equity(Decimal("10120.25"), day(41))
    assert spot_equity(store, "paper-trend-1").value == Decimal("5")


def test_no_row_is_an_error_that_says_how_to_size_a_brand_new_run(tmp_path: Path):
    perp = write_perp_store(tmp_path / "paper.db", [("2026-02-10T00:00:00+00:00", "r", "1")])
    with pytest.raises(
        StoreReadError, match="run 'R' has no equity row.*--perp-db and --perp-run-id"
    ):
        perp_equity(perp, "R")
    spot = write_spot_store(tmp_path / "uniswap.db", [])
    with pytest.raises(StoreReadError, match="run 'paper-ai-1' has no equity row"):
        spot_equity(spot, "paper-ai-1")


def test_a_spot_run_not_quoted_in_a_usd_stable_is_refused(tmp_path: Path):
    weth = write_spot_store(tmp_path / "uniswap.db", [("r", day(40) // 1000, "5")], quote="WETH")
    with pytest.raises(StoreReadError, match="quoted in 'WETH', not a USD stable"):
        spot_equity(weth, "r")
    dai = write_spot_store(tmp_path / "dai.db", [("r", day(40) // 1000, "5")], quote="DAI")
    assert spot_equity(dai, "r").value == Decimal("5")


def test_a_missing_file_is_an_error(tmp_path: Path):
    with pytest.raises(StoreReadError, match="no such file"):
        perp_equity(tmp_path / "nowhere.db", "carry-ETH-1")


def test_the_wrong_kind_of_store_is_an_error(tmp_path: Path):
    spot = write_spot_store(tmp_path / "uniswap.db", [])
    with pytest.raises(StoreReadError, match="no such table"):
        perp_equity(spot, "carry-ETH-1")


def test_a_value_that_is_not_a_number_is_an_error(tmp_path: Path):
    store = write_perp_store(tmp_path / "paper.db", [("2026-02-10T00:00:00+00:00", "r", "lots")])
    with pytest.raises(StoreReadError, match="not a number"):
        perp_equity(store, "r")


def test_a_timestamp_the_perp_package_would_not_write_is_an_error(tmp_path: Path):
    naive = write_perp_store(tmp_path / "paper.db", [("2026-02-10T00:00:00", "r", "1")])
    with pytest.raises(StoreReadError, match="naive"):
        perp_equity(naive, "r")
    spot = write_spot_store(tmp_path / "uniswap.db", [("r", 0, "1")])
    with pytest.raises(StoreReadError, match="bar time 0 is not valid"):
        spot_equity(spot, "r")


def test_the_store_is_opened_read_only(tmp_path: Path, monkeypatch):
    store = write_perp_store(tmp_path / "paper.db", [("2026-02-10T00:00:00+00:00", "r", "1")])
    seen: list[tuple[str, bool]] = []
    real_connect = sqlite3.connect

    def spy(database, *args, **kwargs):
        seen.append((database, kwargs.get("uri", False)))
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", spy)
    perp_equity(store, "r")
    assert len(seen) == 1
    uri, as_uri = seen[0]
    assert as_uri is True and uri.startswith("file:") and uri.endswith("?mode=ro")


def test_a_relative_path_opens(tmp_path: Path, monkeypatch):
    """``Path.as_uri`` would refuse it; the perp package's URI spelling does not."""
    write_perp_store(tmp_path / "paper.db", [("2026-02-10T00:00:00+00:00", "r", "7")])
    monkeypatch.chdir(tmp_path)
    assert perp_equity(Path("paper.db"), "r").value == Decimal("7")
