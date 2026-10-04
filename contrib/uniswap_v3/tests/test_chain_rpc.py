"""The connection: what it reads, what it retries, and what it never says.

The secret is the URL. Each test that provokes a failure also reads the
exception, and the log where one is written, for the key.
"""

from __future__ import annotations

import logging

import pytest
import requests

from contrib.uniswap_v3.chain.errors import (
    BlockNotFound,
    CallReverted,
    ChainError,
    MalformedResponse,
    RpcConfigError,
    RpcRejected,
    RpcUnavailable,
)
from contrib.uniswap_v3.chain.pool_price import _POOL_ABI, read_slot0, read_twap_tick
from contrib.uniswap_v3.chain.rpc import BlockHeader, RpcSettings, connect, http_provider
from contrib.uniswap_v3.constants import ETHEREUM_MAINNET, POOLS
from contrib.uniswap_v3.tests.fakes.rpc import (
    ReplayProvider,
    ScriptedProvider,
    answering,
    block_result,
    encoded,
    rpc_over,
)
from contrib.uniswap_v3.tests.fixtures import BLOCK, CASSETTE

_POOL = POOLS[ETHEREUM_MAINNET]["USDC/WETH-500"]
_KEY = "SECRETKEY-0123456789"


def _http_error(status: int) -> requests.HTTPError:
    response = requests.Response()
    response.status_code = status
    return requests.HTTPError(
        f"{status} Client Error for url: https://node.example/v2/{_KEY}", response=response
    )


def _failing(*failures: Exception, then=None) -> ScriptedProvider:
    """Raises ``failures`` in turn, one per request, and answers ``then`` after them."""
    remaining = list(failures)

    def respond(method, params):
        if remaining:
            raise remaining.pop(0)
        return then

    return ScriptedProvider(respond)


# --- connect ---------------------------------------------------------------


def test_connect_refuses_an_environment_without_the_variable():
    with pytest.raises(RpcConfigError, match="ETH_RPC_URL is not set"):
        connect(ETHEREUM_MAINNET, env={})
    with pytest.raises(RpcConfigError, match="MY_RPC is not set"):
        connect(ETHEREUM_MAINNET, settings=RpcSettings(url_env="MY_RPC"), env={"ETH_RPC_URL": "x"})


@pytest.mark.parametrize("value", [_KEY, f"wss://node.example/v2/{_KEY}", f"https:///v2/{_KEY}"])
def test_connect_refuses_a_value_that_is_not_an_http_url_without_quoting_it(value):
    with pytest.raises(RpcConfigError, match="must hold an http") as caught:
        connect(ETHEREUM_MAINNET, env={"ETH_RPC_URL": value})
    assert _KEY not in str(caught.value)


def test_a_failed_connection_leaks_the_url_into_neither_the_error_nor_the_log(caplog):
    caplog.set_level(logging.DEBUG)
    # Nothing listens on the discard port, so the connection is refused.
    settings = RpcSettings(timeout_seconds=2, attempts=1)
    with pytest.raises(RpcUnavailable) as caught:
        connect(
            ETHEREUM_MAINNET, settings=settings, env={"ETH_RPC_URL": f"http://127.0.0.1:9/v2/{_KEY}"}
        )
    assert "the chain ID failed after 1 attempt(s)" in str(caught.value)
    assert _KEY not in str(caught.value)
    # The exception that held the URL is not reachable from this one.
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert _KEY not in caplog.text
    # The records that would have held it were written, scrubbed.
    assert "<redacted>" in caplog.text


def test_a_key_in_the_query_string_is_scrubbed_too():
    # What urllib3 quotes when a connection fails is the path and query alone.
    http_provider(env={"ETH_RPC_URL": f"https://node.example/rpc?apikey={_KEY}"})
    failure = requests.ConnectionError(f"Max retries exceeded with url: /rpc?apikey={_KEY}")
    rpc, _ = rpc_over(_failing(failure), attempts=1)
    with pytest.raises(RpcUnavailable) as caught:
        rpc.header(7)
    assert _KEY not in str(caught.value) and "<redacted>" in str(caught.value)


@pytest.mark.parametrize(
    "logger", ["urllib3.connection", "urllib3.util.retry", "web3.providers.HTTPProvider", "requests"]
)
def test_every_logger_of_the_http_stack_is_scrubbed_and_no_other(logger, caplog):
    caplog.set_level(logging.DEBUG)
    url = f"https://node.example/v2/{_KEY}"
    http_provider(env={"ETH_RPC_URL": url})
    logging.getLogger(logger).warning("Failed to parse headers (url=%s)", url)
    logging.getLogger(logger).debug("Incremented Retry for (url='%s')", f"/v2/{_KEY}")
    assert _KEY not in caplog.text and caplog.text.count("<redacted>") == 2
    # The application's own records are left as they were written.
    logging.getLogger("contrib.uniswap_v3.somewhere").warning("see https://docs.example/page")
    assert "https://docs.example/page" in caplog.text


def test_settings_refuse_values_that_cannot_work():
    for bad in (
        {"url_env": " "},
        {"timeout_seconds": 0},
        {"timeout_seconds": True},
        {"backoff_seconds": -1},
        {"attempts": 0},
        {"attempts": 1.5},
    ):
        with pytest.raises(ValueError):
            RpcSettings(**bad)


# --- the chain check -------------------------------------------------------


def test_verify_chain_passes_on_the_expected_chain_and_refuses_another():
    rpc, _ = rpc_over(ReplayProvider(CASSETTE))
    rpc.verify_chain()
    assert rpc.chain_id == ETHEREUM_MAINNET

    other, _ = rpc_over(answering({"result": "0x5"}))
    with pytest.raises(RpcConfigError, match="on chain 5, and chain 1 was expected"):
        other.verify_chain()


# --- headers ---------------------------------------------------------------


def test_header_reads_a_recorded_block_and_the_latest():
    rpc, _ = rpc_over(ReplayProvider(CASSETTE))
    assert rpc.header(BLOCK) == BlockHeader(
        number=BLOCK, timestamp=1_693_066_895, base_fee_wei=21_721_091_641
    )
    latest = rpc.header()
    assert latest.number > BLOCK and latest.timestamp > 1_693_066_895


def test_header_of_a_block_before_london_has_no_base_fee():
    rpc, _ = rpc_over(answering({"result": block_result(7, 1_500_000_000, base_fee=None)}))
    assert rpc.header(7) == BlockHeader(number=7, timestamp=1_500_000_000, base_fee_wei=None)


def test_header_raises_when_the_node_has_no_such_block():
    rpc, _ = rpc_over(answering({"result": None}))
    with pytest.raises(BlockNotFound, match="does not have block 7"):
        rpc.header(7)


def test_header_refuses_another_block_than_the_one_asked_for():
    rpc, _ = rpc_over(answering({"result": block_result(8, 1_500_000_000)}))
    with pytest.raises(MalformedResponse, match="asked for block 7, and the node sent block 8"):
        rpc.header(7)


@pytest.mark.parametrize(
    "response",
    [
        {"result": {"number": "0x7", "timestamp": "soon"}},
        {"result": {"number": "0x7"}},
        {"result": "0x7"},
        {},
    ],
)
def test_header_refuses_a_reply_of_the_wrong_shape(response):
    rpc, _ = rpc_over(answering(response))
    with pytest.raises(MalformedResponse):
        rpc.header(7)


@pytest.mark.parametrize("block", [-1, 1.0, "7", True])
def test_a_block_is_a_non_negative_integer(block):
    rpc, _ = rpc_over(ReplayProvider(CASSETTE))
    with pytest.raises(ValueError, match="non-negative integer"):
        rpc.header(block)
    with pytest.raises(ValueError, match="non-negative integer"):
        read_slot0(rpc, _POOL, block)


# --- retries ---------------------------------------------------------------


@pytest.mark.parametrize(
    "failure",
    [
        requests.ConnectionError("refused"),
        requests.Timeout("timed out"),
        requests.exceptions.ChunkedEncodingError("connection broken"),
        _http_error(429),
        _http_error(503),
    ],
)
def test_a_transient_failure_is_retried_with_a_doubling_wait(failure):
    provider = _failing(failure, failure, then={"result": block_result(7, 1_500_000_000)})
    rpc, waits = rpc_over(provider)
    assert rpc.header(7).timestamp == 1_500_000_000
    assert waits == [0.5, 1.0]
    assert len(provider.requests) == 3


def test_a_failure_that_outlasts_the_attempts_raises_without_the_url():
    # A URL no provider was built for: anything shaped like one is scrubbed.
    failure = requests.ConnectionError(
        "Max retries exceeded with url: https://elsewhere.example/v9/NEVER-REGISTERED (Caused by x)"
    )
    provider = _failing(failure, failure, failure)
    rpc, waits = rpc_over(provider)
    with pytest.raises(RpcUnavailable, match=r"block 7 failed after 3 attempt\(s\)") as caught:
        rpc.header(7)
    assert "NEVER-REGISTERED" not in str(caught.value) and "url: <url> (Caused" in str(caught.value)
    assert caught.value.__context__ is None
    # No wait after the last attempt.
    assert waits == [0.5, 1.0]


@pytest.mark.parametrize(
    ("status", "kind"),
    [(400, RpcRejected), (413, RpcRejected), (401, RpcConfigError), (403, RpcConfigError)],
)
def test_an_http_refusal_is_not_retried_and_a_refused_key_is_a_setup_fault(status, kind):
    provider = _failing(_http_error(status))
    rpc, waits = rpc_over(provider)
    with pytest.raises(kind, match=f"HTTPError: {status}") as caught:
        rpc.header(7)
    assert type(caught.value) is kind
    assert _KEY not in str(caught.value)
    assert caught.value.__context__ is None
    assert waits == [] and len(provider.requests) == 1


def test_a_node_error_is_not_retried():
    provider = answering({"error": {"code": -32000, "message": "missing trie node"}})
    rpc, waits = rpc_over(provider)
    with pytest.raises(RpcRejected, match="missing trie node"):
        read_slot0(rpc, _POOL, 7)
    assert waits == [] and len(provider.requests) == 1


# --- calls -----------------------------------------------------------------


def test_a_revert_raises_with_the_contracts_reason():
    revert = {
        "error": {
            "code": 3,
            "message": "execution reverted: OLD",
            "data": "0x08c379a0" + encoded(["string"], ["OLD"])[2:],
        }
    }
    rpc, waits = rpc_over(answering(revert))
    with pytest.raises(CallReverted, match=r"observe\(\) on 0x88e6.* at block 7 reverted .*OLD"):
        read_twap_tick(rpc, _POOL, 7)
    assert waits == []


def test_a_call_to_an_address_without_code_is_refused():
    rpc, _ = rpc_over(answering({"result": "0x"}))
    with pytest.raises(MalformedResponse, match="slot0.*could not be read"):
        read_slot0(rpc, _POOL, 7)


@pytest.mark.parametrize(
    ("address", "function", "args"),
    [
        # Not an EIP-55 checksum.
        (_POOL.address.lower(), "slot0", ()),
        ("0x88e6", "slot0", ()),
        (_POOL.address, "slot1", ()),
        (_POOL.address, "slot0", (1,)),
        (_POOL.address, "observe", ("soon",)),
    ],
)
def test_a_call_that_does_not_fit_the_abi_is_the_callers_error_and_sends_nothing(
    address, function, args
):
    provider = ReplayProvider(CASSETTE)
    rpc, _ = rpc_over(provider)
    with pytest.raises(Exception) as caught:  # noqa: B017, PT011 - whatever web3 raises
        rpc.call(address, _POOL_ABI, function, args, block=BLOCK)
    # Not a chain error: nothing a caller should skip a bar over.
    assert not isinstance(caught.value, ChainError)
    assert provider.requests == []


def test_a_call_asks_the_node_once_and_at_the_block_named():
    provider = ReplayProvider(CASSETTE)
    rpc, _ = rpc_over(provider)
    read_slot0(rpc, _POOL, BLOCK)
    # One request: web3's own chain-ID check before each call is switched off.
    [(method, params)] = provider.requests
    assert method == "eth_call"
    assert params[0]["to"] == _POOL.address and params[1] == hex(BLOCK)
