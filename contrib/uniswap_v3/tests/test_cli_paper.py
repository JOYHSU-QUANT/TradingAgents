"""The command line's ``paper``, and ``backtest --fills quoter``, against a scripted chain."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

from contrib.uniswap_v3 import cli
from contrib.uniswap_v3.chain import rpc as chain_rpc
from contrib.uniswap_v3.constants import ETHEREUM_MAINNET, POOLS
from contrib.uniswap_v3.tests.fakes.node import (
    BTC_TICK,
    DAY,
    DEFAULT_TICK,
    FIRST_DAY,
    UP_HALF_TICK,
    FakeNode,
    block_at,
    sqrt_price_at,
)
from contrib.uniswap_v3.tests.fakes.rpc import block_hash, rpc_over

_EXAMPLE = Path(__file__).resolve().parents[1] / "configs" / "uniswap_v3.example.yaml"
_USDC_WETH = POOLS[ETHEREUM_MAINNET]["USDC/WETH-500"].address.lower()
_WBTC_WETH = POOLS[ETHEREUM_MAINNET]["WBTC/WETH-500"].address.lower()
_OPENING = ("--balance", "USDC=10000", "--gas-eth", "1")
_TEN_PAST = FIRST_DAY + 600


@pytest.fixture
def node(monkeypatch):
    """The chain the commands connect to, its WBTC/WETH pool near 15 WETH per WBTC."""
    chain = FakeNode()
    chain.pool_slot0[_WBTC_WETH] = (sqrt_price_at(BTC_TICK), BTC_TICK)
    chain.pool_twap_tick[_WBTC_WETH] = BTC_TICK

    def connect(chain_id, *, settings=None, env=None):
        return rpc_over(chain.provider, chain_id=chain_id, attempts=1)[0]

    monkeypatch.setattr(chain_rpc, "connect", connect)
    return chain


def _run(node: FakeNode, *argv: str, now: int = _TEN_PAST) -> tuple[int, list[str]]:
    """Run a command at ``now``, the chain's head being where the clock is."""
    node.head = block_at(now)
    lines: list[str] = []
    code = cli.main(list(argv), out=lines.append, now=lambda: float(now))
    return code, lines


def _paper(node: FakeNode, db: Path, *extra: str, run_id: str = "p", **kwargs) -> tuple[int, list[str]]:
    return _run(
        node, "paper", "--config", str(_EXAMPLE), "--db", str(db), "--run-id", run_id, *extra,
        **kwargs,
    )  # fmt: skip


def _tamper(db: Path, *statements: str) -> None:
    """Change the database behind the store's back."""
    connection = sqlite3.connect(db)
    try:
        for statement in statements:
            connection.execute(statement)
        connection.commit()
    finally:
        connection.close()


def _count(db: Path, table: str) -> int:
    connection = sqlite3.connect(db)
    try:
        return connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        connection.close()


def test_paper_makes_the_store_reads_the_latest_bar_and_decides_it(node, tmp_path, capsys):
    db = tmp_path / "store.db"
    code, lines = _paper(node, db, *_OPENING)
    assert code == cli.EXIT_OK
    assert lines == [
        "read 1 bar(s); 0 already stored, 0 without an answer",
        "run p: 1 boundary(ies) from 2024-01-01T00:00:00Z to 2024-01-01T00:00:00Z: 1 decided, "
        "0 already decided, 0 without a bar",
        "filled 1, hold 0, no_trade 0, rejected 0, skipped_suspect 0",
        "the bar at 2024-01-01T00:00:00Z is decided: filled",
    ]
    # A visit made on time decides on a reading that is not final: that is not worth a warning.
    assert capsys.readouterr().err == ""
    assert (_count(db, "decisions"), _count(db, "fills")) == (1, 2)


def test_paper_run_twice_says_the_bar_was_already_decided_and_writes_no_second_row(
    node, tmp_path, capsys
):
    db = tmp_path / "store.db"
    _paper(node, db, *_OPENING)
    code, lines = _paper(node, db, now=FIRST_DAY + 3_600)
    assert code == cli.EXIT_OK
    assert lines == [
        "read 0 bar(s); 1 already stored, 0 without an answer",
        "checked pending readings against the final chain: 2 final, 0 reorged",
        "run p: 1 boundary(ies) from 2024-01-01T00:00:00Z to 2024-01-01T00:00:00Z: 0 decided, "
        "1 already decided, 0 without a bar",
        "filled 1, hold 0, no_trade 0, rejected 0, skipped_suspect 0",
        "the bar at 2024-01-01T00:00:00Z was already decided: filled",
    ]
    assert capsys.readouterr().err == ""
    assert (_count(db, "decisions"), _count(db, "fills"), _count(db, "valuations")) == (1, 2, 1)


def test_paper_before_the_bars_fill_block_exits_3_and_decides_nothing(node, tmp_path, capsys):
    db = tmp_path / "store.db"
    code, lines = _paper(node, db, *_OPENING, now=FIRST_DAY + 240)
    assert code == cli.EXIT_RETRY and lines == []
    err = capsys.readouterr().err
    assert err.startswith("try again later: the bar at ") and "fills at block" in err
    assert (_count(db, "runs"), _count(db, "decisions")) == (0, 0)

    code, lines = _paper(node, db, *_OPENING)
    assert code == cli.EXIT_OK
    assert lines[-1] == "the bar at 2024-01-01T00:00:00Z is decided: filled"


def test_paper_whose_node_is_behind_the_boundary_exits_3(node, tmp_path, capsys):
    lines: list[str] = []
    node.head = block_at(FIRST_DAY) - 10
    code = cli.main(
        ["paper", "--config", str(_EXAMPLE), "--db", str(tmp_path / "s.db"), "--run-id", "p", *_OPENING],
        out=lines.append,
        now=lambda: float(_TEN_PAST),
    )
    assert code == cli.EXIT_RETRY and lines == []
    assert "has not reached the bar boundary" in capsys.readouterr().err


def test_paper_for_a_new_run_without_opening_balances_exits_1_and_reads_no_chain(
    node, tmp_path, capsys
):
    code, lines = _paper(node, tmp_path / "store.db")
    assert code == cli.EXIT_FAILED and lines == []
    assert "there is no run 'p', and a new run needs opening balances" in capsys.readouterr().err
    assert node.provider.requests == []


def test_paper_warns_and_exits_0_when_the_chain_has_no_answer_at_the_boundary(
    node, tmp_path, capsys
):
    node.reverts.add((_USDC_WETH, block_at(FIRST_DAY) - 1))
    db = tmp_path / "store.db"
    code, lines = _paper(node, db, *_OPENING)
    assert code == cli.EXIT_OK
    assert lines == ["read 0 bar(s); 0 already stored, 1 without an answer"]
    err = capsys.readouterr().err
    assert "warning: the chain had no answer at the boundary 2024-01-01T00:00:00Z" in err
    assert err.count("warning:") == 1
    assert _count(db, "runs") == 0


def test_paper_prints_why_a_rebalance_was_rejected_warns_and_exits_0(node, tmp_path, capsys):
    node.quote_bps = 9_900
    code, lines = _paper(node, tmp_path / "store.db", *_OPENING)
    assert code == cli.EXIT_OK
    assert (
        "warning: 1 rebalance(s) were rejected because a swap was refused, the first at "
        "2024-01-01T00:00:00Z" in capsys.readouterr().err
    )
    assert lines[-1].startswith(
        "the bar at 2024-01-01T00:00:00Z is decided: rejected (leg 0 (USDC to WETH) was refused: "
        "the quote of "
    )


def test_report_names_a_paper_runs_mode_and_fills(node, tmp_path):
    db = tmp_path / "store.db"
    _paper(node, db, *_OPENING)
    _paper(node, db, now=_TEN_PAST + DAY)
    code, lines = cli_report(db, "p")
    assert code == cli.EXIT_OK
    assert lines[0] == "run p: paper, fills from the quoter, fixed_weights, values in USDC"
    assert lines[1] == "2 bar(s) decided from 2024-01-01T00:00:00Z to 2024-01-02T00:00:00Z"


def _settled(lines: list[str]) -> list[str]:
    return [line for line in lines if "not final yet" not in line]


def cli_report(db: Path, run_id: str) -> tuple[int, list[str]]:
    lines: list[str] = []
    return cli.main(["report", "--db", str(db), "--run-id", run_id], out=lines.append), lines


def test_a_quoted_backtest_repeats_the_paper_run_and_keeps_to_its_fills(node, tmp_path, capsys):
    db = tmp_path / "store.db"
    for day in range(3):
        _paper(node, db, *_OPENING, now=_TEN_PAST + day * DAY)
    backtest = [
        "backtest", "--config", str(_EXAMPLE), "--db", str(db), "--run-id", "bt",
        "--from", "2024-01-01",
    ]  # fmt: skip

    code, lines = _run(node, *backtest, *_OPENING, "--fills", "quoter", now=_TEN_PAST + 2 * DAY)
    assert code == cli.EXIT_OK
    assert lines[0].endswith("3 decided, 0 already decided, 0 without a bar")
    assert cli_report(db, "bt")[1][0] == (
        "run bt: backtest, fills from the quoter, fixed_weights, values in USDC"
    )
    # But for the first line, and for how many readings were fresh when they were decided,
    # the two runs report alike: the same decisions and the same fills.
    assert _settled(cli_report(db, "bt")[1][1:]) == _settled(cli_report(db, "p")[1][1:])

    capsys.readouterr()
    code, lines = _run(node, *backtest, now=_TEN_PAST + 2 * DAY)
    assert code == cli.EXIT_FAILED and lines == []
    assert "takes its fills from the quoter, and is not carried on" in capsys.readouterr().err


@pytest.mark.parametrize("command", ["paper", "backtest"])
def test_a_command_that_quotes_without_its_requirements_exits_1_with_one_line(
    command, monkeypatch, tmp_path, capsys
):
    monkeypatch.setitem(sys.modules, "contrib.uniswap_v3.chain.quoter", None)
    argv = [command, "--config", str(_EXAMPLE), "--db", str(tmp_path / "s.db"), "--run-id", "p"]
    if command == "backtest":
        argv += ["--from", "2024-01-01", "--fills", "quoter"]
    code = cli.main([*argv, *_OPENING], out=lambda line: None)
    assert code == cli.EXIT_FAILED
    err = capsys.readouterr().err
    assert "needs the packages in contrib/uniswap_v3/requirements.txt" in err
    assert err.count("\n") == 1


def test_a_quoted_backtest_whose_node_answers_with_an_error_exits_3_and_is_carried_on_later(
    node, tmp_path, capsys
):
    db = tmp_path / "store.db"
    # WETH is half as high again on the second day, so that day rebalances and asks for quotes.
    close = block_at(FIRST_DAY + DAY) - 1
    fill_block = close + 26
    for block in (close, fill_block):
        node.slot0[(_USDC_WETH, block)] = (sqrt_price_at(UP_HALF_TICK), UP_HALF_TICK)
    node.twap_tick[(_USDC_WETH, close)] = UP_HALF_TICK
    for day in range(2):
        _paper(node, db, *_OPENING, now=_TEN_PAST + day * DAY)
    backtest = [
        "backtest", "--config", str(_EXAMPLE), "--db", str(db), "--run-id", "bt",
        "--from", "2024-01-01", "--fills", "quoter", *_OPENING,
    ]  # fmt: skip
    node.errors[fill_block] = {"code": -32000, "message": "the node is having a moment"}
    capsys.readouterr()

    code, lines = _run(node, *backtest, now=_TEN_PAST + DAY)
    assert code == cli.EXIT_RETRY and lines == []
    assert capsys.readouterr().err.startswith("try again later: ")
    # The paper run's two, and the first day of the backtest, which stays.
    assert _count(db, "decisions") == 3

    del node.errors[fill_block]
    code, lines = _run(node, *backtest, now=_TEN_PAST + DAY)
    assert code == cli.EXIT_OK
    assert lines[0].endswith("1 decided, 1 already decided, 0 without a bar")


def test_paper_warns_of_a_bar_skipped_as_suspect(node, tmp_path, capsys):
    node.twap_tick[(_USDC_WETH, block_at(FIRST_DAY) - 1)] = DEFAULT_TICK + 600
    code, lines = _paper(node, tmp_path / "store.db", *_OPENING)
    assert code == cli.EXIT_OK
    assert lines[-1].startswith("the bar at 2024-01-01T00:00:00Z is decided: skipped_suspect (")
    assert (
        "warning: 1 bar(s) were skipped as suspect, the first at 2024-01-01T00:00:00Z"
        in capsys.readouterr().err
    )


def test_paper_warns_of_stored_readings_found_off_the_final_chain(node, tmp_path, capsys):
    db = tmp_path / "store.db"
    _paper(node, db, *_OPENING)
    # The first day's close block is another block by the time it is final.
    node.hashes[block_at(FIRST_DAY) - 1] = block_hash(77)
    capsys.readouterr()

    code, lines = _paper(node, db, now=_TEN_PAST + DAY)

    assert code == cli.EXIT_OK
    assert "checked pending readings against the final chain: 0 final, 2 reorged" in lines
    assert "warning: 2 stored reading(s) are no longer on the final chain" in capsys.readouterr().err


def test_paper_whose_clock_is_behind_the_run_exits_1(node, tmp_path, capsys):
    db = tmp_path / "store.db"
    _paper(node, db, *_OPENING, now=_TEN_PAST + DAY)
    capsys.readouterr()
    code, lines = _paper(node, db, now=_TEN_PAST)
    assert code == cli.EXIT_FAILED and lines == []
    assert "the clock is behind" in capsys.readouterr().err


def test_paper_whose_clock_is_before_any_bar_exits_1_with_one_line(node, tmp_path, capsys):
    code = cli.main(
        ["paper", "--config", str(_EXAMPLE), "--db", str(tmp_path / "s.db"), "--run-id", "p", *_OPENING],
        out=lambda line: None,
        now=lambda: 86_400.0 * 365,
    )
    assert code == cli.EXIT_FAILED
    err = capsys.readouterr().err
    assert err.startswith("failed: bars on chain 1 start at ") and err.count("\n") == 1


def test_paper_warns_once_of_a_latest_boundary_without_an_answer_after_catching_up(
    node, tmp_path, capsys
):
    db = tmp_path / "store.db"
    _paper(node, db, *_OPENING)
    node.reverts.add((_USDC_WETH, block_at(FIRST_DAY + 2 * DAY) - 1))
    capsys.readouterr()

    code, lines = _paper(node, db, now=_TEN_PAST + 2 * DAY)

    assert code == cli.EXIT_OK
    assert lines[2].endswith("1 decided, 0 already decided, 1 without a bar")
    err = capsys.readouterr().err
    assert err.count("warning:") == 1
    assert "1 boundary(ies) without a bar, from 2024-01-03T00:00:00Z" in err
    assert "A later visit asks the chain again" in err and "needs a new run" not in err


def test_paper_warns_of_every_boundary_without_an_answer_when_none_has_a_bar(
    node, tmp_path, capsys
):
    db = tmp_path / "store.db"
    _paper(node, db, *_OPENING)
    for day in (1, 2):
        node.reverts.add((_USDC_WETH, block_at(FIRST_DAY + day * DAY) - 1))
    capsys.readouterr()

    code, lines = _paper(node, db, now=_TEN_PAST + 2 * DAY)

    assert code == cli.EXIT_OK
    assert lines[0] == "read 0 bar(s); 0 already stored, 2 without an answer"
    err = capsys.readouterr().err
    assert err.count("warning:") == 1
    assert (
        "no answer at the boundary 2024-01-03T00:00:00Z, nor at 1 boundary(ies) before it, "
        "from 2024-01-02T00:00:00Z" in err
    )


def test_a_backtest_counts_a_rebalance_the_quote_refused_and_does_not_warn_of_it(
    node, tmp_path, capsys
):
    db = tmp_path / "store.db"
    _paper(node, db, *_OPENING)
    node.quote_bps = 9_900
    capsys.readouterr()
    code, lines = _run(
        node, "backtest", "--config", str(_EXAMPLE), "--db", str(db), "--run-id", "bt",
        "--from", "2024-01-01", "--fills", "quoter", *_OPENING,
    )  # fmt: skip
    assert code == cli.EXIT_OK
    assert lines[1] == "filled 0, hold 0, no_trade 0, rejected 1, skipped_suspect 0"
    assert "were rejected" not in capsys.readouterr().err


# --- status of a run ---------------------------------------------------------


def _run_status(node: FakeNode, db: Path, *extra: str, run_id: str = "p") -> tuple[int, list[str]]:
    """``status --run-id``, its lines after the store's own."""
    code, lines = _run(
        node, "status", "--config", str(_EXAMPLE), "--db", str(db), "--run-id", run_id, *extra,
        now=_TEN_PAST + 2 * DAY,
    )  # fmt: skip
    first = next((index for index, line in enumerate(lines) if line.startswith("run ")), None)
    return code, lines if first is None else lines[first:]


def test_status_of_a_run_prints_its_holdings_return_and_when_each_bar_was_decided(node, tmp_path):
    db = tmp_path / "store.db"
    _paper(node, db, *_OPENING)
    _paper(node, db, now=_TEN_PAST + DAY + 1800)

    code, lines = _run_status(node, db)

    assert code == cli.EXIT_OK
    assert lines == [
        "run p: paper, fills from the quoter, fixed_weights, values in USDC, started "
        "2024-01-01T00:10:00Z",
        "2 bar(s) decided from 2024-01-01T00:00:00Z to 2024-01-02T00:00:00Z: filled 1, hold 1, "
        "no_trade 0, rejected 0, skipped_suspect 0",
        "holdings after the bar at 2024-01-02T00:00:00Z: USDC 5000, WBTC 0.06694505, "
        "WETH 1.499219798480956123; gas ETH 0.9972",
        "value 9996.50 USDC; return -0.09% from 2024-01-01T00:00:00Z to 2024-01-02T00:00:00Z, "
        "after gas, as report measures it",
        "latest 2 decision(s):",
        "2024-01-01T00:00:00Z  filled  decided 2024-01-01T00:10:00Z, 00:10:00 after the boundary",
        "2024-01-02T00:00:00Z  hold  decided 2024-01-02T00:40:00Z, 00:40:00 after the boundary",
    ]


def test_status_of_a_run_prints_as_many_decisions_as_bars_asks_for(node, tmp_path):
    db = tmp_path / "store.db"
    _paper(node, db, *_OPENING)
    # Day 1's visit was missed: day 2's decides both, at its own time.
    _paper(node, db, now=_TEN_PAST + 2 * DAY)

    code, lines = _run_status(node, db, "--bars", "2")

    assert code == cli.EXIT_OK
    assert lines[-3:] == [
        "latest 2 decision(s):",
        "2024-01-02T00:00:00Z  hold  decided 2024-01-03T00:10:00Z, 1d 00:10:00 after the boundary",
        "2024-01-03T00:00:00Z  hold  decided 2024-01-03T00:10:00Z, 00:10:00 after the boundary",
    ]


def test_status_of_a_run_that_has_decided_nothing_prints_its_opening_balances(node, tmp_path):
    db = tmp_path / "store.db"
    _paper(node, db, *_OPENING)
    _tamper(
        db,
        "INSERT INTO runs SELECT 'q', mode, chain_id, quote, strategy, config, balances, "
        "gas_eth, created_at, fills FROM runs WHERE run_id = 'p'",
    )

    code, lines = _run_status(node, db, run_id="q")

    assert code == cli.EXIT_OK
    assert lines[1:] == [
        "no bar decided yet; opening balances USDC 10000, WBTC 0, WETH 0; gas ETH 1"
    ]


def test_status_of_a_run_says_why_its_return_is_not_measured(node, tmp_path):
    node.twap_tick[(_USDC_WETH, block_at(FIRST_DAY) - 1)] = DEFAULT_TICK + 600
    db = tmp_path / "store.db"
    _paper(node, db, *_OPENING)

    code, lines = _run_status(node, db)

    assert code == cli.EXIT_OK
    assert lines[3] == (
        "value 10000.00 USDC; return not measured (every one of the run's 1 decided bar(s) "
        "was suspect, so there is none to measure it on)"
    )
    assert lines[5].startswith(
        "2024-01-01T00:00:00Z  skipped_suspect  decided 2024-01-01T00:10:00Z, 00:10:00 after "
        "the boundary ("
    )


def test_status_of_a_decision_stored_without_its_time_says_so(node, tmp_path):
    db = tmp_path / "store.db"
    _paper(node, db, *_OPENING)
    _tamper(db, "UPDATE decisions SET decided_at = NULL")

    code, lines = _run_status(node, db)

    assert code == cli.EXIT_OK
    assert lines[-1] == "2024-01-01T00:00:00Z  filled  decided at a time that was not kept"


def test_status_of_a_run_that_is_not_there_exits_1(node, tmp_path, capsys):
    db = tmp_path / "store.db"
    _paper(node, db, *_OPENING)
    capsys.readouterr()

    code, lines = _run_status(node, db, run_id="nothing")

    assert code == cli.EXIT_FAILED and lines == []
    assert capsys.readouterr().err == "failed: there is no run 'nothing' in the store\n"


@pytest.mark.parametrize(
    ("seconds", "text"),
    [(0, "00:00:00"), (600, "00:10:00"), (86_400 + 61, "1d 00:01:01"), (-90, "-00:01:30")],
)
def test_a_decision_is_said_to_be_made_a_span_after_its_boundary(seconds, text):
    assert cli._after(seconds) == text
