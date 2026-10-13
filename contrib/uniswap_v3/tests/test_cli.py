"""The command line: backfill and status against a scripted chain, and the exit codes."""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from contrib.uniswap_v3 import cli, status as status_module
from contrib.uniswap_v3.chain import rpc as chain_rpc
from contrib.uniswap_v3.chain.errors import RpcConfigError
from contrib.uniswap_v3.config import load_config
from contrib.uniswap_v3.constants import ETHEREUM_MAINNET, POOLS
from contrib.uniswap_v3.domain.metrics import MetricsError
from contrib.uniswap_v3.domain.records import Decision, Outcome, SkipCode
from contrib.uniswap_v3.domain.text import one_ascii_line
from contrib.uniswap_v3.domain.verdicts import Rating
from contrib.uniswap_v3.status import (
    BarStatus,
    Clock,
    CostsStatus,
    Coverage,
    CurveStatus,
    DecisionStatus,
    Holdings,
    Metrics,
    PoolExtent,
    RunStatus,
    ValuationPoint,
    run_header,
    verdicts_seen,
)
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


def test_a_node_that_fails_a_read_exits_3_and_a_wrong_answer_exits_1(node, tmp_path, capsys):
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
    assert one_ascii_line(noisy) == "vieux" + chr(92) + "xe9 forged line?[2J"


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


# --- status --json ---------------------------------------------------------


def _nodes(value, prefix: str = ""):
    """Every ``(path, value)`` in a document, list items under ``path[]``."""
    if prefix:
        yield prefix, value
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _nodes(child, f"{prefix}.{key}" if prefix else key)
    elif isinstance(value, list):
        for child in value:
            yield from _nodes(child, f"{prefix}[]")


def _paths(document) -> set[str]:
    return {path for path, _ in _nodes(document) if not path.endswith("[]")}


_CURVE = ("start", "end", "total_return", "max_drawdown")
_OUTCOMES = ("skipped_suspect", "hold", "no_trade", "filled", "rejected", "partial")
# The document's shape at status.SCHEMA_VERSION 1. A path added, removed or
# moved changes this set; change it together with the version.
_SCHEMA_1_PATHS = {
    "schema_version",
    "source",
    "generated_at",
    "store",
    "store.chain_id",
    "store.interval_seconds",
    "store.quote",
    "store.pools",
    "store.pools[].pool",
    "store.pools[].readings",
    "store.pools[].first",
    "store.pools[].last",
    "store.latest_bars",
    "store.latest_bars[].time",
    "store.latest_bars[].complete",
    "store.latest_bars[].close_block",
    "store.latest_bars[].prices",
    "store.latest_bars[].prices.WBTC",
    "store.latest_bars[].prices.WETH",
    "store.latest_bars[].finality",
    "store.latest_bars[].suspect",
    "store.latest_bars[].flags",
    "store.coverage",
    "store.coverage[].what",
    "store.coverage[].source",
    "store.coverage[].complete",
    "store.coverage[].of",
    "store.coverage[].per_token",
    "store.coverage[].per_token.WBTC",
    "store.coverage[].per_token.WETH",
    "run",
    "run.run_id",
    "run.mode",
    "run.fills",
    "run.strategy",
    "run.quote",
    "run.created_at",
    "run.fork_block",
    "run.opening",
    "run.opening.balances",
    "run.opening.balances.USDC",
    "run.opening.balances.WBTC",
    "run.opening.balances.WETH",
    "run.opening.gas_eth",
    "run.decided",
    "run.first_decided",
    "run.last_decided",
    "run.outcomes",
    *(f"run.outcomes.{outcome}" for outcome in _OUTCOMES),
    "run.reason_codes",
    *(f"run.reason_codes.{outcome}" for outcome in ("skipped_suspect", "rejected", "partial")),
    "run.unsettled",
    "run.clock",
    "run.holdings",
    "run.holdings.balances",
    "run.holdings.balances.USDC",
    "run.holdings.balances.WBTC",
    "run.holdings.balances.WETH",
    "run.holdings.gas_eth",
    "run.holdings_at",
    "run.value",
    "run.metrics",
    "run.metrics.bars",
    "run.metrics.measured_from",
    "run.metrics.measured_to",
    "run.metrics.suspect_left_out",
    "run.metrics.strategy",
    *(f"run.metrics.strategy.{c}" for c in _CURVE),
    "run.metrics.opening_held",
    *(f"run.metrics.opening_held.{c}" for c in _CURVE),
    "run.metrics.all_quote",
    *(f"run.metrics.all_quote.{c}" for c in _CURVE),
    "run.metrics.rebalances",
    "run.metrics.swaps",
    "run.metrics.traded_value",
    "run.metrics.turnover",
    "run.metrics.costs",
    *(f"run.metrics.costs.{c}" for c in ("pool_fees", "slippage", "gas", "gas_eth", "total")),
    "run.metrics_error",
    "run.verdicts_seen",
    "run.verdicts_seen.source",
    "run.verdicts_seen.every",
    "run.verdicts_seen.some",
    "run.verdicts_seen.none",
    "run.recent_decisions",
    "run.recent_decisions[].time",
    "run.recent_decisions[].outcome",
    "run.recent_decisions[].decided_at",
    "run.recent_decisions[].delay_seconds",
    "run.recent_decisions[].reason_code",
    "run.recent_decisions[].reason",
    "run.recent_decisions[].verdicts",
    "run.recent_decisions[].verdicts.WBTC",
    "run.recent_decisions[].verdicts.WETH",
    "run.recent_decisions[].targets_now",
    "run.recent_valuations",
    "run.recent_valuations[].time",
    "run.recent_valuations[].total_value",
}


def _status_document(db: Path, *extra: str):
    code, lines = _status(db, "--json", *extra)
    assert code == cli.EXIT_OK
    (text,) = lines
    assert text.isascii()
    return json.loads(text)


def test_status_json_is_the_lines_model_as_one_document(node, tmp_path):
    db = tmp_path / "store.db"
    _backfill(db)
    with open_store(db) as store:
        for said in (record("WETH", 0), record("WBTC", 0), record("WBTC", 2), record("WETH", 2)):
            store.insert_verdict(said)
    reading = _reading_verdicts(tmp_path)
    assert _backtest_reading(reading, db, "reads") == cli.EXIT_OK

    argv = ("status", "--config", str(reading), "--db", str(db), "--run-id", "reads", "--bars", "3")
    code, lines = _run(*argv, "--json")
    assert code == cli.EXIT_OK
    document = json.loads(lines[0])
    assert list(document)[:3] == ["schema_version", "source", "generated_at"]
    assert document["schema_version"] == status_module.SCHEMA_VERSION == 1
    assert document["source"] == "uniswap_v3"
    assert document["generated_at"] == "2024-01-03T00:10:00Z"  # the clock _run reads
    assert _paths(document) == _SCHEMA_1_PATHS
    store = document["store"]
    assert [p["pool"] for p in store["pools"]] == ["USDC/WETH-500", "WBTC/WETH-500"]
    assert store["pools"][0]["readings"] == 3
    assert store["pools"][0]["first"] == "2024-01-01T00:00:00Z"
    assert all(bar["complete"] for bar in store["latest_bars"])
    assert store["coverage"][0]["what"] == "verdicts"
    run = document["run"]
    assert (run["run_id"], run["mode"], run["fills"], run["decided"]) == (
        "reads",
        "backtest",
        "model",
        3,
    )
    assert run["clock"] is None  # a backtest is not read against the clock
    assert run["metrics"]["bars"] == 3 and run["metrics_error"] is None
    assert run["reason_codes"] == {"skipped_suspect": {}, "rejected": {}, "partial": {}}
    assert run["unsettled"] == 0  # the backfill confirmed every reading
    # Days 0 and 2 saw both tokens' verdicts, day 1 neither: no count equals another.
    assert run["verdicts_seen"] == {
        "source": "test-judge",
        "every": 2,
        "some": 0,
        "none": 1,
    }
    assert [d["verdicts"] for d in run["recent_decisions"]] == [
        {"WBTC": "Buy", "WETH": "Buy"},
        {"WBTC": "none", "WETH": "none"},
        {"WBTC": "Buy", "WETH": "Buy"},
    ]
    assert all(d["targets_now"] is None for d in run["recent_decisions"])
    assert [v["time"] for v in run["recent_valuations"]] == [
        d["time"] for d in run["recent_decisions"]
    ]
    assert run["recent_valuations"][-1]["total_value"] == run["value"]
    # The same reading, as lines, is what status has always printed.
    _, as_lines = _run(*argv)
    assert as_lines[0] == "chain 1, 86400-second bars, prices in USDC"
    assert as_lines[-4] == "latest 3 decision(s):"


def test_status_json_without_a_run_carries_the_store_alone(node, tmp_path):
    db = tmp_path / "store.db"
    _backfill(db)
    document = _status_document(db, "--bars", "2")
    assert document["run"] is None
    assert len(document["store"]["latest_bars"]) == 2
    assert document["store"]["coverage"] == []


def test_status_json_names_no_secret_field_and_carries_no_float(node, tmp_path):
    db = tmp_path / "store.db"
    _backfill(db)
    assert _backtest_reading(_EXAMPLE, db, "plain") == cli.EXIT_OK
    document = _status_document(db, "--run-id", "plain")
    secret = re.compile(r"key|secret|password|mnemonic|address", re.IGNORECASE)
    assert not [path for path in _paths(document) if secret.search(path.rsplit(".", 1)[-1])]
    assert not [path for path, value in _nodes(document) if isinstance(value, float)]
    assert Decimal(document["run"]["value"]) > 0
    assert Decimal(document["run"]["metrics"]["strategy"]["start"]) == 10000


def test_the_clock_states_of_a_paper_run():
    config = load_config(_EXAMPLE)
    last = FIRST_DAY + DAY  # the run decided 2024-01-02
    assert status_module._clock(config, last, last + 600).state == "up_to_date"
    behind = status_module._clock(config, last, last + 2 * DAY + 5)
    assert (behind.state, behind.undecided, behind.latest_boundary) == (
        "behind",
        2,
        "2024-01-04T00:00:00Z",
    )
    assert status_module._clock(config, last, last - 5).state == "ahead"


def test_a_skipped_decisions_reason_code_reason_and_delay_reach_the_model():
    config = load_config(_EXAMPLE)
    wallet = "0x" + "12" * 20
    decision = Decision(
        time=FIRST_DAY,
        outcome=Outcome.SKIPPED_SUSPECT,
        close_block=1,
        reason=f"a reading reorged (seen by {wallet})",
        reason_code=SkipCode.REORGED,
        decided_at=FIRST_DAY + 90,
    )
    # The run reads neither verdicts nor targets, so no store is consulted.
    found = status_module._decision(None, config, decision)
    assert (found.outcome, found.reason_code, found.delay_seconds) == (
        "skipped_suspect",
        "reorged",
        90,
    )
    assert found.reason == "a reading reorged (seen by 0x1212...1212)"
    assert found.verdicts is None and found.targets_now is None
    line = status_module._decision_line(found, paper=True)
    assert line.endswith(
        "decided 2024-01-01T00:01:30Z, 00:01:30 after the boundary "
        "(a reading reorged (seen by 0x1212...1212))"
    )


def test_a_run_whose_return_cannot_be_measured_says_why_in_both_forms(node, tmp_path, monkeypatch):
    db = tmp_path / "store.db"
    _backfill(db)
    assert _backtest_reading(_EXAMPLE, db, "plain") == cli.EXIT_OK

    wallet = "0x" + "12" * 20

    def _unmeasurable(*args, **kwargs):
        raise MetricsError(f"no price for {wallet}")

    monkeypatch.setattr(status_module, "measured", _unmeasurable)
    document = _status_document(db, "--run-id", "plain")
    assert document["run"]["metrics"] is None
    # The error is free text, so it is masked like a reason, in the lines too.
    assert document["run"]["metrics_error"] == "no price for 0x1212...1212"
    code, lines = _status(db, "--run-id", "plain")
    assert code == cli.EXIT_OK
    assert any(line.endswith("; return not measured (no price for 0x1212...1212)") for line in lines)


def test_a_decisions_reason_is_masked_and_cut_in_the_model():
    key, wallet = "0x" + "ab" * 32, "0x" + "Cd" * 20
    assert status_module._text(f"refused by {wallet} with {key}") == (
        "refused by 0xCdCd...CdCd with 0xabab...abab"
    )
    # Masked before it is cut: a key straddling the cut leaves no hex behind.
    assert status_module._text("a" * 990 + key) == "a" * 990 + "0xabab...."
    straddling = status_module._text("a" * 940 + key + "z" * 100)
    assert len(straddling) == status_module.TEXT_LIMIT
    assert "0xabab...abab" in straddling and "ababab" not in straddling
    assert status_module._text(None) is None


def test_verdicts_seen_leaves_out_the_bars_skipped_as_suspect():
    digest = "a" * 64

    def decided(day: int, **verdicts: str) -> Decision:
        return Decision(
            time=FIRST_DAY + day * DAY, outcome=Outcome.HOLD, close_block=1, verdicts=verdicts
        )

    skipped = Decision(
        time=FIRST_DAY + 3 * DAY,
        outcome=Outcome.SKIPPED_SUSPECT,
        close_block=1,
        reason="reorged",
        reason_code=SkipCode.REORGED,
        verdicts={},
    )
    decisions = [
        decided(0, WBTC=digest, WETH=digest),
        decided(1, WETH=digest),
        decided(2, WBTC=digest, WETH=digest),
        skipped,
    ]
    seen = verdicts_seen("test-judge", 2, decisions)
    assert (seen.source, seen.every, seen.some, seen.none) == ("test-judge", 2, 1, 0)


def test_read_status_refuses_a_tail_of_no_bars(node, tmp_path):
    db = tmp_path / "store.db"
    _backfill(db)
    with (
        open_store(db, create=False) as store,
        store.reading(),
        pytest.raises(ValueError, match="bars"),
    ):
        status_module.read_status(
            store, load_config(_EXAMPLE), bars=0, run_id=None, now=lambda: 0.0
        )


def test_the_run_header_names_the_fork_block():
    assert run_header(
        run_id="f", mode="fork", fork_block=19_000_000, fills="chain", strategy="s", quote="USDC"
    ) == (
        "run f: fork (the fork was at block 19000000 when it started), fills from the chain, "
        "s, values in USDC"
    )


# --- the model refuses what no store produces ---------------------------------

_T0 = "2024-01-01T00:00:00Z"
_NO_CODES = {"skipped_suspect": {}, "rejected": {}, "partial": {}}
_HELD = Holdings({"USDC": Decimal("9990")}, Decimal("0.99"))


def _curve(end: str, fall: str = "0") -> CurveStatus:
    start = Decimal("10000")
    return CurveStatus(start, Decimal(end), Decimal(end) / start - 1, Decimal(fall))


def _metrics() -> Metrics:
    return Metrics(
        bars=1,
        measured_from=_T0,
        measured_to=_T0,
        suspect_left_out=0,
        strategy=_curve("10250", "0.01"),
        opening_held=_curve("10000"),
        all_quote=_curve("10000"),
        rebalances=1,
        swaps=2,
        traded_value=Decimal("100"),
        turnover=Decimal("0.01"),
        costs=CostsStatus(Decimal("1"), Decimal("2"), Decimal("3"), Decimal("0.001"), Decimal("6")),
    )


def _decided_run(**changes) -> RunStatus:
    """A backtest that decided one bar and could not be measured, as a valid model."""
    fields = {
        "run_id": "r",
        "mode": "backtest",
        "fills": "model",
        "strategy": "fixed_weights",
        "quote": "USDC",
        "created_at": _T0,
        "fork_block": None,
        "opening": Holdings({"USDC": Decimal("10000")}, Decimal("1")),
        "decided": 1,
        "first_decided": _T0,
        "last_decided": _T0,
        "outcomes": dict.fromkeys(_OUTCOMES, 0) | {"filled": 1},
        "reason_codes": _NO_CODES,
        "unsettled": 0,
        "clock": None,
        "holdings": _HELD,
        "holdings_at": _T0,
        "value": Decimal("9990"),
        "metrics": None,
        "metrics_error": "no bar could be measured",
        "verdicts_seen": None,
        "recent_decisions": (DecisionStatus(_T0, "filled", _T0, 0, None, None, None, None),),
        "recent_valuations": (ValuationPoint(_T0, Decimal("9990")),),
    }
    return RunStatus(**{**fields, **changes})


def _clock(state: str, undecided: int) -> Clock:
    return Clock(state, _T0, _T0, _T0, undecided)


@pytest.mark.parametrize(
    "changes",
    [
        {"mode": "sim"},
        {"outcomes": dict.fromkeys(_OUTCOMES, 0) | {"filled": 2}},
        {"reason_codes": {"rejected": {}}},
        {"unsettled": 2},
        {"holdings": None},
        {"holdings_at": None},
        {"value": None},
        {"first_decided": None},
        {"metrics_error": None},
        {"metrics": _metrics()},
        {"clock": _clock("up_to_date", 0)},
        {"mode": "paper"},
        {"recent_decisions": ()},
        {"recent_valuations": ()},
        {"recent_valuations": (ValuationPoint(_T0, Decimal(1)),) * 2},
        {"decided": 0},
    ],
)
def test_a_decided_run_refuses_a_half_filled_model(changes):
    with pytest.raises(ValueError):
        _decided_run(**changes)


def test_a_run_that_decided_nothing_carries_nothing_else():
    empty = _decided_run(
        decided=0,
        first_decided=None,
        last_decided=None,
        outcomes=dict.fromkeys(_OUTCOMES, 0),
        holdings=None,
        holdings_at=None,
        value=None,
        metrics_error=None,
        recent_decisions=(),
        recent_valuations=(),
    )
    assert empty.decided == 0
    assert status_module._run_lines(empty)[-1].startswith("no bar decided yet")
    with pytest.raises(ValueError):
        _decided_run(**{**empty.__dict__, "holdings": _HELD})


def test_a_paper_run_carries_its_clock_and_a_measured_run_its_return():
    paper = _decided_run(mode="paper", clock=_clock("behind", 2))
    assert status_module._run_lines(paper)[2].startswith("behind: 2 boundary(ies)")
    measured = _decided_run(metrics=_metrics(), metrics_error=None)
    assert status_module._value_line(measured) == (
        "value 9990.00 USDC; return +2.50% from 2024-01-01T00:00:00Z to "
        "2024-01-01T00:00:00Z, after gas, as report measures it"
    )


@pytest.mark.parametrize(
    "build",
    [
        lambda: PoolExtent("p", 1, None, None),
        lambda: PoolExtent("p", 0, _T0, _T0),
        lambda: PoolExtent("p", 2, _T0, None),
        lambda: BarStatus(_T0, True, None, {"WETH": Decimal(1)}, "final", False, ()),
        lambda: BarStatus(_T0, True, 1, {"WETH": Decimal(1)}, "settled", False, ()),
        lambda: BarStatus(_T0, False, 1, {}, None, None, ()),
        lambda: BarStatus(_T0, False, None, {"WETH": Decimal(1)}, None, None, ()),
        lambda: Coverage("verdicts", "s", 3, 2, {}),
        lambda: Coverage("verdicts", "s", 1, 2, {"WETH": 0}),
        lambda: Coverage("verdicts", "s", 1, 2, {"WETH": 3}),
        lambda: _clock("behind", 0),
        lambda: _clock("up_to_date", 1),
        lambda: _clock("ahead", -1),
        lambda: _clock("late", 0),
        lambda: DecisionStatus(_T0, "filled", _T0, None, None, None, None, None),
        lambda: DecisionStatus(_T0, "filled", None, 5, None, None, None, None),
        lambda: DecisionStatus(_T0, "won", None, None, None, None, None, None),
    ],
)
def test_the_models_refuse_impossible_pairings(build):
    with pytest.raises(ValueError):
        build()


def test_the_clock_line_knows_every_state():
    assert status_module._clock_line(_clock("ahead", 0)).startswith("the clock is behind the run")
    assert status_module._clock_line(_clock("up_to_date", 0)).startswith("up to date")
    assert status_module._clock_line(_clock("behind", 3)).startswith("behind: 3 boundary(ies)")
    unknown = Clock.__new__(Clock)
    object.__setattr__(unknown, "state", "late")
    with pytest.raises(ValueError, match="late"):
        status_module._clock_line(unknown)
