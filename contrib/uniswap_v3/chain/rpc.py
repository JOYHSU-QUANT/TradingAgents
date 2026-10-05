"""The connection to a node, and the reads the rest of ``chain/`` is built on.

:func:`connect` reads the endpoint URL from an environment variable, named
by :class:`RpcSettings` (``ETH_RPC_URL`` unless told otherwise).
:class:`Rpc` then offers the latest block's header, the pending block's
(whose time is the node's clock), the header of a named block, a contract
call at a named block, and what an anvil node says of itself. Before its first read it asks
the node for its chain ID and refuses a node on another chain.

For a wallet that signs, it also offers what sending takes: an account's
next nonce, a gas estimate, the priority fee, a receipt, and the sending of
a signed transaction. The send alone is never tried again: the caller holds
the transaction's hash, and looks for its receipt instead.

Failures, by what the node or the transport said:

- A connection error, a timeout, a response cut short, a body that is not
  JSON (a gateway's error page), an HTTP 408, 425, 429 or 5xx and a JSON-RPC
  rate-limit error (code -32005 or 429) are tried again, waiting twice as
  long each time. When the attempts run out the read raises
  :class:`~.errors.RpcUnavailable`.
- "header not found" and its like, which a node behind the head answers,
  raise :class:`~.errors.BlockNotFound`.
- An HTTP 401 or 403, a URL that cannot be requested, and a node that says
  it no longer has the state asked for (it is not an archive node), raise
  :class:`~.errors.RpcConfigError`.
- A revert raises :class:`~.errors.CallReverted`, any other error the node
  answers with :class:`~.errors.RpcRejected`, and a reply that cannot be
  decoded :class:`~.errors.MalformedResponse`.

The URL is a secret, since it ends in the API key. It is never put in an
exception or a log line. The text of every exception caught here is scrubbed
before it is quoted, and the error that replaces it is raised once the
``except`` block is over, so the original is neither its cause nor its
context. Every log record of ``web3``, ``urllib3`` and ``requests``, which
write the URL or its path, has its message and its traceback scrubbed as it
is created. What counts as secret is the URL and, where they are six
characters or longer, its path and each segment of it, its query and each
value in it, and its password, each as written and percent-decoded; the
host name is not. The URL an anvil fork was made from, which it names in
``anvil_nodeInfo``, is made a secret the same way once it is read; until
then, as anything shaped like a URL, it is scrubbed whole.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import traceback
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, TypeGuard, TypeVar
from urllib.parse import parse_qsl, unquote, urlsplit

import requests
import urllib3
from web3 import HTTPProvider, Web3
from web3.exceptions import (
    BlockNotFound as _Web3BlockNotFound,
    ContractLogicError,
    TransactionNotFound,
    Web3RPCError,
)
from web3.providers.base import BaseProvider
from web3.types import RPCEndpoint

from .errors import (
    BlockNotFound,
    CallReverted,
    ChainError,
    MalformedResponse,
    RpcConfigError,
    RpcRejected,
    RpcUnavailable,
)

__all__ = [
    "DEFAULT_URL_ENV",
    "BlockHeader",
    "Log",
    "Receipt",
    "Rpc",
    "RpcSettings",
    "connect",
    "http_provider",
    "http_provider_at",
]

DEFAULT_URL_ENV: Final = "ETH_RPC_URL"

# A key the endpoint does not accept: no later request will fare better.
_UNAUTHORISED_STATUS: Final = frozenset({401, 403})
# Statuses that say "not now" rather than "no"; every 5xx is one as well.
_RETRY_STATUS: Final = frozenset({408, 425, 429})
# How much of a caught exception's text an error quotes. A reply that is not
# JSON is quoted whole by web3, and it can be a page of HTML.
_MAX_QUOTE: Final = 300
# JSON-RPC error codes. -32005 is EIP-1474's "limit exceeded"; some providers
# send the HTTP status, 429, as the code. 3 is a revert.
_RATE_LIMIT_CODES: Final = frozenset({-32005, 429})
_REVERT_CODE: Final = 3
# What a node says, under the catch-all code -32000, when it is behind the
# block asked for, and when it no longer keeps that block's state.
_BEHIND: Final = ("header not found", "block not found", "unknown block")
_NO_STATE: Final = ("missing trie node", "pruned", "pruning", "discarded", "archive")
_HASH_BYTES: Final = 32
# The packages under a request, whose loggers write the URL or its path:
# web3's provider and urllib3's connection pool at DEBUG, urllib3's
# connection at WARNING.
_SCRUBBED_LOGGERS: Final = frozenset({"web3", "urllib3", "requests"})
_URL: Final = re.compile(r"https?://[^\s'\"]+")
# A piece of the URL shorter than this (``v2``) is not a secret, and
# scrubbing it would eat ordinary text.
_MIN_SECRET: Final = 6

_T = TypeVar("_T")


class _Redactor:
    """Removes the registered URLs, and anything shaped like a URL, from text."""

    def __init__(self) -> None:
        self._secrets: list[str] = []
        self._factory: Callable[..., logging.LogRecord] | None = None

    def register(self, url: str) -> None:
        """Treat ``url`` as secret, with the pieces of it that are quoted alone."""
        pieces = {url}
        try:
            parts = urlsplit(url)
            pieces.update({parts.path, parts.query, parts.password or ""})
        except ValueError:
            # Not a URL that can be taken apart; the whole of it is still secret.
            parts = None
        if parts is not None:
            pieces.update(parts.path.split("/"))
            pieces.update(value for _, value in parse_qsl(parts.query))
            if parts.query:
                pieces.add(f"{parts.path}?{parts.query}")
        # A piece can be quoted percent-decoded as well as it is written.
        pieces.update({unquote(piece) for piece in pieces})
        known = set(self._secrets) | {piece for piece in pieces if len(piece) >= _MIN_SECRET}
        # Longest first, so the whole URL goes before the key inside it.
        self._secrets = sorted(known, key=len, reverse=True)
        self.scrub_logs()

    def scrub(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, "<redacted>")
        return _URL.sub("<url>", text)

    def scrub_logs(self) -> None:
        """Scrub the HTTP stack's log records from here on.

        Done where records are created rather than with a filter per logger:
        a filter sees only its own logger's records, so each logger that
        writes the URL would have to be found and named. The record factory
        is one per process and stays in place. This is called again before
        every request, and wraps whatever factory has replaced it since.
        """
        create = logging.getLogRecordFactory()
        if create is self._factory:
            return

        def scrubbed(*args: Any, **kwargs: Any) -> logging.LogRecord:
            record = create(*args, **kwargs)
            if record.name.split(".", 1)[0] in _SCRUBBED_LOGGERS:
                self._scrub_record(record)
            return record

        self._factory = scrubbed
        logging.setLogRecordFactory(scrubbed)

    def _scrub_record(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
        except Exception:
            # Arguments that do not fit the format: they cannot be scrubbed
            # one by one, and logging would print them as they are.
            message = "<a log message that could not be formatted>"
        record.msg, record.args = self.scrub(message), ()
        if record.exc_info:
            trace = "".join(traceback.format_exception(*record.exc_info)).rstrip("\n")
            record.exc_text, record.exc_info = self.scrub(trace), None
        if record.stack_info:
            record.stack_info = self.scrub(record.stack_info)


_REDACTOR: Final = _Redactor()


@dataclass(frozen=True)
class RpcSettings:
    """Where the URL is read from, and how patient a read is.

    ``attempts`` counts the first try. ``backoff_seconds`` is the wait after
    the first failure; each later wait is twice the one before.
    """

    url_env: str = DEFAULT_URL_ENV
    timeout_seconds: float = 10.0
    attempts: int = 3
    backoff_seconds: float = 0.5

    def __post_init__(self) -> None:
        if not isinstance(self.url_env, str) or not self.url_env.strip():
            raise ValueError(f"url_env must be a non-empty string, got {self.url_env!r}")
        for name in ("timeout_seconds", "backoff_seconds"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int | float) or not value >= 0:
                raise ValueError(f"{name} must be a non-negative number, got {value!r}")
        if self.timeout_seconds == 0:
            raise ValueError("timeout_seconds must be above zero")
        if isinstance(self.attempts, bool) or not isinstance(self.attempts, int) or self.attempts < 1:
            raise ValueError(f"attempts must be an integer of at least 1, got {self.attempts!r}")


@dataclass(frozen=True)
class BlockHeader:
    """What the package reads off a block. ``base_fee_wei`` is ``None`` before London.

    ``hash`` is ``0x`` and 64 lowercase hex digits. A block number names
    whichever block the node holds there now; the hash names one block.
    """

    number: int
    hash: str
    timestamp: int
    base_fee_wei: int | None


@dataclass(frozen=True)
class Log:
    """One event a transaction emitted. Every field is ``0x`` and lowercase hex."""

    address: str
    topics: tuple[str, ...]
    data: str


@dataclass(frozen=True)
class Receipt:
    """What the chain says of a mined transaction.

    ``succeeded`` is false for one that reverted, which still paid its gas:
    ``gas_used`` at ``effective_gas_price_wei``.
    """

    tx_hash: str
    block: int
    succeeded: bool
    gas_used: int
    effective_gas_price_wei: int
    logs: tuple[Log, ...]

    @property
    def gas_cost_wei(self) -> int:
        """What the transaction's gas cost, in wei."""
        return self.gas_used * self.effective_gas_price_wei


def _hex(value: object) -> str | None:
    """``value`` (bytes, or a hex string) as ``0x`` and lowercase hex; ``None`` when it is neither."""
    if isinstance(value, bytes):
        return "0x" + value.hex()
    if isinstance(value, str) and re.fullmatch(r"0x[0-9a-fA-F]*", value):
        return value.lower()
    return None


def _receipt(tx_hash: str, raw: object) -> Receipt:
    """``raw``, a receipt as web3 decodes it, checked and kept as a :class:`Receipt`."""
    what = f"the receipt of {tx_hash}"
    if not isinstance(raw, Mapping):
        raise MalformedResponse(f"{what} came back as {type(raw).__name__}, not a mapping")
    numbers = {name: raw.get(name) for name in ("blockNumber", "status", "gasUsed", "effectiveGasPrice")}
    if not all(_is_count(value) for value in numbers.values()) or numbers["status"] not in (0, 1):
        raise MalformedResponse(f"{what} has {numbers!r}")
    if _hex(raw.get("transactionHash")) != tx_hash.lower():
        raise MalformedResponse(f"{what} is of the transaction {raw.get('transactionHash')!r}")
    logs = raw.get("logs")
    if not isinstance(logs, Sequence) or isinstance(logs, str | bytes):
        raise MalformedResponse(f"{what} has logs of {logs!r}")
    kept: list[Log] = []
    for log in logs:
        if not isinstance(log, Mapping) or not isinstance(log.get("topics"), Sequence):
            raise MalformedResponse(f"{what} has a log of {log!r}")
        address, data = _hex(log.get("address")), _hex(log.get("data"))
        topics = tuple(hexed for topic in log["topics"] if (hexed := _hex(topic)) is not None)
        if address is None or data is None or len(topics) != len(log["topics"]):
            raise MalformedResponse(f"{what} has a log of {log!r}")
        kept.append(Log(address, topics, data))
    return Receipt(
        tx_hash=tx_hash.lower(),
        block=numbers["blockNumber"],
        succeeded=numbers["status"] == 1,
        gas_used=numbers["gasUsed"],
        effective_gas_price_wei=numbers["effectiveGasPrice"],
        logs=tuple(kept),
    )


def _lacks_state(message: str) -> bool:
    """Whether a node's error says it no longer has the state a read needs."""
    # geth words it "... state <root> is not available", with the root between.
    return any(phrase in message for phrase in _NO_STATE) or (
        "state" in message and "not available" in message
    )


def _is_count(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _require_block(block: object) -> None:
    if not _is_count(block):
        raise ValueError(f"a block is a non-negative integer, got {block!r}")


def _status(exc: BaseException) -> int | None:
    """The HTTP status an ``HTTPError`` carries, when it carries one."""
    if not isinstance(exc, requests.HTTPError):
        return None
    return getattr(getattr(exc, "response", None), "status_code", None)


def _is_transient(exc: BaseException) -> bool:
    transport = (
        requests.ConnectionError,
        requests.Timeout,
        requests.exceptions.ChunkedEncodingError,
        # The body was not JSON: empty, cut short, or a gateway's error page.
        json.JSONDecodeError,
    )
    status = _status(exc)
    return isinstance(exc, transport) or (
        status is not None and (status in _RETRY_STATUS or status >= 500)
    )


def _is_unusable_url(exc: BaseException) -> bool:
    kinds = requests.exceptions
    return isinstance(
        exc,
        kinds.InvalidURL
        | kinds.MissingSchema
        | kinds.InvalidSchema
        # A host urllib3 cannot parse: an empty label, or one too long.
        | urllib3.exceptions.LocationValueError,
    )


class _ErrorTap(BaseProvider):
    """Passes requests through, keeping the JSON-RPC error of the last response.

    web3 folds some node errors into ``ContractLogicError`` and drops their
    code, so a rate limit and a revert cannot be told apart from the
    exception alone.
    """

    def __init__(self, inner: BaseProvider) -> None:
        super().__init__()
        self._inner = inner
        self.last_error: Mapping[str, Any] | None = None

    def is_connected(self, show_traceback: bool = False) -> bool:
        return self._inner.is_connected(show_traceback)

    def make_request(self, method: Any, params: Any) -> Any:
        response = self._inner.make_request(method, params)
        error = response.get("error") if isinstance(response, Mapping) else None
        self.last_error = error if isinstance(error, Mapping) else None
        return response


class Rpc:
    """A node on one chain: block headers, contract calls at a named block, and what sending takes.

    For one thread at a time: the error of the last response and the chain
    check are plain attributes.
    """

    def __init__(
        self,
        provider: BaseProvider,
        chain_id: int,
        *,
        settings: RpcSettings | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        # Whoever built the provider, its URL is a secret from here on.
        endpoint = getattr(provider, "endpoint_uri", None)
        if endpoint:
            _REDACTOR.register(str(endpoint))
        self._tap = _ErrorTap(provider)
        self._w3 = Web3(self._tap)
        # web3's validation middleware asks the node for its chain ID before
        # every eth_call, doubling the requests. The chain is checked once,
        # before the first read.
        self._w3.middleware_onion.remove("validation")
        self._chain_id = chain_id
        self._chain_verified = False
        self._settings = settings if settings is not None else RpcSettings()
        self._sleep = sleep

    @property
    def chain_id(self) -> int:
        """The chain this connection was opened for."""
        return self._chain_id

    def verify_chain(self) -> None:
        """Refuse a node that reports another chain ID than the one expected.

        Every read does this first, until it has passed once.
        """
        if self._chain_verified:
            return
        reported = self._request("the chain ID", lambda: self._w3.eth.chain_id)
        if reported != self._chain_id:
            raise RpcConfigError(
                f"the node is on chain {reported!r}, and chain {self._chain_id} was expected"
            )
        self._chain_verified = True

    def latest_header(self) -> BlockHeader:
        """The header of the latest block: the one read that names no block."""
        return self._header("the latest block", "latest")

    def pending_header(self) -> BlockHeader:
        """The header of the block the node would mine next, whose time is the node's clock now.

        The latest block's time stands still while no block is mined, as on
        an idle anvil; the pending block's does not.
        """
        return self._header("the pending block", "pending")

    def header(self, block: int) -> BlockHeader:
        """The header of ``block``."""
        _require_block(block)
        header = self._header(f"block {block}", block)
        if header.number != block:
            raise MalformedResponse(
                f"asked for block {block}, and the node sent block {header.number}"
            )
        return header

    def _header(self, what: str, block: Any) -> BlockHeader:
        self.verify_chain()
        raw = self._request(what, lambda: self._w3.eth.get_block(block))
        if not isinstance(raw, Mapping):
            raise MalformedResponse(f"{what} came back as {type(raw).__name__}, not a mapping")
        number, timestamp = raw.get("number"), raw.get("timestamp")
        base_fee = raw.get("baseFeePerGas")
        if not _is_count(number) or not _is_count(timestamp):
            raise MalformedResponse(f"{what} has number {number!r} and timestamp {timestamp!r}")
        if base_fee is not None and not _is_count(base_fee):
            raise MalformedResponse(f"{what} has a base fee of {base_fee!r}")
        block_hash = raw.get("hash")
        if not isinstance(block_hash, bytes) or len(block_hash) != _HASH_BYTES:
            raise MalformedResponse(f"{what} has a hash of {block_hash!r}")
        return BlockHeader(
            number=number,
            hash="0x" + bytes(block_hash).hex(),
            timestamp=timestamp,
            base_fee_wei=base_fee,
        )

    def call(
        self,
        address: str,
        abi: Sequence[Mapping[str, Any]],
        function: str,
        args: Sequence[Any] = (),
        *,
        block: int,
    ) -> Any:
        """What ``function(*args)`` of the contract at ``address`` returns at the end of ``block``.

        An ``eth_call``: nothing is sent or signed. One output comes back
        bare and several come back as a sequence, decoded by the ABI entry.
        An address, a name or an argument that does not fit the ABI raises
        web3's own exception before anything is asked: that is the caller's
        mistake, not an answer of the node's.
        """
        _require_block(block)
        contract = self._w3.eth.contract(address=address, abi=abi)  # type: ignore[call-overload]
        # Encoded once here, outside the request, for the check alone: an
        # address nested in a tuple argument is only looked at on encoding.
        contract.encode_abi(function, args=list(args))
        bound = getattr(contract.functions, function)(*args)
        self.verify_chain()
        return self._request(
            f"{function}() on {address} at block {block}",
            lambda: bound.call(block_identifier=block),
        )

    def transaction_count(self, address: str) -> int:
        """The next nonce of ``address``: its transactions, the ones the node holds unmined included."""
        self.verify_chain()
        count = self._request(
            f"the nonce of {address}",
            lambda: self._w3.eth.get_transaction_count(address, "pending"),  # type: ignore[arg-type]
        )
        if not _is_count(count):
            raise MalformedResponse(f"the nonce of {address} came back as {count!r}")
        return count

    def estimate_gas(self, transaction: Mapping[str, Any]) -> int:
        """The gas ``transaction`` would use on top of the latest block.

        One that would revert raises :class:`~.errors.CallReverted`, and
        nothing is sent.
        """
        self.verify_chain()
        gas = self._request(
            f"a gas estimate for a call to {transaction.get('to')}",
            lambda: self._w3.eth.estimate_gas(dict(transaction)),  # type: ignore[arg-type]
        )
        if not _is_count(gas) or gas == 0:
            raise MalformedResponse(f"a gas estimate came back as {gas!r}")
        return gas

    def max_priority_fee_wei(self) -> int:
        """The priority fee per gas the node suggests, in wei."""
        self.verify_chain()
        fee = self._request("the priority fee", lambda: self._w3.eth.max_priority_fee)
        if not _is_count(fee):
            raise MalformedResponse(f"the priority fee came back as {fee!r}")
        return fee

    def send_raw_transaction(self, raw: bytes) -> str:
        """Hand the signed transaction ``raw`` to the node, and return its hash.

        Tried once, whatever the failure: the signed bytes name their own
        hash, so the caller looks for a receipt rather than sending again.
        """
        self.verify_chain()
        sent = self._request(
            "sending a transaction", lambda: self._w3.eth.send_raw_transaction(raw), attempts=1
        )
        tx_hash = _hex(sent)
        if tx_hash is None or len(tx_hash) != 2 + 2 * _HASH_BYTES:
            raise MalformedResponse(f"a sent transaction's hash came back as {sent!r}")
        return tx_hash

    def receipt(self, tx_hash: str) -> Receipt | None:
        """The receipt of ``tx_hash``, or ``None`` while the node has none: not mined, or unknown."""
        self.verify_chain()

        def read() -> Any:
            try:
                return self._w3.eth.get_transaction_receipt(tx_hash)  # type: ignore[arg-type]
            except TransactionNotFound:
                return None

        raw = self._request(f"the receipt of {tx_hash}", read)
        if raw is None:
            return None
        return _receipt(tx_hash, raw)

    def node_info(self) -> Mapping[str, Any]:
        """What an anvil node says of itself (``anvil_nodeInfo``); another node refuses the method.

        The answer holds the URL the fork was made from, which is a secret
        of its own: it is never to be quoted, and from here on it is
        scrubbed from every error and log line as this connection's own URL
        is, since the fork's errors can quote it.
        """
        self.verify_chain()
        info = self._request(
            "anvil_nodeInfo",
            lambda: self._w3.manager.request_blocking(RPCEndpoint("anvil_nodeInfo"), []),
        )
        if not isinstance(info, Mapping):
            raise MalformedResponse(f"anvil_nodeInfo came back as {type(info).__name__}")
        fork = info.get("forkConfig")
        if isinstance(fork, Mapping) and isinstance(fork.get("forkUrl"), str) and fork["forkUrl"]:
            _REDACTOR.register(fork["forkUrl"])
        return info

    def _request(self, what: str, read: Callable[[], _T], *, attempts: int | None = None) -> _T:
        """Run one read, retrying a transient failure and translating every other one.

        ``attempts`` overrides the settings' count: a send is made once.
        """
        attempts = self._settings.attempts if attempts is None else attempts
        for attempt in range(1, attempts + 1):
            _REDACTOR.scrub_logs()
            try:
                return read()
            except Exception as exc:
                # Whatever a provider or a decoder raises: its text may quote
                # the URL, so nothing of it leaves here unscrubbed.
                failure = self._translate(what, exc, attempt=attempt, attempts=attempts)
            # Raised out here, where the caught exception is no longer being
            # handled and so does not become the new one's context.
            if failure is not None:
                raise failure
            self._sleep(self._settings.backoff_seconds * 2 ** (attempt - 1))
        raise AssertionError("unreachable: the last attempt returns or raises")

    def _translate(
        self, what: str, exc: Exception, *, attempt: int, attempts: int
    ) -> ChainError | None:
        """The error to raise for ``exc`` on ``attempt`` of ``attempts``, or ``None`` to try again."""
        last = attempt == attempts
        text = _REDACTOR.scrub(str(exc))
        if len(text) > _MAX_QUOTE:
            text = f"{text[:_MAX_QUOTE]}... ({len(text) - _MAX_QUOTE} more characters)"
        said = f"{type(exc).__name__}: {text}"
        if isinstance(exc, _Web3BlockNotFound):
            return BlockNotFound(f"the node does not have {what}")
        if isinstance(exc, ContractLogicError | Web3RPCError):
            # The node answered with an error. It is read off the response
            # itself: the exception no longer says which code it came with.
            error = self._tap.last_error or {}
            code, message = error.get("code"), str(error.get("message", "")).lower()
            if code in _RATE_LIMIT_CODES:
                if not last:
                    return None
                return RpcUnavailable(f"{what} was rate-limited on {attempts} attempt(s) ({said})")
            if isinstance(exc, ContractLogicError) and (
                code == _REVERT_CODE or "execution reverted" in message
            ):
                return CallReverted(f"{what} reverted ({said})")
            if any(phrase in message for phrase in _BEHIND):
                return BlockNotFound(f"the node is behind {what} ({said})")
            if _lacks_state(message):
                return RpcConfigError(
                    f"the node no longer has the state for {what}; "
                    f"an archive node is needed ({said})"
                )
            return RpcRejected(f"the node refused {what} ({said})")
        if _is_unusable_url(exc):
            # Its text is the URL, in pieces no scrubbing was registered for.
            return RpcConfigError(
                f"the endpoint URL, or the proxy set for it, cannot be requested "
                f"({type(exc).__name__}) on {what}"
            )
        if _is_transient(exc):
            if not last:
                return None
            return RpcUnavailable(f"{what} failed after {attempts} attempt(s) ({said})")
        if _status(exc) in _UNAUTHORISED_STATUS:
            return RpcConfigError(f"the endpoint refused the credentials on {what} ({said})")
        if isinstance(exc, requests.HTTPError):
            return RpcRejected(f"{what} failed ({said})")
        return MalformedResponse(f"{what} could not be read ({said})")


class _HTTPProvider(HTTPProvider):
    """``HTTPProvider``, except that printing it does not print its URL."""

    def __str__(self) -> str:
        return "RPC connection <redacted>"


def http_provider(
    *, settings: RpcSettings | None = None, env: Mapping[str, str] | None = None
) -> HTTPProvider:
    """The provider for the endpoint the environment names, its URL made a secret.

    From this call on, and for the life of the process, the log records of
    ``web3``, ``urllib3`` and ``requests`` are scrubbed of the URL as they
    are created (:func:`logging.setLogRecordFactory`).
    """
    settings = settings if settings is not None else RpcSettings()
    url = (os.environ if env is None else env).get(settings.url_env, "").strip()
    if not url:
        raise RpcConfigError(f"the environment variable {settings.url_env} is not set")
    try:
        parts = urlsplit(url)
        # Reading the port is what checks it.
        usable = url.startswith(("https://", "http://")) and bool(parts.hostname) and parts.port != 0
    except ValueError:
        usable = False
    if not usable:
        # The value is not quoted: a key pasted without its URL is still a key.
        raise RpcConfigError(f"the environment variable {settings.url_env} must hold an http(s) URL")
    return http_provider_at(url, settings=settings)


def http_provider_at(
    url: str, *, settings: RpcSettings | None = None, direct: bool = False
) -> HTTPProvider:
    """The provider for ``url``, made a secret; ``direct`` takes nothing from the environment.

    Pass ``direct`` for a loopback address: a proxy the environment names
    (``HTTP(S)_PROXY``, ``ALL_PROXY``, a system setting) could forward the
    request anywhere. A direct provider has a session of its own that
    reads no proxy, no ``.netrc`` and no CA bundle from the environment.
    """
    settings = settings if settings is not None else RpcSettings()
    _REDACTOR.register(url)
    session = None
    if direct:
        session = requests.Session()
        session.trust_env = False
    return _HTTPProvider(
        url,
        request_kwargs={"timeout": settings.timeout_seconds},
        session=session,
        # Retries are counted in Rpc._request alone.
        exception_retry_configuration=None,
    )


def connect(
    chain_id: int,
    *,
    settings: RpcSettings | None = None,
    env: Mapping[str, str] | None = None,
) -> Rpc:
    """Open the endpoint the environment names and check that it is on ``chain_id``."""
    rpc = Rpc(http_provider(settings=settings, env=env), chain_id, settings=settings)
    rpc.verify_chain()
    return rpc
