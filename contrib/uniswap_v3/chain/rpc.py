"""The connection to a node, and the two reads the rest of ``chain/`` is built on.

:func:`connect` reads the endpoint URL from an environment variable, named
by :class:`RpcSettings` (``ETH_RPC_URL`` unless told otherwise), and refuses
a node that is on another chain. :class:`Rpc` then offers a block header and
a contract call, each at a named block.

Failures: a connection error, a timeout, a response cut short, an HTTP 429
and an HTTP 5xx are tried again, waiting twice as long each time; when the
attempts run out the read raises :class:`~.errors.RpcUnavailable`. Anything
else raises at once.

The URL is a secret, since it ends in the API key. It is never put in an
exception or a log line. The text of every exception caught here is scrubbed
before it is quoted, and the error that replaces it is raised once the
``except`` block is over, so the original is neither its cause nor its
context. Every log record of ``web3``, ``urllib3`` and ``requests``, which
write the URL or its path, is scrubbed as it is created.
"""

from __future__ import annotations

import logging
import os
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, TypeVar
from urllib.parse import urlsplit

import requests
from web3 import HTTPProvider, Web3
from web3.exceptions import (
    BlockNotFound as _Web3BlockNotFound,
    ContractLogicError,
    Web3RPCError,
)
from web3.providers.base import BaseProvider

from .errors import (
    BlockNotFound,
    CallReverted,
    ChainError,
    MalformedResponse,
    RpcConfigError,
    RpcRejected,
    RpcUnavailable,
)

__all__ = ["DEFAULT_URL_ENV", "BlockHeader", "Rpc", "RpcSettings", "connect", "http_provider"]

DEFAULT_URL_ENV: Final = "ETH_RPC_URL"

_TRANSIENT_STATUS: Final = frozenset({429, 500, 502, 503, 504})
# A key the endpoint does not accept: no later request will fare better.
_UNAUTHORISED_STATUS: Final = frozenset({401, 403})
# The packages under a request, whose loggers write the URL or its path:
# web3's provider and urllib3's connection pool at DEBUG, urllib3's
# connection at WARNING.
_SCRUBBED_LOGGERS: Final = frozenset({"web3", "urllib3", "requests"})
_URL: Final = re.compile(r"https?://[^\s'\"]+")
# A piece of the URL shorter than this (``/v2``) is not a secret, and
# scrubbing it would eat ordinary text.
_MIN_SECRET: Final = 8

_T = TypeVar("_T")


class _Redactor:
    """Removes the registered URLs, and anything shaped like a URL, from text."""

    def __init__(self) -> None:
        self._secrets: list[str] = []
        self._scrubbing_logs = False

    def register(self, url: str) -> None:
        """Treat ``url`` as secret, with its path and query: urllib3 quotes those alone."""
        parts = urlsplit(url)
        pieces = {url, parts.path, parts.path.rsplit("/", 1)[-1], parts.query}
        if parts.query:
            pieces.add(f"{parts.path}?{parts.query}")
        known = set(self._secrets) | {piece for piece in pieces if len(piece) >= _MIN_SECRET}
        # Longest first, so the whole URL goes before the key inside it.
        self._secrets = sorted(known, key=len, reverse=True)
        self._scrub_logs()

    def scrub(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, "<redacted>")
        return _URL.sub("<url>", text)

    def _scrub_logs(self) -> None:
        """From the first registered URL on, scrub the HTTP stack's log records.

        Done where records are created rather than with a filter per logger:
        a filter sees only its own logger's records, so each logger that
        writes the URL would have to be found and named.
        """
        if self._scrubbing_logs:
            return
        self._scrubbing_logs = True
        create = logging.getLogRecordFactory()

        def scrubbed(*args: Any, **kwargs: Any) -> logging.LogRecord:
            record = create(*args, **kwargs)
            if record.name.split(".", 1)[0] in _SCRUBBED_LOGGERS:
                try:
                    message = record.getMessage()
                except Exception:
                    # Arguments that do not fit the format; logging reports it.
                    return record
                cleaned = self.scrub(message)
                if cleaned != message:
                    record.msg, record.args = cleaned, ()
            return record

        logging.setLogRecordFactory(scrubbed)


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
    """What the package reads off a block. ``base_fee_wei`` is ``None`` before London."""

    number: int
    timestamp: int
    base_fee_wei: int | None


def _is_count(value: object) -> bool:
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
    )
    return isinstance(exc, transport) or _status(exc) in _TRANSIENT_STATUS


class Rpc:
    """A node on one chain: block headers and contract calls, each at a named block."""

    def __init__(
        self,
        provider: BaseProvider,
        chain_id: int,
        *,
        settings: RpcSettings | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._w3 = Web3(provider)
        # web3's validation middleware asks the node for its chain ID before
        # every eth_call, doubling the requests. The chain is checked once,
        # by verify_chain.
        self._w3.middleware_onion.remove("validation")
        self._chain_id = chain_id
        self._settings = settings if settings is not None else RpcSettings()
        self._sleep = sleep

    @property
    def chain_id(self) -> int:
        """The chain this connection was opened for."""
        return self._chain_id

    def verify_chain(self) -> None:
        """Refuse a node that reports another chain ID than the one expected."""
        reported = self._request("the chain ID", lambda: self._w3.eth.chain_id)
        if reported != self._chain_id:
            raise RpcConfigError(
                f"the node is on chain {reported!r}, and chain {self._chain_id} was expected"
            )

    def header(self, block: int | None = None) -> BlockHeader:
        """The header of ``block``, or of the latest block when ``block`` is ``None``."""
        if block is not None:
            _require_block(block)
        what = "the latest block" if block is None else f"block {block}"
        raw = self._request(
            what, lambda: self._w3.eth.get_block("latest" if block is None else block)
        )
        if not isinstance(raw, Mapping):
            raise MalformedResponse(f"{what} came back as {type(raw).__name__}, not a mapping")
        number, timestamp = raw.get("number"), raw.get("timestamp")
        base_fee = raw.get("baseFeePerGas")
        if (
            not _is_count(number)
            or not _is_count(timestamp)
            or not (base_fee is None or _is_count(base_fee))
        ):
            raise MalformedResponse(
                f"{what} has number {number!r}, timestamp {timestamp!r} and base fee {base_fee!r}"
            )
        if block is not None and number != block:
            raise MalformedResponse(f"asked for block {block}, and the node sent block {number}")
        return BlockHeader(number=number, timestamp=timestamp, base_fee_wei=base_fee)

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
        """
        _require_block(block)
        # Bound outside the request: an address, a name or an argument that
        # does not fit the ABI is the caller's mistake, not the node's answer.
        contract = self._w3.eth.contract(address=address, abi=abi)  # type: ignore[call-overload]
        bound = getattr(contract.functions, function)(*args)
        return self._request(
            f"{function}() on {address} at block {block}",
            lambda: bound.call(block_identifier=block),
        )

    def _request(self, what: str, read: Callable[[], _T]) -> _T:
        """Run one read, retrying a transient failure and translating every other one."""
        attempts = self._settings.attempts
        for attempt in range(1, attempts + 1):
            try:
                return read()
            except Exception as exc:
                # Whatever a provider or a decoder raises: its text may quote
                # the URL, so nothing of it leaves here unscrubbed.
                failure = self._translate(what, exc, last=attempt == attempts)
            # Raised out here, where the caught exception is no longer being
            # handled and so does not become the new one's context.
            if failure is not None:
                raise failure
            self._sleep(self._settings.backoff_seconds * 2 ** (attempt - 1))
        raise AssertionError("unreachable: the last attempt returns or raises")

    def _translate(self, what: str, exc: Exception, *, last: bool) -> ChainError | None:
        """The error to raise for ``exc``, or ``None`` to try the read again."""
        said = f"{type(exc).__name__}: {_REDACTOR.scrub(str(exc))}"
        if isinstance(exc, ContractLogicError):
            return CallReverted(f"{what} reverted ({said})")
        if isinstance(exc, _Web3BlockNotFound):
            return BlockNotFound(f"the node does not have {what}")
        if isinstance(exc, Web3RPCError):
            return RpcRejected(f"the node refused {what} ({said})")
        if _is_transient(exc):
            if not last:
                return None
            return RpcUnavailable(f"{what} failed after {self._settings.attempts} attempt(s) ({said})")
        if _status(exc) in _UNAUTHORISED_STATUS:
            return RpcConfigError(f"the endpoint refused the credentials on {what} ({said})")
        if isinstance(exc, requests.HTTPError):
            return RpcRejected(f"{what} failed ({said})")
        return MalformedResponse(f"{what} could not be read ({said})")


def http_provider(
    *, settings: RpcSettings | None = None, env: Mapping[str, str] | None = None
) -> HTTPProvider:
    """The provider for the endpoint the environment names, its URL made a secret.

    Every provider on a real endpoint is built here: a URL handed to
    ``HTTPProvider`` directly would not be scrubbed from errors and logs.
    """
    settings = settings if settings is not None else RpcSettings()
    url = (os.environ if env is None else env).get(settings.url_env, "").strip()
    if not url:
        raise RpcConfigError(f"the environment variable {settings.url_env} is not set")
    if not url.startswith(("https://", "http://")) or not urlsplit(url).hostname:
        # The value is not quoted: a key pasted without its URL is still a key.
        raise RpcConfigError(f"the environment variable {settings.url_env} must hold an http(s) URL")
    _REDACTOR.register(url)
    return HTTPProvider(
        url,
        request_kwargs={"timeout": settings.timeout_seconds},
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
