"""The command line: backfill and status against a scripted chain, and the exit codes."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

from contrib.uniswap_v3 import cli
from contrib.uniswap_v3.chain import rpc as chain_rpc
from contrib.uniswap_v3.chain.errors import RpcConfigError
from contrib.uniswap_v3.constants import ETHEREUM_MAINNET, POOLS
from contrib.uniswap_v3.domain.verdicts import Rating
from contrib.uniswap_v3.store.repository import open_store
from contrib.uniswap_v3.tests.fakes.node import (
    BTC_TICK as _BTC_TICK,
    DAY,
    FIRST_DAY,
    FakeNode,
    block_at,
    put_day,
    sqrt_price_at,
)
from contrib.uniswap_v3.tests.fakes.rpc import rpc_over
from contrib.uniswap_v3.tests.fakes.verdicts import record

_EXAMPLE = Path(__file__).resolve().parents[1] / "configs" / "uniswap_v3.example.yaml"
_USDC_WETH = POOLS[ETHEREUM_MAINNET]["USDC/WETH-500"]
_WBTC_WETH = POOLS[ETHEREUM_MAINNET]["WBTC/WETH-500"]


@pytest.fixture
def node(monkeypatch):
    """The chain ``backfill`` connects to, its WBTC/WETH pool near 15 WETH per WBTC.

    The chain ID and settings of each connection are kept on it.
    """
    chain = FakeNode()
    for day in range(4):
        close = block_at(FIRST_DAY + day * DAY) - 1
        chain.slot0[(_WBTC_WETH.address.lower(), close)] = (sqrt_price_at(_BTC_TICK), _BTC_TICK)
        chain.twap_tick[(_WBTC_WETH.address.lower(), close)] = _BTC_TICK
    chain.connections = []

    def connect(chain_id, *, settings=None, env=None):
        chain.connections.append((chain_id, settings))
        return rpc_over(chain.provider, chain_id=chain_id, attempts=1)[0]

    monkeypatch.setattr(chain_rpc, "connect", connect)
    return chain


def _run(*argv: str, now: int = FIRST_DAY + 2 * DAY + 600) -> tuple[int, list[str]]:
    lines: list[str] = []
    code = cli.main(list(argv), out=lines.append, now=lambda: float(now))
    return code, lines


def _backfill(db: Path, *extra: str, start: str = "2024-01-01", **kwargs) -> tuple[int, list[str]]:
    return _run(
        "backfill", "--config", str(_EXAMPLE), "--db", str(db), "--from", start, *extra, **kwargs
    )


def _status(db: Path, *extra: str) -> tuple[int, list[str]]:
    return _run("status", "--config", str(_EXAMPLE), "--db", str(db), *extra)


# --- backfill --------------------------------------------------------------


def test_backfill_estimates_reads_and_reports(node, tmp_path):
    code, lines = _backfill(tmp_path / "store.db")
    assert code == cli.EXIT_OK
    assert lines == [
        "3 bar(s) from 2024-01-01T00:00:00Z to 2024-01-03T00:10:00Z: 3 to read, "
        "about 54 request(s)",
        f"2024-01-01T00:00:00Z  block {block_at(FIRST_DAY) - 1}, final",
        f"2024-01-02T00:00:00Z  block {block_at(FIRST_DAY + DAY) - 1}, final",
        f"2024-01-03T00:00:00Z  block {block_at(FIRST_DAY + 2 * DAY) - 1}, final",
        "wrote 3 bar(s); 0 already stored, 0 without an answer, 0 not reached yet",
    ]
    assert [chain_id for chain_id, _ in node.connections] == [ETHEREUM_MAINNET]
    # The example config names the variable the URL is read from.
    assert node.connections[0][1].url_env == "ETH_RPC_URL"


def test_backfill_run_again_writes_no_second_row(node, tmp_path):
    db = tmp_path / "store.db"
    _backfill(db)
    code, lines = _backfill(db)
    assert code == cli.EXIT_OK
    assert lines[0].endswith("0 to read, about 0 request(s)")
    assert lines[-1] == "wrote 0 bar(s); 3 already stored, 0 without an answer, 0 not reached yet"
    with open_store(db) as store:
        for pool in (_USDC_WETH, _WBTC_WETH):
            assert store.extent(ETHEREUM_MAINNET, pool.address, DAY)[0] == 3


def test_backfill_takes_an_end_and_an_interval_from_the_command_line(node, tmp_path):
    code, lines = _backfill(
        tmp_path / "store.db", "--to", "2024-01-01T02:30:00", "--interval-seconds", "3600"
    )
    assert code == cli.EXIT_OK
    assert lines[0].startswith("3 bar(s) from 2024-01-01T00:00:00Z to 2024-01-01T02:30:00Z")
    assert [line[:20] for line in lines[1:4]] == [
        "2024-01-01T00:00:00Z",
        "2024-01-01T01:00:00Z",
        "2024-01-01T02:00:00Z",
    ]


def test_a_dry_run_prints_the_estimate_and_connects_to_nothing(node, tmp_path):
    code, lines = _backfill(tmp_path / "store.db", "--dry-run")
    assert code == cli.EXIT_OK
    assert len(lines) == 1 and "3 to read" in lines[0]
    assert node.connections == [] and node.provider.requests == []
    # A dry run leaves no store behind a path that had none.
    assert not (tmp_path / "store.db").exists()


def test_a_dry_run_over_a_store_counts_only_what_it_lacks(node, tmp_path):
    db = tmp_path / "store.db"
    _backfill(db, "--to", "2024-01-01")
    code, lines = _backfill(db, "--dry-run")
    assert code == cli.EXIT_OK
    assert len(lines) == 1 and "3 bar(s)" in lines[0] and "2 to read" in lines[0]


def test_backfill_reports_the_readings_it_confirmed(node, tmp_path):
    db = tmp_path / "store.db"
    node.head = block_at(FIRST_DAY + 2 * DAY) + 10
    _backfill(db)
    node.head += 64
    code, lines = _backfill(db)
    assert code == cli.EXIT_OK
    assert lines[-1] == "checked pending readings against the final chain: 2 final, 0 reorged"


def test_a_range_that_cannot_be_backfilled_exits_1(node, tmp_path, capsys):
    code, lines = _backfill(tmp_path / "store.db", start="2024-01-01T00:00:01")
    assert code == cli.EXIT_FAILED and lines == []
    assert "must start on a bar boundary" in capsys.readouterr().err
    assert node.connections == []
    assert not (tmp_path / "store.db").exists()


def test_a_node_that_fails_a_read_exits_3_and_a_wrong_answer_exits_1(
    node, tmp_path, capsys
):
    db = tmp_path / "store.db"
    close = block_at(FIRST_DAY + DAY) - 1
    node.errors[close] = {"code": 429, "message": "too many requests"}
    code, _ = _backfill(db)
    assert code == cli.EXIT_RETRY
    assert capsys.readouterr().err.startswith("try again later: ")

    # A node that answers the read with an error may answer it next time.
    node.errors[close] = {"code": -32000, "message": "internal error"}
    code, lines = _backfill(db)
    assert code == cli.EXIT_RETRY
    assert capsys.readouterr().err.startswith("try again later: ")
    # What the first run wrote before it failed was not read again.
    assert lines[0].startswith("3 bar(s)") and "2 to read" in lines[0]

    # An answer that cannot be right will be the same answer next time.
    del node.errors[close]
    node.slot0[(_USDC_WETH.address.lower(), close)] = (1, 0)
    code, _ = _backfill(db)
    assert code == cli.EXIT_FAILED
    assert capsys.readouterr().err.startswith("failed: ")


def test_boundaries_without_an_answer_exit_0_with_a_warning_on_stderr(node, tmp_path, capsys):
    close = block_at(FIRST_DAY + DAY) - 1
    node.reverts.add((_WBTC_WETH.address.lower(), close))
    code, lines = _backfill(tmp_path / "store.db")
    assert code == cli.EXIT_OK
    assert lines[2].startswith("2024-01-02T00:00:00Z  no answer (")
    assert lines[-1] == "wrote 2 bar(s); 0 already stored, 1 without an answer, 0 not reached yet"
    assert capsys.readouterr().err == (
        "warning: 1 boundary(ies) without an answer, from 2024-01-02T00:00:00Z to "
        "2024-01-02T00:00:00Z; a later run asks again\n"
    )


def test_what_a_node_said_is_reported_on_one_ascii_line(node, tmp_path, capsys):
    close = block_at(FIRST_DAY + DAY) - 1
    node.reverts.add((_WBTC_WETH.address.lower(), close))
    node.revert_message = "execution reverted: vieux" + chr(0xE9) + chr(10) + "forged line"
    code, lines = _backfill(tmp_path / "store.db")
    assert code == cli.EXIT_OK
    assert len(lines) == 5
    assert "vieux" in lines[2] and lines[2].isascii() and "\n" not in lines[2]
    capsys.readouterr()
    noisy = "vieux" + chr(0xE9) + chr(10) + "forged  line" + chr(27) + "[2J"
    assert cli._one_ascii_line(noisy) == "vieux" + chr(92) + "xe9 forged line?[2J"


def test_a_time_the_platform_cannot_print_back_is_refused(monkeypatch, capsys):
    def unprintable(time: int) -> str:
        raise OSError(22, "Invalid argument")

    monkeypatch.setattr(cli, "_iso", unprintable)
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["backfill", "--config", "c.yaml", "--db", "s.db", "--from", "2024-01-01"])
    assert exit_info.value.code == 2
    assert "is outside the times supported" in capsys.readouterr().err


def test_a_node_whose_head_is_behind_exits_3_once_a_boundary_is_overdue(node, tmp_path, capsys):
    db = tmp_path / "store.db"
    # The chain's head is in the third day. Five minutes into the fourth,
    # its boundary is not overdue yet; a second later it is.
    fourth = FIRST_DAY + 3 * DAY
    node.head = block_at(fourth) - 1
    code, lines = _backfill(db, now=fourth + 299)
    assert code == cli.EXIT_OK
    assert lines[-1] == "wrote 3 bar(s); 0 already stored, 0 without an answer, 1 not reached yet"
    assert capsys.readouterr().err == ""

    code, lines = _backfill(db, now=fourth + 300)
    assert code == cli.EXIT_RETRY
    assert lines[-1] == "wrote 0 bar(s); 3 already stored, 0 without an answer, 1 not reached yet"
    assert capsys.readouterr().err == (
        "try again later: the node's head is behind; 1 boundary(ies) from "
        "2024-01-04T00:00:00Z have passed and are not on its chain yet\n"
    )


def test_an_end_in_the_future_is_cut_at_now(node, tmp_path, capsys):
    code, lines = _backfill(tmp_path / "store.db", "--to", "2030-01-01")
    assert code == cli.EXIT_OK
    assert lines[0].startswith("3 bar(s) from 2024-01-01T00:00:00Z to 2024-01-03T00:10:00Z")
    assert capsys.readouterr().err == ""


def test_backfill_without_its_requirements_exits_1_with_one_line(monkeypatch, tmp_path, capsys):
    monkeypatch.setitem(sys.modules, "contrib.uniswap_v3.backfill", None)
    code, _ = _backfill(tmp_path / "store.db")
    assert code == cli.EXIT_FAILED
    assert capsys.readouterr().err.startswith(
        "failed: backfill needs the packages in contrib/uniswap_v3/requirements.txt ("
    )


def test_backfill_refuses_a_store_taken_over_another_twap_window(node, tmp_path, capsys):
    db = tmp_path / "store.db"
    _backfill(db, "--to", "2024-01-01")
    shorter = tmp_path / "shorter.yaml"
    shorter.write_text(
        _EXAMPLE.read_text(encoding="utf-8").replace(
            "twap_window_seconds: 1800", "twap_window_seconds: 600"
        ),
        encoding="utf-8",
    )
    code, _ = _run("backfill", "--config", str(shorter), "--db", str(db), "--from", "2024-01-01")
    assert code == cli.EXIT_FAILED
    assert "a TWAP over [1800] seconds, and the config asks for 600" in capsys.readouterr().err


def test_backfill_connects_with_the_variable_the_config_names(node, tmp_path):
    named = tmp_path / "named.yaml"
    named.write_text(
        _EXAMPLE.read_text(encoding="utf-8").replace("url_env: ETH_RPC_URL", "url_env: MY_NODE"),
        encoding="utf-8",
    )
    code, _ = _run(
        "backfill", "--config", str(named), "--db", str(tmp_path / "s.db"), "--from", "2024-01-01"
    )
    assert code == cli.EXIT_OK
    assert node.connections[0][1].url_env == "MY_NODE"


def test_a_missing_url_exits_1_without_printing_the_environment(monkeypatch, tmp_path, capsys):
    def connect(chain_id, *, settings=None, env=None):
        raise RpcConfigError("the environment variable ETH_RPC_URL is not set")

    monkeypatch.setattr(chain_rpc, "connect", connect)
    code, _ = _backfill(tmp_path / "store.db")
    assert code == cli.EXIT_FAILED
    assert capsys.readouterr().err == "failed: the environment variable ETH_RPC_URL is not set\n"


def test_an_interval_that_does_not_divide_a_day_exits_1(node, tmp_path, capsys):
    code, lines = _backfill(tmp_path / "store.db", "--interval-seconds", "25200")
    assert code == cli.EXIT_FAILED and lines == []
    assert "--interval-seconds: interval_seconds must be a positive integer that divides a day" in (
        capsys.readouterr().err
    )
    assert not (tmp_path / "store.db").exists()


def test_a_config_that_cannot_be_read_exits_1(tmp_path, capsys):
    code, _ = _run(
        "backfill", "--config", str(tmp_path / "none.yaml"), "--db", str(tmp_path / "s.db"),
        "--from", "2024-01-01",
    )  # fmt: skip
    assert code == cli.EXIT_FAILED
    assert "cannot be read" in capsys.readouterr().err


@pytest.mark.parametrize(
    "argv",
    [
        ["backfill", "--config", "c.yaml", "--db", "s.db"],
        ["backfill", "--config", "c.yaml", "--db", "s.db", "--from", "yesterday"],
        ["backfill", "--config", "c.yaml", "--db", "s.db", "--from", "2024-01-01T00:00:00.5"],
        ["status", "--config", "c.yaml", "--db", "s.db", "--interval-seconds", "0"],
        ["status", "--config", "c.yaml", "--db", "s.db", "--bars", "0"],
        ["status", "--config", "c.yaml", "--db", "s.db", "--interval-seconds", "-60"],
        ["trade"],
        [],
    ],
)
def test_a_command_line_that_cannot_be_read_exits_2(argv, capsys):
    with pytest.raises(SystemExit) as exit_info:
        cli.main(argv)
    assert exit_info.value.code == 2
    capsys.readouterr()


def test_a_time_with_an_offset_is_read_as_that_instant(node, tmp_path):
    code, lines = _backfill(tmp_path / "store.db", start="2024-01-01T08:00:00+08:00")
    assert code == cli.EXIT_OK
    assert lines[0].startswith("3 bar(s) from 2024-01-01T00:00:00Z")


# --- status ----------------------------------------------------------------


def test_status_prints_the_extent_of_each_pool_and_the_latest_bars(node, tmp_path):
    db = tmp_path / "store.db"
    _backfill(db)
    code, lines = _status(db, "--bars", "2")
    assert code == cli.EXIT_OK
    assert lines[:3] == [
        "chain 1, 86400-second bars, prices in USDC",
        "USDC/WETH-500: 3 reading(s), 2024-01-01T00:00:00Z to 2024-01-03T00:00:00Z",
        "WBTC/WETH-500: 3 reading(s), 2024-01-01T00:00:00Z to 2024-01-03T00:00:00Z",
    ]
    assert len(lines) == 5
    assert lines[3].startswith(f"2024-01-02T00:00:00Z  block {block_at(FIRST_DAY + DAY) - 1}  ")
    assert lines[4].startswith("2024-01-03T00:00:00Z  ")
    for line in lines[3:]:
        assert "  WBTC 29845.38  WETH 2000.04  " in line
        assert line.endswith("final  ok  flags: none")
    # Status reads the store alone.
    assert len(node.connections) == 1


def test_status_shows_the_flags_and_the_verdict_of_a_bar(node, tmp_path):
    db = tmp_path / "store.db"
    close = block_at(FIRST_DAY + 2 * DAY) - 1
    node.twap_tick[(_WBTC_WETH.address.lower(), close)] = _BTC_TICK + 600
    node.head = block_at(FIRST_DAY + 2 * DAY) + 10
    # The second day is left out, so the third follows a gap.
    _backfill(db, "--to", "2024-01-01")
    _backfill(db, start="2024-01-03")

    code, lines = _status(db)

    assert code == cli.EXIT_OK
    assert lines[-1].endswith(
        "pending  suspect  flags: USDC/WETH-500:gap, WBTC/WETH-500:gap, "
        "WBTC/WETH-500:twap_deviation"
    )
    assert lines[-2].endswith("final  ok  flags: none")


def test_status_says_when_a_boundary_lacks_a_pool(node, tmp_path):
    db = tmp_path / "store.db"
    _backfill(db)
    connection = sqlite3.connect(db)
    connection.execute(
        "DELETE FROM bars WHERE pool = ? AND time = ?", (_WBTC_WETH.address, FIRST_DAY + DAY)
    )
    connection.commit()
    connection.close()
    code, lines = _status(db)
    assert code == cli.EXIT_OK
    assert lines[4] == "2024-01-02T00:00:00Z  incomplete: a configured pool has no reading"


def test_a_store_locked_by_another_writer_exits_3_with_one_line(
    node, tmp_path, capsys, monkeypatch
):
    db = tmp_path / "store.db"
    open_store(db).close()
    connect = sqlite3.connect
    # Without the five seconds a connection waits for a lock by default.
    monkeypatch.setattr(
        sqlite3, "connect", lambda *args, **kwargs: connect(*args, **{**kwargs, "timeout": 0})
    )
    other = connect(db, isolation_level=None)
    other.execute("BEGIN EXCLUSIVE")
    try:
        code, _ = _backfill(db)
    finally:
        other.execute("ROLLBACK")
        other.close()
    # The lock is let go of, so a later run may get through: try again.
    assert code == cli.EXIT_RETRY
    error = capsys.readouterr().err
    assert error.startswith("try again later: the store") and "locked" in error
    assert "Traceback" not in error


def test_status_refuses_readings_taken_over_another_twap_window(node, tmp_path, capsys):
    db = tmp_path / "store.db"
    _backfill(db)
    shorter = tmp_path / "shorter.yaml"
    shorter.write_text(
        _EXAMPLE.read_text(encoding="utf-8").replace(
            "twap_window_seconds: 1800", "twap_window_seconds: 600"
        ),
        encoding="utf-8",
    )
    code, _ = _run("status", "--config", str(shorter), "--db", str(db))
    assert code == cli.EXIT_FAILED
    assert "has a TWAP over 1800 seconds, and the settings ask for 600" in capsys.readouterr().err


def test_status_of_an_empty_store_and_of_no_store(tmp_path, capsys):
    db = tmp_path / "store.db"
    open_store(db).close()
    code, lines = _status(db)
    assert code == cli.EXIT_OK
    assert lines == [
        "chain 1, 86400-second bars, prices in USDC",
        "USDC/WETH-500: 0 reading(s)",
        "WBTC/WETH-500: 0 reading(s)",
    ]

    code, lines = _status(tmp_path / "nowhere.db")
    assert code == cli.EXIT_FAILED and lines == []
    assert "there is no store at" in capsys.readouterr().err
    assert not (tmp_path / "nowhere.db").exists()


# --- verdicts ----------------------------------------------------------------


def _reading_verdicts(tmp_path: Path) -> Path:
    """The shipped example, reading the verdicts of the tests' source."""
    path = tmp_path / "verdicts.yaml"
    path.write_text(
        _EXAMPLE.read_text(encoding="utf-8") + "\nverdicts:\n  source: test-judge\n",
        encoding="utf-8",
    )
    return path


def _backtest_reading(config: Path, db: Path, run_id: str) -> int:
    code, _ = _run(
        "backtest", "--config", str(config), "--db", str(db), "--run-id", run_id,
        "--from", "2024-01-01", "--balance", "USDC=10000", "--gas-eth", "1",
    )  # fmt: skip
    return code


def test_status_counts_the_latest_boundaries_with_a_verdict_for_every_token(node, tmp_path):
    db = tmp_path / "store.db"
    _backfill(db)
    with open_store(db) as store:
        for said in (record("WETH", 0), record("WBTC", 0, Rating.HOLD), record("WETH", 2)):
            store.insert_verdict(said)
    reading = _reading_verdicts(tmp_path)

    code, lines = _run("status", "--config", str(reading), "--db", str(db), "--bars", "3")

    assert code == cli.EXIT_OK
    assert lines[-1] == (
        "verdicts from test-judge: 1 of the latest 3 bar(s) have one for every token "
        "(WBTC 1, WETH 2)"
    )
    code, lines = _run("status", "--config", str(reading), "--db", str(db), "--bars", "1")
    assert code == cli.EXIT_OK
    assert lines[-1] == (
        "verdicts from test-judge: 0 of the latest 1 bar(s) have one for every token "
        "(WBTC 0, WETH 1)"
    )
    # A boundary that lacks a pool's reading makes no bar, and is not counted.
    with open_store(db) as store:
        put_day(store, 3, pools=(_USDC_WETH,))
        store.insert_verdict(record("WETH", 3))
    code, lines = _run("status", "--config", str(reading), "--db", str(db), "--bars", "4")
    assert code == cli.EXIT_OK
    assert any(line.endswith("incomplete: a configured pool has no reading") for line in lines)
    assert lines[-1] == (
        "verdicts from test-judge: 1 of the latest 3 bar(s) have one for every token "
        "(WBTC 1, WETH 2)"
    )
    # The shipped example reads no verdicts, and says nothing of them.
    code, plain = _status(db)
    assert code == cli.EXIT_OK and not any("verdicts" in line for line in plain)


def test_status_of_a_run_says_what_each_decision_saw_of_each_tokens_verdict(node, tmp_path):
    db = tmp_path / "store.db"
    _backfill(db)
    with open_store(db) as store:
        for said in (record("WETH", 0), record("WBTC", 0), record("WBTC", 2)):
            store.insert_verdict(said)
    reading = _reading_verdicts(tmp_path)
    assert _backtest_reading(reading, db, "reads") == cli.EXIT_OK
    assert _backtest_reading(_EXAMPLE, db, "plain") == cli.EXIT_OK

    code, lines = _run(
        "status", "--config", str(reading), "--db", str(db), "--run-id", "reads", "--bars", "3"
    )

    assert code == cli.EXIT_OK
    decided = [line for line in lines if "  decided 20" in line]
    assert [line.split("  verdicts: ")[1] for line in decided] == [
        "WBTC=Buy, WETH=Buy",
        "WBTC=none, WETH=none",
        "WBTC=Buy, WETH=none",
    ]
    # The store now differs from what the decisions saw: a verdict deleted, and one added since.
    connection = sqlite3.connect(db)
    with connection:
        connection.execute(
            "DELETE FROM verdicts WHERE symbol = 'WBTC' AND time = ?", (FIRST_DAY + 2 * DAY,)
        )
    connection.close()
    with open_store(db) as store:
        store.insert_verdict(record("WETH", 1, Rating.SELL))
    code, lines = _run(
        "status", "--config", str(reading), "--db", str(db), "--run-id", "reads", "--bars", "2"
    )
    assert code == cli.EXIT_OK
    decided = [line for line in lines if "  decided 20" in line]
    assert [line.split("  verdicts: ")[1] for line in decided] == [
        "WBTC=none, WETH=changed",
        "WBTC=changed, WETH=none",
    ]
    # A run that reads no verdicts says nothing of them.
    code, plain = _status(db, "--run-id", "plain", "--bars", "3")
    assert code == cli.EXIT_OK
    assert len([line for line in plain if "  decided 20" in line]) == 3
    assert not any("verdicts" in line for line in plain)


def test_a_replay_warns_of_decided_bars_whose_verdicts_changed_since(node, tmp_path, capsys):
    db = tmp_path / "store.db"
    _backfill(db)
    reading = _reading_verdicts(tmp_path)
    assert _backtest_reading(reading, db, "reads") == cli.EXIT_OK
    with open_store(db) as store:
        store.insert_verdict(record("WETH", 1))
    capsys.readouterr()

    assert _backtest_reading(reading, db, "reads") == cli.EXIT_OK

    err = capsys.readouterr().err
    assert (
        "warning: 1 bar(s) decided earlier now have other verdicts in the store than their "
        "decisions saw, from 2024-01-02T00:00:00Z to 2024-01-02T00:00:00Z; their decisions stand"
    ) in err
    with open_store(db) as store:
        assert store.decision("reads", FIRST_DAY + DAY).verdicts == {}
