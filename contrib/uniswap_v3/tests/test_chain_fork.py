"""The fork guard: where a fork may be, what it must answer, and which accounts sign on it."""

from __future__ import annotations

import pytest

from contrib.uniswap_v3.chain.errors import NotAFork, RpcConfigError
from contrib.uniswap_v3.chain.fork import (
    DEFAULT_FORK_URL,
    DEV_ACCOUNTS,
    _is_loopback,
    dev_account,
    open_fork,
    require_anvil,
)
from contrib.uniswap_v3.tests.fakes.fork import FakeAnvil
from contrib.uniswap_v3.tests.fakes.rpc import answering, rpc_over


@pytest.mark.parametrize("index", range(10))
def test_each_dev_account_is_derived_from_the_public_mnemonic_as_anvil_lists_it(index):
    assert dev_account(index).address == DEV_ACCOUNTS[index]


@pytest.mark.parametrize("index", [-1, 10, True, "0", 1.0])
def test_only_the_ten_dev_accounts_can_be_asked_for(index):
    with pytest.raises(ValueError, match="numbered 0 to 9"):
        dev_account(index)


@pytest.mark.parametrize(
    "url",
    [
        DEFAULT_FORK_URL,
        "http://127.0.0.2:9000",
        "http://[::1]:8545",
        "https://127.0.0.1:8545/",
    ],
)
def test_a_url_on_this_machine_is_a_place_a_fork_may_be(url):
    assert _is_loopback(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://eth-mainnet.g.alchemy.com/v2/KEY",
        # A name, though it usually means this machine: a hosts file can point it elsewhere.
        "http://localhost:8545",
        "http://10.0.0.5:8545",
        "http://192.168.1.2:8545",
        "http://localhost.example.com:8545",
        "http://127.0.0.1.example.com:8545",
        "ws://127.0.0.1:8545",
        "127.0.0.1:8545",
        "http://127.0.0.1:99999",
        "",
    ],
)
def test_any_other_url_is_refused_before_anything_is_asked_of_it(url):
    assert not _is_loopback(url)
    with pytest.raises(NotAFork, match="on this machine") as caught:
        open_fork(1, url=url)
    # The URL is not quoted: it could be a provider's, with its key.
    assert "KEY" not in str(caught.value)


def test_a_node_whose_anvil_node_info_names_a_fork_url_is_taken_for_an_anvil_fork():
    rpc, _ = rpc_over(FakeAnvil().provider)
    require_anvil(rpc)


@pytest.mark.parametrize(
    "info",
    [
        # A fresh anvil, forked from nothing.
        {"hardFork": "prague"},
        {"forkConfig": {}},
        {"forkConfig": {"forkUrl": ""}},
        {"forkConfig": {"forkUrl": None}},
        # A proxy that answers the method with nothing in it.
        {},
    ],
)
def test_a_node_that_names_no_fork_url_is_not_a_fork_and_the_answer_is_not_quoted(info):
    anvil = FakeAnvil()
    anvil.node_info = info
    rpc, _ = rpc_over(anvil.provider)
    with pytest.raises(NotAFork, match="without the URL it was forked from") as caught:
        require_anvil(rpc)
    assert "forkConfig" not in str(caught.value)


@pytest.mark.parametrize(
    "response",
    [
        {"error": {"code": -32601, "message": "the method anvil_nodeInfo does not exist"}},
        {"result": "anvil"},
    ],
)
def test_a_node_that_does_not_answer_anvil_node_info_is_not_a_fork(response):
    rpc, _ = rpc_over(answering(response))
    with pytest.raises(NotAFork, match="only an anvil fork is signed for"):
        require_anvil(rpc)


def test_a_fork_is_a_setup_fault_like_any_other():
    assert issubclass(NotAFork, RpcConfigError)
