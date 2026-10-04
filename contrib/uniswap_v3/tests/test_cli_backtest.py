"""The command line's ``backtest`` and ``report``: a stored range replayed, and what it came to."""

from __future__ import annotations

import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from contrib.uniswap_v3 import cli
from contrib.uniswap_v3.config import load_config
from contrib.uniswap_v3.domain.types import RunMode
from contrib.uniswap_v3.engine.step import start_run
from contrib.uniswap_v3.store.repository import open_store
from contrib.uniswap_v3.tests.fakes.engine import ledger as _ledger
from contrib.uniswap_v3.tests.fakes.node import (
    BTC_TICK,
    DAY,
    DEFAULT_TICK,
    FIRST_DAY,
    UP_HALF_TICK as _UP_HALF,
    put_day,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_EXAMPLE = Path(__file__).resolve().parents[1] / "configs" / "uniswap_v3.example.yaml"
_OPENING = ("--balance", "USDC=10000", "--gas-eth", "1")


def _fill_store(path: Path, eth_ticks: list[int | None], *, twap_off: int | None = None) -> Path:
    """A store with one bar a day from 2024-01-01; a ``None`` day is left without one.

    The day ``twap_off`` has a WBTC/WETH close far from its TWAP.
    """
    with open_store(path) as store:
        for day, eth_tick in enumerate(eth_ticks):
            if eth_tick is not None:
                twap = {"twap_tick": BTC_TICK + 600} if day == twap_off else {}
                put_day(store, day, eth_tick=eth_tick, **twap)
    return path


@pytest.fixture
def db(tmp_path):
    """All in USDC to start with, then a rise by half and a fall back."""
    return _fill_store(tmp_path / "store.db", [DEFAULT_TICK, DEFAULT_TICK, _UP_HALF, DEFAULT_TICK])


def _run(*argv: str) -> tuple[int, list[str]]:
    lines: list[str] = []
    code = cli.main(list(argv), out=lines.append, now=lambda: float(FIRST_DAY + 30 * DAY))
    return code, lines


def _backtest_argv(db: Path, *extra: str, start: str = "2024-01-01", run_id: str = "bt") -> list:
    return [
        "backtest", "--config", str(_EXAMPLE), "--db", str(db), "--run-id", run_id,
        "--from", start, *extra,
    ]  # fmt: skip


def _backtest(db: Path, *extra: str, **kwargs) -> tuple[int, list[str]]:
    return _run(*_backtest_argv(db, *extra, **kwargs))


def _report(db: Path, run_id: str = "bt") -> tuple[int, list[str]]:
    return _run("report", "--db", str(db), "--run-id", run_id)


# --- backtest --------------------------------------------------------------


def test_backtest_decides_the_range_and_a_second_run_decides_nothing(db, capsys):
    code, lines = _backtest(db, *_OPENING)
    assert code == cli.EXIT_OK
    assert lines == [
        "run bt: 4 boundary(ies) from 2024-01-01T00:00:00Z to 2024-01-04T00:00:00Z: "
        "4 decided, 0 already decided, 0 without a bar",
        "filled 3, hold 1, no_trade 0, rejected 0, skipped_suspect 0",
    ]
    code, lines = _backtest(db, *_OPENING)
    assert code == cli.EXIT_OK
    assert lines[0].endswith("0 decided, 4 already decided, 0 without a bar")
    assert lines[1] == "filled 3, hold 1, no_trade 0, rejected 0, skipped_suspect 0"
    # A run that is stored is carried on without its opening balances.
    code, lines = _backtest(db, "--to", "2024-01-02T12:00:00")
    assert code == cli.EXIT_OK
    assert lines[0] == (
        "run bt: 2 boundary(ies) from 2024-01-01T00:00:00Z to 2024-01-02T00:00:00Z: "
        "0 decided, 2 already decided, 0 without a bar"
    )
    assert capsys.readouterr().err == ""
    with open_store(db) as store:
        run = store.run("bt")
        assert run.mode is RunMode.BACKTEST and run.created_at == FIRST_DAY + 30 * DAY
        assert run.ledger == _ledger()
        assert len(store.decisions("bt")) == 4


def test_boundaries_without_a_bar_exit_0_with_a_warning_on_stderr(tmp_path, capsys):
    db = _fill_store(tmp_path / "store.db", [DEFAULT_TICK, None, None, DEFAULT_TICK])
    code, lines = _backtest(db, *_OPENING)
    assert code == cli.EXIT_OK
    assert lines[0].endswith("2 decided, 0 already decided, 2 without a bar")
    assert capsys.readouterr().err == (
        "warning: 2 boundary(ies) without a bar, from 2024-01-02T00:00:00Z to "
        "2024-01-03T00:00:00Z; they were not decided, and once one is backfilled the range "
        "needs a new run\n"
    )


def test_opening_balances_no_chain_could_hold_exit_1(db, capsys):
    code, _ = _backtest(db, "--balance", "USDC=1" + "0" * 80, "--gas-eth", "1")
    assert code == cli.EXIT_FAILED
    assert capsys.readouterr().err.startswith("failed: the opening balances cannot be held (")


def test_report_prints_a_run_of_any_mode(db):
    _backtest(db, *_OPENING)
    connection = sqlite3.connect(db)
    connection.execute("UPDATE runs SET mode = 'paper'")
    connection.commit()
    connection.close()
    code, lines = _report(db)
    assert code == cli.EXIT_OK
    assert lines[0] == "run bt: paper, fixed_weights, values in USDC"


def test_backtest_takes_the_bar_length_from_the_command_line(db, capsys):
    code, lines = _backtest(db, *_OPENING, "--interval-seconds", "3600")
    assert code == cli.EXIT_FAILED and lines == []
    assert "the store holds no bar from" in capsys.readouterr().err


def test_backtest_does_not_wait_for_the_disk_and_makes_no_store(db, tmp_path, monkeypatch, capsys):
    opened = []

    def spy(path, **kwargs):
        opened.append(kwargs)
        return open_store(path, **kwargs)

    monkeypatch.setattr(cli, "open_store", spy)
    assert _backtest(db, *_OPENING)[0] == cli.EXIT_OK
    assert opened == [{"create": False, "durable": False}]

    code, lines = _backtest(tmp_path / "nowhere.db", *_OPENING)
    assert code == cli.EXIT_FAILED and lines == []
    assert "there is no store at" in capsys.readouterr().err
    assert not (tmp_path / "nowhere.db").exists()


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ((), "there is no run 'bt', and a new run needs opening balances"),
        (("--balance", "USDC=10000"), "opening balances take both --balance and --gas-eth"),
        (("--gas-eth", "1"), "opening balances take both --balance and --gas-eth"),
        (
            ("--balance", "DAI=10000", "--gas-eth", "1"),
            "--balance names 'DAI' which the config does not list; the config's tokens are "
            "['USDC', 'WBTC', 'WETH']",
        ),
        (
            ("--balance", "USDC=1", "--balance", "USDC=2", "--gas-eth", "1"),
            "--balance names 'USDC' more than once",
        ),
        (("--balance", "USDC=0", "--gas-eth", "1"), "the opening balances are all zero"),
        (
            ("--balance", "USDC=1.0000001", "--gas-eth", "1"),
            "the opening USDC balance 1.0000001 has more than 6 decimal places",
        ),
    ],
)
def test_opening_balances_that_cannot_start_a_run_exit_1_and_start_none(db, capsys, extra, message):
    code, lines = _backtest(db, *extra)
    assert code == cli.EXIT_FAILED and lines == []
    assert capsys.readouterr().err.startswith(f"failed: {message}")
    with open_store(db) as store:
        assert store.run("bt") is None


def test_a_range_that_cannot_be_replayed_exits_1(db, capsys):
    code, _ = _backtest(db, *_OPENING, start="2024-01-01T00:00:01")
    assert code == cli.EXIT_FAILED
    assert capsys.readouterr().err.startswith("failed: the range must start on a bar boundary")
    code, _ = _backtest(db, *_OPENING, "--to", "2023-12-01")
    assert code == cli.EXIT_FAILED
    assert capsys.readouterr().err.startswith("failed: the range ends at")
    with open_store(db) as store:
        assert store.run("bt") is None


def test_other_opening_balances_for_a_stored_run_exit_1(db, capsys):
    _backtest(db, *_OPENING)
    code, _ = _backtest(db, "--balance", "USDC=5", "--gas-eth", "1")
    assert code == cli.EXIT_FAILED
    assert "was started with other opening balances" in capsys.readouterr().err


@pytest.mark.parametrize(
    "extra",
    [
        ["--balance", "USDC"],
        ["--balance", "=5"],
        ["--balance", "USDC=-5"],
        ["--balance", "USDC=1e3"],
        ["--balance", "USDC=ten"],
        ["--gas-eth", "-1"],
        ["--gas-eth", "0.1.2"],
        ["--fills", "quoter"],
        ["--run-id", " "],
    ],
)
def test_a_backtest_command_line_that_cannot_be_read_exits_2(extra, capsys):
    with pytest.raises(SystemExit) as exit_info:
        cli.main(_backtest_argv(Path("s.db"), *_OPENING, *extra))
    assert exit_info.value.code == 2
    capsys.readouterr()


def test_backtest_and_report_need_a_run_id_and_report_takes_no_config(capsys):
    for argv in (
        ["backtest", "--config", "c.yaml", "--db", "s.db", "--from", "2024-01-01"],
        ["backtest", "--config", "c.yaml", "--db", "s.db", "--run-id", "bt"],
        ["report", "--db", "s.db"],
        ["report", "--db", "s.db", "--run-id", "bt", "--config", "c.yaml"],
    ):
        with pytest.raises(SystemExit) as exit_info:
            cli.main(argv)
        assert exit_info.value.code == 2
    capsys.readouterr()


def test_a_backtest_and_its_report_run_with_no_web3_and_no_network(db):
    script = "\n".join(
        [
            "import socket, sys",
            "sys.modules['web3'] = None",
            "def refuse(*args, **kwargs):",
            "    raise AssertionError('the network was reached')",
            "socket.socket.connect = refuse",
            "socket.create_connection = refuse",
            "from contrib.uniswap_v3 import cli",
            f"code = cli.main({[str(arg) for arg in _backtest_argv(db, *_OPENING)]!r})",
            f"code = code or cli.main(['report', '--db', {str(db)!r}, '--run-id', 'bt'])",
            "assert 'contrib.uniswap_v3.chain.rpc' not in sys.modules",
            "sys.exit(code)",
        ]
    )
    done = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=_REPO_ROOT,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert "4 decided, 0 already decided" in done.stdout
    assert "strategy" in done.stdout


# --- report ----------------------------------------------------------------


def test_report_prints_the_run_beside_its_two_comparisons(db):
    _backtest(db, *_OPENING)
    code, lines = _report(db)
    assert code == cli.EXIT_OK
    assert lines[:4] == [
        "run bt: backtest, fixed_weights, values in USDC",
        "4 bar(s) decided from 2024-01-01T00:00:00Z to 2024-01-04T00:00:00Z",
        "decisions: filled 3, hold 1, no_trade 0, rejected 0, skipped_suspect 0",
        f"{'':<18}{'start':>14}{'end':>14}{'return':>11}{'max drawdown':>14}",
    ]
    assert len(lines) == 9
    curve = r" +10000\.00 +\d+\.\d\d +[+-]\d+\.\d\d% +\d+\.\d\d%"
    assert re.fullmatch("strategy" + curve, lines[4])
    # All in USDC and left alone, the opening balances never move.
    for line, name in ((lines[5], "opening balances"), (lines[6], "all in USDC")):
        assert line == f"{name:<18}{'10000.00':>14}{'10000.00':>14}{'+0.00%':>11}{'0.00%':>14}"
    assert re.fullmatch(
        r"3 rebalance\(s\), 6 swap\(s\); \d+\.\d\d USDC sold, \d\.\d\d times the mean equity",
        lines[7],
    )
    costs = re.fullmatch(
        r"costs: (\d+\.\d\d) USDC \(pool fees (\d+\.\d\d), slippage (\d+\.\d\d), "
        r"gas (\d+\.\d\d) for (0\.\d+) ETH\)",
        lines[8],
    )
    assert costs
    # Six swaps and nine pools crossed, at 150,000 gas and 7 gwei each.
    assert costs[5] == "0.00945"


def test_report_counts_why_bars_were_skipped_and_leaves_them_out_of_the_curves(tmp_path):
    db = _fill_store(tmp_path / "store.db", [DEFAULT_TICK] * 4, twap_off=2)
    _backtest(db, *_OPENING)
    code, lines = _report(db)
    assert code == cli.EXIT_OK
    assert lines[2] == "decisions: filled 1, hold 2, no_trade 0, rejected 0, skipped_suspect 1"
    assert lines[3] == "skipped_suspect: twap_deviation 1"
    assert lines[4] == "measured on the 3 bar(s) that were not suspect"
    assert len(lines) == 11


def test_report_counts_why_rebalances_were_rejected(db):
    # No ETH to pay gas from: every rebalance is refused.
    _backtest(db, "--balance", "USDC=10000", "--gas-eth", "0")
    code, lines = _report(db)
    assert code == cli.EXIT_OK
    assert lines[2] == "decisions: filled 0, hold 0, no_trade 0, rejected 4, skipped_suspect 0"
    assert lines[3] == "rejected: gas 4"
    assert lines[-2].startswith("0 rebalance(s), 0 swap(s); 0.00 USDC sold")


def test_report_reads_the_runs_own_config_and_no_file(db, tmp_path):
    custom = tmp_path / "custom.yaml"
    custom.write_text(
        _EXAMPLE.read_text(encoding="utf-8").replace('band: "0.05"', 'band: "0.2"'),
        encoding="utf-8",
    )
    argv = _backtest_argv(db, *_OPENING)
    argv[argv.index("--config") + 1] = str(custom)
    assert _run(*argv)[0] == cli.EXIT_OK
    custom.unlink()
    code, lines = _report(db)
    assert code == cli.EXIT_OK
    assert lines[0] == "run bt: backtest, fixed_weights, values in USDC"


def test_a_run_that_is_not_there_or_has_decided_nothing_exits_1(db, capsys):
    code, lines = _report(db, "nothing")
    assert code == cli.EXIT_FAILED and lines == []
    assert capsys.readouterr().err == "failed: there is no run 'nothing' in the store\n"

    with open_store(db) as store:
        start_run(
            store,
            load_config(_EXAMPLE),
            run_id="idle",
            mode=RunMode.BACKTEST,
            ledger=_ledger(),
            created_at=FIRST_DAY,
        )
    code, lines = _report(db, "idle")
    assert code == cli.EXIT_FAILED and lines == []
    assert capsys.readouterr().err == "failed: the run has no valuation to measure it on\n"
