"""Signed swaps on a local anvil fork of mainnet. Not run by CI.

Skipped without ``ETH_RPC_URL`` (an archive node: the fork is pinned to a
past block) or without ``anvil`` on the PATH. The test starts its own anvil
on a free port and stops it after::

    python -m dotenv run -- pytest -m smoke contrib/uniswap_v3/tests/test_chain_fork_smoke.py -s

The round trip USDC -> WETH + WBTC -> USDC is the acceptance of PR 8: every
balance the wallet holds moves by exactly what the fills say, its gas
included. With ``-s`` it prints the gas each swap used beside the quoter's
estimate, which is what ``execution.quote.gas_overhead_units`` stands for.

The fork runs at the end are the acceptance of PR 9, on two daily bars
backfilled from the node: the wallet ends where the run's ledger says, the
first swap fills at what QuoterV2 quotes at the bar's fill block (a paper
run's block), a second leg that fails leaves the rebalance partial and the
wallet still where the ledger says, and a rerun sends nothing.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from collections.abc import Iterator
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest
import requests
from web3 import HTTPProvider, Web3

from contrib.uniswap_v3.backfill import backfill
from contrib.uniswap_v3.chain.fork import DEV_ACCOUNTS, Fork, open_fork
from contrib.uniswap_v3.chain.quoter import quote_exact_input
from contrib.uniswap_v3.chain.rpc import _REDACTOR, DEFAULT_URL_ENV, Rpc, RpcSettings, connect
from contrib.uniswap_v3.chain.swaps import ChainExecutor
from contrib.uniswap_v3.chain.units import from_raw
from contrib.uniswap_v3.chain.wallet import ForkWallet
from contrib.uniswap_v3.config import load_config
from contrib.uniswap_v3.constants import ETHEREUM_MAINNET, POOLS, TOKENS
from contrib.uniswap_v3.domain.decimal_context import floor_to_places
from contrib.uniswap_v3.domain.execution import fill_block
from contrib.uniswap_v3.domain.ledger import Ledger
from contrib.uniswap_v3.domain.records import FillSource, Outcome
from contrib.uniswap_v3.domain.types import (
    Bar,
    Fill,
    Pool,
    Rejection,
    SwapIntent,
    Token,
    eth_from_wei,
)
from contrib.uniswap_v3.fork_run import run_fork
from contrib.uniswap_v3.store.bar_source import load_bar
from contrib.uniswap_v3.store.repository import open_store
from contrib.uniswap_v3.tests.fakes.rpc import closed_port

pytestmark = pytest.mark.smoke

# 2026-10-02: recent enough for current pools, old enough to be final on any archive node.
FORK_BLOCK = 26_100_000

_USDC = TOKENS[ETHEREUM_MAINNET]["USDC"]
_WETH = TOKENS[ETHEREUM_MAINNET]["WETH"]
_WBTC = TOKENS[ETHEREUM_MAINNET]["WBTC"]
_USDC_WETH = POOLS[ETHEREUM_MAINNET]["USDC/WETH-500"]
_WBTC_WETH = POOLS[ETHEREUM_MAINNET]["WBTC/WETH-500"]
_ERC20 = [
    {
        "name": "balanceOf",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "a", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
    }
]
# Not consulted by a chain executor; the port asks for one.
_BAR = Bar(time=0, close_block=0, prices={"WETH": Decimal(1)}, base_fee_wei=0)


@pytest.fixture(scope="module")
def fork_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    upstream = os.environ.get(DEFAULT_URL_ENV, "")
    anvil = shutil.which("anvil")
    if not upstream:
        pytest.skip(f"{DEFAULT_URL_ENV} is not set")
    if anvil is None:
        pytest.skip("anvil is not on the PATH")
    port = closed_port()
    log = tmp_path_factory.mktemp("anvil") / "stderr.log"
    with log.open("wb") as stderr:
        process = subprocess.Popen(
            [anvil, "--fork-url", upstream, "--fork-block-number", str(FORK_BLOCK)]
            + ["--port", str(port), "--silent"],
            stdout=subprocess.DEVNULL,
            stderr=stderr,
        )

    def said() -> str:
        # anvil quotes the upstream URL, which ends in the node's key: scrubbed as the
        # package scrubs its own node's URL.
        _REDACTOR.register(upstream.strip())
        return _REDACTOR.scrub(log.read_text(encoding="utf-8", errors="replace"))[-1_000:]

    url = f"http://127.0.0.1:{port}"
    try:
        w3 = Web3(HTTPProvider(url))
        for _ in range(120):
            if process.poll() is not None:
                pytest.fail(f"anvil exited with {process.returncode} before it answered: {said()}")
            try:
                if w3.eth.chain_id:
                    break
            except (requests.ConnectionError, requests.Timeout):
                time.sleep(0.5)
        else:
            pytest.fail(f"anvil did not answer within a minute: {said()}")
        yield url
    finally:
        process.terminate()
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        # What anvil wrote holds the upstream URL as it is.
        log.unlink(missing_ok=True)


@pytest.fixture(scope="module")
def fork(fork_url: str) -> Fork:
    # A cold fork reads every account and slot it meets from the archive node: a read,
    # or the one send, can take longer than a warm node's ten seconds.
    return open_fork(ETHEREUM_MAINNET, url=fork_url, settings=RpcSettings(timeout_seconds=60))


@pytest.fixture(scope="module")
def w3(fork_url: str) -> Web3:
    """The fork read directly, not through the code under test."""
    return Web3(HTTPProvider(fork_url))


def _balance(w3: Web3, token: Token | None, address: str) -> Decimal:
    if token is None:
        return eth_from_wei(w3.eth.get_balance(address))
    raw = w3.eth.contract(address=token.address, abi=_ERC20).functions.balanceOf(address).call()
    return from_raw(token, raw)


def _intent(fork: Fork, token_in: Token, route: tuple[Pool, ...], amount: Decimal) -> SwapIntent:
    """A swap of ``amount`` whose minimum is 1% under the quote at the fork's head."""
    rpc = fork.rpc
    quote = quote_exact_input(rpc, token_in, route, amount, block=rpc.latest_header().number)
    floor = floor_to_places(quote.amount_out * Decimal("0.99"), quote.token_out.decimals)
    return SwapIntent(token_in=token_in, route=route, amount_in=amount, min_amount_out=floor)


def _swap(w3: Web3, fork: Fork, executor: ChainExecutor, intent: SwapIntent) -> Fill:
    """Fill ``intent`` and check the wallet moved by exactly what the fill says."""
    me = executor.address
    tokens = (intent.token_in, intent.token_out, None)
    before = [_balance(w3, token, me) for token in tokens]
    head = fork.rpc.latest_header().number
    fill = executor.execute(intent, _BAR)
    assert isinstance(fill, Fill), fill
    after = [_balance(w3, token, me) for token in tokens]
    assert before[0] - after[0] == intent.amount_in
    assert after[1] - before[1] == fill.amount_out
    assert before[2] - after[2] == fill.gas_cost_eth
    assert fill.amount_out >= intent.min_amount_out

    # What the swap's own gas was, beside the quoter's estimate (the overhead's calibration).
    swap_receipt = w3.eth.get_transaction_receipt(w3.eth.get_block(fill.block)["transactions"][-1])
    estimate = quote_exact_input(
        fork.rpc, intent.token_in, intent.route, intent.amount_in, block=head
    ).gas_estimate
    approvals = fill.block - head - 1
    print(
        f"\n{intent.amount_in} {intent.token_in.symbol} -> {fill.amount_out} "
        f"{intent.token_out.symbol}: swap gas {swap_receipt['gasUsed']}, quoter estimate "
        f"{estimate} (overhead {swap_receipt['gasUsed'] - estimate}), approvals {approvals}, "
        f"gas {fill.gas_cost_eth} ETH"
    )
    return fill


def test_the_fork_is_opened_and_the_executor_signs_as_dev_account_zero(fork):
    executor = ChainExecutor(fork)
    assert executor.address == DEV_ACCOUNTS[0]
    assert executor.source is FillSource.CHAIN


def test_a_round_trip_through_both_pools_moves_the_wallet_by_exactly_its_fills(fork, w3):
    executor = ChainExecutor(fork, account=1)
    me = executor.address
    # Test money: ten of the dev account's ETH wrapped, and sold for USDC through the executor.
    deposit = Web3.keccak(text="deposit()")[:4]
    w3.eth.wait_for_transaction_receipt(
        w3.eth.send_transaction(
            {"from": me, "to": _WETH.address, "value": 10 * 10**18, "data": deposit}
        )
    )
    funded = _swap(w3, fork, executor, _intent(fork, _WETH, (_USDC_WETH,), Decimal(10)))
    assert funded.amount_out > Decimal(1_000)

    weth = _swap(w3, fork, executor, _intent(fork, _USDC, (_USDC_WETH,), Decimal(5_000)))
    wbtc = _swap(
        w3, fork, executor, _intent(fork, _USDC, (_USDC_WETH, _WBTC_WETH), Decimal(5_000))
    )
    back = [
        _swap(w3, fork, executor, _intent(fork, _WETH, (_USDC_WETH,), weth.amount_out)),
        _swap(
            w3,
            fork,
            executor,
            _intent(fork, _WBTC, (_WBTC_WETH, _USDC_WETH), wbtc.amount_out),
        ),
    ]
    returned = sum(fill.amount_out for fill in back)
    # Two pool fees each way and the price impact of 5,000 USDC: well under 1% lost.
    assert Decimal(9_900) < returned < Decimal(10_000)


def test_a_swap_whose_quote_is_below_its_minimum_is_refused_and_nothing_is_sent(fork, w3):
    executor = ChainExecutor(fork, account=2)
    me = executor.address
    nonce = w3.eth.get_transaction_count(me)
    eth = _balance(w3, None, me)
    intent = _intent(fork, _WETH, (_USDC_WETH,), Decimal(1))
    greedy = SwapIntent(
        token_in=_WETH,
        route=(_USDC_WETH,),
        amount_in=Decimal(1),
        min_amount_out=intent.min_amount_out * 2,
    )
    refused = executor.execute(greedy, _BAR)
    assert isinstance(refused, Rejection) and "nothing was sent" in refused.reason
    assert (w3.eth.get_transaction_count(me), _balance(w3, None, me)) == (nonce, eth)


# --- PR 9: fork runs over stored bars. These reset the fork, so they come last. ---

# 2026-09-30 and 2026-10-01, 00:00 UTC: both closed before FORK_BLOCK.
_DAYS = (1_790_726_400, 1_790_812_800)
_CONFIG = load_config(Path(__file__).resolve().parents[1] / "configs" / "uniswap_v3.example.yaml")
_OPENING = Ledger(
    balances={"USDC": Decimal(10_000), "WETH": Decimal(0), "WBTC": Decimal(0)}, gas_eth=Decimal(1)
)


@pytest.fixture(scope="module")
def upstream() -> Rpc:
    """The archive node the fork was made from, read directly."""
    return connect(ETHEREUM_MAINNET, settings=RpcSettings(timeout_seconds=60))


@pytest.fixture(scope="module")
def bars_db(tmp_path_factory: pytest.TempPathFactory, fork_url: str, upstream: Rpc) -> Path:
    path = tmp_path_factory.mktemp("fork-run") / "store.db"
    with open_store(path) as store:
        backfill(upstream, store, _CONFIG, start=_DAYS[0], end=_DAYS[1])
    return path


class _GreedySecondSwap:
    """The chain executor, asking for twice the minimum on the second swap it is handed.

    The quote falls short of that, and the swap is refused with nothing sent:
    the second leg of the first rebalance fails after the first was mined.
    """

    source = FillSource.CHAIN

    def __init__(self, executor: ChainExecutor) -> None:
        self._executor = executor
        self.asked = 0

    def execute(self, swap: SwapIntent, bar: Bar) -> Fill | Rejection:
        self.asked += 1
        if self.asked != 2:
            return self._executor.execute(swap, bar)
        greedy = replace(swap, min_amount_out=swap.min_amount_out * 2)
        refused = self._executor.execute(greedy, bar)
        assert isinstance(refused, Rejection), refused
        return Rejection(swap, refused.reason)


def _fork_run(store, fork: Fork, executor, wallet: ForkWallet, run_id: str):
    return run_fork(
        store,
        _CONFIG,
        executor,
        wallet,
        run_id=run_id,
        start=_DAYS[0],
        end=_DAYS[1],
        opening=_OPENING,
        now=int(time.time()),
        fork_block=fork.fork_block(),
    )


def test_a_fork_run_fills_as_the_quoter_says_and_its_wallet_is_its_ledger(
    fork, w3, bars_db, upstream
):
    executor = ChainExecutor(fork, account=3)
    wallet = ForkWallet(executor, tokens=_CONFIG.tokens, settings=_CONFIG.execution)
    with open_store(bars_db) as store:
        summary = _fork_run(store, fork, executor, wallet, "fork-acceptance")
        print(f"\noutcomes {dict(summary.outcomes)}; holdings {wallet.holdings()}")
        assert summary.decided == 2
        assert store.ledger("fork-acceptance") == wallet.holdings()
        assert store.decision("fork-acceptance", _DAYS[0]).outcome is Outcome.FILLED
        # The first swap meets the pools as a paper run's quote of it does: same block, same answer.
        first = store.fills("fork-acceptance", _DAYS[0])[0]
        bar = load_bar(store, _CONFIG, _DAYS[0]).bar
        pools = {pool.address: pool for pool in _CONFIG.pools}
        quoted = quote_exact_input(
            upstream,
            _USDC,
            tuple(pools[address] for address in first.route),
            first.amount_in,
            block=fill_block(bar, _CONFIG.execution),
        )
        assert first.amount_out == quoted.amount_out

        # Run again: every bar is decided, and nothing is sent.
        nonce = w3.eth.get_transaction_count(executor.address)
        again = _fork_run(store, fork, executor, wallet, "fork-acceptance")
        assert (again.decided, again.already_decided) == (0, 2)
        assert w3.eth.get_transaction_count(executor.address) == nonce


def test_a_second_leg_that_fails_leaves_the_rebalance_partial_with_the_wallet_still_its_ledger(
    fork, w3, bars_db
):
    executor = ChainExecutor(fork, account=4)
    wallet = ForkWallet(executor, tokens=_CONFIG.tokens, settings=_CONFIG.execution)
    greedy = _GreedySecondSwap(executor)
    with open_store(bars_db) as store:
        summary = _fork_run(store, fork, greedy, wallet, "fork-partial")
        decision = store.decision("fork-partial", _DAYS[0])
        print(f"\noutcomes {dict(summary.outcomes)}; {decision.reason}")
        assert decision.outcome is Outcome.PARTIAL
        assert "nothing was sent" in decision.reason
        assert [fill.token_out for fill in store.fills("fork-partial", _DAYS[0])] == ["WETH"]
        assert store.ledger("fork-partial") == wallet.holdings()
        assert store.open_send("fork-partial") is None

        nonce = w3.eth.get_transaction_count(executor.address)
        assert _fork_run(store, fork, greedy, wallet, "fork-partial").decided == 0
        assert w3.eth.get_transaction_count(executor.address) == nonce
