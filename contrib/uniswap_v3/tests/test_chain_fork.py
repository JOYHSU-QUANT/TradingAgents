"""The fork guard: where a fork may be, what it must answer, and which accounts sign on it."""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from contrib.uniswap_v3.chain import fork as fork_module
from contrib.uniswap_v3.chain.errors import NotAFork, RpcConfigError, RpcRejected
from contrib.uniswap_v3.chain.fork import (
    DEFAULT_FORK_URL,
    DEV_ACCOUNTS,
    Fork,
    _is_loopback,
    dev_account,
    open_fork,
    require_anvil,
)
from contrib.uniswap_v3.chain.rpc import Rpc, RpcSettings, http_provider_at
from contrib.uniswap_v3.tests.fakes.fork import FORK_URL, FakeAnvil
from contrib.uniswap_v3.tests.fakes.rpc import answering, closed_port, rpc_over


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


def test_the_url_a_fork_was_made_from_is_kept_out_of_its_errors_from_then_on():
    rpc, _ = rpc_over(FakeAnvil().provider)
    require_anvil(rpc)
    # A fork's error quoting where it reads from, its key alone and not as a URL.
    key = FORK_URL.rsplit("/", 1)[-1]
    failing, _ = rpc_over(answering({"error": {"code": -32000, "message": f"upstream {key} failed"}}))
    with pytest.raises(RpcRejected) as caught:
        failing.latest_header()
    assert key not in str(caught.value) and "<redacted>" in str(caught.value)


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


def _opening(monkeypatch, anvil: FakeAnvil) -> dict:
    """``open_fork`` builds its provider on ``anvil``; what it was asked for is kept."""
    made: dict = {}

    def provider(url, **kwargs):
        made.update(url=url, **kwargs)
        return anvil.provider

    monkeypatch.setattr(fork_module, "http_provider_at", provider)
    return made


def test_open_fork_opens_an_anvil_fork_at_a_loopback_address_directly_past_any_proxy(monkeypatch):
    anvil = FakeAnvil()
    made = _opening(monkeypatch, anvil)
    fork = open_fork(1, url="http://127.0.0.1:9545")
    assert isinstance(fork, Fork) and fork.rpc.chain_id == 1
    assert made["url"] == "http://127.0.0.1:9545"
    # A proxy named in the environment could forward "loopback" anywhere.
    assert made["direct"] is True
    assert anvil.provider.chain_checks == 1 and anvil.calls("anvil_nodeInfo")


def test_open_fork_refuses_another_chain_and_a_node_that_is_not_a_fork(monkeypatch):
    anvil = FakeAnvil()
    _opening(monkeypatch, anvil)
    with pytest.raises(RpcConfigError, match="the node is on chain 1, and chain 5 was expected"):
        open_fork(5)
    anvil.node_info = None
    with pytest.raises(NotAFork):
        open_fork(1)


def test_a_fork_url_in_the_http_stacks_logs_is_scrubbed_even_before_it_is_known(caplog):
    # A node's answer can be logged as it arrives, before anvil_nodeInfo is read.
    rpc, _ = rpc_over(FakeAnvil().provider)
    require_anvil(rpc)
    unknown = "https://never-registered.invalid/an-upstream-key-1"
    with caplog.at_level(logging.DEBUG, logger="web3"):
        logging.getLogger("web3.manager.RequestManager").debug(
            "response: %s", {"forkConfig": {"forkUrl": unknown}}
        )
    assert "an-upstream-key-1" not in caplog.text and "<url>" in caplog.text


class _ChainIdNode(BaseHTTPRequestHandler):
    """A node on this machine that answers every JSON-RPC request with chain 1."""

    def do_POST(self):  # noqa: N802 (the name http.server calls)
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        body = json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": "0x1"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def test_a_direct_provider_reaches_a_loopback_node_whatever_proxy_the_environment_names(
    monkeypatch,
):
    # Every proxy variable requests reads points at a port nothing listens on: a
    # request that took any of them would fail to connect.
    dead = f"http://127.0.0.1:{closed_port()}"
    for name in ("ALL_PROXY", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "http_proxy", "https_proxy"):
        monkeypatch.setenv(name, dead)
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    server = HTTPServer(("127.0.0.1", 0), _ChainIdNode)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        settings = RpcSettings(attempts=1)
        direct = http_provider_at(url, settings=settings, direct=True)
        Rpc(direct, 1, settings=settings).verify_chain()
        assert url not in str(direct)
    finally:
        server.shutdown()
        server.server_close()


def test_a_fork_is_a_setup_fault_like_any_other():
    assert issubclass(NotAFork, RpcConfigError)
