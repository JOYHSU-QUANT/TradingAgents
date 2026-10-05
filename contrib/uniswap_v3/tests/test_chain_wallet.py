"""A fork run's wallet against a scripted anvil: reset to the bar's fill block, given the ledger, read back."""

from __future__ import annotations

from decimal import Decimal

import pytest

from contrib.uniswap_v3.chain.errors import MalformedResponse, RpcConfigError
from contrib.uniswap_v3.chain.fork import DEV_ACCOUNTS, Fork
from contrib.uniswap_v3.chain.swaps import ChainExecutor
from contrib.uniswap_v3.chain.wallet import ForkWallet
from contrib.uniswap_v3.domain.execution import ExecutionSettings, ForkSettings, fill_block
from contrib.uniswap_v3.domain.ledger import Ledger
from contrib.uniswap_v3.ports import Wallet
from contrib.uniswap_v3.tests.fakes.engine import USDC, WBTC, WETH, bar, ledger as _ledger
from contrib.uniswap_v3.tests.fakes.fork import FakeAnvil, balance_key
from contrib.uniswap_v3.tests.fakes.rpc import rpc_over

D = Decimal
_ME = DEV_ACCOUNTS[0]
_SETTINGS = ExecutionSettings()
_TOKENS = (USDC, WETH, WBTC)


def _wallet(anvil: FakeAnvil) -> ForkWallet:
    rpc, _ = rpc_over(anvil.provider)
    return ForkWallet(ChainExecutor(Fork(rpc)), tokens=_TOKENS, settings=_SETTINGS)


def test_a_fork_wallet_is_a_wallet():
    assert isinstance(_wallet(FakeAnvil()), Wallet)


def test_prepare_resets_the_fork_to_the_bars_fill_block_and_gives_the_wallet_the_ledger():
    anvil = FakeAnvil()
    anvil.fund(USDC.address, _ME, 5)
    wallet = _wallet(anvil)
    opening = _ledger("10000", weth="1.5", wbtc="0.00000001", gas="0.25")
    wallet.prepare(bar(0), opening)
    assert anvil.resets == [fill_block(bar(0), _SETTINGS)] == [1_026]
    assert anvil.head == 1_026
    assert anvil.eth_of(_ME) == 25 * 10**16
    assert anvil.balance(USDC.address, _ME) == 10_000 * 10**6
    assert anvil.balance(WETH.address, _ME) == 15 * 10**17
    assert anvil.balance(WBTC.address, _ME) == 1
    assert wallet.holdings() == opening


def test_the_slots_tried_are_put_back_and_each_tokens_slot_is_found_once():
    anvil = FakeAnvil()
    wallet = _wallet(anvil)
    # A word the probe overwrites on its way to USDC's slot 9.
    usdc = USDC.address.lower()
    kept = (usdc, balance_key(_ME, 4))
    real_reset = anvil._reset

    def reset_keeping_a_word(block):
        real_reset(block)
        anvil.storage[kept] = 77

    anvil._reset = reset_keeping_a_word
    wallet.prepare(bar(0), _ledger())
    assert anvil.storage[kept] == 77
    assert len(anvil.calls("eth_getStorageAt")) == 10 + 4 + 1
    wallet.prepare(bar(1), _ledger("1"))
    assert len(anvil.calls("eth_getStorageAt")) == 15
    assert wallet.holdings() == _ledger("1")


def test_a_token_whose_balances_are_not_found_cannot_be_given_to_the_wallet():
    anvil = FakeAnvil()
    anvil.balance_slots[WBTC.address.lower()] = 64
    with pytest.raises(RpcConfigError, match="the balances of WBTC are not in a mapping declared"):
        _wallet(anvil).prepare(bar(0), _ledger())


def test_a_fork_that_is_not_where_it_was_reset_to_is_refused():
    anvil = FakeAnvil()
    real_reset = anvil._reset
    anvil._reset = lambda block: real_reset(block + 1)
    with pytest.raises(MalformedResponse, match="reset to block 1026, and is at block 1027"):
        _wallet(anvil).prepare(bar(0), _ledger())


def test_a_ledger_without_a_token_of_the_wallets_is_refused_before_the_fork_is_touched():
    anvil = FakeAnvil()
    wallet = _wallet(anvil)
    with pytest.raises(ValueError, match=r"the ledger holds no balance of \['WBTC'\]"):
        wallet.prepare(bar(0), Ledger(balances={"USDC": D(1), "WETH": D(0)}, gas_eth=D(1)))
    assert anvil.resets == []


def test_a_fork_wallet_is_a_chain_executors():
    with pytest.raises(ValueError, match="a fork wallet is a chain executor's"):
        ForkWallet(object(), tokens=_TOKENS, settings=_SETTINGS)


def test_a_fork_run_signs_as_any_of_anvils_dev_accounts_and_no_other():
    # domain/ cannot import chain/: the bound is written twice, and held together here.
    assert ForkSettings(account=len(DEV_ACCOUNTS) - 1).account == len(DEV_ACCOUNTS) - 1
    with pytest.raises(ValueError, match="account must be an integer from 0 to 9"):
        ForkSettings(account=len(DEV_ACCOUNTS))


def test_the_fork_names_the_block_it_was_forked_at():
    anvil = FakeAnvil()
    fork = Fork(rpc_over(anvil.provider)[0])
    assert fork.fork_block() == 99
    anvil.node_info["forkConfig"]["forkBlockNumber"] = "99"
    with pytest.raises(MalformedResponse, match="names the fork block as '99'"):
        fork.fork_block()


@pytest.mark.parametrize(
    ("call", "match"),
    [
        (lambda rpc: rpc.set_balance(_ME, -1), "a balance is a non-negative integer of wei"),
        (lambda rpc: rpc.set_storage(_ME, bytes(31), 0), "a storage slot is 32 bytes"),
        (lambda rpc: rpc.set_storage(_ME, bytes(32), 2**256), "fits 256 bits"),
        (lambda rpc: rpc.storage_at(_ME, b"x", block=1), "a storage slot is 32 bytes"),
    ],
)
def test_an_anvil_write_that_cannot_be_made_is_refused_before_it_is_sent(call, match):
    anvil = FakeAnvil()
    rpc, _ = rpc_over(anvil.provider)
    with pytest.raises(ValueError, match=match):
        call(rpc)
    assert anvil.provider.requests == []
