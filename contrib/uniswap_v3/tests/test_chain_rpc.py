"""The connection: what it reads, what it retries, and what it never says.

The secret is the URL. The tests that provoke a transport failure read the
exception for the key, and the ones that write a log record read the log.
"""

from __future__ import annotations

import logging
import socket

import pytest
import requests
from web3 import HTTPProvider
from web3.exceptions import InvalidAddress, MismatchedABI

from contrib.uniswap_v3.chain import errors
from contrib.uniswap_v3.chain.errors import (
    BlockNotFound,
    CallReverted,
    ChainError,
    InsufficientLiquidity,
    MalformedResponse,
    RpcConfigError,
    RpcRejected,
    RpcUnavailable,
    TransientChainError,
    UnansweredRead,
)
from contrib.uniswap_v3.chain.pool_price import _POOL_ABI, read_slot0, read_twap_tick
from contrib.uniswap_v3.chain.quoter import _QUOTER_ABI
from contrib.uniswap_v3.chain.rpc import BlockHeader, Rpc, RpcSettings, connect, http_provider
from contrib.uniswap_v3.constants import ETHEREUM_MAINNET, POOLS, QUOTER_V2
from contrib.uniswap_v3.tests.fakes.rpc import (
    CassetteMiss,
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
    """Raises ``failures`` in turn, one per read, and answers ``then`` after them.

    The chain check is not a read: the provider answers it itself.
    """
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


def _closed_port() -> int:
    """A loopback port nothing listens on: one the system just handed out and took back."""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def test_a_failed_connection_leaks_the_url_into_neither_the_error_nor_the_log(caplog):
    caplog.set_level(logging.DEBUG)
    settings = RpcSettings(timeout_seconds=2, attempts=1)
    url = f"http://127.0.0.1:{_closed_port()}/v2/{_KEY}"
    with pytest.raises(RpcUnavailable) as caught:
        connect(ETHEREUM_MAINNET, settings=settings, env={"ETH_RPC_URL": url})
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


def test_a_logged_traceback_and_an_unformattable_record_are_scrubbed(caplog):
    caplog.set_level(logging.DEBUG)
    http_provider(env={"ETH_RPC_URL": f"https://node.example/v2/{_KEY}"})
    http_provider(env={"ETH_RPC_URL": "https://node.example/v2/key-spelled-out-in-a-source-line"})
    log = logging.getLogger("urllib3.connection")
    try:
        raise requests.ConnectionError(f"Max retries exceeded with url: /v2/{_KEY}")
    except requests.ConnectionError:
        # The stack is printed as source lines, the first line of this call
        # among them: the comment has to stay on it.
        log.warning("the request failed", exc_info=True, stack_info=True)  # key-spelled-out-in-a-source-line
    # More placeholders than arguments: logging would print the arguments raw.
    log.warning("bad %s %s", f"/v2/{_KEY}")
    assert _KEY not in caplog.text and "key-spelled-out" not in caplog.text
    assert "stack_info=True)  # <redacted>" in caplog.text
    assert "ConnectionError: Max retries exceeded with url: <redacted>" in caplog.text
    assert "<a log message that could not be formatted>" in caplog.text


def test_scrubbing_survives_another_record_factory_being_installed(caplog):
    caplog.set_level(logging.DEBUG)
    ours = logging.getLogRecordFactory()
    try:
        logging.setLogRecordFactory(logging.LogRecord)
        http_provider(env={"ETH_RPC_URL": f"https://node.example/v2/{_KEY}"})
        logging.getLogger("urllib3.connectionpool").debug("POST /v2/%s HTTP/1.1", _KEY)
        assert _KEY not in caplog.text and "POST <redacted> HTTP/1.1" in caplog.text
    finally:
        logging.setLogRecordFactory(ours)


@pytest.mark.parametrize(
    ("url", "quoted"),
    [
        # A short key: the path it sits in is long enough to be registered.
        ("https://node.example/v2/abc12", "POST /v2/abc12 HTTP/1.1"),
        # One segment of the path, quoted without the rest.
        ("https://node.example/v2/longer-key-1/eth", "the key longer-key-1 was refused"),
        ("https://user:hunter2-pass@node.example/", "auth hunter2-pass refused"),
        ("https://node.example/rpc?apikey=query-key-1&x=1", "sent query-key-1 to the node"),
    ],
)
def test_the_pieces_of_a_url_that_are_quoted_alone_are_secret_too(url, quoted):
    http_provider(env={"ETH_RPC_URL": url})
    rpc, _ = rpc_over(_failing(requests.ConnectionError(quoted)), attempts=1)
    with pytest.raises(RpcUnavailable) as caught:
        rpc.header(7)
    assert "<redacted>" in str(caught.value)
    for secret in ("abc12", "longer-key-1", "hunter2-pass", "query-key-1"):
        assert secret not in str(caught.value)


def test_a_provider_built_by_hand_has_its_url_made_secret_all_the_same():
    url = f"http://127.0.0.1:{_closed_port()}/v2/BUILT-BY-HAND-KEY"
    provider = HTTPProvider(
        url, request_kwargs={"timeout": 2}, exception_retry_configuration=None
    )
    rpc = Rpc(provider, ETHEREUM_MAINNET, settings=RpcSettings(attempts=1))
    with pytest.raises(RpcUnavailable) as caught:
        rpc.header(7)
    assert "BUILT-BY-HAND-KEY" not in str(caught.value) and "<redacted>" in str(caught.value)


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

    other, _ = rpc_over(answering({"result": "0x5"}, chain_id=None))
    with pytest.raises(RpcConfigError, match="on chain 5, and chain 1 was expected"):
        other.verify_chain()


def test_every_read_checks_the_chain_first_until_it_has_passed_once():
    provider = ReplayProvider(CASSETTE)
    rpc, _ = rpc_over(provider)
    assert provider.chain_checks == 0
    rpc.header(BLOCK)
    read_slot0(rpc, _POOL, BLOCK)
    rpc.latest_header()
    assert provider.chain_checks == 1

    # A node on another chain answers no read, however the Rpc was built.
    wrong = ScriptedProvider(lambda method, params: {"result": block_result(7, 1_500_000_000)})
    rpc, _ = rpc_over(wrong, chain_id=5)
    for read in (lambda: rpc.header(7), rpc.latest_header, lambda: read_slot0(rpc, _POOL, 7)):
        with pytest.raises(RpcConfigError, match="on chain 1, and chain 5 was expected"):
            read()
    assert wrong.requests == [] and wrong.chain_checks == 3


# --- headers ---------------------------------------------------------------


def test_header_reads_a_recorded_block_and_the_latest():
    rpc, _ = rpc_over(ReplayProvider(CASSETTE))
    assert rpc.header(BLOCK) == BlockHeader(
        number=BLOCK, timestamp=1_693_066_895, base_fee_wei=21_721_091_641
    )
    latest = rpc.latest_header()
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
        _http_error(500),
        _http_error(503),
        # What a proxy in front of a node answers when the node is down.
        _http_error(522),
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
    provider = answering({"error": {"code": -32602, "message": "invalid argument 0"}})
    rpc, waits = rpc_over(provider)
    with pytest.raises(RpcRejected, match="the node refused slot0.*invalid argument 0"):
        read_slot0(rpc, _POOL, 7)
    assert waits == [] and len(provider.requests) == 1


_RATE_LIMITS = [
    # Infura's shape: web3 reads the mapping under "data" as a revert.
    {"code": -32005, "message": "daily request count exceeded", "data": {"see": "the dashboard"}},
    {"code": -32005, "message": "limit exceeded", "data": None},
    {"code": 429, "message": "Your app has exceeded its compute units per second capacity."},
]


@pytest.mark.parametrize("error", _RATE_LIMITS)
def test_a_rate_limit_sent_as_a_json_rpc_error_is_retried(error):
    remaining = [{"error": error}, {"error": error}]
    slot0 = encoded(["uint160", "int24", "uint16", "uint16", "uint16", "uint8", "bool"],
                    [2**96, 0, 0, 1, 1, 0, True])
    provider = ScriptedProvider(
        lambda method, params: remaining.pop(0) if remaining else {"result": slot0}
    )
    rpc, waits = rpc_over(provider)
    assert read_slot0(rpc, _POOL, 7).sqrt_price_x96 == 2**96
    assert waits == [0.5, 1.0] and len(provider.requests) == 3


@pytest.mark.parametrize("error", _RATE_LIMITS)
def test_a_rate_limit_that_outlasts_the_attempts_is_not_a_revert(error):
    for read in (lambda rpc: read_slot0(rpc, _POOL, 7), lambda rpc: rpc.header(7)):
        rpc, waits = rpc_over(answering({"error": error}))
        with pytest.raises(RpcUnavailable, match=r"was rate-limited on 3 attempt\(s\)") as caught:
            read(rpc)
        assert type(caught.value) is RpcUnavailable and waits == [0.5, 1.0]


@pytest.mark.parametrize(
    ("error", "kind", "message"),
    [
        # A node behind the block asked for: the block is not there yet.
        ({"code": -32000, "message": "header not found"}, BlockNotFound, "the node is behind"),
        ({"code": -32000, "message": "Unknown block"}, BlockNotFound, "the node is behind"),
        # A node that has dropped the block's state: it is not an archive node.
        (
            {"code": -32000, "message": "missing trie node 5f1e (path ) state 0x9a is not available"},
            RpcConfigError,
            "an archive node is needed",
        ),
        (
            {"code": -32000, "message": "historical state 9a2b is pruned", "data": None},
            RpcConfigError,
            "an archive node is needed",
        ),
        (
            {"code": -32000, "message": "historical state 0xab is not available"},
            RpcConfigError,
            "an archive node is needed",
        ),
        (
            {"code": -32000, "message": "state already discarded"},
            RpcConfigError,
            "an archive node is needed",
        ),
        # "Not available" alone is not about state.
        (
            {"code": -32601, "message": "the method eth_call is not available"},
            RpcRejected,
            "the node refused",
        ),
        # An error with no revert in it that web3 nevertheless reads as one.
        ({"code": -32000, "message": "out of gas", "data": None}, RpcRejected, "the node refused"),
    ],
)
def test_a_node_error_is_classed_by_what_the_node_said(error, kind, message):
    provider = answering({"error": error})
    rpc, waits = rpc_over(provider)
    with pytest.raises(kind, match=message) as caught:
        read_twap_tick(rpc, _POOL, 7)
    assert type(caught.value) is kind
    assert waits == [] and len(provider.requests) == 1


def test_the_error_classes_say_what_a_caller_can_do():
    by_action = {
        TransientChainError: {RpcUnavailable, BlockNotFound},
        UnansweredRead: {CallReverted, InsufficientLiquidity, MalformedResponse, RpcRejected},
    }
    assert not by_action[TransientChainError] & by_action[UnansweredRead]
    assert not issubclass(TransientChainError, UnansweredRead)
    assert not issubclass(UnansweredRead, TransientChainError)
    for action, kinds in by_action.items():
        assert all(issubclass(kind, action) for kind in kinds)
    # Every class is in exactly one group; the setup fault is a group of its own.
    leaves = {
        kind
        for kind in vars(errors).values()
        if isinstance(kind, type) and issubclass(kind, ChainError) and not kind.__subclasses__()
    }
    assert leaves == by_action[TransientChainError] | by_action[UnansweredRead] | {RpcConfigError}
    assert not issubclass(RpcConfigError, TransientChainError | UnansweredRead)


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


_QUOTE_WITH_A_LOWERCASE_TOKEN = (
    (_POOL.token0.address.lower(), _POOL.token1.address, 10, 500, 0),
)


@pytest.mark.parametrize(
    ("address", "abi", "function", "args", "kind"),
    [
        # Not an EIP-55 checksum.
        (_POOL.address.lower(), _POOL_ABI, "slot0", (), InvalidAddress),
        ("0x88e6", _POOL_ABI, "slot0", (), InvalidAddress),
        (_POOL.address, _POOL_ABI, "slot1", (), MismatchedABI),
        (_POOL.address, _POOL_ABI, "slot0", (1,), MismatchedABI),
        (_POOL.address, _POOL_ABI, "observe", ("soon",), MismatchedABI),
        # An address inside a tuple argument, which only encoding looks at.
        (
            QUOTER_V2[ETHEREUM_MAINNET],
            _QUOTER_ABI,
            "quoteExactInputSingle",
            _QUOTE_WITH_A_LOWERCASE_TOKEN,
            InvalidAddress,
        ),
    ],
)
def test_a_call_that_does_not_fit_the_abi_is_the_callers_error_and_sends_nothing(
    address, abi, function, args, kind
):
    provider = ReplayProvider(CASSETTE)
    rpc, _ = rpc_over(provider)
    # web3's own exception, not a chain error: nothing to skip a bar over.
    with pytest.raises(kind) as caught:
        rpc.call(address, abi, function, args, block=BLOCK)
    assert not isinstance(caught.value, ChainError)
    assert provider.requests == [] and provider.chain_checks == 0


def test_a_request_the_cassette_lacks_fails_the_test_rather_than_becoming_a_chain_error():
    rpc, _ = rpc_over(ReplayProvider(CASSETTE))
    with pytest.raises(CassetteMiss, match="not in the cassette: eth_getBlockByNumber"):
        rpc.header(5)


def test_the_connection_can_be_asked_whether_it_is_up():
    class Up(ScriptedProvider):
        def is_connected(self, show_traceback=False):
            return True

    rpc, _ = rpc_over(Up(lambda method, params: {"result": "0x1"}))
    assert rpc._w3.is_connected() is True


def test_a_call_asks_the_node_once_and_at_the_block_named():
    provider = ReplayProvider(CASSETTE)
    rpc, _ = rpc_over(provider)
    read_slot0(rpc, _POOL, BLOCK)
    read_slot0(rpc, _POOL, BLOCK)
    # One request a call, and one chain check in all: web3's own check
    # before each call is switched off.
    [(method, params), again] = provider.requests
    assert (method, params) == again and method == "eth_call"
    assert params[0]["to"] == _POOL.address and params[1] == hex(BLOCK)
    assert provider.chain_checks == 1
