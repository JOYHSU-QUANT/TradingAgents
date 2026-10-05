"""A fork run end to end against a scripted anvil: stored bars, signed swaps, the wallet reconciled."""

from __future__ import annotations

import sqlite3
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path

import pytest

from contrib.uniswap_v3 import cli
from contrib.uniswap_v3.chain import fork as fork_module
from contrib.uniswap_v3.chain.errors import RpcUnavailable, TransactionUnconfirmed
from contrib.uniswap_v3.chain.fork import DEV_ACCOUNTS, Fork
from contrib.uniswap_v3.chain.swaps import ChainExecutor
from contrib.uniswap_v3.chain.units import from_raw
from contrib.uniswap_v3.chain.wallet import ForkWallet
from contrib.uniswap_v3.config import load_config
from contrib.uniswap_v3.constants import SWAP_ROUTER_02
from contrib.uniswap_v3.domain.execution import fill_block
from contrib.uniswap_v3.domain.records import Outcome
from contrib.uniswap_v3.domain.types import RunMode, eth_from_wei
from contrib.uniswap_v3.engine.executors import ModelExecutor
from contrib.uniswap_v3.engine.step import EngineError, UnsettledSend
from contrib.uniswap_v3.fork_run import reconcile_open_send, run_fork
from contrib.uniswap_v3.store.bar_source import load_bar
from contrib.uniswap_v3.store.repository import open_store
from contrib.uniswap_v3.tests.fakes.engine import ledger as _ledger
from contrib.uniswap_v3.tests.fakes.fork import APPROVE_GAS, FakeAnvil
from contrib.uniswap_v3.tests.fakes.node import DAY, FIRST_DAY, put_day
from contrib.uniswap_v3.tests.fakes.rpc import rpc_over

D = Decimal
_EXAMPLE = Path(__file__).resolve().parents[1] / "configs" / "uniswap_v3.example.yaml"
_CONFIG = load_config(_EXAMPLE)
_ME = DEV_ACCOUNTS[0]
_ROUTER = SWAP_ROUTER_02[1].lower()
_PRICE = 10**9 + 10**8
_RUN = "f"


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "store.db"
    with open_store(path) as store:
        for day in range(3):
            put_day(store, day)
    return path


def _anvil(db: Path, *, refuse: str | None = None) -> FakeAnvil:
    """An anvil whose pools pay a swap its value at the first bar's close, fees aside.

    A swap for ``refuse`` is quoted and paid one smallest unit.
    """
    with open_store(db) as store:
        prices = {**load_bar(store, _CONFIG, FIRST_DAY).bar.prices, "USDC": D(1)}
    tokens = {token.address.lower(): token for token in _CONFIG.tokens}

    def pay(token_in: str, token_out: str, raw: int) -> int:
        sold, bought = tokens[token_in], tokens[token_out]
        if bought.symbol == refuse:
            return 1
        value = from_raw(sold, raw) * prices[sold.symbol]
        amount = (value / prices[bought.symbol]).scaleb(bought.decimals)
        return int(amount.to_integral_value(rounding=ROUND_FLOOR))

    anvil = FakeAnvil()
    anvil.quote_for = pay
    return anvil


def _parts(anvil: FakeAnvil) -> tuple[ChainExecutor, ForkWallet]:
    rpc, _ = rpc_over(anvil.provider)
    executor = ChainExecutor(Fork(rpc), sleep=lambda seconds: None)
    return executor, ForkWallet(executor, tokens=_CONFIG.tokens, settings=_CONFIG.execution)


def _run_fork(store, executor, wallet, *, opening=None):
    return run_fork(
        store,
        _CONFIG,
        executor,
        wallet,
        run_id=_RUN,
        start=FIRST_DAY,
        opening=opening,
        now=FIRST_DAY + 30 * DAY,
        fork_block=99,
    )


def test_a_fork_run_signs_each_bars_swaps_on_a_fork_of_its_fill_block_and_keeps_what_the_chain_paid(db):
    anvil = _anvil(db)
    executor, wallet = _parts(anvil)
    with open_store(db) as store:
        summary = _run_fork(store, executor, wallet, opening=_ledger())
        run = store.run(_RUN)
        assert (run.mode, run.fork_block) == (RunMode.FORK, 99)
        assert summary.decided == 3
        assert dict(summary.outcomes) == {Outcome.FILLED: 1, Outcome.HOLD: 2}
        first = load_bar(store, _CONFIG, FIRST_DAY).bar
        # Only the bar that traded touched the fork, at the block a quoted fill prices it at.
        assert anvil.resets == [fill_block(first, _CONFIG.execution)]
        fills = store.fills(_RUN)
        assert [(fill.token_in, fill.token_out) for fill in fills] == [
            ("USDC", "WETH"),
            ("USDC", "WBTC"),
        ]
        # Approve and swap for each: the fills were mined after the fork's block.
        assert [fill.block for fill in fills] == [first.close_block + 28, first.close_block + 30]
        assert store.ledger(_RUN) == wallet.holdings()
        assert store.open_send(_RUN) is None


def test_a_second_leg_refused_leaves_the_run_partial_with_the_wallet_where_the_ledger_says(db):
    anvil = _anvil(db, refuse="WBTC")
    executor, wallet = _parts(anvil)
    with open_store(db) as store:
        summary = _run_fork(store, executor, wallet, opening=_ledger())
        decision = store.decision(_RUN, FIRST_DAY)
        assert decision.outcome is Outcome.PARTIAL
        assert "leg 1 (USDC to WBTC) was refused" in decision.reason
        assert "nothing was sent" in decision.reason
        # The next bars are decided from the partial holdings: the run still wants WBTC.
        assert summary.outcomes[Outcome.PARTIAL] == 1
        assert summary.executor_rejected == (FIRST_DAY + DAY, FIRST_DAY + 2 * DAY)
        assert [fill.token_out for fill in store.fills(_RUN)] == ["WETH"]
        assert store.ledger(_RUN).balances["WBTC"] == 0
        assert store.ledger(_RUN) == wallet.holdings()


class _FailsOnTheSecondSwap:
    """The chain executor, with the router refusing to estimate any swap after the first."""

    source = ChainExecutor.source

    def __init__(self, executor: ChainExecutor, anvil: FakeAnvil) -> None:
        self._executor = executor
        self._anvil = anvil
        self.asked = 0

    def execute(self, swap, bar):
        self.asked += 1
        if self.asked == 2:
            self._anvil.estimate_reverts = {_ROUTER}
        return self._executor.execute(swap, bar)


def test_a_send_that_fails_after_an_approval_is_left_open_reconciled_and_not_sent_again(db):
    anvil = _anvil(db)
    executor, wallet = _parts(anvil)
    failing = _FailsOnTheSecondSwap(executor, anvil)
    with open_store(db) as store:
        with pytest.raises(UnsettledSend):
            _run_fork(store, failing, wallet, opening=_ledger())
        opened = store.open_send(_RUN)
        assert [leg.token_out for leg in opened.legs] == ["WETH"]
        assert opened.failure.startswith("SwapNotFilled: ")
        assert opened.failed_gas_eth == eth_from_wei(APPROVE_GAS * _PRICE)

        found = reconcile_open_send(store, _CONFIG, wallet, _RUN)
        assert found.gas_known and found.agrees
        assert found.held == found.expected
        assert found.expected.gas_eth == (
            D(1) - opened.legs[0].gas_cost_eth - opened.failed_gas_eth
        )

        sent, resets = len(anvil.sent), list(anvil.resets)
        with pytest.raises(UnsettledSend, match="has an open send at the bar"):
            _run_fork(store, executor, wallet)
        assert (len(anvil.sent), anvil.resets) == (sent, resets)


def test_a_wallet_changed_since_the_send_does_not_agree_with_what_was_written(db):
    anvil = _anvil(db)
    executor, wallet = _parts(anvil)
    with open_store(db) as store:
        with pytest.raises(UnsettledSend):
            _run_fork(store, _FailsOnTheSecondSwap(executor, anvil), wallet, opening=_ledger())
        anvil.eth[_ME.lower()] += 1
        assert not reconcile_open_send(store, _CONFIG, wallet, _RUN).agrees


def test_a_failed_gas_beyond_the_ledgers_eth_is_named_as_such(db):
    anvil = _anvil(db)
    executor, wallet = _parts(anvil)
    with open_store(db) as store, pytest.raises(UnsettledSend):
        _run_fork(store, _FailsOnTheSecondSwap(executor, anvil), wallet, opening=_ledger())
    connection = sqlite3.connect(db)
    connection.execute("UPDATE sends SET failed_gas_eth = '5'")
    connection.commit()
    connection.close()
    with open_store(db) as store, pytest.raises(EngineError, match="is said to have cost 5 ETH"):
        reconcile_open_send(store, _CONFIG, wallet, _RUN)


def test_a_run_without_an_open_send_has_nothing_to_reconcile(db):
    _, wallet = _parts(_anvil(db))
    with open_store(db) as store:
        assert reconcile_open_send(store, _CONFIG, wallet, "nope") is None


def test_a_fork_run_is_not_filled_by_an_executor_that_does_not_sign(db):
    _, wallet = _parts(_anvil(db))
    with open_store(db) as store:
        with pytest.raises(EngineError, match="a fork run fills from the chain, not from the model"):
            _run_fork(store, ModelExecutor("USDC", _CONFIG.execution), wallet, opening=_ledger())
        assert store.run(_RUN) is None


# --- the command line --------------------------------------------------------


@pytest.fixture
def forked(monkeypatch):
    """Whatever URL the command opens, it opens the fake anvil set here."""
    held: dict[str, FakeAnvil] = {}

    def open_fork(chain_id, *, url, settings):
        assert url == "http://127.0.0.1:8545" and chain_id == 1
        return Fork(rpc_over(held["anvil"].provider)[0])

    monkeypatch.setattr(fork_module, "open_fork", open_fork)
    return held


def _fork_cli(db: Path, *extra: str) -> tuple[int, list[str]]:
    lines: list[str] = []
    argv = [
        "fork", "--config", str(_EXAMPLE), "--db", str(db), "--run-id", _RUN,
        "--from", "2024-01-01", *extra,
    ]  # fmt: skip
    code = cli.main(argv, out=lines.append, now=lambda: float(FIRST_DAY + 30 * DAY))
    return code, lines


def test_the_fork_command_replays_the_range_on_the_fork_and_report_names_the_fork_block(
    db, forked, capsys
):
    forked["anvil"] = _anvil(db)
    code, lines = _fork_cli(db, "--balance", "USDC=10000", "--gas-eth", "1")
    assert code == cli.EXIT_OK, capsys.readouterr().err
    assert lines == [
        "run f: 3 boundary(ies) from 2024-01-01T00:00:00Z to 2024-01-03T00:00:00Z: 3 decided, "
        "0 already decided, 0 without a bar",
        "filled 1, hold 2, no_trade 0, partial 0, rejected 0, skipped_suspect 0",
    ]
    report: list[str] = []
    assert cli.main(["report", "--db", str(db), "--run-id", _RUN], out=report.append) == 0
    assert report[0] == (
        "run f: fork (the fork was at block 99 when it started), fills from the chain, "
        "fixed_weights, values in USDC"
    )
    # Run again, it decides nothing, and sends nothing.
    sent = len(forked["anvil"].sent)
    code, lines = _fork_cli(db)
    assert code == cli.EXIT_OK and "0 decided, 3 already decided" in lines[0]
    assert len(forked["anvil"].sent) == sent


def test_the_fork_command_prints_an_open_send_beside_the_wallet_and_exits_1(
    db, forked, monkeypatch, capsys
):
    anvil = forked["anvil"] = _anvil(db)
    real = ChainExecutor.execute
    asked = []

    def second_fails(self, swap, bar):
        asked.append(swap)
        if len(asked) == 2:
            anvil.estimate_reverts = {_ROUTER}
        return real(self, swap, bar)

    monkeypatch.setattr(ChainExecutor, "execute", second_fails)
    code, lines = _fork_cli(db, "--balance", "USDC=10000", "--gas-eth", "1")
    assert code == cli.EXIT_FAILED
    assert "failed: the swaps of the bar at" in capsys.readouterr().err
    assert lines[0].startswith("open send at the bar 2024-01-01T00:00:00Z, begun ")
    assert lines[0].endswith(": 1 leg(s) filled")
    assert lines[1].startswith("stopped by: SwapNotFilled: ")
    assert lines[2].startswith("leg 0: 3000 USDC for ")
    assert "the failed swap's gas, " in lines[4]
    assert lines[-2] == "the wallet agrees with what was written"
    assert lines[-1].startswith("the run goes no further")

    # Run again, it says the same and sends nothing more.
    sent = len(anvil.sent)
    code, again = _fork_cli(db)
    assert code == cli.EXIT_FAILED and again == lines
    assert "has an open send at the bar" in capsys.readouterr().err
    assert len(anvil.sent) == sent


def test_report_counts_why_rebalances_were_left_partial(db, forked):
    forked["anvil"] = _anvil(db, refuse="WBTC")
    code, _ = _fork_cli(db, "--balance", "USDC=10000", "--gas-eth", "1")
    assert code == cli.EXIT_OK
    report: list[str] = []
    cli.main(["report", "--db", str(db), "--run-id", _RUN], out=report.append)
    assert "partial: executor 1" in report


def test_an_open_send_whose_failed_gas_is_not_known_is_compared_without_eth(
    db, forked, monkeypatch
):
    forked["anvil"] = _anvil(db)
    real = ChainExecutor.execute
    asked = []

    def second_lost(self, swap, bar):
        asked.append(swap)
        if len(asked) == 2:
            raise TransactionUnconfirmed("no receipt came", tx_hashes=("0x" + "cd" * 32,))
        return real(self, swap, bar)

    monkeypatch.setattr(ChainExecutor, "execute", second_lost)
    code, lines = _fork_cli(db, "--balance", "USDC=10000", "--gas-eth", "1")
    assert code == cli.EXIT_FAILED
    assert lines[1].startswith("stopped by: TransactionUnconfirmed: no receipt came")
    assert any("the failed swap's gas not known, so ETH is not compared" in line for line in lines)
    assert lines[-2] == "the wallet agrees with what was written"


def test_a_wallet_moved_beside_the_swaps_does_not_agree(db, forked, monkeypatch):
    anvil = forked["anvil"] = _anvil(db)
    real = ChainExecutor.execute

    def second_moves_eth(self, swap, bar):
        fill = real(self, swap, bar)
        if len(anvil.sent) == 4:
            # Something beside the swaps spent a wei of the wallet's ETH.
            anvil.eth[_ME.lower()] -= 1
        return fill

    monkeypatch.setattr(ChainExecutor, "execute", second_moves_eth)
    code, lines = _fork_cli(db, "--balance", "USDC=10000", "--gas-eth", "1")
    assert code == cli.EXIT_FAILED
    assert lines[1].startswith("stopped by: EngineError: after the swaps of the bar at")
    assert lines[-2] == "the wallet does NOT agree with what was written"


def test_an_open_send_the_wallet_cannot_be_read_beside_still_exits_1(
    db, forked, monkeypatch, capsys
):
    anvil = forked["anvil"] = _anvil(db)
    with open_store(db) as store:
        executor, wallet = _parts(anvil)
        with pytest.raises(UnsettledSend):
            _run_fork(store, _FailsOnTheSecondSwap(executor, anvil), wallet, opening=_ledger())

    def unreadable(self):
        raise RpcUnavailable("the node is down")

    monkeypatch.setattr(ForkWallet, "holdings", unreadable)
    code, lines = _fork_cli(db)
    assert code == cli.EXIT_FAILED and lines == []
    assert "the open send could not be set beside the wallet" in capsys.readouterr().err


def test_a_fork_run_warns_of_rebalances_left_partial(db, forked, capsys):
    forked["anvil"] = _anvil(db, refuse="WBTC")
    code, _ = _fork_cli(db, "--balance", "USDC=10000", "--gas-eth", "1")
    assert code == cli.EXIT_OK
    err = capsys.readouterr().err
    assert "rebalance(s) were left partial" in err
    assert "rebalance(s) were rejected because a swap was refused" in err


def test_a_carried_on_fork_run_does_not_ask_where_the_fork_is(db, forked):
    anvil = forked["anvil"] = _anvil(db)
    assert _fork_cli(db, "--balance", "USDC=10000", "--gas-eth", "1")[0] == cli.EXIT_OK
    anvil.node_info["forkConfig"]["forkBlockNumber"] = "not a block"
    assert _fork_cli(db)[0] == cli.EXIT_OK


def test_a_send_settled_before_it_could_be_compared_says_so():
    assert cli._reconciliation_lines(None) == ["the run has no open send any more"]


def test_the_fork_command_refuses_a_fork_url_that_is_not_on_this_machine(db, capsys):
    code, _ = _fork_cli(db, "--fork-url", "http://localhost:8545")
    assert code == cli.EXIT_FAILED
    assert "a fork is opened only at an http(s) URL on this machine" in capsys.readouterr().err
